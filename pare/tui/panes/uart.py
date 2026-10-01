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

import base64
import logging
from typing import Protocol, runtime_checkable

from rich.text import Text
from textual.css.query import NoMatches
from textual.widgets import Input, RichLog

from pare.protocol import PaneActivityMessage
from pare.tui.panes.base import Pane
from pare.tui.sanitize import for_display
from pare.tui.sources.base import ConsoleSlice, ConsoleSource

logger = logging.getLogger(__name__)

# The one style device text never gets, applied to a marker line solely
# because of its `is_marker` flag (see `_append`). A board cannot forge it:
# the flag is set by this pane when a slice's honesty field fires, never by
# parsing bytes.
MARKER_STYLE = "bold yellow on grey23"

# Measured 2026-09-20 against pare-bench (100.97.133.126) over tailscale0.
# Median console_read RTT: 12.01ms, p95: 13.37ms (N=100, direct wire).
# Floor rule (spec §6.2): p95 * 2 = 26.74ms; clamped to the 0.05s reasonableness
# floor. Interactive ceiling 0.5s. Matches frida's ~50-100ms expected range.
# Re-measure when the endpoint, tailscale wire type, or transport changes.
DEFAULT_UART_POLL_INTERVAL = 0.05

# Observed bytes are batched across this many non-empty polls before being
# flushed as one PaneActivityMessage -- spec S5/req. 3: a message per poll
# floods the daemon's connection read loop, which awaits handle_other
# inline (task-3-report.md). A run of output that goes quiet flushes
# sooner, on its own boundary -- see `_accumulate_observed` -- so this is
# a latency CAP for a continuous stream, not the only trigger.
DEFAULT_OBSERVED_FLUSH_POLLS = 4


@runtime_checkable
class DaemonSessionLike(Protocol):
    """What `UartPane` needs from `pare.tui.session.DaemonSession` to log
    pane activity: just `send`. A Protocol (not the concrete class) so a
    test can pass a bare spy without constructing a real socket-backed
    session, matching how `ConsoleSource` above is a Protocol for the same
    reason."""

    async def send(self, msg: object) -> None: ...


