"""The Pi's local status page — served BY the Pi, about everything else.

Spec: docs/superpowers/specs/2026-09-06-arcticbase-artifacts-design.md §8.1-8.3.

A status page hosted on the inference server cannot report that the inference
server is unreachable: cold-boot the Pi with it down and Chromium shows its own
interstitial, in kiosk mode, to someone holding two probes. Worse, an
already-loaded page cannot tell "no network" from "service down" -- `fetch()`
collapses both into an opaque `TypeError: Failed to fetch`. So this runs on the
Pi and probes outward, where the distinction is still visible.

Design rules that the tests pin:

* **Probes run in order and stop at the first failure.** §10 says there are five
  candidates when something does not arrive; a screen that reddens three at once
  sends the operator hunting. Unreached probes report "not checked" -- never
  "ok", and never an inference.
* **"Now" is the server's, never the Pi's.** The Pi has no RTC: one left off
  over a weekend boots believing it is Friday, and every age on screen would be
  wrong by days. Time comes from the `Date` response header of a probe we are
  making anyway -- free, and no ArcticBase change. If no `Date` arrives, the age
  is "unknown" rather than computed from a clock we do not trust.
* **Stdlib only.** This runs on a Raspberry Pi that may also be driving a
  target. No pip install, no virtualenv to rot.

Run it with `python3 -m bench.status_server --server http://<host>:2929`.
"""
from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from collections.abc import Callable
from typing import Any

# datetime.UTC is 3.11+. Raspberry Pi OS Bookworm ships 3.11, but Bullseye
# ships 3.9 and this file is meant to need nothing but system python -- so it
# uses timezone.utc, which has worked since 3.2. One line, and it removes a
# whole class of "works on my machine" failure on a device I cannot inspect.
UTC = timezone.utc

PROBE_ORDER = ("network", "arcticbase", "heartbeat", "project")

HEARTBEAT_WORKBENCH = "pare-daemon-status"
HEARTBEAT_TITLE = "heartbeat"

STALE_STATE = "STALE"

# §8.2: say so if the Pi's own clock is off by more than a minute.
CLOCK_SKEW_TOLERANCE_SECONDS = 60

# Short: this page is the thing an operator stares at when the bench is wrong.
PROBE_TIMEOUT = 3.0


class ProbeError(Exception):
    """A probe that ran and found something wrong. Carries what to fix."""


@dataclass
class Probe:
    name: str
    state: str      # "ok" | "FAIL" | "not checked"
    detail: str


def parse_http_date(raw: str) -> datetime | None:
    try:
        return parsedate_to_datetime(raw).astimezone(UTC)
    except Exception:
        return None


# --- the probes --------------------------------------------------------------

def probe_network(run_tailscale: Callable[[list[str]], str], *,
                  server_host: str) -> str:
    """Is the tailnet up, and is the inference server's peer online?

    Runs locally and needs nothing off-box, which is the point: this is the one
    probe that still works when everything else is unreachable.
    """
    try:
        raw = run_tailscale(["tailscale", "status", "--json"])
    except FileNotFoundError as exc:
        raise ProbeError(f"tailscale is not installed or not on PATH: {exc}") from exc
    except subprocess.CalledProcessError as exc:
        # Surface stderr: it is the actual diagnostic, and discarding it left
        # this probe guessing. Two causes are common and they need opposite
        # fixes, so name both rather than asserting one -- the same reason §5.5
        # insists EROFS be distinguished from a permission error.
        detail = (exc.stderr or "").strip().splitlines()
        raise ProbeError(
            f"tailscale status failed (exit {exc.returncode})"
            + (f": {detail[0]}" if detail else "")
            + ". Either tailscaled is not running, or this service user has no "
              "access to the local API -- `tailscale set --operator=<user>` "
              "grants it.") from exc
    except Exception as exc:
        raise ProbeError(f"could not run tailscale status "
                         f"({type(exc).__name__}: {exc})") from exc
    try:
        status = json.loads(raw)
    except ValueError as exc:
        raise ProbeError(f"tailscale status returned unparseable JSON: {exc}") from exc

    if not (status.get("Self") or {}).get("Online", False):
        raise ProbeError("this Pi is not online on the tailnet "
                         "(tailscale up may be needed)")
    for peer in (status.get("Peer") or {}).values():
        if server_host in (peer.get("TailscaleIPs") or []) or \
                peer.get("HostName") == server_host:
            name = peer.get("HostName") or server_host
            if not peer.get("Online", False):
                raise ProbeError(f"the inference server {name} is offline on the "
                                 f"tailnet -- the Pi's own network is fine")
            return f"tailnet up · {name} online"
    raise ProbeError(f"no tailnet peer matches {server_host}; check the server "
                     f"address this page was started with")


