"""Tests for pare.tui.session.DaemonSession -- the single owner of the daemon
socket's read stream (spec I1).

`agent_core.client.DaemonConnection.receive()` does not take `_read_lock`
(agent_core/client.py:57-63), so a second consumer -- another reader task, or
any use of the high-level `chat()`/`command()`/`command_stream()` helpers --
races on the shared StreamReader and silently interleaves or reorders
messages. Nothing raises, so an integration test cannot reliably catch the
corruption; the enforcement here is a source-level guard plus a behavioral
guard against a second reader task ever being started.

The remaining tests exercise a real (fake) daemon over a unix socket: message
ordering reaches subscribers intact, a raising subscriber does not stop the
reader, and stop() is idempotent including on a session that was never
started.
"""
from __future__ import annotations

import ast
import asyncio
import contextlib
import inspect
import re

import pytest

from agent_core.protocol import ResponseMessage, encode_message

from pare.tui import session as session_module
from pare.tui.session import DaemonDisconnected, DaemonSession


# --- single-consumer enforcement -------------------------------------------


def test_module_never_calls_the_high_level_connection_helpers():
    """chat()/command()/command_stream() each do their own readline() under
    DaemonConnection's _read_lock; using any of them from this module would
    be a second consumer of the stream racing the reader task's receive()."""
    source = inspect.getsource(session_module)
    assert not re.search(r"\.chat\(", source)
    assert not re.search(r"\.command\(", source)
    assert not re.search(r"\.command_stream\(", source)


def test_module_consumes_receive_exactly_once_at_the_source_level():
    """Only one call site may drive DaemonConnection.receive() -- the reader
    loop. A second call site anywhere in the module would be a second
    consumer of the same StreamReader.

    Parsed via ast (not a text search) so docstrings and comments that merely
    mention `.receive()` in prose don't count as call sites."""
    tree = ast.parse(inspect.getsource(session_module))
    receive_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "receive"
    ]
    assert len(receive_calls) == 1


async def test_start_refuses_a_second_reader_task(tmp_path):
    """start() must not let a second reader task exist alongside the first --
    that would be exactly the second receive() consumer the module exists to
    prevent."""
    sock_path = tmp_path / "d.sock"

    async def handle(reader, writer):
        # Block until the client closes (EOF), rather than a fixed sleep --
        # server.wait_closed() waits for this connection to fully detach, so
        # the handler must notice the client-side close() and close its own
        # writer, not just return after read() hits EOF.
        await reader.read()
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()

    server = await asyncio.start_unix_server(handle, path=str(sock_path))
    session = DaemonSession(sock_path, channel_id="c1", cwd="/tmp")
    try:
        await session.start()
        with pytest.raises(RuntimeError):
            await session.start()
    finally:
        await _bounded(session.stop())
        server.close()
        await _bounded(server.wait_closed())


# --- helpers for the behavioral tests ---------------------------------------


async def _serve_then_close(sock_path, messages, closed: asyncio.Event):
    """Accept exactly one connection, write the given messages, close the
    writer, then set `closed`. Returns the listening Server."""

    async def handle(reader, writer):
        for msg in messages:
            writer.write(encode_message(msg))
        await writer.drain()
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        closed.set()

    return await asyncio.start_unix_server(handle, path=str(sock_path))


async def _wait_until(predicate, timeout=2.0, interval=0.01):
    async def poll():
        while not predicate():
            await asyncio.sleep(interval)

    await asyncio.wait_for(poll(), timeout=timeout)


_TEARDOWN_TIMEOUT = 2.0


async def _bounded(aw, timeout: float = _TEARDOWN_TIMEOUT):
    """Bound a teardown await (session.stop() / server.wait_closed()) so a
    future regression that left the transport open fails the test with a
    TimeoutError instead of hanging the whole suite indefinitely -- the
    correct path completes in milliseconds, so this bound is generous."""
    return await asyncio.wait_for(aw, timeout=timeout)


# --- ordering -----------------------------------------------------------


async def test_messages_reach_a_subscriber_in_arrival_order(tmp_path):
    sock_path = tmp_path / "d.sock"
    sent = [ResponseMessage(text="one"), ResponseMessage(text="two"), ResponseMessage(text="three")]
    closed = asyncio.Event()
    server = await _serve_then_close(sock_path, sent, closed)

    session = DaemonSession(sock_path, channel_id="c1", cwd="/tmp")
    received: list[object] = []
    session.subscribe(received.append)

    try:
        await session.start()
        await asyncio.wait_for(closed.wait(), timeout=2)
        await _wait_until(lambda: len(received) >= len(sent))
        assert [m.text for m in received if isinstance(m, ResponseMessage)] == [
            "one",
            "two",
            "three",
        ]
    finally:
        await _bounded(session.stop())
        server.close()
        await _bounded(server.wait_closed())


