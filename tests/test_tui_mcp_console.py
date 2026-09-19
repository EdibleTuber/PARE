"""McpConsoleSource: the highest-risk file in this plan.

Every test here runs against a REAL `streamable_http` MCP server (FastMCP,
served by uvicorn in a background thread) and the REAL
`agent_core.workers.client.MCPClient` -- not a stub of either. Only the tool
BODIES are test doubles. That matters because the bug this module exists to
avoid lives in anyio's cancel-scope machinery
(`agent_core/workers/client_pool.py`'s module docstring):
`MCPClient.connect()` enters two nested anyio cancel scopes bound to the
task that entered them, so `close()` must run in that same task or it
raises

    RuntimeError: Attempted to exit cancel scope in a different task than
                  it was entered in

A pure in-memory stub of `MCPClient` cannot reproduce that -- there is no
real cancel scope to violate. A real transport, even a local one, can, and
`test_naive_connect_and_close_in_different_tasks_hits_the_cancel_scope_bug`
below reproduces the exact error text against the raw client to prove the
danger is real before trusting `McpConsoleSource` to avoid it.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import socket
import threading
import time
from typing import Any, Awaitable, Callable

import pytest
import uvicorn
from mcp.server.fastmcp import FastMCP

from agent_core.workers.client import MCPClient

from pare.tui.sources.mcp_console import McpConsoleSource, McpConsoleSourceError

pytestmark = pytest.mark.filterwarnings(
    "ignore::DeprecationWarning"
)


# --- test worker: a real streamable_http MCP server -----------------------

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _WorkerServer:
    """A real `streamable_http` MCP server in a background thread.

    Not a stub of `MCPClient` -- a genuine ASGI/anyio/httpx server, so
    `McpConsoleSource` exercises the same transport and cancel-scope
    machinery it hits against the real worker. Only the tool bodies (passed
    in as plain async functions) are test doubles.
    """

    def __init__(self, tools: dict[str, Callable[..., Awaitable[str]]]) -> None:
        mcp = FastMCP("test-hardware-worker", stateless_http=True)
        for tool_name, fn in tools.items():
            mcp.tool(name=tool_name)(fn)
        self.port = _free_port()
        self.endpoint = f"http://127.0.0.1:{self.port}/mcp"
        config = uvicorn.Config(
            mcp.streamable_http_app(),
            host="127.0.0.1",
            port=self.port,
            log_level="warning",
            lifespan="on",
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    async def start(self) -> None:
        self._thread.start()
        deadline = time.monotonic() + 5.0
        while not self._server.started and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        if not self._server.started:
            raise RuntimeError("test worker server did not start in time")

    async def stop(self) -> None:
        self._server.should_exit = True
        deadline = time.monotonic() + 5.0
        while self._thread.is_alive() and time.monotonic() < deadline:
            await asyncio.sleep(0.01)


@contextlib.asynccontextmanager
async def worker_server(tools: dict[str, Callable[..., Awaitable[str]]]):
    server = _WorkerServer(tools)
    await server.start()
    try:
        yield server.endpoint
    finally:
        await server.stop()


# --- tool bodies, built per test -------------------------------------------

def _ok(**fields: Any) -> str:
    return json.dumps(fields)


def _err(message: str) -> str:
    return json.dumps({"error": message})


def _session_tools(
    session: str | None = "sess-1",
    read_reply: Callable[[str, int, int | None], dict] | None = None,
) -> dict[str, Callable[..., Awaitable[str]]]:
    """The three tools `McpConsoleSource` calls, mirroring the real worker's
    reply shapes (`pare_hardware_mcp/tools.py`). `read_reply` lets a test
    control exactly what `console_read` answers."""
    sent: list[tuple[str, str]] = []

    async def console_status() -> str:
        if session is None:
            return _ok(open=False, session=None)
        return _ok(open=True, session=session)

    async def console_read(session: str, cursor: int = 0, limit: int | None = None) -> str:
        if read_reply is not None:
            return json.dumps(read_reply(session, cursor, limit))
        data = b""
        return _ok(
            session=session, data_b64=base64.b64encode(data).decode("ascii"),
            next_cursor=cursor, dropped=0, remaining=0, limit_applied=0,
            alive=True, capture_gaps=[],
        )

    async def console_send(session: str, data_b64: str) -> str:
        sent.append((session, data_b64))
        return _ok(session=session, sent=len(base64.b64decode(data_b64)))

    tools = {
        "console_status": console_status,
        "console_read": console_read,
        "console_send": console_send,
    }
    tools["_sent_log"] = sent  # not a real tool; smuggled out for assertions
    return tools


def _make_tools(session="sess-1", read_reply=None):
    tools = _session_tools(session, read_reply)
    sent_log = tools.pop("_sent_log")
    return tools, sent_log


# --- Step 3: the deliberately WRONG shape, proven wrong ---------------------

async def test_naive_connect_and_close_in_different_tasks_hits_the_cancel_scope_bug():
    """The trap this task exists to avoid: `connect()` in one task, `close()`
    in another -- exactly the natural `on_mount`/`on_unmount` shape.

    Run against the RAW `MCPClient`, not `McpConsoleSource`: this proves the
    bug is real and structural in the dependency this module wraps,
    independent of anything `McpConsoleSource` does. The test below
    (`test_owner_task_runs_connect_and_close_in_the_same_task`) is what
    proves `McpConsoleSource` itself does not hit it -- if THIS test ever
    stopped raising, the same-task test would no longer be discriminating
    anything.
    """
    tools, _ = _make_tools()
    async with worker_server(tools) as endpoint:
        client = MCPClient(endpoint=endpoint)

        async def connect_in_one_task() -> None:
            await client.connect()
            await client.initialize()

        # "on_mount"
        await asyncio.create_task(connect_in_one_task())

        async def close_in_another_task() -> None:
            await client.close()

        # "on_unmount" -- a DIFFERENT asyncio Task from the one above.
        with pytest.raises(RuntimeError, match="different task"):
            await asyncio.create_task(close_in_another_task())


class _NaiveOnMountOnUnmountSource(McpConsoleSource):
    """The trap this task exists to avoid, reproduced against
    `McpConsoleSource`'s OWN public interface rather than the raw client:
    `start()` connects directly in whatever task calls it (no owner task at
    all), and `stop()` closes directly in whatever task calls it -- exactly
    Textual's `on_mount`/`on_unmount` shape, where each lifecycle callback
    is its own task. Kept here, permanently failing, so this suite proves
    the discrimination the brief requires rather than a one-off manual
    check that could bitrot silently."""

    async def start(self) -> None:
        self._client = self._client_factory(self.endpoint)
        await self._client.connect()
        await self._client.initialize()

    async def stop(self) -> None:
        await self._client.close()
        self._client = None


async def test_naive_on_mount_on_unmount_shape_hits_the_bug_on_mcpconsolesource_itself():
    """Same discrimination as the raw-client test above, but driven through
    `McpConsoleSource`'s own `start()`/`stop()` on the naive subclass, to
    prove the SAME assertions used for the real (owner-task) implementation
    below would have caught this shape had it shipped instead."""
    tools, _ = _make_tools()
    async with worker_server(tools) as endpoint:
        recorders: list[_TaskRecordingClient] = []

        def factory(ep: str) -> _TaskRecordingClient:
            rec = _TaskRecordingClient(MCPClient(endpoint=ep))
            recorders.append(rec)
            return rec

        source = _NaiveOnMountOnUnmountSource(endpoint, client_factory=factory)

        async def on_mount() -> None:
            await source.start()

        await asyncio.create_task(on_mount())

        async def on_unmount() -> None:
            await source.stop()

        with pytest.raises(RuntimeError, match="different task"):
            await asyncio.create_task(on_unmount())

        rec = recorders[0]
        assert rec.connect_task is not rec.close_task


# --- the owner-task pattern, proven right -----------------------------------

class _TaskRecordingClient:
    """Wraps a real `MCPClient`, recording which asyncio Task ran `connect`
    and which ran `close` -- the one fact a pure stub cannot manufacture
    (there is no real anyio cancel scope behind a stub to violate), and the
    one fact needed to prove `McpConsoleSource`'s owner-task pattern
    structurally, independent of whether this particular run happened to
    trip anyio's own check."""

    def __init__(self, real: MCPClient) -> None:
        self._real = real
        self.connect_task: asyncio.Task | None = None
        self.close_task: asyncio.Task | None = None

    async def connect(self) -> None:
        self.connect_task = asyncio.current_task()
        await self._real.connect()

    async def initialize(self):
        return await self._real.initialize()

    async def call_tool(self, name: str, arguments: dict | None = None):
        return await self._real.call_tool(name, arguments)

    async def close(self) -> None:
        self.close_task = asyncio.current_task()
        await self._real.close()


