"""The heartbeat against a real ArcticBase. Skips when none is reachable."""
from __future__ import annotations

import json
import os
import urllib.request
import uuid

import pytest

from pare.arcticbase import ArcticBaseClient
from pare.heartbeat import HEARTBEAT_TITLE, Heartbeat

BASE = os.environ.get("PARE_ARCTICBASE_URL", "http://127.0.0.1:2929")


def _reachable() -> bool:
    try:
        with urllib.request.urlopen(f"{BASE}/api/health", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _reachable(), reason=f"no ArcticBase reachable at {BASE}")


def _objects(slug):
    with urllib.request.urlopen(f"{BASE}/api/workbenches/{slug}/objects", timeout=5) as r:
        return json.loads(r.read())


def _content(slug, oid):
    url = f"{BASE}/api/workbenches/{slug}/objects/{oid}/content"
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.loads(r.read())


@pytest.fixture
def hb():
    wb = f"hb-{uuid.uuid4().hex[:12]}"
    return Heartbeat(ArcticBaseClient(BASE), boot_id="boot-test", workbench=wb), wb


def test_a_beat_lands_and_is_readable(hb):
    beat, wb = hb
    assert beat.beat(active_slug="proj-1a2b3c4d", workers={"frida": True}) is True
    objs = _objects(wb)
    assert len(objs) == 1
    payload = _content(wb, objs[0]["id"])
    assert payload["boot_id"] == "boot-test"
    assert payload["active_slug"] == "proj-1a2b3c4d"
    assert payload["workers"] == {"frida": True}


def test_many_beats_update_one_object_rather_than_accumulating(hb):
    """The reason upsert exists. 1440 beats a day, each minting a new object,
    would turn the status workbench into a landfill within a day."""
    beat, wb = hb
    for i in range(6):
        assert beat.beat(active_slug=f"proj-{i:08x}") is True
    objs = _objects(wb)
    assert len(objs) == 1, f"{len(objs)} objects after 6 beats"
    assert objs[0]["title"] == HEARTBEAT_TITLE
    # and it holds the LATEST beat, not the first
    assert _content(wb, objs[0]["id"])["active_slug"] == "proj-00000005"


def test_an_unreachable_host_is_recorded_not_raised():
    beat = Heartbeat(ArcticBaseClient("http://127.0.0.1:59999", timeout=0.3),
                     boot_id="b")
    assert beat.beat(active_slug=None) is False
    assert beat.last_error is not None
    assert beat.last_beat_at is None
