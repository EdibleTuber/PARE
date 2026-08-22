# PARE + mitm Quick Start

Capture and inspect **HTTPS traffic** from a device/emulator through PARE:
start a mitmproxy daemon, route the target app through it, and query the
capture with four read-only tools — while mitmweb's own UI stays open
side-by-side for a human view of the same capture.

This builds on the base [`QUICKSTART.md`](../QUICKSTART.md) (inference server,
vault, daemon, CLI). Read that first; this guide adds the mitm worker.

## What the mitm worker provides

Unlike `frida`, the mitm worker does **not** own the capture. An
operator-launched `mitmweb -s <addon>` process is the daemon that runs the
proxy, the mitmweb web UI, and a localhost control API. The MCP worker
(`pare-mitm-mcp`) is a thin HTTP client of that control API — it can't start,
stop, or configure the proxy, and it can't modify or replay traffic. It just
exposes four **read-only**, tier-**low** tools over whatever the daemon has
already captured, so mitmweb (browser) and PARE (REPL) can look at one live
capture side-by-side.

| Tool | Wire tier | What it does |
|---|---|---|
| `list_flows` | low | Summary rows (`id, ts, kind, method, host, path, scheme, status, content_type, resp_size, error`) for captured flows. Filterable by `host`, `method`, `status`, `since` (unix ts), `limit`. `kind="error"` rows (failed TLS/connection attempts, e.g. cert pinning) are surfaced, not hidden. |
| `get_flow` | low | Full detail for one flow id: request line + headers + body, response headers + body. JSON bodies pretty-printed; binary bodies shown as `<binary N bytes>`. |
| `search_flows` | low | Regex/substring search across a scope (`url`, `headers`, `req-body`, `resp-body`, `all`); returns matching flow ids with a context snippet. Runs daemon-side. |
| `capture_health` | low | Is the proxy reachable? Returns `{reachable, flows, tls_errors, last_flow_ts}` — the tool for disambiguating "daemon down" vs. "nothing triggered yet" vs. "pinning is breaking the handshake" (`tls_errors > 0`). |

Tools surface to the model as `mitm_list_flows`, `mitm_get_flow`,
`mitm_search_flows`, `mitm_capture_health`. All four auto-execute (still
audited) — nothing in this worker prompts for operator approval.

## 1. Prerequisites

Beyond the base quickstart:

- **The `pare-mitm-mcp` worker installed** into PARE's venv — **with
  `--no-deps`** (it is not a PARE dependency by default):
  ```bash
  .venv/bin/pip install -e ~/Projects/pare-mitm-mcp --no-deps
  ```

  > **Why `--no-deps` (verified 2026-08-04).** The worker is a pure *client* of
  > the daemon — it never imports mitmproxy, only `mcp` (already in PARE's
  > venv) and the stdlib. A plain install would pull mitmproxy, which pins
  > `typing-extensions<=4.14`, **downgrading** PARE's `typing-extensions`
  > (4.15.0) out from under `pydantic`/`mcp`. `--no-deps` keeps PARE's
  > environment untouched and is the architecturally correct split: PARE runs
  > the client, a separate env runs the daemon.

  Confirm the worker imports cleanly under PARE's venv:
  ```bash
  .venv/bin/python -c "import pare_mitm_mcp.server as s; print(type(s.build_server()).__name__)"
  # -> FastMCP
  ```
- **A separate environment that has `mitmweb`** to run the daemon. The
  `pare-mitm-mcp` repo's own venv already does:
  ```bash
  ~/Projects/pare-mitm-mcp/.venv/bin/mitmweb --version   # -> Mitmproxy: 12.x
  ```
- **A device or emulator you can route through an HTTP(S) proxy** — a
  physical phone/tablet on the same LAN as the PARE host, or an emulator/AVD
  with proxy support.

## 2. Start the daemon

From inside a PARE session:

```
/mitm up
```

That's all it takes — no `PATH` juggling. The launcher locates the `mitmweb`
binary itself, trying in order:

