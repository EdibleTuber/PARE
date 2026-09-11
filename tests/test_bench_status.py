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
    probe_workbench_exists,
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
    """Green AND a confirmed workbench. `workbench_exists=True` is passed
    explicitly because this test used to encode "green implies somewhere to go"
    -- which is the belief that put a 404 on the bench screen. Handing off is
    now conditional on the target existing, so the premise must be stated."""
    now = datetime.now(UTC)
    status = build_status(
        probes={"network": "1 peer online", "arcticbase": "ok (v0.1.0)",
                "heartbeat": "boot abc · 12s old", "project": "proj-1a2b3c4d"},
        server_base=SERVER, expected_slug="proj-1a2b3c4d", pi_now=now,
        workbench_exists=True)
    assert status["ok"] is True
    assert status["first_failure"] is None
    assert status["handoff_url"] == f"{SERVER}/wb/proj-1a2b3c4d"


def test_an_unconfirmed_workbench_withholds_the_handoff():
    """Fail closed. If the Pi cannot confirm the target exists, not redirecting
    is better than landing the operator on a not-found page."""
    status = build_status(
        probes={"network": "ok", "arcticbase": "ok", "heartbeat": "ok",
                "project": "proj-1a2b3c4d"},
        server_base=SERVER, expected_slug="proj-1a2b3c4d",
        pi_now=datetime.now(UTC), workbench_exists=None)
    assert status["handoff_url"] is None
    assert "could not confirm" in status["handoff_blocked"]


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


def test_a_slightly_negative_age_reads_as_zero_not_as_minus_one():
    """Found on real hardware, not in these tests -- here I control both clocks.

    The beat's `ts` has sub-second precision; `server_now` comes from the Date
    header, which is whole seconds and can round DOWN below it. The screen
    showed "-1s old", which is harmless (a negative age never trips staleness)
    but reads as a broken display, and this page's whole job is being trusted.
    """
    server_now = datetime.now(UTC)
    payload = {"boot_id": "abc", "active_slug": "p",
               "ts": (server_now + timedelta(milliseconds=900)).isoformat().replace("+00:00", "Z"),
               "stale_after_seconds": 180}
    detail, _ = probe_heartbeat(_hb_fetch(payload), base_url=SERVER,
                                server_now=server_now)
    assert "-" not in detail.split("·")[-1], f"negative age leaked: {detail}"
    assert "0s" in detail


def test_a_beat_from_far_in_the_future_is_a_failure_not_a_zero():
    """Clamping hides rounding; it must not hide a real clock disagreement.
    Reuses the bound the daemon published, so the two sides cannot disagree
    about the threshold in either direction."""
    server_now = datetime.now(UTC)
    payload = {"boot_id": "abc", "active_slug": "p",
               "ts": (server_now + timedelta(seconds=400)).isoformat().replace("+00:00", "Z"),
               "stale_after_seconds": 180}
    with pytest.raises(ProbeError) as e:
        probe_heartbeat(_hb_fetch(payload), base_url=SERVER, server_now=server_now)
    assert "future" in str(e.value).lower()


# --- the probe must not green-light a surface it never checked --------------

def test_arcticbase_is_not_ok_when_the_page_we_hand_off_to_is_missing():
    """Found on the bench: /api/health returned 200, the probe went green, and
    the kiosk then redirected to /wb/<slug> which returned {"detail":"Not Found"}
    because ArcticBase's frontend had never been built. The probe was checking
    the API and handing off to the UI -- two different surfaces."""
    def fetch(url, timeout=None):
        if url.endswith("/api/health"):
            return 200, b'{"status":"ok","version":"0.1.0"}', {"Date": "Wed, 10 Sep 2026 04:00:00 GMT"}
        return 404, b'{"detail":"Not Found"}', {}

    with pytest.raises(ProbeError) as e:
        probe_arcticbase(fetch, base_url=SERVER)
    msg = str(e.value)
    assert "api" in msg.lower() and "frontend" in msg.lower(), msg


def test_arcticbase_is_ok_when_both_the_api_and_the_ui_answer():
    def fetch(url, timeout=None):
        if url.endswith("/api/health"):
            return 200, b'{"status":"ok","version":"0.1.0"}', {"Date": "Wed, 10 Sep 2026 04:00:00 GMT"}
        return 200, b"<!doctype html><title>Arctic Base</title>", {}

    detail, server_now = probe_arcticbase(fetch, base_url=SERVER)
    assert "0.1.0" in detail
    assert server_now is not None


# --- the handoff target must EXIST, not merely be a route that serves -------

def test_no_handoff_when_the_projects_workbench_does_not_exist_yet():
    """Found on the bench, again. All four probes green, the screen handed off
    to /wb/hardware-9c3412cc, and the SPA rendered a 404 -- because nothing had
    created that workbench yet.

    ArcticBase cannot tell us this from the handoff URL: /wb/<anything> returns
    200, because the SPA fallback serves index.html for every client-side route.
    The only server-side truth is /api/workbenches/<slug>. So checking that the
    UI serves (which is what probe_arcticbase does) can never catch it -- that
    verified the surface, and a handoff targets a specific resource on it.
    """
    status = build_status(
        probes={"network": "ok", "arcticbase": "ok", "heartbeat": "ok",
                "project": "hardware-9c3412cc"},
        server_base=SERVER, expected_slug="hardware-9c3412cc",
        pi_now=datetime.now(UTC), workbench_exists=False)
    assert status["handoff_url"] is None
    # Everything IS healthy -- the daemon is fine, ArcticBase is fine, there is
    # simply nothing published yet. Reporting a failure would be a false alarm.
    assert status["ok"] is True
    assert "nothing published" in status["handoff_blocked"].lower()


def test_handoff_happens_once_the_workbench_exists():
    status = build_status(
        probes={"network": "ok", "arcticbase": "ok", "heartbeat": "ok",
                "project": "hardware-9c3412cc"},
        server_base=SERVER, expected_slug="hardware-9c3412cc",
        pi_now=datetime.now(UTC), workbench_exists=True)
    assert status["handoff_url"] == f"{SERVER}/wb/hardware-9c3412cc"
    assert status["handoff_blocked"] is None


def test_workbench_existence_is_read_from_the_api_not_the_spa_route():
    """/wb/<slug> is useless for this: it returns 200 for a slug that does not
    exist. Pin the endpoint actually queried so nobody 'simplifies' it back."""
    seen = []

    def fetch(url, timeout=None):
        seen.append(url)
        return 200, b"{}", {}

    assert probe_workbench_exists(fetch, base_url=SERVER, slug="p-1a2b3c4d") is True
    assert seen == [f"{SERVER}/api/workbenches/p-1a2b3c4d"]


def test_a_404_from_the_api_means_it_does_not_exist():
    def fetch(url, timeout=None):
        return 404, b'{"detail":"Not Found"}', {}
    assert probe_workbench_exists(fetch, base_url=SERVER, slug="nope") is False


def test_an_unreachable_host_is_not_mistaken_for_an_absent_workbench():
    """Unknown is not the same as absent. Claiming absence on a transport error
    would put a wrong explanation on the screen."""
    def fetch(url, timeout=None):
        raise OSError("connection refused")
    assert probe_workbench_exists(fetch, base_url=SERVER, slug="p") is None
