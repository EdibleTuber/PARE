"""The `Pane` widget base and the dock that manages panes.

A `Pane` owns exactly one `ConsoleSource` (`pare.tui.sources.base`), its own
read cursor, and its own poll timer. `PaneDock` owns mounting/unmounting
panes, focus-cycling between them, and their layout -- nothing here imports
`pare/tui/sources/mcp_console.py` or any concrete source; both classes are
built and tested entirely against the `ConsoleSource` Protocol, which is why
`tests/test_tui_pane_dock.py` can drive them with a stub defined inline in
the test file rather than any real source. The only pane implemented in v1
is UART (a later task) -- this module is the boundary it is built on.

Spec I3: a pane whose source errors renders the error in place and keeps
retrying with backoff; the chat and other panes stay unaffected. This
covers the CONNECT path as well as reads: a pane whose `attach()` raises
at mount (the real failure behind a mistyped endpoint) renders the error
in place and retries `attach()` with backoff on its own timer -- the
exception never escapes to the app. Each pane
has its own `set_interval` timer (Textual's polling primitive -- see
`tests/test_tui_approval.py`'s `_FakePoller` for the established pattern in
this codebase), so one pane's backoff, or one pane's source raising on every
call, never delays or starves another pane's timer.
"""
from __future__ import annotations

from typing import Sequence

from textual.containers import Vertical
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import Static

from pare.tui.sources.base import ConsoleSlice, ConsoleSource