1. `PARE_MITM_MITMWEB`, if you set it (an explicit override; if it points at
   something unusable the launcher fails loudly rather than guessing);
2. alongside the running interpreter (mitmproxy installed in the same venv);
3. `PATH`;
4. the worker repo's own venv — for an editable install it finds
   `~/Projects/pare-mitm-mcp/.venv/bin/mitmweb`.

Step 4 is what makes `/mitm up` work from PARE even though PARE's venv
deliberately has no mitmproxy (see `--no-deps` above). The startup line tells
you which binary it used:

```
mitm daemon up (proxy :8080, ui :8081, control :8788, mitmweb: /home/…/pare-mitm-mcp/.venv/bin/mitmweb)
```

It's **idempotent** — running it again while the daemon is up just prints
`mitm daemon already up` rather than spawning a second instance. You can also
run the launcher directly if you want its stdout in front of you:

```bash
~/Projects/PARE/.venv/bin/pare-mitm-daemon up
```

> **One requirement:** `/mitm up` shells out to the `pare-mitm-daemon` console
> script by name, so **activate PARE's venv** before starting PARE (the same
> reason PARE's README gives for `pare-frida-mcp`). If you launch PARE without
> activating, `/mitm up` reports that `pare-mitm-daemon` isn't on `PATH`.

Check status and stop:

```
/mitm status
/mitm down
```

`/mitm status` works even when the worker isn't mounted in this PARE process
— it calls the daemon directly. `/mitm down` in v1 does **not** kill the
process (mitmweb is operator-launched in its own terminal/session); it prints
the `pkill` invocation to run yourself.

**Bind split** (this is what makes the side-by-side model safe by default):

| Listener | Bind | Purpose |
|---|---|---|
| Proxy (device traffic) | `0.0.0.0:8080` | so a phone/emulator on the LAN can reach it |
| mitmweb UI | `127.0.0.1:8081` | human browser view, local only |
| Control API (worker) | `127.0.0.1:8788` | `pare-mitm-mcp`'s HTTP client, local only |

All three ports are configurable via `PARE_MITM_PROXY_PORT`,
`PARE_MITM_WEB_PORT`, `PARE_MITM_CONTROL_HOST`/`PARE_MITM_CONTROL_PORT` — see
the [`pare-mitm-mcp` README](https://github.com/EdibleTuber/pare-mitm-mcp#configuration)
for the full list (also covers `PARE_MITM_MAX_FLOWS`, the ring-buffer
retention cap).

### Opening the mitmweb UI (the auth token)

The mitmweb UI requires an auth token, so a bare `http://127.0.0.1:8081` will
answer **403**. `/mitm up` prints the full URL with the token on its own line:

```
mitm daemon up (proxy :8080, ui :8081, control :8788, mitmweb: /home/…/mitmweb)
ui: http://127.0.0.1:8081/?token=…
```

Open that URL — that's the mitmweb view you'll watch alongside the PARE CLI.

**Lost the token?** Just run `/mitm up` again. It's idempotent, and the
already-up path re-prints the same URL. The token is fixed (not regenerated
per launch): it's persisted at `~/.local/state/pare-mitm/web_token` (mode
0600) so the URL stays valid across restarts. Override it with
`PARE_MITM_WEB_PASSWORD` if you'd rather choose your own.

> mitmproxy logs a note that the token is stored as a plaintext password
> rather than an argon2 hash. For a UI bound to `127.0.0.1` with the token
> file at 0600, that's a proportionate trade — but you can set
> `PARE_MITM_WEB_PASSWORD` to an argon2 hash if you prefer.

Daemon output (previously discarded) is now captured at
`~/.local/state/pare-mitm/daemon.log` — check there first if the daemon
fails to come up.

### 2b. Smoke-test the stack with no device (2 minutes)

Do this **before** touching the emulator. It proves the proxy, the CA, the
control API, and all four tools work, so that if the device later shows
nothing you know the problem is device-side. Verified end-to-end 2026-08-04.

