"""The approval modal must collect an operator decision AND route it back to
the daemon WITHOUT stalling anything else on the event loop (spec I2).

Field names for both approval messages come from
agent_core/protocol/messages.py (verified against the installed package,
version 1.10.0):

    ToolApprovalRequestMessage(proposal_id, worker, tool, arguments,
                                declared_tier, effective_tier, type=...)
    ToolApprovalResponseMessage(proposal_id, approved, justification=None,
                                 scope="once", type=...)

The `critical`-never-session-approved rule mirrors
agent_core/workers/risk_pool.py:414 (`if effective != "critical" and
self.is_session_approved(...)`) and agent_core/adapters/cli.py:64-75 (a
critical tool additionally requires a non-empty justification to approve).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from agent_core.protocol import ToolApprovalRequestMessage, ToolApprovalResponseMessage
from textual.css.query import NoMatches
from textual.widgets import Button, Static

from pare.tui.app import PareTUI
from pare.tui.sanitize import clip_args
from pare.tui.widgets.approval import ApprovalModal


class _FakeSession:
    """Stands in for DaemonSession: records every message sent through it,
    without needing a real connected socket."""

    def __init__(self) -> None:
        self.sent: list[object] = []

    async def send(self, msg: object) -> None:
        self.sent.append(msg)


_POLLER_INTERVAL = 1 / 100  # seconds; 100 ticks/sec nominal


class _FakePoller(Static):
    """A stand-in for a device pane poller (Tasks 7-9 do not exist yet in
    this worktree). What matters for spec I2 is only that SOMETHING keeps
    advancing its own state on the event loop via Textual's own timer
    machinery while a modal sits on top -- exactly the shape a real pane's
    poll-and-render loop has."""

    def __init__(self) -> None:
        super().__init__("cursor=0", id="poller")
        self.cursor = 0

    def on_mount(self) -> None:
        self.set_interval(_POLLER_INTERVAL, self._tick)

    def _tick(self) -> None:
        self.cursor += 1
        self.update(f"cursor={self.cursor}")


class _AppUnderTest(PareTUI):
    """PareTUI plus one fake pane poller mounted alongside Header/Footer."""

    def compose(self):
        yield from super().compose()
        yield _FakePoller()


def _make_app() -> _AppUnderTest:
    app = _AppUnderTest(Path("/nonexistent/pare.sock"), "chan-1", "/tmp")
    app.session = _FakeSession()
    return app


def _make_request(**overrides) -> ToolApprovalRequestMessage:
    fields = dict(
        proposal_id="proposal-1",
        worker="hw0",
        tool="reset_board",
        arguments={"target": "hw0"},
        declared_tier="high",
        effective_tier="high",
    )
    fields.update(overrides)
    return ToolApprovalRequestMessage(**fields)


@pytest.mark.asyncio
async def test_pane_poller_keeps_making_progress_while_modal_is_open():
    """The discriminating observation is PROGRESS, not mere responsiveness:
    a poller's own cursor must advance while the modal sits open and
    unanswered. This would fail against any implementation that resolves the
    decision via a blocking/synchronous wait on the event loop.

    Measured against REAL wall-clock time (`loop.time()`), not a fixed tick
    count after a fixed pause: a synchronous block anywhere in the mount or
    open-and-waiting path -- including inside the modal's own on_mount,
    before this test gets a chance to sample a "before" cursor value at all
    -- still elapses real time while producing zero ticks during that
    stretch. Comparing ticks actually observed to ticks the nominal rate
    would predict for the real time that passed catches that; a plain
    "some progress happened somewhere in this pause" check would not, since
    a single tick sneaking in after a block released would still satisfy it.
    """
    app = _make_app()
    async with app.run_test() as pilot:
        poller = app.query_one("#poller", _FakePoller)
        await pilot.pause(0.05)
        baseline = poller.cursor
        assert baseline > 0, "sanity: the fake poller must progress at all before the modal opens"

        loop = asyncio.get_running_loop()
        t0 = loop.time()
        c0 = poller.cursor

        # A single fixed real-time pause spans triggering the request,
        # mounting the modal, AND idling with it open and unanswered --
        # deliberately no bare `await pilot.pause()` (idle-wait, cost
        # highly variable in a headless run) inside this measured window,
        # since its unpredictable overhead would swamp a small injected
        # block and hide exactly the regression this test exists to catch.
        app.handle_tool_approval_request(_make_request())
        await pilot.pause(0.3)
        assert isinstance(app.screen, ApprovalModal), "modal must be mounted and on top"

        elapsed = loop.time() - t0
        ticks = poller.cursor - c0
        expected = elapsed / _POLLER_INTERVAL

        assert ticks >= expected * 0.7, (
            f"pane poller advanced only {ticks} tick(s) over {elapsed:.3f}s "
            f"while the approval modal was open and unanswered (nominal "
            f"rate predicts ~{expected:.0f}) -- something is blocking the "
            "event loop"
        )
        # Never leave the daemon waiting at the end of the test.
        await pilot.press("escape")
        await pilot.pause()


@pytest.mark.asyncio
async def test_critical_request_offers_no_session_approval_option():
    app = _make_app()
    async with app.run_test() as pilot:
        app.handle_tool_approval_request(
            _make_request(effective_tier="critical", declared_tier="critical")
        )
        await pilot.pause()
        modal = app.screen
        assert isinstance(modal, ApprovalModal)

        with pytest.raises(NoMatches):
            modal.query_one("#approve-session", Button)

        await pilot.press("escape")
        await pilot.pause()


@pytest.mark.asyncio
async def test_high_request_offers_session_approval_option():
    """Asserting only the critical case's absence would pass against a modal
    that never offers session-approval at all -- this asserts presence too."""
    app = _make_app()
    async with app.run_test() as pilot:
        app.handle_tool_approval_request(_make_request(effective_tier="high"))
        await pilot.pause()
        modal = app.screen
        assert isinstance(modal, ApprovalModal)

        session_button = modal.query_one("#approve-session", Button)
        assert isinstance(session_button, Button)

        await pilot.press("escape")
        await pilot.pause()


@pytest.mark.asyncio
async def test_dismissal_without_a_decision_sends_deny():
    app = _make_app()
    request = _make_request()
    async with app.run_test() as pilot:
        app.handle_tool_approval_request(request)
        await pilot.pause()
        assert isinstance(app.screen, ApprovalModal)

        await pilot.press("escape")
        await pilot.pause()

        assert not isinstance(app.screen, ApprovalModal), "modal must actually be dismissed"
        assert len(app.session.sent) == 1
        response = app.session.sent[0]
        assert isinstance(response, ToolApprovalResponseMessage)
        assert response.proposal_id == request.proposal_id
        assert response.approved is False


@pytest.mark.asyncio
async def test_approve_once_sends_approved_response_with_once_scope():
    app = _make_app()
    request = _make_request()
    async with app.run_test() as pilot:
        app.handle_tool_approval_request(request)
        await pilot.pause()
        await pilot.click("#approve-once")
        await pilot.pause()

        assert len(app.session.sent) == 1
        response = app.session.sent[0]
        assert response.approved is True
        assert response.scope == "once"


@pytest.mark.asyncio
async def test_arguments_are_rendered_through_clip_args_not_raw():
    """Escape bytes in a request argument must not reach the rendered output
    raw -- the modal must render arguments through clip_args, exactly as the
    CLI does (agent_core/adapters/cli.py:31-46)."""
    app = _make_app()
    dangerous_arguments = {"payload": "abc\x1b[31mRED\x1b[0mdef\x07"}
    request = _make_request(arguments=dangerous_arguments)
    async with app.run_test() as pilot:
        app.handle_tool_approval_request(request)
        await pilot.pause()
        modal = app.screen
        assert isinstance(modal, ApprovalModal)

        rendered = modal.query_one("#approval-args", Static).content
        assert isinstance(rendered, str)
        assert "\x1b" not in rendered
        assert "\x07" not in rendered
        # And it must be the REAL sanitiser's output, not some ad hoc
        # stripping that happens to also remove ESC/BEL.
        assert rendered == clip_args(dangerous_arguments)

        await pilot.press("escape")
        await pilot.pause()