def probe_arcticbase(fetch: Callable[..., tuple[int, bytes, dict]], *,
                     base_url: str) -> tuple[str, datetime | None]:
    """Is the workbench host answering? Also yields the server's clock."""
    try:
        status, body, headers = fetch(f"{base_url.rstrip('/')}/api/health",
                                      timeout=PROBE_TIMEOUT)
    except Exception as exc:
        raise ProbeError(f"{base_url} did not answer "
                         f"({type(exc).__name__}: {exc})") from exc
    if status != 200:
        raise ProbeError(f"{base_url}/api/health returned {status}")
    try:
        version = json.loads(body).get("version", "?")
    except ValueError:
        version = "?"
    date_header = headers.get("Date") or headers.get("date")
    server_now = parse_http_date(date_header) if date_header else None

    # The API answering is NOT the same as the page being servable, and this
    # probe hands the kiosk off to the UI. On this bench /api/health returned
    # 200 while / and /wb/<slug> returned {"detail":"Not Found"}, because
    # ArcticBase's frontend had never been built -- so the screen went green and
    # then redirected to a 404. Checking one surface and handing off to another
    # is the "looks fine then isn't" failure this whole page exists to prevent.
    try:
        ui_status, _, _ = fetch(base_url.rstrip("/") + "/", timeout=PROBE_TIMEOUT)
    except Exception as exc:
        raise ProbeError(f"{base_url} serves its API but its web UI did not "
                         f"answer ({type(exc).__name__}: {exc})") from exc
    if ui_status != 200:
        raise ProbeError(
            f"{base_url} serves its API (v{version}) but its web UI returns "
            f"{ui_status} -- the ArcticBase frontend has not been built, so "
            f"there is nowhere to hand off to. Build frontend/dist and set "
            f"ARCTIC_BASE_FRONTEND_DIST.")
    return f"ok (v{version}, UI served)", server_now


def probe_heartbeat(fetch: Callable[..., tuple[int, bytes, dict]], *,
                    base_url: str, server_now: datetime | None,
                    ) -> tuple[str, dict[str, Any]]:
    """Is the daemon still beating, judged by the server's clock?"""
    base = base_url.rstrip("/")
    wb = f"{base}/api/workbenches/{HEARTBEAT_WORKBENCH}"
    try:
        status, body, _ = fetch(f"{wb}/objects", timeout=PROBE_TIMEOUT)
    except Exception as exc:
        raise ProbeError(f"could not list the daemon-status workbench "
                         f"({type(exc).__name__}: {exc})") from exc
    if status == 404:
        raise ProbeError("the daemon-status workbench does not exist: the daemon "
                         "has never written a heartbeat")
    if status != 200:
        raise ProbeError(f"listing the daemon-status workbench returned {status}")
    objects = json.loads(body)
    oid = next((o["id"] for o in objects if o.get("title") == HEARTBEAT_TITLE), None)
    if oid is None:
        raise ProbeError("no heartbeat object: the daemon has never written one "
                         "(distinct from a beat that stopped)")

    status, body, _ = fetch(f"{wb}/objects/{oid}/content", timeout=PROBE_TIMEOUT)
    if status != 200:
        raise ProbeError(f"reading the heartbeat returned {status}")
    payload = json.loads(body)
    boot = payload.get("boot_id", "?")
    stale_after = float(payload.get("stale_after_seconds") or 180)

    stamp = _parse_iso(payload.get("ts"))
    if server_now is None or stamp is None:
        # Never fall back to the Pi's clock. An age computed from a wrong clock
        # is worse than no age, because it looks like an answer.
        return f"boot {boot} · age unknown (no server clock)", payload
    age = (server_now - stamp).total_seconds()
    if age > stale_after:
        raise ProbeError(f"{STALE_STATE}: last beat {int(age)}s ago, over the "
                         f"{int(stale_after)}s bound the daemon published "
                         f"(boot {boot}) -- the daemon is wedged or stopped")
    if age < -stale_after:
        # Beyond the published bound in the other direction is not rounding, it
        # is a real disagreement about what time it is -- and an age computed
        # across two clocks that disagree is not an age. Reusing stale_after
        # keeps one threshold for both directions.
        raise ProbeError(f"the heartbeat is timestamped {int(abs(age))}s in the "
                         f"FUTURE relative to the workbench host (boot {boot}); "
                         f"the daemon host and the workbench host disagree about "
                         f"the time, so no age here can be trusted")
    # Clamp small negatives. The Date header is whole seconds and can round down
    # below a sub-second `ts`, which put "-1s old" on the bench screen during the
    # first real run. Harmless, but it reads as a broken display.
    age = max(0.0, age)
    return f"boot {boot} · {int(age)}s old", payload