```bash
# 1) plain HTTP through the proxy
curl -s -o /dev/null -w "%{http_code}\n" -x http://127.0.0.1:8080 http://example.com/

# 2) HTTPS trusting the mitm CA -> proves interception + decryption
curl -s -o /dev/null -w "%{http_code}\n" -x http://127.0.0.1:8080 \
     --cacert ~/.mitmproxy/mitmproxy-ca-cert.pem https://example.com/

# 3) HTTPS *rejecting* the CA -> simulates a pinned/untrusting app
curl -s -o /dev/null -x http://127.0.0.1:8080 https://example.com/; echo "exit=$?"
```

Expected: `200`, `200`, `exit=60`. Then:

```bash
pare-mitm-daemon status
# -> up — 3 flows, 1 tls-errors
```

That third curl is the important one: it is the same signal a
CA-untrusting or pinned app produces, and it **must** show up as
`tls-errors`, not as silence. Now check the tools see it (from PARE's venv):

```bash
~/Projects/PARE/.venv/bin/python -c "
import asyncio, json
from pare_mitm_mcp import tools
print(json.loads(asyncio.run(tools.capture_health())))
print(json.loads(asyncio.run(tools.list_flows()))['summary'])"
```

Expected — note the hint on the second line, which is what stops the agent
from mis-reading a pinned app as 'nothing triggered yet':

```
{'summary': 'proxy reachable — 3 flows, 1 tls-errors', 'reachable': True, ...}
3 flows (1 tls-error rows — pinning?)
```

## 3. Point the device at the proxy, then trust the CA

**On an emulator/AVD** (the usual case here). The emulator reaches the host
loopback at the special address **`10.0.2.2`** — no LAN IP needed:

```bash
adb shell settings put global http_proxy 10.0.2.2:8080
adb shell settings get global http_proxy      # verify -> 10.0.2.2:8080
```

To clear it later: `adb shell settings put global http_proxy :0`. You can
also bake it in at boot with `emulator -avd <name> -http-proxy http://10.0.2.2:8080`.

**On a physical device**: find the PARE host's LAN IP (`ip addr`) and set the
device's Wi-Fi proxy to `<host-ip>:8080` (the proxy port, **not** the
web/control ports).

Then install the CA — with the proxy set, browse to `http://mitm.it` on the
device, or push `~/.mitmproxy/mitmproxy-ca-cert.cer` over and install it via
Settings.

**Footgun: on Android 7+, a user-installed CA is not trusted by app traffic
by default.** Android split the trust store in Nougat — a CA you install as
a "user" certificate (via Settings, or the `mitm.it` flow) is trusted by the
browser and by apps that opt in, but **not** by apps targeting API 24+ unless
they explicitly declare a network security config that trusts user CAs. Most
apps don't. You will see the CA install "succeed" and then still get
`tls_errors > 0` from `capture_health` — that's not a proxy misconfiguration,
it's this default. Fixes are all operator-side and out of scope for this
worker: push the CA into the **system** trust store (needs root), or patch
the target app's network security config, or use a Frida CA-pin/trust
override alongside step 4 below.

## 4. Defeat certificate pinning

If the target app pins certificates, plaintext never reaches the proxy no
matter how correctly steps 1–3 are done — you'll see `capture_health` report
`tls_errors > 0` and `list_flows` return `kind="error"` rows instead of real
traffic. PARE **cannot see or verify whether an app is pinning** — the mitm
worker only reports the symptom (`tls_errors > 0`), and this ships no
first-class pinning-bypass tool of its own. Defeating pinning is entirely
operator-driven, typically via Frida (`objection`'s universal SSL-unpinning
script, or a hand-written unpin hook) run *before* you drive traffic. See
[`docs/frida-quickstart.md`](frida-quickstart.md) for attaching and hooking
with PARE's `frida_*` tools.

## 5. A guided session

With the daemon up (step 2), the device routed and CA-trusted (step 3), and
pinning handled if needed (step 4):

