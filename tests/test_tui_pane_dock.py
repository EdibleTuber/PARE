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


#: The package `pare/tui/panes/base.py` lives in, used to resolve a relative
#: import (`from . import x`, `from .. import x`, ...) to an absolute dotted
#: path the same way Python's own import machinery would.
_PANES_PACKAGE = ("pare", "tui", "panes")


def _resolve_from_module(level: int, module: str | None) -> str:
    """Absolute dotted path a `from`-import's `module` resolves to, given
    `level` (`node.level`: 0 = absolute, 1 = current package, 2 = parent,
    ...). Mirrors CPython's own relative-import resolution."""
    if level == 0:
        return module or ""
    trim = level - 1
    bits = _PANES_PACKAGE[: len(_PANES_PACKAGE) - trim] if trim < len(_PANES_PACKAGE) else ()
    base = ".".join(bits)
    if module:
        return f"{base}.{module}" if base else module
    return base


def _all_import_targets(path: Path) -> set[str]:
    """Every fully-qualified dotted path this file imports, in EVERY
    spelling `import`/`from ... import ...` can take -- `alias.name`
    (the actual imported name/submodule), not just the module string, and
    with relative imports resolved to absolute. This is what closes the
    gap a module-string-only or root-only check misses: `from
    pare.tui.sources import mcp_console` has a module string of
    "pare.tui.sources" (innocuous on its own) and an imported NAME of
    "mcp_console" -- the name is the part that matters and is exactly what
    a root/module-string check skips.
    """
    tree = ast.parse(path.read_text())
    targets: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                # covers `import pare.tui.sources.mcp_console [as x]`: the
                # full dotted path is in alias.name regardless of asname.
                targets.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            resolved_module = _resolve_from_module(node.level, node.module)
            if resolved_module:
                targets.add(resolved_module)
            for alias in node.names:
                # covers `from pare.tui.sources import mcp_console [as x]`:
                # the module string alone ("pare.tui.sources") would pass
                # any check that stops there -- the imported NAME
                # ("mcp_console") is what has to be joined on and checked.
                targets.add(f"{resolved_module}.{alias.name}" if resolved_module else alias.name)
    return targets


def test_pane_base_and_dock_have_no_mcp_import():
    """Nothing under `pare.tui.sources` other than `base` may be imported by
    `pare/tui/panes/base.py` -- and no import anywhere in it may be
    MCP-shaped by name, regardless of which of `import`, `import ... as
    ...`, or `from ... import ...` (absolute or relative) spelled it.

    Requirement 2 is "the dock must not depend on any concrete source";
    resolving every import to its full dotted path and checking the actual
    imported name (not just a top-level root, and not just the `from`
    module string) is what makes this hold for `from pare.tui.sources
    import mcp_console` -- the natural way to import a sibling submodule --
    and not only for `import mcp_console` or `import ...mcp_console...` as
    a bare root.
    """
    base_path = Path(__file__).resolve().parent.parent / "pare" / "tui" / "panes" / "base.py"
    targets = _all_import_targets(base_path)

    assert not any("mcp" in target.lower() for target in targets), (
        f"pare/tui/panes/base.py imports {targets!r} -- the dock and Pane base must not "
        "import anything MCP-shaped or any concrete source"
    )

    sources_prefix = "pare.tui.sources"
    for target in targets:
        if target == sources_prefix or not target.startswith(sources_prefix + "."):
            continue
        remainder = target[len(sources_prefix) + 1 :]
        first_segment = remainder.split(".")[0]
        assert first_segment == "base", (
            f"pare/tui/panes/base.py imports {target!r} -- only "
            f"{sources_prefix}.base may be imported; any other submodule under "
            f"{sources_prefix} (mcp_console, fake_console, or anything future) is a "
            "concrete source the dock must not depend on"
        )


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
