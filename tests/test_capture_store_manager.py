import stat
from pathlib import Path
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