```
> Is the capture reachable, and has anything come through yet?

  → mitm_capture_health
  → {"reachable": true, "flows": 0, "tls_errors": 0, "last_flow_ts": null}

Reachable, but nothing captured yet — go trigger the request in the app.

(you drive the app: log in, load a screen, whatever you're investigating)

> List what's come in during the last minute.

  → mitm_list_flows(since=<now - 60>)
  → 6 flows: GET api.example.com/v1/session (200), POST api.example.com/v1/login (200), ...

> Search the captures for "session_token".

  → mitm_search_flows(pattern="session_token", scope="all")
  → 2 matches: flow f-0021 (resp-body), flow f-0023 (headers)

> Show me the full detail on f-0021.

  → mitm_get_flow(id="f-0021")
  → POST api.example.com/v1/login
    request headers: {...}
    request body: {"user": "...", "password": "..."}
    response headers: {...}
    response body: {"session_token": "eyJ...", "expires_in": 3600}
```

Flip over to the mitmweb tab in your browser at any point — it's the same
live capture, just a different lens on it.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| No flows at all, `capture_health` shows `flows: 0, tls_errors: 0` | One of: daemon isn't up (`/mitm status`), device isn't actually routed through the proxy (check the device's proxy settings and that it's on the same network as the PARE host), or you just haven't triggered a request yet. Nothing wrong here yet — go drive the app. |
| `capture_health` shows `tls_errors > 0` | **The diagnostic that distinguishes pinning from nothing-happened.** Either the app is pinning certificates (see step 4) or the device hasn't trusted the mitmproxy CA correctly (see step 3's Android 7+ footgun). `list_flows` will show `kind="error"` rows alongside/instead of real traffic. |
| A tool call returns `{"error": true, "summary": "daemon not reachable — start it with /mitm up"}` | The worker is mounted but can't reach the control API — the daemon isn't running (or died). Run `/mitm up` (or `/mitm status`, which works even with the worker unmounted). |
| `/mitm up`/`down` replies `pare-mitm-daemon not found — install the worker into this venv: pip install -e ~/Projects/pare-mitm-mcp` | The worker isn't installed into whatever venv the PARE daemon process is running with `PATH` set from. Install it (step 1) and make sure the daemon's shell has that venv activated/on `PATH`. |
| `/mitm up` prints `mitmweb not found — is mitmproxy installed in this env?` | Same root cause as above, but for the `mitmweb` binary specifically — confirm `.venv/bin/mitmweb --version` runs. |
| `/mitm up` prints `mitm daemon did not come up within 5s — check the port isn't held (:8080/:8081/:8788)` | Another process already has one of the three ports. Free it, or move the daemon's ports with `PARE_MITM_PROXY_PORT` / `PARE_MITM_WEB_PORT` / `PARE_MITM_CONTROL_PORT` (and set the same values before starting the worker, so the two sides still agree). |

## A short security note

- **The proxy listener (`0.0.0.0:8080` by default) is an open forward proxy
  on your LAN.** Anything that can reach that port can tunnel traffic through
  it. Scope it to your device's IP with a firewall rule before pointing a
  device at it on anything other than an isolated lab network:
  ```bash
  sudo ufw allow from 192.168.1.50 to any port 8080 proto tcp   # replace with your device's IP
  sudo ufw deny 8080/tcp
  ```
  (add the allow rule *before* the deny — `ufw` matches in rule order). The
  mitmweb UI (`8081`) and control API (`8788`) already bind `127.0.0.1` and
  don't need a rule.
- **Captures hold live tokens, session cookies, and credentials.** The
  `pare-mitm-mcp` repo's `.gitignore` already excludes `*.mitm`, `*.flows`,
  and `captures/` — don't override that by exporting a `.mitm` file into the
  repo. The worker itself keeps flows in an in-memory ring buffer only
  (`PARE_MITM_MAX_FLOWS`, default 5000) and writes nothing to disk on its own.
- **Response/request bodies flow into the model's context.** `get_flow` and
  `search_flows` hand raw bodies (which may include auth tokens or PII from
  the target app) to the model. That's fine while PARE's inference is local
  — the traffic never leaves the box — but it's worth knowing before pointing
  PARE at a hosted/cloud model, where that context would leave the machine.
