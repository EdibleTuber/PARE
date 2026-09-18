"""The pane/source boundary (Task 7): `Pane`, `PaneDock`, `ConsoleSource`,
`ConsoleSlice`.

The dock and the `Pane` base must be testable against a stub source alone --
Task 8's reusable `FakeConsoleSource` does not exist yet in this worktree
(it is a later task's deliverable, and imports `ConsoleSlice` from this
task), so the stub below is defined locally, only for these tests, and is
thrown away once Task 8 lands.

What these tests discriminate (spec I3 + the task brief):

  - One pane's failure does not affect another: a source that raises on
    every read must not stop a healthy sibling pane's cursor from
    advancing.
  - Backoff resets on success: a source that fails then succeeds must
    return its pane to the ORIGINAL poll interval, not merely retry while
    staying backed off. Asserting only "it retried" would pass a pane that
    is permanently backed off after its first failure; asserting the
    interval returns to `poll_interval` exactly would not.
  - The dock (and the `Pane` base) import nothing MCP-shaped -- checked by
    parsing the module source with `ast`, not by grepping for a substring
    that could appear in a comment.
"""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest
from textual.app import App, ComposeResult

from pare.tui.panes.base import Pane, PaneDock
from pare.tui.sources.base import ConsoleSlice, ConsoleSource

# Real time, kept small so the tests run fast without racing the event loop.
_POLL_INTERVAL = 0.02
_MAX_BACKOFF = 0.08


class _StubSource:
    """Minimal inline stand-in for a `ConsoleSource` (Task 8's
    `FakeConsoleSource` does not exist in this worktree -- see module
    docstring). Every `read()` call appends one byte at the requested
    cursor and reports the fields `console_read` returns, verbatim."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.reads = 0

    async def attach(self) -> str | None:
        return "session-1"

    async def read(self, cursor: int, limit: int | None = None) -> ConsoleSlice:
        self.reads += 1
        if self.fail:
            raise RuntimeError("stub source failure")
        return ConsoleSlice(
            data=b"x",
            next_cursor=cursor + 1,
            dropped=0,
            remaining=0,
            capture_gaps=[],
            alive=True,
            limit_applied=4096,
        )

    async def send(self, session: str, data: bytes) -> None:
        raise NotImplementedError

    async def status(self) -> dict:
        return {}


class _FlakySource:
    """Fails its first `fail_times` reads, then succeeds forever after --
    drives the backoff-resets discrimination."""

    def __init__(self, fail_times: int) -> None:
        self.fail_times = fail_times
        self.calls = 0

    async def attach(self) -> str | None:
        return "session-1"

    async def read(self, cursor: int, limit: int | None = None) -> ConsoleSlice:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("flaky source failure")
        return ConsoleSlice(
            data=b"x",
            next_cursor=cursor + 1,
            dropped=0,
            remaining=0,
            capture_gaps=[],
            alive=True,
            limit_applied=4096,
        )

    async def send(self, session: str, data: bytes) -> None:
        raise NotImplementedError

    async def status(self) -> dict:
        return {}


class _RecordingPane(Pane):
    """A `Pane` subclass that only records slices/errors -- no rendering,
    since the base's `on_slice`/`on_error` are no-ops by design."""

    def __init__(self, source: ConsoleSource, **kwargs) -> None:
        super().__init__(source, **kwargs)
        self.slices: list[ConsoleSlice] = []
        self.errors: list[Exception] = []

    def on_slice(self, slice_: ConsoleSlice) -> None:
        self.slices.append(slice_)

    def on_error(self, exc: Exception) -> None:
        self.errors.append(exc)


class _DockApp(App):
    def __init__(self, panes) -> None:
        super().__init__()
        self._panes = panes

    def compose(self) -> ComposeResult:
        yield PaneDock(self._panes, id="dock")


def _confirms_protocol(source: object) -> None:
    assert isinstance(source, ConsoleSource)


def test_stub_satisfies_console_source_protocol():
    # Sanity check on the stubs themselves before trusting them for the
    # real assertions below.
    _confirms_protocol(_StubSource())
    _confirms_protocol(_FlakySource(fail_times=1))