def probe_project(heartbeat: dict[str, Any], *, expected_slug: str | None) -> str:
    """Is the daemon scoped to the project this screen is showing?

    The failure this catches looks healthiest of all: everything green, and the
    operator reading findings for a different target.
    """
    active = heartbeat.get("active_slug")
    if active is None:
        return "daemon has no project scoped (no project)"
    if expected_slug is None:
        # Nothing pinned on screen yet, so there is nothing to disagree with.
        return f"{active} (adopted from the daemon)"
    if active != expected_slug:
        raise ProbeError(f"the daemon is working on {active!r} but this screen is "
                         f"showing {expected_slug!r} -- everything else is green, "
                         f"which is what makes this dangerous")
    return active


def probe_workbench_exists(fetch, *, base_url: str, slug: str) -> bool | None:
    """Does this project's workbench actually exist? True/False, or None if unknown.

    Asks the API, NOT the handoff URL. `/wb/<slug>` returns 200 for a slug that
    does not exist, because ArcticBase's SPA fallback serves index.html for
    every client-side route -- the not-found is rendered by the app after its
    own API call fails. So the handoff URL cannot be used to validate itself,
    which is how the bench screen came to hand off to a 404 twice.

    None rather than False on a transport error: unknown is not absent, and
    claiming absence would put a confident wrong explanation on the screen.
    """
    try:
        status, _, _ = fetch(f"{base_url.rstrip('/')}/api/workbenches/{slug}",
                             timeout=PROBE_TIMEOUT)
    except Exception:
        return None
    if status == 200:
        return True
    if status == 404:
        return False
    return None


# --- assembly ----------------------------------------------------------------

def build_status(*, probes: dict[str, Any], server_base: str,
                 expected_slug: str | None, pi_now: datetime,
                 server_now: datetime | None = None,
                 workbench_exists: bool | None = None) -> dict[str, Any]:
    """Fold probe outcomes into what the page renders.

    `probes` maps a probe name to its detail string, or to a ProbeError. Names
    absent from it were never reached, and say so.
    """
    out: list[dict[str, str]] = []
    first_failure: str | None = None
    for name in PROBE_ORDER:
        if first_failure is not None or name not in probes:
            out.append(Probe(name, "not checked", "").__dict__)
            continue
        result = probes[name]
        if isinstance(result, ProbeError):
            first_failure = name
            out.append(Probe(name, "FAIL", str(result)).__dict__)
        else:
            out.append(Probe(name, "ok", str(result)).__dict__)

    ok = first_failure is None and all(p["state"] == "ok" for p in out)
    slug = expected_slug

    # Green does NOT imply somewhere to go. The daemon can be perfectly healthy
    # and scoped to a project nothing has published to yet, in which case its
    # workbench does not exist and handing off lands on the SPA's not-found.
    # That is not a failure of anything -- so the probes stay green and the
    # handoff is withheld with a reason, rather than reddening a healthy bench.
    handoff_blocked: str | None = None
    if ok and slug and workbench_exists is False:
        handoff_blocked = (
            f"the daemon is scoped to {slug}, but no workbench exists for it "
            f"yet -- nothing published. It appears with the first finding.")
    elif ok and slug and workbench_exists is None:
        handoff_blocked = (
            f"could not confirm a workbench exists for {slug}; not handing off "
            f"to a page that may not be there")

    return {
        "ok": ok,
        "first_failure": first_failure,
        "probes": out,
        "handoff_url": (f"{server_base.rstrip('/')}/wb/{slug}"
                        if ok and slug and handoff_blocked is None else None),
        "handoff_blocked": handoff_blocked,
        "clock_warning": _clock_warning(pi_now, server_now),
        "server_now": server_now.isoformat() if server_now else None,
        "generated_at_server_time": server_now is not None,
    }


