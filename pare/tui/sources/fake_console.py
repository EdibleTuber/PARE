"""An in-memory ConsoleSource for development and tests.

The UART pane is built against this, not against the Pi: bench integration is
a separate plan, and this double can produce conditions a real board cannot be
made to produce on demand -- a ring-buffer eviction, a suspended capture, a
session that dies mid-read.

This must model `console_read`'s contract faithfully, not just its field
names -- the pane's tests pass or fail against THIS behaviour, and the pane
only ever meets the real behaviour on hardware this plan does not include.
Two subtleties matter enough to call out (see
`pare_hardware_mcp/ringbuffer.py:173-182` and
`pare_hardware_mcp/session.py:209-254` for the real formulas this mirrors):

- `dropped` is a pure function of `(cursor, oldest_retained)`, recomputed on
  every read -- it does NOT drain to zero after being reported once, and the
  bytes it counts are excluded from `data` (they fell off the front of the
  ring buffer; they were never returned).
- `capture_gaps` entries are dicts (`at_cursor`, `duration_s`, `reason`,
  `in_progress`), and a read only reports the gaps whose `at_cursor` falls
  inside the byte window that read just returned.
"""
from __future__ import annotations

from pare.tui.sources.base import ConsoleSlice


class FakeConsoleSource:
    """Scriptable console. feed/drop/gap/die/fail_next_read/fail_next_send
    set up a condition; read() reports it the way console_read would."""

    def __init__(self, session: str | None = "fake-1") -> None:
        self._session = session
        self._buf = bytearray()
        self._oldest_retained = 0
        self._gaps: list[dict] = []
        self._alive = True
        self._fail_read: Exception | None = None
        self._fail_send: Exception | None = None
        self.sent: list[bytes] = []

    # --- scripting -----------------------------------------------------
    def feed(self, data: bytes) -> None:
        self._buf.extend(data)

    def drop(self, n: int) -> None:
        """Evict `n` more bytes from the front of the ring buffer -- as if
        `n` freshly-written bytes had pushed them out. Advances
        `oldest_retained`, clamped so it can never run ahead of `head`
        (there is nothing to evict that has not been written yet)."""
        head = len(self._buf)
        self._oldest_retained = min(head, self._oldest_retained + n)

    def gap(self, at: int, duration_s: float = 0.5, reason: str = "baud-scan",
            in_progress: bool = False) -> None:
        self._gaps.append({
            "at_cursor": at,
            "duration_s": duration_s,
            "reason": reason,
            "in_progress": in_progress,
        })

    def die(self) -> None:
        self._alive = False

    def fail_next_read(self, exc: Exception) -> None:
        self._fail_read = exc

    def fail_next_send(self, exc: Exception) -> None:
        self._fail_send = exc

    # --- ConsoleSource -------------------------------------------------
    async def attach(self) -> str | None:
        return self._session

    async def status(self) -> dict:
        return {"session": self._session, "alive": self._alive}

    async def send(self, session: str, data: bytes) -> None:
        if self._fail_send is not None:
            exc, self._fail_send = self._fail_send, None
            raise exc
        self.sent.append(bytes(data))

    async def read(self, cursor: int = 0, limit: int | None = None) -> ConsoleSlice:
        if self._fail_read is not None:
            exc, self._fail_read = self._fail_read, None
            raise exc

        head = len(self._buf)
        dropped = max(0, self._oldest_retained - cursor)
        start = max(cursor, self._oldest_retained)
        available = head - start
        applied = available if limit is None else max(0, min(limit, available))
        data = bytes(self._buf[start:start + applied])
        next_cursor = start + applied
        remaining = head - next_cursor
        gaps = [g for g in self._gaps if start <= g["at_cursor"] <= next_cursor]

        return ConsoleSlice(
            data=data,
            next_cursor=next_cursor,
            dropped=dropped,
            remaining=remaining,
            capture_gaps=gaps,
            alive=self._alive,
            limit_applied=applied,
        )
