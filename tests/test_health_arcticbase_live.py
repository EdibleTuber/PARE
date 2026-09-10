"""/health's ArcticBase block against a real instance. Skips when none is up."""
from __future__ import annotations

import os
import urllib.request
from types import SimpleNamespace

import pytest

from pare.commands.health import Health

BASE = os.environ.get("PARE_ARCTICBASE_URL", "http://127.0.0.1:2929")


def _reachable() -> bool:
    try:
        with urllib.request.urlopen(f"{BASE}/api/health", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _reachable(), reason=f"no ArcticBase reachable at {BASE}")


@pytest.mark.asyncio
async def test_a_reachable_host_reports_ok_with_its_version(tmp_path):
    (tmp_path / ".pare").mkdir()
    cfg = SimpleNamespace(inference_url="i", model="m", vault_path="v",
                          apk_re_agents_url="a", arcticbase_url=BASE,
                          project_marker=".pare")
    agent = SimpleNamespace(name="pare", config=cfg, worker_manager=None,
                            worker_registry=None, _heartbeat=None)
    ctx = SimpleNamespace(agent=agent, cwd=str(tmp_path))
    out = "\n".join([m.text async for m in Health().run("", ctx)])
    assert f"arcticbase: {BASE} · ok (v" in out
    assert f"{BASE}/wb/" in out, "the operator needs the page to open"
