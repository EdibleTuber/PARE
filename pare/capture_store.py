"""Per-project capture store manager for the PARE daemon.

The daemon is one long-lived process that many CLI launches attach to. Each
launch stamps its os.getcwd() onto every message (see pare/cli.py); this
manager resolves that cwd to a project store (git-style .pare/ walk-up, $HOME
ceiling, XDG fallback outside a project), opens it once, caches it per resolved
root, writes a .pare/.gitignore, and holds an advisory lock so a second daemon
on the same project fails loudly instead of racing FTS writes.

Off-event-loop capture writes (see write_async) are now owned by
agent_core.CaptureStore itself (agent_core 1.11.0): each disk-backed store
opens its own writer thread with its own sqlite connection, so this manager
no longer needs a writer thread of its own -- it just awaits store.write().
"""
from __future__ import annotations

import fcntl
import logging
import os
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

    async def write_async(self, db_path: Path, record: CaptureRecord) -> None:
        """Delegate to the store's own writer thread (agent_core 1.11.0).

        Task 3's per-manager writer thread is gone: one writer per store now,
        owned by agent_core.CaptureStore, so a manager holding N stores has
        N writer threads (one per db_path), not one thread with N connections.
        That's the correct pattern -- a store owns its connection, so the
        thread that writes to it must too. See
        docs/superpowers/specs/2026-09-19-on-loop-capture-write-design.md D3.
        """
        store = self._cache.get(db_path)
        if store is None:
            raise RuntimeError(f"write_async before resolve for {db_path}")
        await store.write(record)

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
