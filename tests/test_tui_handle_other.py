"""PareAgent.handle_other must log PaneActivityMessage without stalling the
daemon's connection read loop (spec R3; agent_core/daemon.py:136 awaits
handle_other inline, unlike ChatMessage/CommandMessage which are dispatched
via asyncio.create_task).

The blocking test below is the one that matters: it must fail against a
handle_other that awaits the store write inline, and pass once the write is
handed off to a task/queue instead. See task-3-report.md for the actual
red/green output captured while developing this file.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import subprocess
import sys
import time
from unittest.mock import MagicMock

import pytest

from agent_core.capture import CaptureStore
from agent_core.daemon import Daemon
from agent_core.protocol import ToolApprovalResponseMessage, encode_message
from agent_core.workers.tool_approval import ToolApprovalRegistry, ToolCallSpec

from pare.agent import PareAgent
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


def _agent_with_store(store) -> PareAgent:
    agent = PareAgent()
    agent._capture_stores = MagicMock()
    agent._capture_stores.resolve.return_value = store
    agent.channels = _FakeChannels()
    agent.tool_approval_registry = ToolApprovalRegistry()
    return agent


async def _register_pending_approval(agent: PareAgent):
    spec = ToolCallSpec(worker="w", tool="t", arguments={},
                        declared_tier="low", effective_tier="low")
    proposal_id, fut = await agent.tool_approval_registry.request(spec, timeout_seconds=5.0)
    return proposal_id, fut


@pytest.mark.asyncio
async def test_slow_pane_write_does_not_stall_approval_routing():
    """The discriminating test (spec R3 / brief step 1).

    Sends a PaneActivityMessage whose store write is made deliberately slow
    (via the _persist_pane_activity seam), then a ToolApprovalResponseMessage
    on the SAME connection, and asserts the approval is routed without
    waiting for the write.

    Verified failing against a deliberately synchronous handle_other before
    this file's handle_other landed -- see task-3-report.md for the actual
    pytest output from that run.
    """
    real_store = CaptureStore.open_memory()
    agent = _agent_with_store(real_store)
    proposal_id, fut = await _register_pending_approval(agent)

    DELAY = 0.4
    order: list[str] = []
    real_persist = agent._persist_pane_activity

    async def slow_persist(store, msg):
        order.append("write_start")
        await asyncio.sleep(DELAY)          # simulated slow disk
        await real_persist(store, msg)
        order.append("write_done")

    agent._persist_pane_activity = slow_persist

    daemon = Daemon(agent)
    reader = asyncio.StreamReader()
    reader.feed_data(encode_message(_pane_msg()))
    reader.feed_data(encode_message(
        ToolApprovalResponseMessage(proposal_id=proposal_id, approved=True)))
    reader.feed_eof()
    writer = _FakeWriter()

    handler_task = asyncio.create_task(daemon._handle_connection(reader, writer))
    try:
        # Generous relative to DELAY: the connection handler processes both
        # buffered lines before anything can yield to the scheduled writer
        # task, so this should resolve almost immediately when the fix is in
        # place, and fails (TimeoutError) when the write is awaited inline.
        await asyncio.wait_for(handler_task, timeout=DELAY / 2)

        assert fut.done(), "approval should already be routed once the connection handler returns"
        decision = fut.result()
        assert decision.approved is True
        # The write must not have finished yet -- proves the approval really
        # was resolved before the slow write, not just before some unrelated
        # point.
        assert "write_done" not in order

        # The deferred write still happens -- this is about ordering, not
        # about dropping the record.
        await asyncio.wait_for(agent._pane_activity_queue.join(), timeout=DELAY * 5)
        assert order == ["write_start", "write_done"]
        rows = real_store.search(worker="pane:sent")
        assert len(rows) == 1
        assert rows[0]["tool"] == "hw0"
    finally:
        if not handler_task.done():
            handler_task.cancel()
        await _stop_worker(agent)


@pytest.mark.asyncio
async def test_fall_through_unrelated_message_reaches_base_handle_other():
    """An unrelated registered message type reaching handle_other must not
    raise and must not be treated as pane activity (brief requirement 3)."""
    store = CaptureStore.open_memory()
    agent = _agent_with_store(store)
    ctx = MagicMock()
    ctx.cwd = None

    other = ToolApprovalResponseMessage(proposal_id="does-not-exist", approved=False)
    # Must not raise, and must not touch the capture store manager at all.
    await agent.handle_other(other, ctx)

    agent._capture_stores.resolve.assert_not_called()
    assert agent._pane_activity_queue.qsize() == 0
    assert agent.pane_activity_write_failures == 0


@pytest.mark.asyncio
async def test_pane_activity_written_to_cwd_resolved_store():
    """A message carrying a cwd must write to the store that cwd resolves to,
    not to the daemon's default (brief requirement: store binding)."""
    default_store = CaptureStore.open_memory()
    project_store = CaptureStore.open_memory()

    agent = PareAgent()
    agent._capture_stores = MagicMock()

    def resolve(cwd, channel_id):
        return project_store if cwd == "/some/project" else default_store

    agent._capture_stores.resolve.side_effect = resolve

    ctx = MagicMock()
    ctx.cwd = "/some/project"
    ctx.channel_id = "tui-1"

    msg = _pane_msg(session="proj-session")
    try:
        await agent.handle_other(msg, ctx)
        await asyncio.wait_for(agent._pane_activity_queue.join(), timeout=2.0)

        assert len(project_store.search(worker="pane:sent")) == 1
        assert default_store.search(worker="pane:sent") == []
        agent._capture_stores.resolve.assert_called_once_with("/some/project", "tui-1")
    finally:
        await _stop_worker(agent)


@pytest.mark.asyncio
async def test_pane_activity_write_failure_is_surfaced(caplog):
    """A store write that raises must not be silent (brief requirement:
    failure surfacing / spec R4)."""
    store = MagicMock()
    store.write.side_effect = RuntimeError("disk is gone")
    agent = _agent_with_store(store)
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
    store = CaptureStore.open_memory()
    agent = _agent_with_store(store)
    agent._pane_activity_queue = asyncio.Queue(maxsize=1)
    # Fill the queue without letting the (not-yet-started) worker drain it.
    agent._pane_activity_queue.put_nowait((store, _pane_msg()))

    ctx = MagicMock()
    ctx.cwd = None
    ctx.channel_id = "tui-1"

    try:
        start = time.monotonic()
        await agent.handle_other(_pane_msg(), ctx)
        elapsed = time.monotonic() - start

        assert elapsed < 0.5, "handle_other must not block waiting for queue space"
        assert agent.pane_activity_write_failures == 1
    finally:
        await _stop_worker(agent)


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