async def test_owner_task_runs_connect_and_close_in_the_same_task():
    tools, _ = _make_tools()
    async with worker_server(tools) as endpoint:
        recorders: list[_TaskRecordingClient] = []

        def factory(ep: str) -> _TaskRecordingClient:
            rec = _TaskRecordingClient(MCPClient(endpoint=ep))
            recorders.append(rec)
            return rec

        source = McpConsoleSource(endpoint, client_factory=factory)
        # start() is called from THIS task (the test's), and stop() below is
        # called from this SAME test task too -- the affinity that matters
        # is connect() vs close() inside the OWNER task, which start()/stop()
        # never touch directly.
        await source.start()
        owner = source._owner
        assert owner is not None

        await source.stop()

        rec = recorders[0]
        assert rec.connect_task is not None
        assert rec.close_task is not None
        # The actual invariant: connect and close ran in the SAME task --
        # and that task is the owner, not whatever task called start()/stop().
        assert rec.connect_task is rec.close_task
        assert rec.connect_task is owner
        assert owner.done()
        assert not owner.cancelled()
        assert owner.exception() is None
        # And the source really did tear down, not just avoid the exception.
        assert source._client is None


async def test_full_round_trip_attach_read_send_status():
    """A real MCP call for every `ConsoleSource` method, over the real
    transport, confirming the whole path (dispatch, JSON decode, base64
    decode, field mapping) works end to end -- not just the teardown path."""
    def reply(session: str, cursor: int, limit: int | None) -> dict:
        data = b"hello"
        return {
            "session": session,
            "data_b64": base64.b64encode(data).decode("ascii"),
            "next_cursor": cursor + len(data),
            "dropped": 2,
            "remaining": 3,
            "limit_applied": len(data),
            "alive": True,
            "capture_gaps": [{"at_cursor": 1, "duration_s": 0.5,
                               "reason": "baud-scan", "in_progress": False}],
        }

    tools, sent_log = _make_tools(session="sess-1", read_reply=reply)
    async with worker_server(tools) as endpoint:
        source = McpConsoleSource(endpoint)
        await source.start()
        try:
            session = await source.attach()
            assert session == "sess-1"

            slice_ = await source.read(cursor=0)
            assert slice_.data == b"hello"
            assert slice_.next_cursor == 5
            assert slice_.dropped == 2
            assert slice_.remaining == 3
            assert slice_.limit_applied == 5
            assert slice_.alive is True
            assert slice_.capture_gaps == [
                {"at_cursor": 1, "duration_s": 0.5, "reason": "baud-scan",
                 "in_progress": False}
            ]

            await source.send("sess-1", b"world")
            assert sent_log == [("sess-1", base64.b64encode(b"world").decode("ascii"))]

            status = await source.status()
            assert status == {"open": True, "session": "sess-1"}
        finally:
            await source.stop()