class UartPane(Pane):
    """A `Pane` that reads UART console output through a `ConsoleSource`
    and renders it alongside honesty markers for what was NOT captured.

    `snapshot_lines()` is the plain-text seam the tests assert against: each
    line is either device text (sanitised through `for_display`, never
    decoded raw) or a marker. Markers are tracked separately from text as a
    `(text, is_marker)` pair rather than recognised by pattern-matching the
    string later -- a marker's distinctness in the actual display (the
    `#uart-log` child, written by `_append`) comes from a style applied to a
    flag the device can never set, not from a text prefix a malicious board
    could also emit. `snapshot_lines` still gives markers a distinct textual
    framing for readability in tests/logs, but that framing alone is NOT the
    security boundary; see the module report for the caveat that plain-text
    framing is inherently spoofable by a board that can emit arbitrary
    printable bytes.
    """

    DEFAULT_CSS = """
    #uart-log {
        height: 1fr;
    }
    """

    #: UART accepts operator input (`submit`, below). Overrides `Pane`'s
    #: default of False, which every other v1 pane (read-only) keeps.
    can_send = True

    #: `PaneActivityMessage.source` -- the worker this pane's traffic
    #: belongs to (`workers.yaml:110`'s `hardware` entry owns `console_*`).
    ACTIVITY_SOURCE = "hardware"

    def __init__(
        self,
        source: ConsoleSource,
        *,
        read_limit: int | None = None,
        channel_id: str | None = None,
        cwd: str | None = None,
        poll_interval: float = DEFAULT_UART_POLL_INTERVAL,
        daemon_session: DaemonSessionLike | None = None,
        observed_flush_polls: int = DEFAULT_OBSERVED_FLUSH_POLLS,
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
        # Task 9's callers (and Task 9's tests) construct `UartPane` with no
        # session/logging hook at all -- a daemon_session of None makes
        # `_log_activity` a silent no-op rather than a required argument,
        # so none of that existing construction breaks.
        self._daemon_session = daemon_session
        # A daemon that has died BUT not yet been detached (a window that
        # closes shortly after when `DaemonSession._read_loop` dispatches
        # `DaemonDisconnected` and the app calls `_set_pane_daemon_session
        # (None)`) can still be non-None here while its socket is gone --
        # so `_daemon_session.send` in `_log_activity` can raise. Mark the
        # first such failure so the operator sees the log is short; hold
        # the flag until a subsequent send succeeds, so a persistent
        # daemon-down streak produces one marker, not one per flush.
        self._log_send_failure_marked = False
        self._observed_flush_polls = observed_flush_polls
        # (text, is_marker) pairs, oldest first. is_marker is what makes a
        # line visually distinct in `render()` -- it is never derived from
        # parsing the text itself, so device bytes cannot forge it.
        self._lines: list[tuple[str, bool]] = []
        # Set when a read reports `alive=False`: the session is gone and
        # stays gone (only a raising attach enters connect-retry mode), so
        # there is nowhere to send. Distinct from `session is None` (never
        # had a session) -- both disable the input via `_sync_input_state`.
        self._session_dead = False
        # Observed-output batch pending a flush -- bytes only, no I/O; see
        # `_accumulate_observed`/`_flush_observed`.
        self._observed_buf = bytearray()
        self._observed_start: int | None = None
        self._observed_end: int = 0
        self._observed_polls_since_flush = 0
        self._observed_should_flush = False

    def compose(self):
        """The pane's display and input: a log the slices are written into
        (the widget shows its children, so there is no `render()` of the
        whole line list any more) and a line input docked below it. The
        log is not keyboard-focusable -- it is read with the wheel, and
        stealing Tab stops would put the caret away from the input."""
        log = RichLog(id="uart-log", wrap=True, min_width=1)
        log.can_focus = False
        yield log
        yield Input(id="uart-input", disabled=True)

    def on_focus(self) -> None:
        """A focus that lands on the pane container moves to the pane's
        input. `PaneDock`'s focus cycle calls `pane.focus()` on the pane
        itself, and that is how an operator gets to the console -- the
        caret has to end up somewhere they can type. When focus is ALREADY
        on the input (the event bubbling up from it), nothing moves; when
        the input is disabled (no session) the focus stays on the pane.

        NOTE: a Textual 8.x message is dispatched to EVERY handler found
        in the MRO (not just the most-derived one) -- that is why the
        mount/input-setup work lives in `attach()` (plain virtual
        dispatch) rather than an `on_mount` override, which would run the
        base's `Pane.on_mount` -- and its `attach()` -- a second time.
        """
        if self.screen is None or self.screen.focused is not self:
            return
        try:
            inp = self.query_one("#uart-input", Input)
        except NoMatches:
            return
        if not inp.disabled:
            inp.focus()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter in the pane's input: send the line through `submit` (the
        ungated write path, spec S3.2) and clear the field. A bare Enter
        (empty line) sends a lone newline -- what a bare Enter does at a
        real console. The pane has exactly one input child, and a disabled
        input cannot hold focus, so nothing else can submit here."""
        if event.input.disabled:
            return
        line = event.input.value
        event.input.clear()
        await self.submit(line)

    async def attach(self) -> str | None:
        """Attach, do not own: a `None` session means no live console to
        read -- report it and offer `console_open` as an explicit operator
        action, never retry on a timer or as a side effect of being
        visible.

        This is the one place the session can be set (mount AND every
        connect retry go through it), so the input's enabled state is
        synced here -- see `on_focus`'s note on why not in `on_mount`.
        The session is passed EXPLICITLY: the base assigns
        `self.session = await self.attach()` only after this returns, so
        `self.session` is still the stale value in here."""
        session = await super().attach()
        if session is None:
            self._mark(
                "no live console session -- run console_open to attach"
            )
        self._sync_input_state(session)
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
        # `_succeed` calls `on_slice` synchronously (base.py's contract --
        # it is not awaited there), so the accumulation into
        # `_observed_buf` above is pure bytes bookkeeping, no I/O. The
        # actual log send is async and belongs here, in `advance` (already
        # async), not inside `on_slice`.
        if self._observed_should_flush:
            await self._flush_observed()
        return slice_

    def on_slice(self, slice_: ConsoleSlice) -> None:
        """Append device text, then a marker for every honesty field this
        slice exercises. Order matches the read: text first, then what the
        read is telling you it could not give you."""
        self._accumulate_observed(slice_)

        text = for_display(slice_.data)
        if text:
            for line in text.splitlines():
                self._append(line, False)

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
            self._session_dead = True
            self._sync_input_state(self.session)

    def on_error(self, exc: Exception) -> None:
        self._mark(f"read failed: {exc}")

    def on_attach_error(self, exc: Exception) -> None:
        """A failed CONNECT (the pane never attached) is rendered
        differently from a read failure: it names the fact that the pane
        is retrying, because the base keeps retrying `attach()` on its own
        backoff timer (spec I3). The raw exception text is kept verbatim --
        note that the mcp client SDK renders an HTTP 404 (which a mistyped
        endpoint, e.g. one missing the worker's `/mcp` path, produces) as
        'Session terminated', so the operator should be told to check the
        endpoint when that text appears here."""
        self._mark(f"connect failed: {exc} -- retrying (check the endpoint "
                   "URL; the worker serves its MCP endpoint at /mcp)")

    def _mark(self, text: str) -> None:
        self._append(text, True)

    def _append(self, text: str, is_marker: bool) -> None:
        """Append one line to the pane's record and, while the pane is
        mounted, to its visible log as well.

        The log follows the tail while the operator is at the tail and
        holds still while they are scrolled up reading: `scroll_end` is
        decided per write by `is_vertical_scroll_end` AT WRITE TIME, so a
        fresh console stays pinned to the newest output, scrolling up is
        never yanked away, and following resumes once they reach the tail.

        `is_marker` is the sole input to styling -- markers get
        `MARKER_STYLE`, device text gets none, keyed off the flag the
        device cannot set, not off the text (class docstring).

        The guard is "the log child exists", NOT `is_mounted`: during
        `on_mount` (where `attach()` runs) the composed children already
        exist but `is_mounted` is still False, and a write there is simply
        deferred by the RichLog until its size is known -- so a mount-time
        line (the no-session marker) still reaches the display. A bare
        `attach()`/`advance()` pane (the unmounted tests) has no children
        at all: NoMatches, and only the `_lines` record is kept.
        """
        self._lines.append((text, is_marker))
        try:
            log = self.query_one("#uart-log", RichLog)
        except NoMatches:
            return
        follow = log.is_vertical_scroll_end
        if is_marker:
            log.write(Text(text, style=MARKER_STYLE), scroll_end=follow)
        else:
            log.write(text, scroll_end=follow)

    def _sync_input_state(self, session: str | None) -> None:
        """(Re)apply the input's enabled state and hint from the session
        state: with no session (or a dead one) there is nowhere to send,
        so the input says so instead of accepting lines that can only
        end as 'send failed' markers. The session value is always passed
        explicitly (see `attach` for why `self.session` cannot be read
        there). NoMatches-tolerant: a bare `attach()` (unmounted tests)
        has no input child to sync."""
        try:
            inp = self.query_one("#uart-input", Input)
        except NoMatches:
            return
        live = session is not None and not self._session_dead
        inp.disabled = not live
        inp.placeholder = (
            "type a line -- Enter sends it to the device"
            if live
            else "no live console session -- nothing to send to"
        )

    # --- write path (operator -> device) --------------------------------

    async def submit(self, line: str) -> None:
        """Send one line to the device, sent on Enter -- v1 is line-at-a-
        time local editing (spec S3.3); raw per-keystroke mode is deferred.

        Deliberately NOT gated (spec S3.2): this calls `Pane.send` ->
        `self.source.send` directly, with no approval prompt, no daemon
        tool-pool call. The risk gate exists to contain a WORKER the model
        dispatched to and cannot trust to self-report its wire tier
        (`workers.yaml:113-118`); an operator typing at a console they are
        already looking at is not that caller, and gating them would mean
        prompting them to approve the key they just pressed.

        The write is attempted first, in a `try`; the "sent" log is
        emitted in the `finally` that follows it, unconditionally -- so an
        exception from `source.send` (scripted via
        `FakeConsoleSource.fail_next_send`, or a pane with no live
        session) can never suppress the record of what the operator tried
        to send (req. 2). Only on failure does anything become visible in
        the pane, via a marker (req. 4) -- there is no other state here
        (submit never touches `self.cursor`, which belongs to the read
        path) that could be advanced as though the write had succeeded.
        """
        data = line.encode("utf-8", errors="replace") + b"\n"
        error: Exception | None = None
        try:
            await super().send(data)
        except Exception as exc:
            error = exc
        finally:
            await self._log_activity("sent", data, self.cursor, self.cursor)
        if error is not None:
            self._mark(f"send failed: {error}")

    # --- observed-output batching (device -> log) -----------------------

    def _accumulate_observed(self, slice_: ConsoleSlice) -> None:
        """Fold one poll's bytes into the pending observed batch. Pure
        bookkeeping -- no I/O, no `await` -- because `on_slice` is called
        synchronously from `_succeed` (base.py); the actual flush (async)
        happens back in `advance`, gated on `_observed_should_flush`.

        Two flush triggers, either sets `_observed_should_flush`:
          - the batch has now been fed by `_observed_flush_polls`
            non-empty polls in a row -- a latency cap so a continuous
            stream still flushes periodically instead of growing forever;
          - this poll came back EMPTY while a batch was already pending --
            the stream just went quiet, which is the natural place to
            close out a burst as one message. An empty poll never starts
            or extends a batch, and never flushes an already-empty one, so
            an empty poll on its own produces no observed log at all.
        """
        if not slice_.data:
            if self._observed_buf:
                self._observed_should_flush = True
            return

        if not self._observed_buf:
            self._observed_start = slice_.next_cursor - len(slice_.data)
        self._observed_buf.extend(slice_.data)
        self._observed_end = slice_.next_cursor
        self._observed_polls_since_flush += 1
        if self._observed_polls_since_flush >= self._observed_flush_polls:
            self._observed_should_flush = True

    async def _flush_observed(self) -> None:
        """Emit the pending observed batch as one PaneActivityMessage and
        reset it. A no-op if nothing is pending (defensive: nothing today
        sets `_observed_should_flush` on an empty buffer, but a future
        caller flushing unconditionally -- e.g. on detach -- should not
        emit an empty "observed" record)."""
        self._observed_should_flush = False
        if not self._observed_buf:
            return
        data = bytes(self._observed_buf)
        start = self._observed_start if self._observed_start is not None else self._observed_end
        await self._log_activity("observed", data, start, self._observed_end)
        self._observed_buf.clear()
        self._observed_start = None
        self._observed_polls_since_flush = 0

    async def _log_activity(
        self, kind: str, data: bytes, cursor_start: int, cursor_end: int
    ) -> None:
        """Emit one `PaneActivityMessage` through the daemon session, if
        this pane was built with one. `daemon_session=None` (Task 9's
        construction, and every existing caller/test of `UartPane`) makes
        this a silent no-op rather than a required collaborator -- a pane
        with nowhere to log to still reads and writes, it just isn't
        recorded.
        """
        if self._daemon_session is None:
            return
        msg = PaneActivityMessage(
            source=self.ACTIVITY_SOURCE,
            session=self.session or "",
            kind=kind,
            data_b64=base64.b64encode(data).decode("ascii"),
            cursor_start=cursor_start,
            cursor_end=cursor_end,
            channel_id=self.channel_id,
            cwd=self.cwd,
        )
        try:
            await self._daemon_session.send(msg)
        except Exception as exc:
            # Task 13 (b) closed: the socket can die between a real daemon
            # crash and `DaemonDisconnected` reaching the app, and either
            # caller (`_flush_observed` from a Textual timer, `submit` from
            # user input) would otherwise raise uncaught here. R4's "not
            # silent" is met by `logger.exception` plus a visible marker on
            # the first failure of a streak.
            logger.exception("pane activity log send failed (kind=%s)", kind)
            if not self._log_send_failure_marked:
                self._mark(f"log send failed: {exc}")
                self._log_send_failure_marked = True
        else:
            self._log_send_failure_marked = False

    def snapshot_lines(self) -> list[str]:
        """Plain-text seam for tests: markers get a distinct framing so a
        human (or a diff) can tell them from device text at a glance, but
        see the class docstring -- the real distinctness guarantee is the
        `is_marker` flag driving `_append`'s styling of the `#uart-log`
        display, not this framing."""
        out = []
        for text, is_marker in self._lines:
            out.append(f"-- {text} --" if is_marker else text)
        return out
