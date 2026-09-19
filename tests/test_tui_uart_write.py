"""The UART pane's write path: `submit(line)`.

Four things must hold (task-11-brief.md):

- a failed write is still logged (`kind="sent"`), and surfaces visibly in
  the pane -- an operator needs the record of a write that did NOT reach
  the device, not just the ones that did;
- observed output is logged too, but batched: several polls must produce
  fewer log messages than polls, and an empty poll must never produce an
  observed log on its own;
- `channel_id`/`cwd` are present on every emitted message;
- sending is not gated -- `submit` must go straight to `source.send`, never
  through anything approval-shaped.

All against the real `FakeConsoleSource` (`pare/tui/sources/fake_console.py`)
and a bare spy standing in for `pare.tui.session.DaemonSession` (only
`.send` is used through `DaemonSessionLike`, so the spy needs nothing else).
"""
from __future__ import annotations

import base64

import pytest

from pare.protocol import PaneActivityMessage
from pare.tui.panes.uart import UartPane
from pare.tui.sources.fake_console import FakeConsoleSource


class SpySession:
    """Stands in for `pare.tui.session.DaemonSession`: records every
    message handed to `send` and nothing else -- `UartPane` only ever
    calls `.send` on this collaborator (`DaemonSessionLike`)."""

    def __init__(self) -> None:
        self.sent: list[object] = []

    async def send(self, msg: object) -> None:
        self.sent.append(msg)


async def _attached_pane(**kwargs):
    src = FakeConsoleSource()
    log = SpySession()
    pane = UartPane(source=src, daemon_session=log, channel_id="ch-1", cwd="/tmp/proj", **kwargs)
    # `Pane.on_mount` (never run here -- no Textual app is mounted) is what
    # normally stores `attach()`'s result on `self.session`; do that step
    # by hand, the same way `test_tui_uart_read.py`'s `_pane_after` does
    # for the read path (which never needs `self.session` at all).
    pane.session = await pane.attach()
    return pane, src, log


def _by_kind(log: SpySession, kind: str) -> list[PaneActivityMessage]:
    return [m for m in log.sent if isinstance(m, PaneActivityMessage) and m.kind == kind]


async def test_submit_logs_a_sent_message():
    pane, src, log = await _attached_pane()

    await pane.submit("hello")

    sent = _by_kind(log, "sent")
    assert len(sent) == 1
    assert base64.b64decode(sent[0].data_b64) == b"hello\n"
    assert sent[0].cursor_start == sent[0].cursor_end


async def test_submit_reaches_the_source_directly():
    pane, src, log = await _attached_pane()

    await pane.submit("hello")

    assert src.sent == [b"hello\n"]


async def test_failed_write_is_still_logged_and_shown():
    """The discriminating case: a write that raises must still produce a
    "sent" log (req. 2) and a visible marker in the pane (req. 4) -- and
    must not be silently indistinguishable from a write that succeeded."""
    pane, src, log = await _attached_pane()
    src.fail_next_send(RuntimeError("device gone"))

    await pane.submit("hello")

    sent = _by_kind(log, "sent")
    assert len(sent) == 1, "a failed write must still be logged"
    assert base64.b64decode(sent[0].data_b64) == b"hello\n"

    assert any(is_marker for _, is_marker in pane._lines), (
        "a failed write must surface a visible marker in the pane"
    )
    rendered = "\n".join(pane.render_lines())
    assert "send failed" in rendered
    assert "device gone" in rendered

    # And the source really did see (and reject) the attempt -- this is
    # not a write that silently never happened.
    assert src.sent == []


async def test_observed_output_is_batched_across_polls():
    """Several polls, each with data, must not each produce their own
    observed log -- that is exactly the per-poll flood req. 3 forbids."""
    pane, src, log = await _attached_pane()

    num_polls = 8
    for i in range(num_polls):
        src.feed(f"line {i}\n".encode())
        await pane.advance()

    observed = _by_kind(log, "observed")
    assert len(observed) < num_polls
    assert len(observed) >= 1  # the flush cap must still fire eventually

    # No bytes are lost across the batching -- concatenating every
    # observed message's payload reproduces everything fed.
    all_observed = b"".join(base64.b64decode(m.data_b64) for m in observed)
    expected = b"".join(f"line {i}\n".encode() for i in range(num_polls))
    assert all_observed == expected


async def test_empty_poll_produces_no_observed_log():
    pane, src, log = await _attached_pane()

    await pane.advance()  # nothing fed yet -- an empty read

    assert _by_kind(log, "observed") == []


async def test_quiet_poll_flushes_a_pending_batch():
    """A burst of output followed by a quiet poll closes out the batch on
    that boundary, rather than holding it until the poll-count cap."""
    pane, src, log = await _attached_pane(observed_flush_polls=100)

    src.feed(b"boot ok\n")
    await pane.advance()
    assert _by_kind(log, "observed") == [], "not flushed yet -- still pending"

    await pane.advance()  # empty poll: the stream just went quiet

    observed = _by_kind(log, "observed")
    assert len(observed) == 1
    assert base64.b64decode(observed[0].data_b64) == b"boot ok\n"


async def test_channel_id_and_cwd_present_on_every_message():
    pane, src, log = await _attached_pane()

    await pane.submit("ping")           # a "sent" message
    src.feed(b"hi\n")
    await pane.advance()                # buffered, not yet flushed
    await pane.advance()                # empty poll: flushes the "observed"

    kinds = {m.kind for m in log.sent}
    assert kinds == {"sent", "observed"}, "expected one of each kind logged"
    for msg in log.sent:
        assert isinstance(msg, PaneActivityMessage)
        assert msg.channel_id == "ch-1"
        assert msg.cwd == "/tmp/proj"


async def test_no_daemon_session_makes_logging_a_silent_no_op():
    """Task 9's construction (`UartPane(source=src)`, no daemon_session)
    must keep working -- submit and advance must not raise just because
    there is nowhere to log to."""
    src = FakeConsoleSource()
    pane = UartPane(source=src)
    pane.session = await pane.attach()

    await pane.submit("hello")
    src.feed(b"hi\n")
    await pane.advance()

    assert src.sent == [b"hello\n"]


async def test_submit_requests_no_approval_and_takes_no_gate_path():
    """Regression guard for a future "hardening": submit must call
    `source.send` directly and must never construct, send, or wait on
    anything approval-shaped."""
    from agent_core.protocol import ToolApprovalRequestMessage

    pane, src, log = await _attached_pane()

    await pane.submit("hello")

    assert src.sent == [b"hello\n"], "must reach the source directly"
    assert not any(
        isinstance(m, ToolApprovalRequestMessage) for m in log.sent
    ), "submit must never emit an approval-shaped message"
    # Every message actually emitted is exactly the pane-activity log --
    # nothing else went out over the same channel a gate would use.
    assert all(isinstance(m, PaneActivityMessage) for m in log.sent)


async def test_uart_pane_has_no_tool_pool_collaborator():
    """Structural guard: `UartPane` has no tool-pool-shaped attribute at
    all today. A future "hardening" patch that threads one in and routes
    `submit` through it changes this -- which is the regression this
    plan's brief names explicitly."""
    pane, src, log = await _attached_pane()
    assert not hasattr(pane, "tool_pool")
    assert not hasattr(pane, "_tool_pool")
