"""PareAgent.handle_other must log PaneActivityMessage without stalling the
daemon's connection read loop (spec R3; agent_core/daemon.py:136 awaits
handle_other inline, unlike ChatMessage/CommandMessage which are dispatched
via asyncio.create_task).

Two blocking tests matter here, not one -- see
test_slow_pane_write_does_not_stall_approval_routing_after_park's docstring
for why the pre-buffered version alone is not enough (fix-round 1, Finding 1:
deferring the write to a task only moves the stall by one hop unless that
task's own write is *also* off the event loop). See task-3-report.md for the
actual red/green output captured while developing this file and its
fix-round.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_core.capture import CaptureStore
from agent_core.daemon import Daemon
from agent_core.protocol import ToolApprovalResponseMessage, encode_message
from agent_core.workers.tool_approval import ToolApprovalRegistry, ToolCallSpec

from pare.agent import PareAgent
from pare.capture_store import CaptureStoreManager
from pare.protocol import PaneActivityMessage


async def _stop_worker(agent: PareAgent) -> None:
    """Cancel the lazily-started pane-activity consumer task. Every test that
    calls handle_other() with a PaneActivityMessage starts this task (it
    otherwise runs forever), so it must be torn down explicitly -- pytest
    -asyncio tears down the event loop per test, and an un-cancelled task
    left on it logs a noisy 'Task was destroyed but it is pending!' warning."""
    task = getattr(agent, "_pane_activity_task", None)
    if task is not None and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


class _FakeChannels:
    """Just enough of agent_core.channels.ChannelStore for Daemon._handle_connection."""

    async def get_or_create(self, channel_id):
        return MagicMock(name=f"conversation:{channel_id}")


class _FakeWriter:
    """Just enough of asyncio.StreamWriter for Daemon._handle_connection."""

    def __init__(self) -> None:
        self.written: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.written.append(data)

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass


def _pane_msg(**overrides) -> PaneActivityMessage:
    fields = dict(
        source="hw0", session="s1", kind="sent",
        data_b64=base64.b64encode(b"hello device").decode(),
        cursor_start=0, cursor_end=0, channel_id="tui-1",
    )
    fields.update(overrides)
    return PaneActivityMessage(**fields)


def _agent_with_mock_stores() -> PareAgent:
    """An agent whose _capture_stores is a bare MagicMock: resolve()/
    resolve_db_path()/write_async() all need explicit configuration per
    test. Used where the test cares about handle_other's own behaviour, not
    about real on-disk writes -- those tests use a real CaptureStoreManager
    (see _real_manager) instead."""
    agent = PareAgent()
    agent._capture_stores = MagicMock()
    agent.channels = _FakeChannels()
    agent.tool_approval_registry = ToolApprovalRegistry()
    return agent


def _real_manager(tmp_path: Path) -> CaptureStoreManager:
    """A real CaptureStoreManager rooted entirely under tmp_path. `home` is
    set to tmp_path itself (not e.g. tmp_path/"home") so that a cwd one level
    under tmp_path hits the $HOME ceiling on its very first parent -- without
    that, a cwd with no .pare marker anywhere under tmp_path would walk the
    marker search up through the REAL filesystem to `/`, since nothing would
    ever match `d == home`."""
    return CaptureStoreManager(marker=".pare", home=tmp_path, xdg_state=tmp_path / "xdg")


async def _register_pending_approval(agent: PareAgent):
    spec = ToolCallSpec(worker="w", tool="t", arguments={},
                        declared_tier="low", effective_tier="low")
    proposal_id, fut = await agent.tool_approval_registry.request(spec, timeout_seconds=5.0)
    return proposal_id, fut


@pytest.mark.asyncio
async def test_slow_pane_write_does_not_stall_approval_routing():
    """handle_other itself must return without awaiting the write (req. 1).

    Sends a PaneActivityMessage whose write_async is made deliberately slow,
    then a ToolApprovalResponseMessage on the SAME connection -- both lines
    fed into the StreamReader before the handler task starts -- and asserts
    the approval is routed without waiting for the write.

    This test alone is NOT sufficient evidence that pane activity can never
    stall an approval: see
    test_slow_pane_write_does_not_stall_approval_routing_after_park for the
    case this one structurally cannot see (fix-round 1, Finding 1). Keeping
    both: this one still catches a regression to the OLD bug (handle_other
    itself awaiting the write inline), which the after-park test does not
    directly exercise since it doesn't inject the delay at the
    handle_other-awaited layer at all.
    """
    real_backing_store = CaptureStore.open_memory()
    order: list[str] = []
    DELAY = 0.4

    async def fake_write_async(db_path, record):
        order.append("write_start")
        await asyncio.sleep(DELAY)          # simulated slow disk
        real_backing_store.write(record)
        order.append("write_done")

    agent = _agent_with_mock_stores()
    agent._capture_stores.resolve.return_value = real_backing_store
    agent._capture_stores.resolve_db_path.return_value = Path("/fake/pane.db")
    agent._capture_stores.write_async = AsyncMock(side_effect=fake_write_async)

    proposal_id, fut = await _register_pending_approval(agent)

    daemon = Daemon(agent)
    reader = asyncio.StreamReader()
    reader.feed_data(encode_message(_pane_msg()))
    reader.feed_data(encode_message(
        ToolApprovalResponseMessage(proposal_id=proposal_id, approved=True)))
    reader.feed_eof()
    writer = _FakeWriter()

    handler_task = asyncio.create_task(daemon._handle_connection(reader, writer))
    try:
        # Wide relative to DELAY, deliberately (fix-round 1 free hardening):
        # the real discriminator below is "write_done" not in order, an
        # ordering fact, not a timing one -- a tight timeout here only adds
        # flake risk on a loaded runner and buys no extra discrimination
        # (confirmed: this margin still fails against a naive inline-await
        # handler -- see task-3-report.md).
        await asyncio.wait_for(handler_task, timeout=DELAY * 20)

        assert fut.done(), "approval should already be routed once the connection handler returns"
        decision = fut.result()
        assert decision.approved is True
        assert "write_done" not in order

        await asyncio.wait_for(agent._pane_activity_queue.join(), timeout=DELAY * 5)
        assert order == ["write_start", "write_done"]
        rows = real_backing_store.search(worker="pane:sent")
        assert len(rows) == 1
        assert rows[0]["tool"] == "hw0"
    finally:
        if not handler_task.done():
            handler_task.cancel()
        await _stop_worker(agent)


@pytest.mark.asyncio
async def test_slow_pane_write_does_not_stall_approval_routing_after_park(monkeypatch, tmp_path):
    """The case the pre-buffered test above cannot see (fix-round 1, Finding 1).

    There, both lines are fed into the StreamReader before the handler task
    starts, so readline() never truly suspends: the connection handler drains
    both messages in one uninterrupted synchronous stretch, and the scheduled
    writer task never gets a turn before routing has already happened -- true
    regardless of whether the write itself is properly isolated from the
    event loop. A real socket does not deliver a later approval that way.

    Here the pane-activity line is fed first, the handler task is given a
    moment to fully process it and reach the second readline() call (where,
    with no data buffered, it genuinely parks), and ONLY THEN is the approval
    line fed. This is what actually exercises whether _pane_activity_worker's
    write is off the event loop.

    The injected delay is a REAL blocking time.sleep on CaptureStore.write
    (not asyncio.sleep): the bug this test exists to catch is a *synchronous*
    sqlite call blocking whichever thread runs it. An earlier version of
    _pane_activity_worker called store.write(record) directly on the worker
    task -- deferring the write off handle_other, but not off the event loop,
    since the worker task runs on that same loop. That version stalled the
    approval by exactly the write's duration, one hop later than before.
    Verified: this test fails against that version (see task-3-report.md).
    """
    manager = _real_manager(tmp_path)
    agent = PareAgent()
    agent._capture_stores = manager
    agent.channels = _FakeChannels()
    agent.tool_approval_registry = ToolApprovalRegistry()
    proposal_id, fut = await _register_pending_approval(agent)

    DELAY = 0.4
    real_write = CaptureStore.write

    def slow_write(self, record):
        time.sleep(DELAY)                   # genuinely blocking -- not asyncio.sleep
        return real_write(self, record)

    monkeypatch.setattr(CaptureStore, "write", slow_write)

    daemon = Daemon(agent)
    reader = asyncio.StreamReader()
    reader.feed_data(encode_message(_pane_msg(cwd=str(tmp_path / "work"))))
    writer = _FakeWriter()

    loop = asyncio.get_running_loop()
    approval_bytes = encode_message(
        ToolApprovalResponseMessage(proposal_id=proposal_id, approved=True))

    # Feed the approval from a SEPARATE OS thread, deliberately: if the
    # buggy version of this code is running, the write's time.sleep(DELAY)
    # genuinely freezes the event-loop thread -- there is no `await` in this
    # test coroutine that could "arrive in between", because nothing on that
    # thread runs again until the block clears. A background thread can still
    # queue the feed via call_soon_threadsafe while that thread is frozen;
    # when the callback actually RUNS (immediately if the loop is free, only
    # after the block clears if it isn't) is exactly the fact this test
    # needs to observe. An earlier version of this test used
    # `await asyncio.sleep(0.05)` in-loop before feeding the approval, which
    # cannot discriminate at all: that sleep is itself stretched by however
    # long the block lasts, so by the time it returns (on the buggy version)
    # the write has already finished and there is nothing left to see.
    def feed_approval_after_delay() -> None:
        time.sleep(0.05)
        loop.call_soon_threadsafe(reader.feed_data, approval_bytes)
        loop.call_soon_threadsafe(reader.feed_eof)

    handler_task = asyncio.create_task(daemon._handle_connection(reader, writer))
    feeder = threading.Thread(target=feed_approval_after_delay, daemon=True)
    try:
        start = time.monotonic()
        feeder.start()
        await asyncio.wait_for(handler_task, timeout=DELAY * 10)
        elapsed = time.monotonic() - start

        assert fut.done(), "approval should be routed without waiting for the slow write"
        assert elapsed < DELAY / 2, (
            f"total time from starting the connection to the approval being "
            f"routed was {elapsed:.3f}s (feeder waits only 0.05s) -- the "
            f"write is still stalling the read loop"
        )
    finally:
        if not handler_task.done():
            handler_task.cancel()
        feeder.join(timeout=5.0)
        await _stop_worker(agent)
        manager.close_all()


@pytest.mark.asyncio
async def test_fall_through_unrelated_message_reaches_base_handle_other():
    """An unrelated registered message type reaching handle_other must not
    raise and must not be treated as pane activity (brief requirement 3)."""
    agent = _agent_with_mock_stores()
    ctx = MagicMock()
    ctx.cwd = None

    other = ToolApprovalResponseMessage(proposal_id="does-not-exist", approved=False)
    # Must not raise, and must not touch the capture store manager at all.
    await agent.handle_other(other, ctx)

    agent._capture_stores.resolve.assert_not_called()
    agent._capture_stores.resolve_db_path.assert_not_called()
    agent._capture_stores.write_async.assert_not_called()
    assert agent._pane_activity_queue.qsize() == 0
    assert agent.pane_activity_write_failures == 0


@pytest.mark.asyncio
async def test_pane_activity_written_to_cwd_resolved_store(tmp_path):
    """A message carrying a cwd must write to the store that cwd resolves to,
    not to the daemon's default (brief requirement: store binding). Uses a
    real CaptureStoreManager so this exercises actual db_path resolution,
    not a mock that could silently ignore cwd."""
    project_cwd = tmp_path / "project"
    # A REAL .pare marker: without one, resolve_capture_db falls through to
    # its XDG fallback, which is keyed only by channel_id and ignores cwd
    # entirely -- that would make project_cwd and the "no cwd" case below
    # resolve to the very same db_path, proving nothing.
    (project_cwd / ".pare").mkdir(parents=True)
    manager = _real_manager(tmp_path)
    agent = PareAgent()
    agent._capture_stores = manager
    agent.channels = _FakeChannels()
    agent.tool_approval_registry = ToolApprovalRegistry()

    ctx = MagicMock()
    ctx.cwd = str(project_cwd)
    ctx.channel_id = "tui-1"

    msg = _pane_msg(session="proj-session")
    try:
        await agent.handle_other(msg, ctx)
        await asyncio.wait_for(agent._pane_activity_queue.join(), timeout=2.0)

        project_store = manager.resolve(str(project_cwd), "tui-1")
        assert len(project_store.search(worker="pane:sent")) == 1

        # Same channel_id, no cwd -> a DIFFERENT db_path (the XDG fallback,
        # not project_cwd's). If handle_other ignored ctx.cwd, the record
        # would show up here instead.
        default_store = manager.resolve(None, "tui-1")
        assert default_store.search(worker="pane:sent") == []
    finally:
        await _stop_worker(agent)
        manager.close_all()


@pytest.mark.asyncio
async def test_pane_activity_write_failure_is_surfaced(caplog):
    """A store write that raises must not be silent (brief requirement:
    failure surfacing / spec R4)."""
    agent = _agent_with_mock_stores()
    agent._capture_stores.resolve.return_value = CaptureStore.open_memory()
    agent._capture_stores.resolve_db_path.return_value = Path("/fake/pane.db")
    agent._capture_stores.write_async = AsyncMock(side_effect=RuntimeError("disk is gone"))

    ctx = MagicMock()
    ctx.cwd = None
    ctx.channel_id = "tui-1"

    try:
        with caplog.at_level("ERROR"):
            await agent.handle_other(_pane_msg(), ctx)
            await asyncio.wait_for(agent._pane_activity_queue.join(), timeout=2.0)

        assert agent.pane_activity_write_failures == 1
        assert any("pane-activity capture write failed" in r.message for r in caplog.records)
    finally:
        await _stop_worker(agent)


@pytest.mark.asyncio
async def test_pane_activity_queue_drops_when_full_instead_of_blocking():
    """Requirement 2: the hand-off is bounded. Once full, handle_other drops
    and counts rather than blocking put() (which would reintroduce the exact
    stall this design exists to avoid)."""
    agent = _agent_with_mock_stores()
    agent._capture_stores.resolve.return_value = CaptureStore.open_memory()
    agent._capture_stores.resolve_db_path.return_value = Path("/fake/pane.db")
    agent._pane_activity_queue = asyncio.Queue(maxsize=1)
    # Fill the queue without letting the (not-yet-started) worker drain it.
    agent._pane_activity_queue.put_nowait((Path("/fake/other.db"), _pane_msg()))

    ctx = MagicMock()
    ctx.cwd = None
    ctx.channel_id = "tui-1"

    try:
        start = time.monotonic()
        await agent.handle_other(_pane_msg(), ctx)
        elapsed = time.monotonic() - start

        # A hang guard, not a tight discriminator: if put() blocked (the bug
        # this requirement forbids), this test would hang rather than cross
        # some clean numeric boundary -- there's no pytest-timeout in this
        # project to turn that hang into a failure. The real assertion is
        # the counter below; this just keeps a regression from wedging the
        # whole suite instead of just this test.
        assert elapsed < 0.5, "handle_other must not block waiting for queue space"
        assert agent.pane_activity_write_failures == 1
    finally:
        await _stop_worker(agent)


@pytest.mark.asyncio
async def test_ashutdown_drains_queued_pane_activity_before_cancelling(tmp_path):
    """Fix-round 1, Finding 2: ashutdown must not silently drop records
    already accepted into the queue. It gives the worker a bounded window to
    finish writing them before cancelling the task, rather than cancelling
    immediately and losing whatever was still queued with no counter bump
    and no log line.

    Deliberately does NOT await agent._pane_activity_queue.join() itself
    before calling ashutdown() -- ashutdown draining it is exactly the
    behaviour under test."""
    manager = _real_manager(tmp_path)
    agent = PareAgent()
    agent._capture_stores = manager
    agent.channels = _FakeChannels()
    agent.tool_approval_registry = ToolApprovalRegistry()
    agent.worker_manager = None
    agent.mcp_pool = MagicMock()
    agent.mcp_pool.close_all = AsyncMock()

    ctx = MagicMock()
    ctx.cwd = str(tmp_path / "work")
    ctx.channel_id = "tui-1"

    try:
        await agent.handle_other(_pane_msg(), ctx)
        await agent.ashutdown()

        store = manager.resolve(str(tmp_path / "work"), "tui-1")
        assert len(store.search(worker="pane:sent")) == 1
        assert agent.pane_activity_write_failures == 0
    finally:
        manager.close_all()


def test_importing_pare_agent_registers_pane_activity_message():
    """pare/agent.py's `from pare.protocol import PaneActivityMessage` is
    what makes agent_core's decode_message recognise a 'pane_activity' line
    (see pare/agent.py's comment on that import, and
    agent_core/protocol/transport.py's register_message()). Run in a fresh
    subprocess: some other test module here imports pare.protocol directly,
    which would make this pass vacuously if it shared this process's already
    -populated _MESSAGE_TYPES registry."""
    script = (
        "import pare.agent\n"
        "from agent_core.protocol import decode_message\n"
        "msg = decode_message(b'"
        '{"type": "pane_activity", "source": "s", "session": "sess", '
        '"kind": "sent", "data_b64": "", "cursor_start": 0, "cursor_end": 0}'
        "')\n"
        "assert type(msg).__name__ == 'PaneActivityMessage', type(msg)\n"
    )
    result = subprocess.run([sys.executable, "-c", script],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
