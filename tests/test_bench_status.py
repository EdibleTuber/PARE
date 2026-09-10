"""The Pi's local status page — §8.1, §8.2, §8.3.

The whole reason this exists: a page served by the inference server cannot
report that the inference server is unreachable, and an already-loaded page
cannot tell "no network" from "service down" because fetch() collapses both into
one opaque TypeError. So the probing happens HERE, on the Pi, where the
distinction is still visible.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from bench.status_server import (
    PROBE_ORDER,
    STALE_STATE,
    ProbeError,
    build_status,
    parse_http_date,
    probe_arcticbase,
    probe_heartbeat,
    probe_network,
    probe_project,
)

SERVER = "http://100.82.222.92:2929"


# --- ordering: name the FIRST broken link, never guess about the rest -------

def test_the_probes_run_in_the_order_the_spec_lists():
    assert PROBE_ORDER == ("network", "arcticbase", "heartbeat", "project")


def test_a_failed_probe_stops_the_rest_and_they_report_not_checked():
    """§10 says there are five candidates when something does not arrive. A
    screen that fails three of them at once sends the operator hunting; naming
    the first broken link tells them what to fix. Unreached probes must say
    'not checked' -- never 'ok', and never a guess."""
    status = build_status(
        probes={"network": ProbeError("tailscaled is not running")},
        server_base=SERVER, expected_slug=None, pi_now=datetime.now(UTC))
    by_name = {p["name"]: p for p in status["probes"]}
    assert by_name["network"]["state"] == "FAIL"
    for later in ("arcticbase", "heartbeat", "project"):
        assert by_name[later]["state"] == "not checked"
    assert status["ok"] is False
    assert status["first_failure"] == "network"


def test_all_green_hands_off_to_the_workbench():
    now = datetime.now(UTC)
    status = build_status(
        probes={"network": "1 peer online", "arcticbase": "ok (v0.1.0)",
                "heartbeat": "boot abc · 12s old", "project": "proj-1a2b3c4d"},
        server_base=SERVER, expected_slug="proj-1a2b3c4d", pi_now=now)
    assert status["ok"] is True
    assert status["first_failure"] is None
    assert status["handoff_url"] == f"{SERVER}/wb/proj-1a2b3c4d"


def test_nothing_hands_off_while_anything_is_red():
    status = build_status(
        probes={"network": "ok", "arcticbase": ProbeError("connection refused")},
        server_base=SERVER, expected_slug="p-1a2b3c4d", pi_now=datetime.now(UTC))
    assert status["handoff_url"] is None


# --- network -----------------------------------------------------------------

def test_a_stopped_tailscaled_is_a_network_failure_not_a_service_one():
    def run(_):
        raise FileNotFoundError("tailscale: command not found")
    with pytest.raises(ProbeError) as e:
        probe_network(run, server_host="100.82.222.92")
    assert "tailscale" in str(e.value).lower()


def test_the_server_peer_being_offline_is_named_specifically():
    payload = {"Self": {"Online": True},
               "Peer": {"k1": {"TailscaleIPs": ["100.82.222.92"],
                               "HostName": "agenthost", "Online": False}}}
    with pytest.raises(ProbeError) as e:
        probe_network(lambda _: json.dumps(payload), server_host="100.82.222.92")
    assert "agenthost" in str(e.value)


def test_an_online_server_peer_passes():
    payload = {"Self": {"Online": True},
               "Peer": {"k1": {"TailscaleIPs": ["100.82.222.92"],
                               "HostName": "agenthost", "Online": True}}}
    assert "agenthost" in probe_network(
        lambda _: json.dumps(payload), server_host="100.82.222.92")


# --- arcticbase, and the clock ----------------------------------------------

def test_a_refused_connection_is_an_arcticbase_failure():
    def fetch(url, timeout=None):
        raise OSError("[Errno 111] Connection refused")
    with pytest.raises(ProbeError):
        probe_arcticbase(fetch, base_url=SERVER)


def test_now_comes_from_the_servers_date_header_never_the_pis_clock():
    """§8.2: the Pi has no RTC. One off over a weekend boots believing it is
    Friday, and every age on screen would be wrong by days."""
    stamp = "Wed, 10 Sep 2026 04:00:00 GMT"

    def fetch(url, timeout=None):
        return 200, json.dumps({"status": "ok", "version": "0.1.0"}).encode(), \
            {"Date": stamp}
    detail, server_now = probe_arcticbase(fetch, base_url=SERVER)
    assert "0.1.0" in detail
    assert server_now == parse_http_date(stamp)


def test_a_missing_date_header_is_reported_not_papered_over_with_local_time():
    def fetch(url, timeout=None):
        return 200, b'{"status":"ok"}', {}
    detail, server_now = probe_arcticbase(fetch, base_url=SERVER)
    assert server_now is None, "falling back to the Pi's clock would be a lie"


# --- heartbeat ---------------------------------------------------------------

def _hb_fetch(payload, *, objects=None):
    objs = objects if objects is not None else [
        {"id": "obj_1", "title": "heartbeat"}]

    def fetch(url, timeout=None):
        if url.endswith("/objects"):
            return 200, json.dumps(objs).encode(), {}
        return 200, json.dumps(payload).encode(), {}
    return fetch


def test_a_fresh_beat_passes_and_reports_its_age_and_boot_id():
    server_now = datetime.now(UTC)
    payload = {"boot_id": "abc123", "active_slug": "p-1a2b3c4d",
               "ts": (server_now - timedelta(seconds=20)).isoformat().replace("+00:00", "Z"),
               "stale_after_seconds": 180}
    detail, hb = probe_heartbeat(_hb_fetch(payload), base_url=SERVER,
                                 server_now=server_now)
    assert "abc123" in detail and "20s" in detail
    assert hb["active_slug"] == "p-1a2b3c4d"


def test_a_stale_beat_fails_using_the_bound_the_daemon_published():
    """The daemon puts stale_after_seconds in the payload precisely so the two
    sides cannot disagree about what stale means."""
    server_now = datetime.now(UTC)
    payload = {"boot_id": "abc", "active_slug": "p-1a2b3c4d",
               "ts": (server_now - timedelta(seconds=400)).isoformat().replace("+00:00", "Z"),
               "stale_after_seconds": 180}
    with pytest.raises(ProbeError) as e:
        probe_heartbeat(_hb_fetch(payload), base_url=SERVER, server_now=server_now)
    assert STALE_STATE in str(e.value)


def test_an_absent_heartbeat_object_says_never_written():
    with pytest.raises(ProbeError) as e:
        probe_heartbeat(_hb_fetch({}, objects=[]), base_url=SERVER,
                        server_now=datetime.now(UTC))
    assert "never" in str(e.value).lower()


def test_without_a_server_clock_the_age_is_unknown_rather_than_computed():
    payload = {"boot_id": "abc", "active_slug": "p", "ts": "2026-09-10T04:00:00Z",
               "stale_after_seconds": 180}
    detail, _ = probe_heartbeat(_hb_fetch(payload), base_url=SERVER, server_now=None)
    assert "age unknown" in detail.lower()


# --- the wrong-workbench probe ----------------------------------------------

def test_a_daemon_scoped_to_a_different_project_is_a_failure():
    """The screen showing project A while the daemon works on project B is the
    failure mode that looks healthiest."""
    with pytest.raises(ProbeError) as e:
        probe_project({"active_slug": "other-9f8e7d6c"}, expected_slug="shown-1a2b3c4d")
    assert "other-9f8e7d6c" in str(e.value) and "shown-1a2b3c4d" in str(e.value)


def test_a_daemon_with_no_active_project_is_reported_but_not_a_mismatch():
    detail = probe_project({"active_slug": None}, expected_slug=None)
    assert "no project" in detail.lower()


def test_with_nothing_pinned_on_screen_the_daemons_project_is_adopted():
    """Nothing to disagree with yet -- the first beat defines what is displayed."""
    assert "p-1a2b3c4d" in probe_project({"active_slug": "p-1a2b3c4d"},
                                         expected_slug=None)


# --- the Pi's own clock ------------------------------------------------------

def test_a_skewed_pi_clock_is_called_out_on_screen():
    """§8.2: say so if the Pi's own clock differs by more than a minute. It does
    not invalidate anything -- every age already comes from the server -- but an
    operator reading a wrong wall clock beside a correct age needs to know."""
    server_now = datetime.now(UTC)
    status = build_status(
        probes={"network": "ok", "arcticbase": "ok", "heartbeat": "ok",
                "project": "ok"},
        server_base=SERVER, expected_slug=None,
        pi_now=server_now + timedelta(hours=3), server_now=server_now)
    assert status["clock_warning"] is not None
    assert "3" in status["clock_warning"] or "10800" in status["clock_warning"]


def test_a_pi_clock_within_a_minute_raises_no_warning():
    server_now = datetime.now(UTC)
    status = build_status(
        probes={"network": "ok", "arcticbase": "ok", "heartbeat": "ok",
                "project": "ok"},
        server_base=SERVER, expected_slug=None,
        pi_now=server_now + timedelta(seconds=15), server_now=server_now)
    assert status["clock_warning"] is None


def test_a_failed_tailscale_surfaces_its_stderr_and_names_both_causes():
    """The message is the whole value of this probe. Discarding stderr left it
    asserting "is tailscaled running?" when the likelier cause on a service
    account is no access to the local API -- opposite fixes, and sending the
    operator to the wrong one costs a bench trip."""
    import subprocess

    def run(argv):
        raise subprocess.CalledProcessError(
            1, argv, output="", stderr="failed to connect to local tailscaled\n")

    with pytest.raises(ProbeError) as e:
        probe_network(run, server_host="100.82.222.92")
    msg = str(e.value)
    assert "failed to connect to local tailscaled" in msg, "stderr was dropped"
    assert "operator" in msg, "the permission fix is not named"
    assert "not running" in msg, "the other cause is not named"
