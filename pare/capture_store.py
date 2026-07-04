"""Per-project capture store manager for the PARE daemon.

The daemon is one long-lived process that many CLI launches attach to. Each
launch stamps its os.getcwd() onto every message (see pare/cli.py); this
manager resolves that cwd to a project store (git-style .pare/ walk-up, $HOME
ceiling, XDG fallback outside a project), opens it once, caches it per resolved
root, writes a .pare/.gitignore, and holds an advisory lock so a second daemon
on the same project fails loudly instead of racing FTS writes.
"""
from __future__ import annotations

import fcntl
import os
from pathlib import Path

from agent_core.capture import CaptureStore, resolve_capture_db


class CaptureStoreManager:
    def __init__(self, *, marker: str | None, home: Path, xdg_state: Path) -> None:
        self._marker = marker
        self._home = Path(home)
        self._xdg_state = Path(xdg_state)
        self._cache: dict[Path, CaptureStore] = {}
        self._locks: dict[Path, object] = {}
        self.last_db_path: Path | None = None

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
        if is_project:
            self._write_gitignore(pare_dir)
            self._take_lock(pare_dir)
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