def _clock_warning(pi_now: datetime, server_now: datetime | None) -> str | None:
    if server_now is None:
        return None
    skew = (pi_now - server_now).total_seconds()
    if abs(skew) <= CLOCK_SKEW_TOLERANCE_SECONDS:
        return None
    direction = "ahead of" if skew > 0 else "behind"
    return (f"this Pi's clock is {int(abs(skew))}s {direction} the server's. "
            f"Every age shown here uses the server's clock, so they are correct "
            f"-- but the Pi has no RTC, so do not trust its wall clock.")


def _parse_iso(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(UTC)
    except Exception:
        return None


def run_tailscale(argv: list[str]) -> str:
    # check=True so a non-zero exit becomes CalledProcessError, which carries
    # stderr. probe_network reads it: the message is the difference between
    # "start tailscaled" and "grant this user the local API".
    return subprocess.run(argv, capture_output=True, text=True, timeout=5,
                          check=True).stdout


def http_fetch(url: str, timeout: float = PROBE_TIMEOUT) -> tuple[int, bytes, dict]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers or {})


def collect(*, server_base: str, expected_slug: str | None,
            fetch=http_fetch, tailscale=run_tailscale) -> dict[str, Any]:
    """Run every probe in order, stopping at the first failure."""
    probes: dict[str, Any] = {}
    server_now: datetime | None = None
    host = server_base.split("//", 1)[-1].split(":", 1)[0]

    try:
        probes["network"] = probe_network(tailscale, server_host=host)
    except ProbeError as exc:
        probes["network"] = exc
        return build_status(probes=probes, server_base=server_base,
                            expected_slug=expected_slug,
                            pi_now=datetime.now(UTC), server_now=None)
    try:
        probes["arcticbase"], server_now = probe_arcticbase(
            fetch, base_url=server_base)
    except ProbeError as exc:
        probes["arcticbase"] = exc
        return build_status(probes=probes, server_base=server_base,
                            expected_slug=expected_slug,
                            pi_now=datetime.now(UTC), server_now=None)
    try:
        probes["heartbeat"], payload = probe_heartbeat(
            fetch, base_url=server_base, server_now=server_now)
    except ProbeError as exc:
        probes["heartbeat"] = exc
        return build_status(probes=probes, server_base=server_base,
                            expected_slug=expected_slug,
                            pi_now=datetime.now(UTC), server_now=server_now)
    try:
        probes["project"] = probe_project(payload, expected_slug=expected_slug)
    except ProbeError as exc:
        probes["project"] = exc

    slug = expected_slug or payload.get("active_slug")
    exists = (probe_workbench_exists(fetch, base_url=server_base, slug=slug)
              if slug else None)
    return build_status(probes=probes, server_base=server_base,
                        expected_slug=slug, pi_now=datetime.now(UTC),
                        server_now=server_now, workbench_exists=exists)


# --- serving -----------------------------------------------------------------

