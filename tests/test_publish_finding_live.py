"""publish_finding against a real ArcticBase. Skips when none is reachable."""
from __future__ import annotations

import json
import os
import urllib.request
from html.parser import HTMLParser
from types import SimpleNamespace

import pytest

from pare.project_slug import resolve_project_slug
from pare.tools.publish_finding import PublishFinding

BASE = os.environ.get("PARE_ARCTICBASE_URL", "http://127.0.0.1:2929")


def _reachable() -> bool:
    try:
        with urllib.request.urlopen(f"{BASE}/api/health", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _reachable(), reason=f"no ArcticBase reachable at {BASE}")


def _ctx(cwd):
    cfg = SimpleNamespace(arcticbase_url=BASE, project_marker=".pare")
    return SimpleNamespace(cwd=str(cwd), agent=SimpleNamespace(config=cfg))


def _project(tmp_path):
    (tmp_path / ".pare").mkdir()
    return tmp_path


@pytest.mark.asyncio
async def test_a_finding_reaches_the_workbench_and_reads_back(tmp_path):
    root = _project(tmp_path)
    body = "# Keystore\n\nUnlocks with a **static** passphrase.\n"
    out = json.loads(await PublishFinding().run(
        {"title": "Keystore", "markdown": body}, _ctx(root)))

    assert out["status"] == "ok", out
    from pathlib import Path
    assert out["slug"] == resolve_project_slug(root, marker=".pare", home=Path("/nonexistent"))
    assert out["url"].endswith(f"/wb/{out['slug']}")

    url = f"{BASE}/api/workbenches/{out['slug']}/objects/{out['object_id']}/content"
    with urllib.request.urlopen(url, timeout=5) as r:
        stored = r.read().decode()
    # The tool strips surrounding whitespace -- models emit stray blank lines,
    # and leading/trailing whitespace carries no markdown meaning. The body
    # itself must survive untouched.
    assert stored == body.strip()
    assert "**static**" in stored


DANGEROUS_TAGS = {"script", "iframe", "object", "embed", "base", "form"}


class _Auditor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.findings = []

    def handle_starttag(self, tag, attrs):
        if tag in DANGEROUS_TAGS:
            self.findings.append(f"<{tag}>")
        for k, v in attrs:
            if k.lower().startswith("on"):
                self.findings.append(f"{k}={v!r}")
            if v and v.strip().lower().startswith(("javascript:", "vbscript:")):
                self.findings.append(f"{k}={v!r}")


@pytest.mark.asyncio
async def test_a_hostile_finding_renders_inert(tmp_path):
    """The whole D5 chain, end to end, for the first time.

    Everything up to now tested the renderer in isolation. This publishes
    through the real tool and reads what ArcticBase actually serves into the
    viewer's iframe -- which has no sandbox attribute, so this output is the
    only thing standing between a hostile finding and script execution.
    """
    root = _project(tmp_path)
    hostile = (
        "# Report\n\n"
        "<script>alert(1)</script>\n\n"
        '<img src=x onerror="alert(1)">\n\n'
        "[click](javascript:alert(1))\n\n"
        '[x](//e/"/onmouseover="alert`1`)\n\n'
        "| a |\n|---|\n| <svg onload=alert(1)> |\n"
    )
    out = json.loads(await PublishFinding().run(
        {"title": "hostile", "markdown": hostile}, _ctx(root)))
    assert out["status"] == "ok", out

    url = f"{BASE}/api/workbenches/{out['slug']}/objects/{out['object_id']}/render"
    with urllib.request.urlopen(url, timeout=5) as r:
        rendered = r.read().decode()

    auditor = _Auditor()
    auditor.feed(rendered)
    assert not auditor.findings, f"live markup served to the iframe: {auditor.findings}"
