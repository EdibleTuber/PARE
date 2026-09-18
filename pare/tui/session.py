"""The daemon session: the single owner of the daemon socket's read stream.

`agent_core.client.DaemonConnection.receive()` does not take the connection's
internal `_read_lock` (agent_core/client.py:57-63) -- it assumes exactly one
task drains it for the connection's lifetime. Any second reader, or any use
of the high-level `chat()`/`command()`/`command_stream()` helpers (which do
their own `readline()` calls under that lock), races on the shared
`asyncio.StreamReader`. Nothing raises when that happens: lines arrive
interleaved or out of order and a turn simply renders wrong, intermittently.

`DaemonSession` is the fix: it is the only thing in pare.tui that touches
`DaemonConnection`, and it uses only `connect()`, `send()`, `receive()` and
`close()`. `send_chat`/`send_command`/`send`/`subscribe` are the entire
surface the rest of the TUI is allowed to use to talk to the daemon.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from agent_core.client import DaemonConnection
from agent_core.protocol import ChatMessage, CommandMessage

logger = logging.getLogger(__name__)


@dataclass
class DaemonDisconnected:
    """Delivered to subscribers when the reader loop ends because the
    connection dropped -- EOF from the daemon, or a read error -- so
    connection loss is an event subscribers can observe rather than a silent
    stop (spec requirement 4). Never dispatched for a deliberate `stop()`.

    This is a pare.tui-local event, not a wire message: it is not registered
    with agent_core.protocol and never round-trips through encode/decode.
    """
    reason: str


class DaemonSession:
    """Owns the one `DaemonConnection` and its one reader task.

    `start()` connects and spawns the reader task; `stop()` cancels and awaits
    it. Every decoded message -- and, on connection loss, a `DaemonDisconnected`
    -- is fanned out synchronously to every handler registered via
    `subscribe()`. A subscriber that raises is logged and skipped; it never
    stops the reader loop or blocks delivery to the other subscribers.
    """

    def __init__(self, socket_path: Path, channel_id: str, cwd: str) -> None:
        self.socket_path = socket_path
        self.channel_id = channel_id
        self.cwd = cwd
        self._conn = DaemonConnection(socket_path)
        self._subscribers: list[Callable[[object], None]] = []
        self._reader_task: asyncio.Task | None = None

    async def start(self) -> None:
        """Connect and spawn the single reader task.

        Raises RuntimeError if a reader task is already running -- that would
        be a second consumer of `receive()` on the same connection, which is
        exactly the race this class exists to prevent.
        """
        if self._reader_task is not None and not self._reader_task.done():
            raise RuntimeError(
                "DaemonSession.start() called while a reader task is already "
                "running -- refusing to create a second consumer of "
                "DaemonConnection.receive()"
            )
        await self._conn.connect()
        self._reader_task = asyncio.create_task(self._read_loop())

    async def stop(self) -> None:
        """Cancel and await the reader task, then close the connection.

        Idempotent: safe to call on a session that was never started (no
        reader task, nothing connected) and safe to call more than once.
        """
        task, self._reader_task = self._reader_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self._conn.close()

    async def send_chat(self, text: str) -> None:
        """Send a chat turn, stamped with this session's channel_id and cwd
        (the pattern pare/cli.py and agent_core.adapters.cli.run_repl use)."""
        await self.send(ChatMessage(text=text, channel_id=self.channel_id, cwd=self.cwd))

    async def send_command(self, name: str, args: str) -> None:
        """Send a slash-command, stamped the same way as send_chat."""
        await self.send(
            CommandMessage(name=name, args=args, channel_id=self.channel_id, cwd=self.cwd)
        )

    async def send(self, msg: object) -> None:
        """Send a pre-built message through the connection's write side.

        `DaemonConnection.send()` only writes; it does not touch the read
        side or `_read_lock`, so calling this concurrently with the reader
        task is fine -- it is a second reader that is forbidden, not a
        concurrent writer.
        """
        await self._conn.send(msg)

    def subscribe(self, handler: Callable[[object], None]) -> None:
        """Register a handler invoked synchronously, in order, for every
        message the reader task decodes (and for DaemonDisconnected)."""
        self._subscribers.append(handler)

    async def _read_loop(self) -> None:
        """The one and only consumer of `self._conn.receive()`.

        Dispatches every decoded message to subscribers. On cancellation
        (from `stop()`) it exits quietly -- that is a deliberate shutdown, not
        a connection loss. On natural end-of-stream (the daemon closed its
        end) or a read error, it dispatches DaemonDisconnected so subscribers
        can react instead of the session just going quiet.
        """
        try:
            async for msg in self._conn.receive():
                self._dispatch(msg)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("daemon connection read loop failed")
            self._dispatch(DaemonDisconnected(reason="error"))
            return
        self._dispatch(DaemonDisconnected(reason="eof"))

    def _dispatch(self, msg: object) -> None:
        """Invoke every subscriber with msg. A raising subscriber is logged
        and skipped -- it must not wedge the reader loop or block delivery to
        the subscribers registered after it."""
        for handler in list(self._subscribers):
            try:
                handler(msg)
            except Exception:
                logger.exception(
                    "subscriber %r raised while handling %s", handler, type(msg).__name__
                )