async def test_connection_loss_notifies_subscribers(tmp_path):
    """When the daemon closes its end, subscribers see a DaemonDisconnected --
    connection loss must be observable, not a silent stop (requirement 4)."""
    sock_path = tmp_path / "d.sock"
    closed = asyncio.Event()
    server = await _serve_then_close(sock_path, [ResponseMessage(text="bye")], closed)

    session = DaemonSession(sock_path, channel_id="c1", cwd="/tmp")
    received: list[object] = []
    session.subscribe(received.append)

    try:
        await session.start()
        await asyncio.wait_for(closed.wait(), timeout=2)
        await _wait_until(lambda: any(isinstance(m, DaemonDisconnected) for m in received))
        disconnects = [m for m in received if isinstance(m, DaemonDisconnected)]
        assert len(disconnects) == 1
        assert disconnects[0].reason == "eof"
    finally:
        await _bounded(session.stop())
        server.close()
        await _bounded(server.wait_closed())


# --- a raising subscriber must not wedge the reader --------------------


async def test_a_raising_subscriber_does_not_stop_later_messages(tmp_path):
    sock_path = tmp_path / "d.sock"
    sent = [ResponseMessage(text="first"), ResponseMessage(text="second")]
    closed = asyncio.Event()
    server = await _serve_then_close(sock_path, sent, closed)

    session = DaemonSession(sock_path, channel_id="c1", cwd="/tmp")

    def bad_subscriber(msg):
        raise ValueError("boom")

    good_received: list[object] = []
    session.subscribe(bad_subscriber)
    session.subscribe(good_received.append)

    try:
        await session.start()
        await asyncio.wait_for(closed.wait(), timeout=2)
        await _wait_until(
            lambda: len([m for m in good_received if isinstance(m, ResponseMessage)]) >= 2
        )
        texts = [m.text for m in good_received if isinstance(m, ResponseMessage)]
        assert texts == ["first", "second"]
    finally:
        await _bounded(session.stop())
        server.close()
        await _bounded(server.wait_closed())


# --- stop() idempotency --------------------------------------------------


async def test_stop_on_a_never_started_session_does_not_raise(tmp_path):
    session = DaemonSession(tmp_path / "unused.sock", channel_id="c1", cwd="/tmp")
    await _bounded(session.stop())
    await _bounded(session.stop())


async def test_stop_is_idempotent_after_start(tmp_path):
    sock_path = tmp_path / "d.sock"

    async def handle(reader, writer):
        await reader.read()
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()

    server = await asyncio.start_unix_server(handle, path=str(sock_path))
    session = DaemonSession(sock_path, channel_id="c1", cwd="/tmp")
    try:
        await session.start()
        await _bounded(session.stop())
        await _bounded(session.stop())
    finally:
        server.close()
        await _bounded(server.wait_closed())


# --- outgoing message stamping -------------------------------------------


async def test_send_chat_and_send_command_stamp_channel_id_and_cwd(tmp_path):
    sock_path = tmp_path / "d.sock"
    written: list[bytes] = []

    async def handle(reader, writer):
        # Read exactly two NDJSON lines and stash their raw bytes, then close
        # our side -- server.wait_closed() waits for this connection to fully
        # detach, not just for the handler coroutine to stop reading.
        for _ in range(2):
            line = await reader.readline()
            if not line:
                break
            written.append(line)
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()

    server = await asyncio.start_unix_server(handle, path=str(sock_path))
    session = DaemonSession(sock_path, channel_id="chan-9", cwd="/work/proj")
    try:
        await session.start()
        await session.send_chat("hello")
        await session.send_command("status", "extra")
        await _wait_until(lambda: len(written) >= 2)
    finally:
        await _bounded(session.stop())
        server.close()
        await _bounded(server.wait_closed())

    import json

    chat_obj = json.loads(written[0])
    cmd_obj = json.loads(written[1])
    assert chat_obj["channel_id"] == "chan-9"
    assert chat_obj["cwd"] == "/work/proj"
    assert chat_obj["text"] == "hello"
    assert cmd_obj["channel_id"] == "chan-9"
    assert cmd_obj["cwd"] == "/work/proj"
    assert cmd_obj["name"] == "status"
    assert cmd_obj["args"] == "extra"