# §8.3: the page carries a permanently-moving element so "the screen is alive"
# is answerable from three feet away -- a blank or frozen screen otherwise looks
# identical to a crash. The tick uses performance.now() deltas, NOT the clock:
# it measures elapsed time since the last successful poll, which is a duration
# and so is unaffected by the Pi having no RTC.
_PAGE = """<!doctype html>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>PARE bench</title>
<style>
 :root{color-scheme:dark}
 body{margin:0;background:#111;color:#eee;font:16px/1.5 ui-monospace,monospace}
 header{display:flex;align-items:baseline;gap:1rem;padding:1rem 1.5rem;
        border-bottom:2px solid #333}
 h1{font-size:1.5rem;margin:0;letter-spacing:.05em}
 #tick{margin-left:auto;font-size:1.5rem;color:#6c8}
 ul{list-style:none;margin:0;padding:1rem 1.5rem}
 li{display:flex;gap:1rem;padding:.6rem 0;border-bottom:1px solid #222;
    font-size:1.25rem;align-items:flex-start}
 .name{width:7.5rem;flex:none;text-transform:uppercase;letter-spacing:.08em;color:#999}
 .state{width:7rem;flex:none;font-weight:700}
 .ok .state{color:#6c8} .FAIL .state{color:#f66} .nc .state{color:#666}
 .detail{color:#bbb}
 #banner{padding:1rem 1.5rem;font-size:1.35rem;font-weight:700}
 #banner.ok{background:#132;color:#8fb}
 #banner.bad{background:#311;color:#f99}
 #warn{padding:.75rem 1.5rem;background:#332600;color:#fd6;font-size:1.05rem}
 a{color:#8cf}
 #actions{display:flex;gap:1rem;padding:1rem 1.5rem;border-top:2px solid #333}
 button{font:inherit;font-size:1.25rem;padding:.9rem 1.4rem;border:2px solid #555;
        border-radius:.4rem;background:#222;color:#eee;min-height:3.2rem;
        min-width:11rem;touch-action:manipulation}
 button:disabled{opacity:.35}
 #wb{border-color:#8cf;color:#8cf}
 #pwr{margin-left:auto;border-color:#a44;color:#f99}
 #pwr.armed{background:#511;color:#fee;border-color:#f66}
</style>
<header><h1>PARE BENCH</h1><span id="tick">—</span></header>
<div id="banner">probing…</div>
<div id="warn" hidden></div>
<ul id="probes"></ul>
<div id="actions">
  <button id="wb" hidden>OPEN WORKBENCH</button>
  <button id="pwr">SHUT DOWN</button>
</div>
<script>
let sincePoll = null, base = null;
function paintTick(){
  const el = document.getElementById('tick');
  if (base === null) { el.textContent = '—'; return; }
  const secs = Math.floor((performance.now() - base) / 1000);
  el.textContent = secs + 's since check';
}
async function poll(){
  try {
    const r = await fetch('/status.json', {cache:'no-store'});
    const s = await r.json();
    base = performance.now();
    const banner = document.getElementById('banner');
    if (s.ok) {
      banner.className = 'ok';
      // Say WHY there is no handoff offered. Otherwise the screen reads ALL
      // GREEN and then does nothing, and the operator stands there waiting for
      // a handoff that is never coming.
      banner.textContent = 'ALL GREEN' + (s.handoff_url
        ? ''
        : (s.handoff_blocked ? ' — ' + s.handoff_blocked : ''));
      // HANDOFF IS MANUAL, deliberately. It used to redirect after 2.5s, which
      // meant this page -- the only screen carrying the shutdown control, and
      // the only one that works when ArcticBase is down -- was visible for two
      // and a half seconds at a time. Chromium runs --kiosk under cage: one
      // client, no window manager, no URL bar, so once the redirect fired
      // there was no way back without a keyboard. A button costs one tap and
      // makes Back (Alt+Left) mean "stay here" instead of bouncing forward.
      const wb = document.getElementById('wb');
      wb.hidden = !s.handoff_url;
      wb.onclick = () => { location.href = s.handoff_url; };
    } else {
      banner.className = 'bad';
      banner.textContent = 'FIRST FAILURE: ' + (s.first_failure || 'unknown').toUpperCase();
    }
    const warn = document.getElementById('warn');
    warn.hidden = !s.clock_warning;
    warn.textContent = s.clock_warning || '';
    document.getElementById('probes').innerHTML = s.probes.map(p =>
      '<li class="' + (p.state === 'ok' ? 'ok' : p.state === 'FAIL' ? 'FAIL' : 'nc') + '">' +
      '<span class="name">' + p.name + '</span>' +
      '<span class="state">' + p.state + '</span>' +
      '<span class="detail">' + (p.detail || '') + '</span></li>').join('');
  } catch (e) {
    // The local server itself is down. Say so -- do NOT keep showing stale
    // green probes, which is the "looks healthy" failure this page exists for.
    document.getElementById('banner').className = 'bad';
    document.getElementById('banner').textContent =
      'LOCAL STATUS SERVICE NOT ANSWERING (pare-bench-status)';
    base = null;
  }
}
// SHUTDOWN: arm, then confirm. A bench touchscreen gets brushed by a sleeve,
// an elbow, a cable being dressed -- a single tap that halts the machine would
// be wrong roughly as often as it was right. The armed state reverts on its own
// so a stray tap leaves nothing latched.
const pwr = document.getElementById('pwr');
let armTimer = null;
function disarm(){
  clearTimeout(armTimer); armTimer = null;
  pwr.classList.remove('armed'); pwr.textContent = 'SHUT DOWN';
}
pwr.onclick = async () => {
  if (!pwr.classList.contains('armed')) {
    pwr.classList.add('armed'); pwr.textContent = 'TAP AGAIN TO CONFIRM';
    armTimer = setTimeout(disarm, 5000);
    return;
  }
  disarm();
  pwr.disabled = true; pwr.textContent = 'SHUTTING DOWN…';
  try {
    const r = await fetch('/power/off', {method:'POST'});
    if (!r.ok) throw new Error(await r.text());
  } catch (e) {
    // Say what went wrong. A dead button on a machine you cannot otherwise
    // reach is worse than no button, because you stop looking for the cause.
    pwr.disabled = false; pwr.textContent = 'SHUTDOWN FAILED';
    document.getElementById('banner').className = 'bad';
    document.getElementById('banner').textContent = 'SHUTDOWN REFUSED: ' + e.message;
  }
};

setInterval(paintTick, 1000);
setInterval(poll, 10000);
poll(); paintTick();
</script>
"""