@pytest.mark.asyncio
async def test_one_pane_failure_does_not_affect_another():
    """Two docked panes, one source raising on every call: the healthy
    pane's cursor must keep advancing regardless."""
    healthy_source = _StubSource(fail=False)
    failing_source = _StubSource(fail=True)
    healthy = _RecordingPane(healthy_source, id="healthy", poll_interval=_POLL_INTERVAL,
                              max_backoff=_MAX_BACKOFF)
    failing = _RecordingPane(failing_source, id="failing", poll_interval=_POLL_INTERVAL,
                              max_backoff=_MAX_BACKOFF)

    app = _DockApp([healthy, failing])
    async with app.run_test() as pilot:
        await pilot.pause(0.2)

        assert healthy.cursor > 0, (
            "healthy pane made no progress while a sibling pane's source "
            "was raising on every call"
        )
        assert healthy.healthy is True
        assert healthy.errors == []

        assert failing.cursor == 0, "sanity: the failing pane's cursor must never advance"
        assert failing.healthy is False
        assert failing.errors, "failing pane must have rendered at least one error"
        assert isinstance(failing.errors[0], RuntimeError)


@pytest.mark.asyncio
async def test_backoff_resets_on_success_not_stays_backed_off():
    """A source that fails then succeeds must return its pane to the
    ORIGINAL poll interval -- not merely keep retrying while backed off.

    Discrimination: a permanently-backed-off implementation still retries
    (so "it retried" alone would pass); this asserts the interval is
    observed at its backed-off value first, and THEN observed back at
    exactly `poll_interval` once reads start succeeding.
    """
    flaky = _FlakySource(fail_times=3)
    pane = _RecordingPane(flaky, poll_interval=_POLL_INTERVAL, max_backoff=_MAX_BACKOFF)

    app = _DockApp([pane])
    async with app.run_test() as pilot:
        # Let the first few (failing) polls happen and back the pane off.
        await pilot.pause(_POLL_INTERVAL * 3.5)
        assert pane.errors, "sanity: the flaky source must have failed at least once"
        backed_off_interval = pane._interval
        assert backed_off_interval > pane.poll_interval, (
            "pane must be backed off above its normal poll interval after "
            "repeated failures"
        )

        # Let enough real time pass for the source to stop failing and for
        # at least one more poll (at the backed-off interval) to land.
        await pilot.pause(backed_off_interval * 2)

        assert pane.slices, "pane must have recovered at least one successful slice"
        assert pane._interval == pane.poll_interval, (
            f"pane interval is {pane._interval} after recovering -- expected it back "
            f"at the original poll_interval ({pane.poll_interval}), not left backed off "
            f"at {backed_off_interval}"
        )
        assert pane.healthy is True
        assert pane.cursor > 0


def _imported_module_roots(path: Path) -> set[str]:
    """Every top-level module name this file imports, via `ast` (not a
    substring grep, which a comment mentioning MCP could trip)."""
    tree = ast.parse(path.read_text())
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def test_pane_base_and_dock_have_no_mcp_import():
    base_path = Path(__file__).resolve().parent.parent / "pare" / "tui" / "panes" / "base.py"
    roots = _imported_module_roots(base_path)
    assert not any("mcp" in root.lower() for root in roots), (
        f"pare/tui/panes/base.py imports {roots!r} -- the dock and Pane base must not "
        "import anything MCP-shaped or any concrete source"
    )
    # And, narrowly, the two concrete-source modules this task must not
    # depend on don't exist as importable names at all.
    tree = ast.parse(base_path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert "mcp_console" not in node.module
            assert "fake_console" not in node.module


@pytest.mark.asyncio
async def test_dock_focus_cycles_between_panes():
    a = _RecordingPane(_StubSource(), id="pane-a", poll_interval=1.0)
    b = _RecordingPane(_StubSource(), id="pane-b", poll_interval=1.0)
    app = _DockApp([a, b])
    async with app.run_test() as pilot:
        await pilot.pause()
        dock = app.query_one(PaneDock)
        assert dock.panes == [a, b]

        a.focus()
        await pilot.pause()
        dock.focus_next_pane()
        await pilot.pause()
        assert app.screen.focused is b

        dock.focus_next_pane()
        await pilot.pause()
        assert app.screen.focused is a


@pytest.mark.asyncio
async def test_pane_attach_receives_none_session_without_becoming_unhealthy():
    """`attach() -> None` (no live session) is an expected state, not a
    failure -- it must not mark the pane unhealthy or trigger backoff."""

    class _NoSessionSource(_StubSource):
        async def attach(self) -> str | None:
            return None

    pane = _RecordingPane(_NoSessionSource(), poll_interval=_POLL_INTERVAL)
    app = _DockApp([pane])
    async with app.run_test() as pilot:
        await pilot.pause(_POLL_INTERVAL * 2)
        assert pane.session is None
        assert pane.healthy is True
        assert pane.cursor > 0, "pane must still poll and advance even with no session id"
