import sqlite3
import stat
from pathlib import Path

import pytest

from agent_core.capture import CaptureRecord, CaptureStore
from pare.capture_store import CaptureStoreManager


def _mgr(tmp_path):
    return CaptureStoreManager(marker=".pare", home=tmp_path / "home",
                               xdg_state=tmp_path / "state")


def test_project_store_is_cached_and_gitignored(tmp_path):
    proj = tmp_path / "home" / "work" / "acme"
    (proj / ".pare").mkdir(parents=True)
    mgr = _mgr(tmp_path)
    s1 = mgr.resolve(str(proj / "src"), "c1")
    s2 = mgr.resolve(str(proj), "c1")
    assert s1 is s2  # same resolved root -> one cached store
    gi = proj / ".pare" / ".gitignore"
    assert gi.read_text().strip() == "*"
    assert stat.S_IMODE((proj / ".pare").stat().st_mode) == 0o700
    mgr.close_all()


def test_outside_project_uses_xdg_fallback_keyed_by_channel(tmp_path):
    mgr = _mgr(tmp_path)
    store = mgr.resolve(str(tmp_path / "elsewhere"), "cli-xyz")
    assert (tmp_path / "state") in Path(mgr.last_db_path).parents
    assert "cli-xyz" in Path(mgr.last_db_path).name
    mgr.close_all()


def test_none_cwd_does_not_crash(tmp_path):
    mgr = _mgr(tmp_path)
    store = mgr.resolve(None, "c1")  # falls back to os.getcwd() internally
    assert store is not None
    mgr.close_all()


def test_contended_lock_raises_and_does_not_leak_store(tmp_path):
    """A second manager on the same project can't take the advisory lock; it
    must raise RuntimeError AND not cache/leak the store it opened."""
    proj = tmp_path / "home" / "work" / "acme"
    (proj / ".pare").mkdir(parents=True)
    holder = _mgr(tmp_path)
    holder.resolve(str(proj), "c1")           # takes the flock
    contender = _mgr(tmp_path)
    with pytest.raises(RuntimeError):
        contender.resolve(str(proj), "c2")
    # the store opened before the failed lock must not be retained (leak-free)
    assert contender._cache == {}
    holder.close_all()


async def test_write_async_delegates_to_store_and_persists(tmp_path):
    """write_async's public contract (agent_core 1.11.0): it delegates to
    CaptureStore.write on the store's own writer thread. close_all()'s
    cascade to store.close() does a bounded drain, so once it returns the
    record must be durably committed -- verified here by reopening the same
    db_path as a fresh CaptureStore and reading the row back. Replaces the
    Task 3-era coverage that asserted on this manager's own
    _writer_thread/_writer_queue, which no longer exist."""
    proj = tmp_path / "home" / "work" / "acme"
    (proj / ".pare").mkdir(parents=True)
    mgr = _mgr(tmp_path)
    mgr.resolve(str(proj), "c1")
    db_path = mgr.last_db_path
    record = CaptureRecord(
        worker="pane:sent", tool="tmux", session_id="s1", launch_ts=0.0,
        summary="hi", body="hello world", rows=1, addrs=[],
    )
    await mgr.write_async(db_path, record)
    mgr.close_all()

    reopened = CaptureStore.open(db_path)
    try:
        rows = reopened.recent(limit=5)
        assert any(r["summary"] == "hi" for r in rows)
    finally:
        reopened.close()


async def test_write_async_before_resolve_raises(tmp_path):
    """write_async requires resolve() to have opened (and cached) the store
    first -- there is no implicit open-on-write, unlike the Task 3 writer
    thread, which opened its own connection lazily on first use."""
    mgr = _mgr(tmp_path)
    record = CaptureRecord(
        worker="pane:sent", tool="tmux", session_id=None, launch_ts=0.0,
        summary="x", body="y", rows=1, addrs=[],
    )
    with pytest.raises(RuntimeError, match="write_async before resolve"):
        await mgr.write_async(tmp_path / "never-resolved.db", record)


async def test_write_async_propagates_write_failure(tmp_path):
    """Spec R4 ("a write that fails must not be silent"): write_async must
    surface a failed commit to ITS OWN caller, not just enqueue-and-forget.
    Regression guard for fix-round-1: store.write() alone returns once the
    record is queued, not once committed (agent_core capture/store.py:
    191-208), so a write_async that only awaited store.write() would never
    see the writer thread's INSERT exception -- _pane_activity_worker's
    try/except (pare/agent.py) would never bump
    pane_activity_write_failures. write_async must instead await
    store.get(ref) so the pending-writes future's exception propagates."""
    proj = tmp_path / "home" / "work" / "acme"
    (proj / ".pare").mkdir(parents=True)
    mgr = _mgr(tmp_path)
    mgr.resolve(str(proj), "c1")
    db_path = mgr.last_db_path
    store = mgr._cache[db_path]

    def _boom(conn, ref, record):
        raise sqlite3.OperationalError("simulated disk failure")

    store._insert_record = _boom  # instance-level override of the writer-thread's insert

    record = CaptureRecord(
        worker="pane:sent", tool="tmux", session_id=None, launch_ts=0.0,
        summary="x", body="y", rows=1, addrs=[],
    )
    with pytest.raises(sqlite3.OperationalError):
        await mgr.write_async(db_path, record)
    mgr.close_all()