class Pane(Widget):
    """Base widget for one device pane the dock manages.

    Declares:
      - how it attaches (`attach`, called once from `on_mount`);
      - how it advances (`advance`: read from `self.cursor`, apply the
        slice, or record a failure and back off -- spec I3);
      - how it reports source health (`healthy`, `last_error`);
      - whether it accepts input (`can_send`, `accepts_input`, `send`).

    Subclasses render by overriding `on_slice` (successful read),
    `on_error` (failed read), and `on_attach_error` (failed connect); all
    are no-ops by default so a subclass that only needs cursor/backoff
    bookkeeping -- as in this task's tests -- requires no rendering code at
    all. Nothing here or in `PaneDock` names
    MCP or any concrete source; a `Pane` works against any object
    satisfying `ConsoleSource`.
    """

    can_focus = True

    #: Whether this pane can send data back through its source. False for
    #: every v1 pane by default; a future input-capable pane sets this True.
    can_send: bool = False

    DEFAULT_POLL_INTERVAL = 0.5
    DEFAULT_MAX_BACKOFF = 8.0

    def __init__(
        self,
        source: ConsoleSource,
        *,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        max_backoff: float = DEFAULT_MAX_BACKOFF,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self.source = source
        self.poll_interval = poll_interval
        self.max_backoff = max_backoff
        self.session: str | None = None
        self.cursor: int = 0
        self.healthy: bool = True
        self.last_error: str | None = None
        self._interval = poll_interval
        self._timer: Timer | None = None
        # Non-None while the pane is in connect-retry mode: the most recent
        # `attach()` exception. Distinct from a read failure (a pane that IS
        # attached, whose source then raises): while this is set, the poll
        # tick retries `attach()` itself rather than reading (spec I3).
        self._attach_failed: Exception | None = None

    async def on_mount(self) -> None:
        """Attach once, then start polling at the normal interval.

        A `None` session id (no live session -- `attach`'s contract) is not
        a failure: it does not touch `healthy` or the poll interval. Only
        `advance()`'s own read failures drive backoff.

        An `attach()` EXCEPTION is a failure (spec I3): it is rendered in
        place via `on_attach_error` and retried with backoff on this pane's
        own timer -- it never escapes to the app.
        """
        try:
            self.session = await self.attach()
        except Exception as exc:
            self._attach_failed = exc
            self._fail_attach(exc)
            return
        self._reschedule(self.poll_interval)

    async def attach(self) -> str | None:
        return await self.source.attach()

    async def advance(self) -> ConsoleSlice | None:
        """Read the next slice from `self.cursor` and apply it.

        Never raises: a source failure is caught here, turned into
        `healthy = False` / `last_error`, rendered via `on_error`, and
        answered with bounded backoff on THIS pane's own timer only --
        never a shared one, so it cannot affect any other pane. Returns the
        slice on success, `None` on failure, so a caller can tell the two
        apart without inspecting state afterwards.
        """
        try:
            slice_ = await self.source.read(self.cursor)
        except Exception as exc:
            self._fail(exc)
            return None
        self._succeed(slice_)
        return slice_

    def _fail(self, exc: Exception) -> None:
        self.healthy = False
        self.last_error = str(exc)
        self.on_error(exc)
        self._backoff()

    def _fail_attach(self, exc: Exception) -> None:
        """Bookkeeping for a failed `attach()` (a CONNECT failure, not a
        read failure): same healthy/last_error/backoff shape as `_fail`,
        rendered through `on_attach_error` so a subclass can tell the two
        apart in its in-place marker."""
        self.healthy = False
        self.last_error = str(exc)
        self.on_attach_error(exc)
        self._backoff()

    def _backoff(self) -> None:
        backed_off = min(self._interval * 2, self.max_backoff)
        if backed_off != self._interval:
            self._reschedule(backed_off)

    def _succeed(self, slice_: ConsoleSlice) -> None:
        self.cursor = slice_.next_cursor
        self.healthy = True
        self.last_error = None
        if self._interval != self.poll_interval:
            self._reschedule(self.poll_interval)
        self.on_slice(slice_)

    def _reschedule(self, interval: float) -> None:
        """Replace the poll timer with one running at `interval`.

        `textual.timer.Timer` exposes no way to change an already-running
        timer's interval (`Timer.reset()` restarts it at the SAME
        interval), so both backing off and resetting work by stopping the
        current timer and starting a fresh one via `Widget.set_interval`.
        """
        if self._timer is not None:
            self._timer.stop()
        self._interval = interval
        self._timer = self.set_interval(interval, self._poll_tick)

    async def _poll_tick(self) -> None:
        if self._attach_failed is not None:
            await self._retry_attach()
            return
        await self.advance()

    async def _retry_attach(self) -> None:
        """One connect retry. On success, leave connect-retry mode and
        resume the normal poll interval; on failure, record it and back
        off until the next retry. A `None` session is NOT a failure
        (`attach`'s contract -- see `UartPane.attach`): it leaves retry
        mode and hands the pane to the read path, which reports it."""
        try:
            self.session = await self.attach()
        except Exception as exc:
            self._attach_failed = exc
            self._fail_attach(exc)
            return
        self._attach_failed = None
        self._reschedule(self.poll_interval)

    def on_slice(self, slice_: ConsoleSlice) -> None:
        """Render a successful read. No-op by default; subclasses override."""

    def on_error(self, exc: Exception) -> None:
        """Render a READ failure in place (spec I3). No-op by default;
        subclasses override."""

    def on_attach_error(self, exc: Exception) -> None:
        """Render a CONNECT failure (a raising `attach`) in place (spec
        I3). No-op by default; subclasses override."""

    def accepts_input(self) -> bool:
        return self.can_send

    async def send(self, data: bytes) -> None:
        if not self.can_send:
            raise RuntimeError(f"{type(self).__name__} does not accept input")
        if self.session is None:
            raise RuntimeError("pane has no live session to send to")
        await self.source.send(self.session, data)


class PaneDock(Vertical):
    """Mounts, lays out, and focus-cycles a set of `Pane` widgets.

    Owns lifecycle (mounting/unmounting panes), focus (cycling Textual
    focus between them), and layout (a vertical stack by default, with an
    optional one-line title strip above the panes; a titled dock with no
    panes renders just the strip). Knows NOTHING about
    `ConsoleSource`, MCP, or any concrete source: every method here
    operates purely on the `Pane` interface, which is what lets it be
    tested against a stub source alone.
    """

    def __init__(
        self,
        panes: Sequence[Pane] = (),
        *,
        id: str | None = None,
        title: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self._panes: list[Pane] = list(panes)
        self._title = title

    def compose(self):
        if self._title is not None:
            yield Static(self._title, classes="pane-label")
        yield from self._panes

    @property
    def panes(self) -> list[Pane]:
        """A snapshot of the docked panes, in dock order."""
        return list(self._panes)

    async def add_pane(self, pane: Pane) -> None:
        """Mount an additional pane at runtime."""
        self._panes.append(pane)
        await self.mount(pane)

    async def remove_pane(self, pane: Pane) -> None:
        """Unmount and stop tracking one pane.

        Does not affect any other docked pane: each pane owns its own poll
        timer and cursor, so removing one never touches another's state.
        """
        self._panes.remove(pane)
        await pane.remove()

    def focus_next_pane(self) -> None:
        """Cycle Textual focus to the next docked pane."""
        self._cycle_focus(1)

    def focus_previous_pane(self) -> None:
        """Cycle Textual focus to the previous docked pane."""
        self._cycle_focus(-1)

    def _cycle_focus(self, step: int) -> None:
        if not self._panes:
            return
        current = self.screen.focused if self.screen is not None else None
        try:
            index = self._panes.index(current)
        except ValueError:
            index = -1 if step > 0 else 0
        self._panes[(index + step) % len(self._panes)].focus()
