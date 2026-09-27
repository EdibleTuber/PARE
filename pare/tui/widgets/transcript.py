"""Turn accumulation for the chat transcript.

Ported from agent_core/adapters/cli.py:118-146 rather than imported: the CLI's
_TurnPrinter returns (text, end) tuples shaped for print(), and it is private.
The wire behaviour it encodes is what matters here, not its signature.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Literal

from agent_core.protocol import (
    ErrorMessage,
    LearningCandidateProposalMessage,
    ResponseMessage,
    StreamChunkMessage,
    ToolProgressMessage,
)

from rich.text import Text
from textual.events import Resize
from textual.timer import Timer
from textual.widgets import RichLog

from pare.tui.sanitize import clip_args


def is_turn_end(msg: object) -> bool:
    """A turn closes on ResponseMessage or ErrorMessage — the same rule the
    REPL's drain loop uses (agent_core/adapters/cli.py:202-203)."""
    return isinstance(msg, (ResponseMessage, ErrorMessage))


class TurnAccumulator:
    """Decides what text each message contributes, so a streamed turn's answer
    is not rendered twice. One instance per session; reset() between turns."""

    def __init__(self) -> None:
        self._streamed: list[str] = []

    def reset(self) -> None:
        self._streamed.clear()

    def feed(self, msg: object) -> str | None:
        if isinstance(msg, StreamChunkMessage):
            self._streamed.append(msg.token)
            return msg.token
        if isinstance(msg, ResponseMessage):
            if self._streamed and msg.text == "".join(self._streamed):
                return None      # duplicate of the stream: already rendered
            return msg.text
        if isinstance(msg, ErrorMessage):
            return f"Error: {msg.error}"
        if isinstance(msg, ToolProgressMessage):
            return f"  [{msg.tool}({clip_args(msg.arguments)})]"
        if isinstance(msg, LearningCandidateProposalMessage):
            return f"\n[Learning candidate: {msg.title}]\n{msg.body}\n"
        # Never drop an unrecognised type: a message we cannot render is still
        # evidence something happened. Mirrors _default_format's fallback
        # (agent_core/adapters/cli.py:115).
        return f"[unrendered {type(msg).__name__}]"


@dataclass(frozen=True)
class Committed:
    """One finished transcript entry. `kind` is "markdown" for model text
    (a reply, or the streamed part of one) and "plain" for everything the
    TUI itself phrases (tool lines, errors, fallbacks)."""

    kind: Literal["markdown", "plain"]
    text: str


class TurnRenderer:
    """Turns the daemon's message stream into finished transcript entries
    plus one in-progress reply (`live`).

    Stream tokens only grow `live`; nothing is committed per token. Every
    other message first commits `live` (so text streamed before it stays
    before it), then commits its own entry. The dedup rule is
    TurnAccumulator's, unchanged: a ResponseMessage equal to the whole
    turn's stream -- across tool lines -- adds nothing.
    """

    def __init__(self) -> None:
        self._acc = TurnAccumulator()
        self.live = ""

    def flush(self) -> list[Committed]:
        """Commit the in-progress text, if any, without ending the turn."""
        if not self.live:
            return []
        out = [Committed("markdown", self.live)]
        self.live = ""
        return out

    def end_turn(self) -> list[Committed]:
        """Close the turn without a terminator (the connection dropped):
        commit the in-progress text and reset the dedup state."""
        out = self.flush()
        self._acc.reset()
        return out

    def feed(self, msg: object) -> list[Committed]:
        if isinstance(msg, StreamChunkMessage):
            self._acc.feed(msg)
            self.live += msg.token
            return []
        out = self.flush()
        text = self._acc.feed(msg)
        if text:
            kind = "markdown" if isinstance(msg, ResponseMessage) else "plain"
            out.append(Committed(kind, text))
        if is_turn_end(msg):
            self._acc.reset()
        return out


