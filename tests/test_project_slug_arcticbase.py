"""Check derived slugs against a real ArcticBase, when one is reachable.

This is the integration half of a deliberate pair. `test_project_slug.py` pins
the slug boundaries (64 accepted, 65 rejected, no leading '-', no uppercase) as
plain unit assertions that ALWAYS run, including in CI. Those boundaries were
established by probing a live instance, so the unit test is not guesswork -- but
it is a snapshot, and a snapshot can drift from the thing it was taken of.

This file re-confirms it against the real service. It skips when none is
reachable, which is honest and visible; it is not the only thing standing
between us and a wrong alphabet.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

import pytest

from pare.project_slug import derive_slug

# Overridable so this can point at a real deployment, and so the skip path
# itself is testable -- pointing it at a dead port must SKIP, not error.
BASE = os.environ.get("PARE_ARCTIC_BASE_URL", "http://127.0.0.1:2929/api")


def _reachable() -> bool:
    try:
        with urllib.request.urlopen(f"{BASE}/health", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _reachable(), reason=f"no ArcticBase reachable at {BASE}")


def _create_workbench(slug: str) -> int:
    payload = json.dumps({"slug": slug, "title": "slug agreement probe"}).encode()
    req = urllib.request.Request(
        f"{BASE}/workbenches", data=payload,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


@pytest.mark.parametrize("dirname", [
    "target", "My Project", "___", "...", "-dash-lead", "UPPER_CASE",
    "a" * 200, "ünïcodé", "proj.v2", "9lives",
])
def test_arcticbase_accepts_every_slug_we_derive(tmp_path, dirname):
    root = tmp_path / dirname
    root.mkdir()
    slug = derive_slug(root)
    status = _create_workbench(slug)
    assert status == 201, f"ArcticBase rejected derived slug {slug!r} with {status}"


def test_arcticbase_still_rejects_what_our_pattern_rejects(tmp_path):
    """If this fails, ArcticBase loosened its rule and ours is now stricter.

    That is not automatically a bug -- being stricter than the consumer is safe
    -- but it means the boundaries pinned in the unit tests have drifted, and
    somebody should look rather than discover it at publish time.
    """
    for bad in ("UPPER", "has space", "-leading", "x" * 65):
        assert _create_workbench(bad) == 400, f"ArcticBase accepted {bad!r}"
