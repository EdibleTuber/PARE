"""The daemon heartbeat — §8.2.

The beat exists to reveal a wedged daemon, so the tests that matter are about
what makes it STOP, and about it never being mistaken for a different daemon's.
"""
from __future__ import annotations

import json

import pytest

from pare.heartbeat import HEARTBEAT_WORKBENCH, Heartbeat


class _FakeClient:
    """Records calls. Not a stand-in for ArcticBase's behaviour -- the real
    round-trip is covered against a live instance in test_heartbeat_live.py."""

    def __init__(self, fail=False):
        self.fail = fail
        self.workbenches = []
        self.writes = []

    def ensure_workbench(self, slug, title):
        self.workbenches.append(slug)
        return True

    def upsert(self, slug, *, kind, title, content):
        if self.fail:
            raise RuntimeError("workbench host is down")
        self.writes.append({"slug": slug, "kind": kind, "title": title,
                            "content": content})
        return "obj_fake"


def _payload(client, i=-1):
    return json.loads(client.writes[i]["content"])


def test_the_beat_goes_to_the_dedicated_workbench_never_a_project():
    """§8.2: put_object_content appends an audit row per call with no rotation.
    A 60s beat in a project's workbench would make its audit trail -- the record
    you want after a bricked target -- mostly heartbeat noise."""
    c = _FakeClient()
    Heartbeat(c, boot_id="b1").beat(active_slug="some-project-1a2b3c4d")
    assert c.writes[0]["slug"] == HEARTBEAT_WORKBENCH
    assert HEARTBEAT_WORKBENCH != "some-project-1a2b3c4d"


def test_the_beat_carries_the_boot_id_and_the_active_slug():
    """A restarted or differently-scoped daemon must be visible, not continuous."""
    c = _FakeClient()
    Heartbeat(c, boot_id="boot-abc").beat(active_slug="proj-1a2b3c4d")
    p = _payload(c)
    assert p["boot_id"] == "boot-abc"
    assert p["active_slug"] == "proj-1a2b3c4d"


def test_the_boot_id_is_stable_across_beats_but_differs_per_daemon():
    a, b = Heartbeat(_FakeClient()), Heartbeat(_FakeClient())
    assert a.boot_id == a.boot_id
    assert a.boot_id != b.boot_id


def test_the_beat_timestamp_is_utc_and_iso():
    c = _FakeClient()
    Heartbeat(c, boot_id="b").beat(active_slug="p-1a2b3c4d")
    ts = _payload(c)["ts"]
    assert ts.endswith("Z") or "+00:00" in ts


def test_a_beat_with_no_active_project_still_reports_liveness():
    """The daemon can be perfectly alive outside a project. That must not look
    like a dead daemon -- it is a different fact."""
    c = _FakeClient()
    Heartbeat(c, boot_id="b").beat(active_slug=None)
    p = _payload(c)
    assert p["active_slug"] is None
    assert p["boot_id"] == "b"


def test_the_beat_records_the_worker_fleet_when_given_one():
    c = _FakeClient()
    Heartbeat(c, boot_id="b").beat(active_slug=None, workers={"frida": True, "mitm": False})
    assert _payload(c)["workers"] == {"frida": True, "mitm": False}


def test_a_failing_workbench_host_does_not_propagate():
    """A beat that raises would kill the sweep loop it rides on, taking worker
    liveness down with it -- the heartbeat must never be the thing that breaks
    the daemon it reports on."""
    hb = Heartbeat(_FakeClient(fail=True), boot_id="b")
    hb.beat(active_slug=None)          # must not raise
    assert hb.last_error is not None


def test_the_workbench_is_ensured_once_not_on_every_beat():
    """1440 beats a day; ensure_workbench is a GET plus maybe a POST each time."""
    c = _FakeClient()
    hb = Heartbeat(c, boot_id="b")
    for _ in range(5):
        hb.beat(active_slug=None)
    assert c.workbenches == [HEARTBEAT_WORKBENCH]
    assert len(c.writes) == 5