async def test_attach_returns_none_when_no_session_is_open():
    tools, _ = _make_tools(session=None)
    async with worker_server(tools) as endpoint:
        source = McpConsoleSource(endpoint)
        await source.start()
        try:
            assert await source.attach() is None
        finally:
            await source.stop()


# --- bounded stop ------------------------------------------------------------

class _HangingOwnerSource(McpConsoleSource):
    """Simulates an owner that ignores the stop signal entirely -- e.g. wedged
    somewhere that never checks `self._stop`. `stop()` must still return,
    bounded by `stop_timeout`, instead of hanging the caller forever."""

    async def _own(self) -> None:
        client = self._client_factory(self.endpoint)
        await client.connect()
        await client.initialize()
        self._client = client
        self._ready.set()
        await asyncio.Event().wait()  # never set -- ignores self._stop completely


async def test_stop_is_bounded_when_the_owner_ignores_the_stop_signal():
    tools, _ = _make_tools()
    async with worker_server(tools) as endpoint:
        source = _HangingOwnerSource(endpoint, stop_timeout=0.2)
        await source.start()
        owner = source._owner
        assert owner is not None

        started = time.monotonic()
        # The outer wait_for is a safety net for the TEST, not the thing
        # under test -- stop() itself is what must be bounded.
        await asyncio.wait_for(source.stop(), timeout=5.0)
        elapsed = time.monotonic() - started
        assert elapsed < 2.0, "stop() did not honour its own bound"

        for _ in range(200):
            if owner.done():
                break
            await asyncio.sleep(0.01)
        assert owner.done()
        assert owner.cancelled()


# --- error replies are failures, never empty reads ---------------------------

