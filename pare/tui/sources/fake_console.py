"""An in-memory ConsoleSource for development and tests.

The UART pane is built against this, not against the Pi: bench integration is
a separate plan, and this double can produce conditions a real board cannot be
made to produce on demand — a ring-buffer eviction, a suspended capture, a
session that dies mid-read.
"""
from __future__ import annotations

from pare.tui.sources.base import ConsoleSlice


class FakeConsoleSource:
    """Scriptable console. feed/drop/gap/die/fail_next set up a condition;
    read() reports it the way console_read would."""

    def __init__(self, session: str | None = "fake-1") -> None:
        self._session = session
        self._buf = bytearray()
        self._dropped = 0
        self._gaps: list[int] = []
        self._alive = True
        self._fail: Exception | None = None
        self.sent: list[bytes] = []

    # --- scripting -----------------------------------------------------
    def feed(self, data: bytes) -> None:
        self._buf.extend(data)

    def drop(self, n: int) -> None:
        self._dropped += n

    def gap(self, at: int) -> None:
        self._gaps.append(at)

    def die(self) -> None:
        self._alive = False

    def fail_next(self, exc: Exception) -> None:
        self._fail = exc

    # --- ConsoleSource -------------------------------------------------
    async def attach(self) -> str | None:
        return self._session

    async def status(self) -> dict:
        return {"session": self._session, "alive": self._alive}

    async def send(self, session: str, data: bytes) -> None:
        if self._fail is not None:
            exc, self._fail = self._fail, None
            raise exc
        self.sent.append(bytes(data))

    async def read(self, cursor: int = 0, limit: int | None = None) -> ConsoleSlice:
        if self._fail is not None:
            exc, self._fail = self._fail, None
            raise exc
        available = bytes(self._buf[cursor:])
        applied = len(available) if limit is None else min(limit, len(available))
        data = available[:applied]
        next_cursor = cursor + len(data)
        gaps = [g for g in self._gaps if cursor <= g <= next_cursor]
        dropped, self._dropped = self._dropped, 0
        return ConsoleSlice(
            data=data,
            next_cursor=next_cursor,
            dropped=dropped,
            remaining=len(self._buf) - next_cursor,
            capture_gaps=gaps,
            alive=self._alive,
            limit_applied=applied,
        )