POWEROFF_CMD = ("systemctl", "poweroff")
"""Routed through logind rather than calling /sbin/poweroff directly, so the
authorization decision is polkit's and is declared in a rule file the operator
can read -- see deploy/polkit/50-pare-poweroff.rules. Running as `pare`
(User=pare in the unit) this returns immediately and systemd does the work."""

ALLOWED_ORIGIN = "http://127.0.0.1:8080"


def _origin_is_same_site(origin: str | None) -> bool:
    """Reject a cross-origin POST, which is the whole CSRF defence here.

    The bind is loopback (main() defaults --bind 127.0.0.1), so only something
    ON this Pi can reach the endpoint at all. That is not sufficient by itself:
    the thing on this Pi is a Chromium kiosk pointed at ArcticBase, which
    renders model-authored objects in an iframe with no `sandbox` attribute,
    from a same-origin URL. The artifacts design leans entirely on markdown
    being rendered with `html: False` to keep script out of that frame. If that
    ever regresses -- an upgrade, a plugin that emits raw HTML -- a workbench
    object could POST to this endpoint and halt the bench mid-dump.

    A browser attaches `Origin` to any cross-origin POST, and the workbench is
    a different origin (a tailnet host on :2929), so requiring our own origin
    blocks it. A missing Origin is accepted because a same-origin `fetch` from
    this page may omit it, and because curl-from-the-Pi is the operator, who
    can run `systemctl poweroff` anyway.
    """
    return origin is None or origin == ALLOWED_ORIGIN


def make_handler(*, server_base: str, expected_slug: str | None,
                 poweroff=None):
    from http.server import BaseHTTPRequestHandler

    run_poweroff = poweroff or (lambda: subprocess.run(
        POWEROFF_CMD, check=True, capture_output=True, text=True, timeout=10))

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send(self, body: bytes, content_type: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            # ArcticBase serves index.html with no Cache-Control (§8.3), which
            # is why the kiosk runs --incognito. Do not repeat the mistake here.
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:            # noqa: N802  (stdlib naming)
            if self.path.startswith("/status.json"):
                status = collect(server_base=server_base,
                                 expected_slug=expected_slug)
                self._send(json.dumps(status).encode(), "application/json")
            elif self.path in ("/", "/index.html"):
                self._send(_PAGE.encode(), "text/html; charset=utf-8")
            else:
                self.send_error(404)

        def do_POST(self) -> None:           # noqa: N802  (stdlib naming)
            if self.path != "/power/off":
                self.send_error(404)
                return
            if not _origin_is_same_site(self.headers.get("Origin")):
                # 403 with the offending origin named: if this ever fires it is
                # either a real cross-origin attempt or a misconfiguration, and
                # both need the value to diagnose.
                self._fail(403, f"cross-origin POST refused from "
                                f"{self.headers.get('Origin')!r}")
                return
            try:
                run_poweroff()
            except Exception as exc:          # noqa: BLE001 - surface anything
                detail = getattr(exc, "stderr", "") or str(exc)
                self._fail(500, f"poweroff failed: {detail.strip()[:300]}")
                return
            self._send(b"shutting down\n", "text/plain; charset=utf-8")

        def _fail(self, code: int, message: str) -> None:
            body = message.encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):  # keep the journal readable
            pass

    return Handler


def main(argv: list[str] | None = None) -> int:
    import argparse
    from http.server import ThreadingHTTPServer

    ap = argparse.ArgumentParser(description="PARE bench status page (runs on the Pi)")
    ap.add_argument("--server", required=True,
                    help="ArcticBase base URL, e.g. http://100.82.222.92:2929")
    ap.add_argument("--slug", default=None,
                    help="project slug this screen is pinned to; omit to adopt "
                         "whatever the daemon reports")
    # Loopback by default: the kiosk browser is on this same Pi, and a status
    # page that reveals the tailnet layout has no reason to be reachable from
    # anywhere else.
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    args = ap.parse_args(argv)

    handler = make_handler(server_base=args.server, expected_slug=args.slug)
    httpd = ThreadingHTTPServer((args.bind, args.port), handler)
    print(f"bench status page on http://{args.bind}:{args.port} "
          f"watching {args.server}", flush=True)
    httpd.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