class TranscriptLog(RichLog):
    """A RichLog that re-wraps its history when its width changes.

    RichLog renders each write to lines at the width it has at that moment
    and never revisits them, so after the layout key narrows the chat every
    earlier line is wider than the log (clipped), and after widening it
    they stay in a narrow column. This keeps what was written and, on a
    width change, clears and writes it all again.

    A replay runs synchronously on the event loop, and its cost grows with
    the lines it renders (measured before this bound: ~0.8 s per replay for
    5,000 one-line replies), so it is bounded twice over:

    - Debounced: a resize only (re)arms a REWRAP_DEBOUNCE timer, so a
      terminal drag replays once, at the final width.
    - Capped: the RichLog keeps at most HISTORY_LIMIT lines, and the
      history keeps only entries with at least one of those lines still in
      the log (each entry records the absolute line where it ends), so a
      replay renders about one log's worth of lines however long the
      replies are. The deque's maxlen is the same number as a hard ceiling:
      every entry renders to at least one line, so it can never evict an
      entry the line cap still shows.

    A replay at the same or a narrower width reproduces the visible lines
    exactly. After widening, the kept entries wrap to fewer lines, so
    scrollback shrinks; entries already dropped do not come back.
    """

    #: Lines kept on screen, and the ceiling on entries kept for re-wrap
    #: (one number, see the class docstring). ~40 screens of scrollback at
    #: 40 rows.
    HISTORY_LIMIT = 1500
    REWRAP_DEBOUNCE = 0.1

    def __init__(self, *args, **kwargs) -> None:
        # RichLog's default min_width (78) floors every render width, so in
        # a chat column narrower than that nothing could wrap to fit.
        kwargs.setdefault("min_width", 1)
        kwargs["max_lines"] = self.HISTORY_LIMIT
        super().__init__(*args, **kwargs)
        # (content, write kwargs, absolute line index where it ends)
        self._history: deque[tuple[object, dict, int]] = deque(maxlen=self.HISTORY_LIMIT)
        self._history_width: int | None = None
        self._rewrapping = False
        self._rewrap_timer: Timer | None = None

    def write(self, content, width=None, expand=False, shrink=True,
              scroll_end=None, animate=False):
        # Before the size is known RichLog queues the write and replays it
        # through write() later; record it once, on that replay.
        record = self._size_known and not self._rewrapping
        if record and isinstance(content, Text):
            content = content.copy()
        result = super().write(content, width, expand, shrink, scroll_end, animate)
        if record:
            kwargs = {"width": width, "expand": expand, "shrink": shrink}
            self._history.append((content, kwargs, self._line_end()))
            self._prune_history()
        return result

    def _line_end(self) -> int:
        """Absolute index one past the last line written. RichLog's trim
        advances `_start_line` by what it drops, so this only grows until a
        clear()."""
        return self._start_line + len(self.lines)

    def _prune_history(self) -> None:
        """Drop entries none of whose lines are still in the log."""
        while self._history and self._history[0][2] <= self._start_line:
            self._history.popleft()

    def on_resize(self, event: Resize) -> None:
        # Textual also runs RichLog.on_resize (after this one), which
        # replays the writes deferred until the first size is known.
        width = event.size.width
        if not self._size_known:
            # The deferred writes will be rendered at this width.
            self._history_width = width
            return
        if width == self._history_width and self._rewrap_timer is None:
            return
        # A write that lands inside the debounce window is recorded as usual
        # and RichLog renders it at the width it has then (the new one); the
        # replay below re-renders it with everything else. A drag that ends
        # back at the starting width still replays, since writes made
        # mid-drag were rendered at an intermediate width.
        if self._rewrap_timer is not None:
            self._rewrap_timer.stop()
        self._rewrap_timer = self.set_timer(self.REWRAP_DEBOUNCE, self._debounced_rewrap)

    def _debounced_rewrap(self) -> None:
        self._rewrap_timer = None
        self._history_width = self.size.width
        self._rewrap()

    def _rewrap(self) -> None:
        at_end = self.is_vertical_scroll_end
        self.clear()
        self._rewrapping = True
        replayed: list[tuple[object, dict, int]] = []
        try:
            for content, kwargs, _ in self._history:
                super().write(content, scroll_end=False, **kwargs)
                replayed.append((content, kwargs, self._line_end()))
        finally:
            self._rewrapping = False
        self._history.clear()
        self._history.extend(replayed)
        self._prune_history()
        if at_end:
            self.scroll_end(animate=False, immediate=False, x_axis=False)
