"""The ArcticBase client — integration half, against a real service.

Skips when none is reachable. The properties that matter are pinned as unit
tests in test_arcticbase_client.py, which always run; this file confirms them
against the real thing and covers the HTTP behaviours a fake would only have
asserted back at me.
"""
from __future__ import annotations

import json
import os
import urllib.request
import uuid

import pytest

from pare.arcticbase import ArcticBaseClient, ContentTooLarge, PublishFailed

BASE = os.environ.get("PARE_ARCTICBASE_URL", "http://127.0.0.1:2929")


def _reachable() -> bool:
    try:
        with urllib.request.urlopen(f"{BASE}/api/health", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _reachable(), reason=f"no ArcticBase reachable at {BASE}")


@pytest.fixture
def client():
    return ArcticBaseClient(BASE)


@pytest.fixture
def workbench(client):
    slug = f"itest-{uuid.uuid4().hex[:12]}"
    client.ensure_workbench(slug, "integration test")
    return slug


def _content(slug, oid):
    url = f"{BASE}/api/workbenches/{slug}/objects/{oid}/content"
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.read().decode("utf-8")


def test_ensure_workbench_creates_then_is_idempotent(client):
    slug = f"itest-{uuid.uuid4().hex[:12]}"
    assert client.ensure_workbench(slug, "t") is True
    assert client.ensure_workbench(slug, "t") is False


def test_a_report_round_trips_as_markdown(client, workbench):
    body = "# Finding\n\nThe keystore unlocks with a **static** passphrase.\n"
    oid = client.publish_report(workbench, "Finding", body)
    assert _content(workbench, oid) == body


def test_a_descriptor_round_trips_as_json(client, workbench):
    descriptor = {"host": "pare-bench", "path": "/mnt/bench-store/p/dump.bin",
                  "size": 2097152, "sha256": "ab" * 32, "produced_by": "hardware"}
    oid = client.publish_descriptor(workbench, "dump.bin", descriptor)
    assert json.loads(_content(workbench, oid)) == descriptor


def test_the_two_call_path_is_the_one_the_server_actually_caps(client, workbench):
    """The whole reason publish() is two calls rather than one.

    With the client's own check lifted, an oversized body must still be refused
    -- by the server, on the PUT. If this ever passes, the PUT stopped being
    enforced and §5.1's control is gone, whatever the environment says.
    """
    unchecked = ArcticBaseClient(BASE, max_bytes=64 * 1024 * 1024)
    with pytest.raises(PublishFailed) as e:
        unchecked.publish(workbench, kind="md", title="huge", content="A" * (9 * 1024 * 1024))
    assert "413" in str(e.value)


def test_the_same_body_through_the_single_call_json_path_is_NOT_capped(workbench):
    """Documents why we do not use POST-with-inline-content.

    This is the measurement behind the §5.1 correction, kept as a test so the
    day ArcticBase fixes it is visible rather than assumed.
    """
    payload = json.dumps({"kind": "md", "title": "huge-inline",
                          "content": "A" * (9 * 1024 * 1024)}).encode()
    req = urllib.request.Request(
        f"{BASE}/api/workbenches/{workbench}/objects", data=payload,
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        assert r.status == 201, "ArcticBase now caps the JSON path -- update §5.1"


def test_the_client_refuses_oversized_content_before_creating_anything(client, workbench):
    small = ArcticBaseClient(BASE, max_bytes=64)
    before = _object_count(workbench)
    with pytest.raises(ContentTooLarge):
        small.publish(workbench, kind="md", title="t", content="x" * 200)
    assert _object_count(workbench) == before, "a metadata-only object was left behind"


def _object_count(slug):
    with urllib.request.urlopen(f"{BASE}/api/workbenches/{slug}/objects", timeout=5) as r:
        return len(json.loads(r.read()))