async def test_error_reply_raises_instead_of_looking_like_an_empty_read():
    def reply(session: str, cursor: int, limit: int | None) -> dict:
        return {"error": f"no such session {session!r}"}

    tools, _ = _make_tools(session="sess-1", read_reply=reply)
    async with worker_server(tools) as endpoint:
        source = McpConsoleSource(endpoint)
        await source.start()
        try:
            await source.attach()
            with pytest.raises(McpConsoleSourceError, match="no such session"):
                await source.read(cursor=0)
        finally:
            await source.stop()


async def test_read_before_attach_raises_rather_than_reading_nothing():
    tools, _ = _make_tools()
    async with worker_server(tools) as endpoint:
        source = McpConsoleSource(endpoint)
        await source.start()
        try:
            with pytest.raises(McpConsoleSourceError, match="attach"):
                await source.read(cursor=0)
        finally:
            await source.stop()


# --- base64 decoding is byte-exact -------------------------------------------

async def test_non_utf8_data_reaches_console_slice_as_exact_bytes():
    raw = bytes([0xFF, 0xFE, 0x00, 0x01, 0x80, 0x81, 0x9F, 0xC0])
    with pytest.raises(UnicodeDecodeError):
        raw.decode("utf-8")  # sanity: this really is not valid UTF-8

    def reply(session: str, cursor: int, limit: int | None) -> dict:
        return {
            "session": session,
            "data_b64": base64.b64encode(raw).decode("ascii"),
            "next_cursor": cursor + len(raw),
            "dropped": 0, "remaining": 0, "limit_applied": len(raw),
            "alive": True, "capture_gaps": [],
        }

    tools, _ = _make_tools(session="sess-1", read_reply=reply)
    async with worker_server(tools) as endpoint:
        source = McpConsoleSource(endpoint)
        await source.start()
        try:
            await source.attach()
            slice_ = await source.read(cursor=0)
            assert slice_.data == raw
            assert isinstance(slice_.data, bytes)
        finally:
            await source.stop()


# --- limit_applied is authoritative over the requested limit -----------------

async def test_limit_applied_is_authoritative_not_the_requested_limit():
    max_limit = 4

    def reply(session: str, cursor: int, limit: int | None) -> dict:
        applied = min(limit, max_limit) if limit is not None else max_limit
        data = b"X" * applied
        return {
            "session": session,
            "data_b64": base64.b64encode(data).decode("ascii"),
            "next_cursor": cursor + applied,
            "dropped": 0, "remaining": 100 - applied, "limit_applied": applied,
            "alive": True, "capture_gaps": [],
        }

    tools, _ = _make_tools(session="sess-1", read_reply=reply)
    async with worker_server(tools) as endpoint:
        source = McpConsoleSource(endpoint)
        await source.start()
        try:
            await source.attach()
            slice_ = await source.read(cursor=0, limit=999)
            assert slice_.limit_applied == max_limit
            assert len(slice_.data) == max_limit
        finally:
            await source.stop()


# --- the tool-name prefix is explicit, not implicit ---------------------------

async def test_direct_connection_uses_bare_tool_names_by_default():
    tools, _ = _make_tools(session="sess-1")
    async with worker_server(tools) as endpoint:
        source = McpConsoleSource(endpoint)  # worker_prefix defaults to ""
        await source.start()
        try:
            assert await source.attach() == "sess-1"
        finally:
            await source.stop()


async def test_worker_prefix_is_applied_when_given_explicitly():
    prefix = "hardware_"
    bare_tools, _ = _make_tools(session="sess-1")
    prefixed_tools = {f"{prefix}{name}": fn for name, fn in bare_tools.items()}
    async with worker_server(prefixed_tools) as endpoint:
        source = McpConsoleSource(endpoint, worker_prefix=prefix)
        await source.start()
        try:
            assert await source.attach() == "sess-1"
        finally:
            await source.stop()


async def test_bare_name_against_a_prefixed_worker_fails_closed():
    """Connecting with the wrong (empty) prefix must not silently succeed --
    it should fail the tool call, not guess."""
    prefix = "hardware_"
    bare_tools, _ = _make_tools(session="sess-1")
    prefixed_tools = {f"{prefix}{name}": fn for name, fn in bare_tools.items()}
    async with worker_server(prefixed_tools) as endpoint:
        source = McpConsoleSource(endpoint)  # no prefix -- names won't match
        await source.start()
        try:
            with pytest.raises(McpConsoleSourceError):
                await source.attach()
        finally:
            await source.stop()
