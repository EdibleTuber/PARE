"""Per-project capture store manager for the PARE daemon.

The daemon is one long-lived process that many CLI launches attach to. Each
launch stamps its os.getcwd() onto every message (see pare/cli.py); this
manager resolves that cwd to a project store (git-style .pare/ walk-up, $HOME
ceiling, XDG fallback outside a project), opens it once, caches it per resolved
root, writes a .pare/.gitignore, and holds an advisory lock so a second daemon
on the same project fails loudly instead of racing FTS writes.

Also owns the one background thread that does off-event-loop capture writes
(see write_async) -- sqlite3 connections are thread-affine (CaptureStore.open
uses the default check_same_thread=True), so a write coming from anywhere
other than the thread that opened its connection must go through its OWN
connection on its OWN thread, never the event-loop thread's store object.
"""
from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import queue
import threading
from pathlib import Path
from typing import IO

from agent_core.capture import CaptureRecord, CaptureStore, resolve_capture_db

logger = logging.getLogger(__name__)


class CaptureStoreManager:
    def __init__(self, *, marker: str | None, home: Path, xdg_state: Path) -> None:
        self._marker = marker
        self._home = Path(home)
        self._xdg_state = Path(xdg_state)
        self._cache: dict[Path, CaptureStore] = {}
        self._locks: dict[Path, IO[str]] = {}
        self.last_db_path: Path | None = None
        # Writer-thread state (write_async). One thread total, not one per
        # project: writes routed here are low-volume (pane activity), and
        # sqlite already wants a single writer at a time, so serializing them
        # through one thread costs nothing real and keeps this simple.
        self._writer_thread: threading.Thread | None = None
        self._writer_queue: "queue.SimpleQueue[tuple | None]" = queue.SimpleQueue()
        # Connections opened ON the writer thread, keyed by db_path -- never
        # shared with self._cache above, which holds connections opened on
        # whichever thread called resolve() (normally the event loop thread).
        self._writer_connections: dict[Path, CaptureStore] = {}

    def resolve_db_path(self, cwd: str | None, channel_id: str) -> Path:
        """Pure resolution of cwd -> db_path, with no I/O beyond the marker
        walk-up's stat() calls -- no store is opened. Used by callers (e.g.
        PareAgent.handle_other) that need to know WHERE a write will land
        without paying to open a main-thread connection there, most notably
        when the actual write is going to happen on write_async's dedicated
        thread instead. Shares resolve()'s exact logic (same pure function,
        same inputs) so the two can never disagree on a given (cwd,
        channel_id)."""
        base = Path(cwd) if cwd else Path(os.getcwd())
        db_path, _is_project = resolve_capture_db(
            base, self._marker, home=self._home, xdg_state=self._xdg_state,
            channel_id=channel_id,
        )
        return Path(db_path).resolve()

    def resolve(self, cwd: str | None, channel_id: str) -> CaptureStore:
        base = Path(cwd) if cwd else Path(os.getcwd())
        db_path, is_project = resolve_capture_db(
            base, self._marker, home=self._home, xdg_state=self._xdg_state,
            channel_id=channel_id,
        )
        db_path = Path(db_path).resolve()
        self.last_db_path = db_path
        cached = self._cache.get(db_path)
        if cached is not None:
            return cached
        store = CaptureStore.open(db_path)          # 0o700 dir / 0o600 db (Plan 1)
        pare_dir = db_path.parent
        try:
            if is_project:
                self._write_gitignore(pare_dir)
                self._take_lock(pare_dir)
        except Exception:
            store.close()  # don't leak the handle if gitignore/lock fails (e.g. lock contested)
            raise
        self._cache[db_path] = store
        return store

    @staticmethod
    def _write_gitignore(pare_dir: Path) -> None:
        gi = pare_dir / ".gitignore"
        if not gi.exists():
            gi.write_text("*\n")
            gi.chmod(0o600)

    def _take_lock(self, pare_dir: Path) -> None:
        lock_path = pare_dir / "daemon.lock"
        fh = open(lock_path, "w")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            fh.close()
            raise RuntimeError(
                f"another PARE daemon holds {lock_path}; refusing to share a "
                f"capture store (set a different project dir or stop the other daemon)"
            ) from exc
        self._locks[pare_dir] = fh  # held for process lifetime

    def _ensure_writer_thread(self) -> None:
        if self._writer_thread is None:
            self._writer_thread = threading.Thread(
                target=self._writer_loop, name="pare-capture-writer", daemon=True)
            self._writer_thread.start()

    def _writer_loop(self) -> None:
        """Runs on the dedicated writer thread only. Opens (and reuses) its
        own CaptureStore per db_path -- deliberately never touches
        self._cache, whose connections belong to whatever thread called
        resolve().

        Also closes those connections itself, on this same thread, when told
        to stop -- sqlite3 connections are thread-affine for close() too, not
        just execute(); close_all() calling store.close() from the event-loop
        thread on a connection opened here raises the same
        sqlite3.ProgrammingError write_async's docstring describes.
        """
        while True:
            item = self._writer_queue.get()
            if item is None:              # shutdown sentinel
                break
            db_path, record, fut, loop = item
            try:
                store = self._writer_connections.get(db_path)
                if store is None:
                    store = CaptureStore.open(db_path)
                    self._writer_connections[db_path] = store
                store.write(record)
            except Exception as exc:
                loop.call_soon_threadsafe(_safe_set_exception, fut, exc)
            else:
                loop.call_soon_threadsafe(_safe_set_result, fut, None)
        for store in self._writer_connections.values():
            store.close()
        self._writer_connections.clear()

    async def write_async(self, db_path: Path, record: CaptureRecord) -> None:
        """Write `record` to the store at db_path without blocking the
        calling coroutine's event loop for the write's duration.

        Runs on a dedicated background thread with its OWN sqlite connection
        to db_path -- never the caller's CaptureStore object. sqlite3
        connections are thread-affine (CaptureStore.open uses the default
        check_same_thread=True; confirmed by direct repro that calling
        store.write via asyncio.to_thread on a connection opened elsewhere
        raises sqlite3.ProgrammingError), so scheduling this work on *some*
        thread is not enough -- it has to be done through a connection that
        thread itself opened. Concurrent use of the SAME db file from the
        caller's own (main-thread) CaptureStore is safe because
        CaptureStore.open sets journal_mode=WAL and busy_timeout=5000
        (agent_core capture/store.py:59-60) -- exactly sqlite's documented
        setup for multiple connections against one file.
        """
        self._ensure_writer_thread()
        loop = asyncio.get_running_loop()
        fut: "asyncio.Future[None]" = loop.create_future()
        self._writer_queue.put((db_path, record, fut, loop))
        await fut

    def close_all(self) -> None:
        for store in self._cache.values():
            store.close()
        self._cache.clear()
        for fh in self._locks.values():
            try:
                fh.close()
            except Exception:
                pass
        self._locks.clear()
        if self._writer_thread is not None:
            self._writer_queue.put(None)
            self._writer_thread.join(timeout=5.0)
            if self._writer_thread.is_alive():
                # The thread (and its connections) are leaked in this case --
                # closing them from here would hit the same thread-affinity
                # error the sentinel handshake above exists to avoid. This
                # should only happen if a write is truly wedged past the join
                # timeout; logging it is the best available signal.
                logger.warning("capture writer thread did not stop within 5s of close_all()")
            self._writer_thread = None
            # _writer_loop closes self._writer_connections itself, on its own
            # thread, once it sees the sentinel -- never here.


def _safe_set_result(fut: "asyncio.Future", result) -> None:
    # The event loop that owns `fut` may already be closed/the future may
    # already be cancelled by the time this callback runs (shutdown races);
    # InvalidStateError there is expected and not a bug in the write itself.
    if not fut.done():
        fut.set_result(result)


def _safe_set_exception(fut: "asyncio.Future", exc: Exception) -> None:
    if not fut.done():
        fut.set_exception(exc)
