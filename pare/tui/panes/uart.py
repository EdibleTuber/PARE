"""The UART pane: renders device output AND what the capture did not get.

This is the feature the whole project started from. `console_read`'s reply
(`ConsoleSlice`, `pare.tui.sources.base`) carries more than bytes -- see
`pare-hardware-mcp/src/pare_hardware_mcp/tools.py:181-186` --

    `dropped` counts only bytes evicted by ring-buffer wraparound; it cannot
    see a capture that was SUSPENDED (a baud scan pausing the reader),
    because that leaves no byte range in cursor space at all, only a hole
    in time. `capture_gaps` exists so this byte-contiguous stream does not
    read as an unbroken one.

A pane that appends `for_display(slice.data)` to scrollback and ignores
`dropped`, `capture_gaps`, `alive`, and a clamped `limit_applied` displays a
lie: it shows an unbroken transcript over a capture that had holes. Spec R1
requires each of those fields make a visible difference on screen.
"""
from __future__ import annotations

from rich.text import Text

from pare.tui.panes.base import Pane
from pare.tui.sanitize import for_display
from pare.tui.sources.base import ConsoleSlice, ConsoleSource

# Provisional per spec R2: the real interval must come from a round-trip
# measurement against the DEPLOYED worker, and there is no deployed worker
# in this plan -- only FakeConsoleSource, an in-memory double with no I/O
# latency to measure. This number is picked only to be readable while
# iterating against the fake. Plan B must replace it with a value derived
# from a measurement, not inherit this one.
DEFAULT_UART_POLL_INTERVAL = 0.5


class UartPane(Pane):
    """A `Pane` that reads UART console output through a `ConsoleSource`
    and renders it alongside honesty markers for what was NOT captured.

    `render_lines()` is the plain-text seam the tests assert against: each
    line is either device text (sanitised through `for_display`, never
    decoded raw) or a marker. Markers are tracked separately from text as a
    `(text, is_marker)` pair rather than recognised by pattern-matching the
    string later -- a marker's distinctness in the actual widget (`render`,
    below) comes from a style applied to a flag the device can never set,
    not from a text prefix a malicious board could also emit. `render_lines`
    still gives markers a distinct textual framing for readability in
    tests/logs, but that framing alone is NOT the security boundary; see
    the module report for the caveat that plain-text framing is inherently
    spoofable by a board that can emit arbitrary printable bytes.
    """

    def __init__(
        self,
        source: ConsoleSource,
        *,
        read_limit: int | None = None,
        channel_id: str | None = None,
        cwd: str | None = None,
        poll_interval: float = DEFAULT_UART_POLL_INTERVAL,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(
            source,
            poll_interval=poll_interval,
            name=name,
            id=id,
            classes=classes,
        )
        self.read_limit = read_limit
        self.channel_id = channel_id
        self.cwd = cwd
        # (text, is_marker) pairs, oldest first. is_marker is what makes a
        # line visually distinct in `render()` -- it is never derived from
        # parsing the text itself, so device bytes cannot forge it.
        self._lines: list[tuple[str, bool]] = []

    async def attach(self) -> str | None:
        """Attach, do not own: a `None` session means no live console to
        read -- report it and offer `console_open` as an explicit operator
        action, never retry on a timer or as a side effect of being
        visible."""
        session = await super().attach()
        if session is None:
            self._mark(
                "no live console session -- run console_open to attach"
            )
        return session

    async def advance(self) -> ConsoleSlice | None:
        """One poll step, reading at most `self.read_limit` bytes.

        Overrides `Pane.advance()` (rather than relying on it unmodified)
        only to thread `read_limit` through to `source.read()`: the base
        implementation (`pare/tui/panes/base.py`, an earlier task's file)
        calls `self.source.read(self.cursor)` with no limit argument at
        all, which would make `read_limit` a dead constructor argument.
        Success/failure bookkeeping (cursor advance, backoff, healthy/
        last_error) is still delegated to the base's `_succeed`/`_fail`
        so this pane doesn't duplicate that logic.
        """
        try:
            slice_ = await self.source.read(self.cursor, self.read_limit)
        except Exception as exc:
            self._fail(exc)
            return None
        self._succeed(slice_)
        return slice_

    def on_slice(self, slice_: ConsoleSlice) -> None:
        """Append device text, then a marker for every honesty field this
        slice exercises. Order matches the read: text first, then what the
        read is telling you it could not give you."""
        text = for_display(slice_.data)
        if text:
            for line in text.splitlines():
                self._lines.append((line, False))

        if slice_.dropped:
            self._mark(
                f"{slice_.dropped} byte(s) dropped -- evicted by "
                "ring-buffer wraparound before they could be read"
            )

        for gap in slice_.capture_gaps:
            in_progress = " (still in progress)" if gap.get("in_progress") else ""
            self._mark(
                f"capture gap: suspended {gap['duration_s']:.1f}s at "
                f"cursor {gap['at_cursor']} ({gap['reason']}){in_progress}"
            )

        # `remaining > 0` is the documented way to tell a clamped read from
        # a buffer that happened to end exactly at limit_applied
        # (ConsoleSlice docstring, pare/tui/sources/base.py) -- limit_applied
        # alone cannot: it equals whatever ceiling was used whether or not
        # anything was left behind.
        if slice_.remaining > 0:
            self._mark(
                f"read capped at {slice_.limit_applied} byte(s) -- "
                f"{slice_.remaining} more byte(s) already waiting"
            )

        if not slice_.alive:
            self._mark("session ended -- device is no longer connected")

    def on_error(self, exc: Exception) -> None:
        self._mark(f"read failed: {exc}")

    def _mark(self, text: str) -> None:
        self._lines.append((text, True))

    def render_lines(self) -> list[str]:
        """Plain-text seam for tests: markers get a distinct framing so a
        human (or a diff) can tell them from device text at a glance, but
        see the class docstring -- the real distinctness guarantee is the
        `is_marker` flag driving `render()`'s styling, not this framing."""
        out = []
        for text, is_marker in self._lines:
            out.append(f"-- {text} --" if is_marker else text)
        return out

    def render(self) -> Text:
        """Actual widget content: markers get a style device text never
        gets, keyed off the tracked `is_marker` flag rather than the
        string -- so a board cannot forge the style by choosing bytes."""
        body = Text()
        for text, is_marker in self._lines:
            style = "bold yellow on grey23" if is_marker else ""
            body.append(text, style=style)
            body.append("\n")
        return body
