# Networked MCP workers — design

**Date:** 2026-09-05
**Status:** draft, pending review
**Repos:** `agent_core`, `pare-frida-mcp`, `pare-static-mcp`, `pare-mitm-mcp`, `PARE`
**Follows:** [`2026-09-04-dynamic-worker-loading-design.md`](2026-09-04-dynamic-worker-loading-design.md)
**Unblocks:** `pare-hardware-mcp` (a Tigard worker on a Raspberry Pi), and the
ArcticBase interaction surface that worker's autonomous mode depends on.

## 1. Why

PARE's daemon now runs on a headless inference server. The hardware it needs to reach
does not:

| Where | What lives there | Why it stays there |
|---|---|---|
| Inference server | PARE daemon, model, `static`, `mitm` | headless; the model is here |
| Laptop | rooted Android emulator | you need to see and touch it |
| Raspberry Pi | Tigard, wired to a target | it goes to the bench, and it has the screen |

Every worker today is `transport: stdio` — a subprocess the daemon spawns. **A daemon
cannot spawn a subprocess on another machine**, so the Pi worker is impossible as
designed, and the emulator is only reachable by tunnelling adb and frida back to the
server.

The alternative is to run each worker on the machine that owns its hardware and have
the daemon connect over the network. PARE's side is already built for this:
`WorkerSpec` accepts `transport: streamable_http` with an `endpoint`,
`MCPClient.from_spec` dispatches on it, and `agent_core` has a live Streamable-HTTP
conformance fixture. The eight commented-out `apk_re_agents` entries in `workers.yaml`
are exactly this shape.

What is missing is on the worker side, and in the trust model.

### The tunnel alternative, and why it loses

The frida worker could stay on the server and reach the emulator by forwarding adb
(port 5037) or `frida-server` (27042). It works, but: adb over TCP is entirely
unauthenticated, so exposing it means anyone who reaches the port owns the device; it
needs a tunnel per machine per protocol; and it does nothing for the Pi, which still
cannot host a stdio worker. Running the worker next to its hardware solves all three
with one mechanism.

## 2. Goals

1. A worker can serve over Streamable HTTP instead of stdio, chosen at launch, with no
   change to the tools it exposes.
2. PARE can authenticate to a networked worker. Today it **cannot** — `MCPClient`
   passes only a URL.
3. `workers.yaml` stays committable: no secret material in it.
4. Moving `pare-frida-mcp` to the laptop is the proof, and removes the adb-tunnel
   question entirely.
5. The risk model is unchanged in meaning and unweakened in practice by the worker
   being remote.

## 3. Non-goals

- **TLS between daemon and worker.** The transport is a tunnel or a trusted LAN
  segment; adding self-signed certificate management to a Raspberry Pi is a larger
  project than this one and buys little over an SSH tunnel or WireGuard.
- **OAuth / JWT / WorkOS.** FastMCP ships providers for all three. A static bearer
  token is sufficient for a single-operator lab and has no rotation infrastructure to
  build.
- **Service discovery.** Endpoints are declared in `workers.yaml` by hand, like every
  other worker.
- **Making stdio workers remote.** stdio stays exactly as it is; this is an additional
  transport, not a replacement. `static` has no reason to leave the server.

## 4. Decisions

**D1 — The worker chooses its transport at launch; the daemon learns it from
`workers.yaml`.** A worker binary can serve either way. Nothing in a worker's tool
code changes, and a worker can be run as stdio for local development and HTTP in
deployment without a code path diverging.

**D2 — Bearer token authentication, required, never anonymous.** FastMCP accepts an
`auth=` provider and ships `BearerAuthProvider`; the client side needs
`streamablehttp_client(url, headers={"Authorization": f"Bearer {token}"})`. A worker
launched with HTTP transport and no token configured **refuses to start** rather than
serving open. There is no anonymous mode and no flag to disable auth — a worker that
can write flash must not be one misconfiguration away from being world-callable.

**D3 — The token lives in the environment, referenced by name from `workers.yaml`.**
The spec gains `auth_token_env: PARE_HARDWARE_TOKEN`; the daemon reads that variable
at connect time. `workers.yaml` is committed to git and describes the fleet — it must
never carry a secret. A named-variable indirection also means the same file works for
different operators and machines.

**D4 — Bind to loopback by default.** A worker serving HTTP binds `127.0.0.1` unless
explicitly told otherwise, and reaching it from another host is the operator's
deliberate act — an SSH tunnel, WireGuard, or an explicit bind address they had to
type. The default must not be the one that exposes a JTAG interface to the LAN.

**D5 — Networked in-house workers stay `kind: internal`.** They advertise per-tool
wire tiers and those tiers escalate above the floor, exactly as for a stdio worker.
`external_mcp` (floor-only, wire tiers ignored) is for third-party workers and would
throw away tier information we control. The compensating control is D6.

**D6 — Destructive tools on networked workers get operator pins, from day one.** A
remote worker is more plausibly tampered with than a local subprocess, and the tier a
worker self-reports is exactly what an attacker who owns it would lie about. Pins in
`workers.yaml` are the operator's authoritative ceiling and are the only part of the
chain not under the worker's control. This is the same conclusion the mitm audit
reached the hard way.

**D7 — A shared serving helper in `agent_core`, not four copies.** All four workers'
`main()` functions are already near-identical, and all four already import
`agent_core.workers.risk` for `RISK_TIER_META_KEY`. One helper standardises the
environment-variable names, the refuse-to-start-without-a-token rule, and the bind
default — so a new worker cannot get the security posture wrong by copying an old one
badly.

## 5. Architecture

### 5.1 `agent_core` — client side

`MCPClient.connect()` currently does `streamablehttp_client(self.endpoint)`. The
underlying function accepts `headers`, `timeout`, `sse_read_timeout` and `auth`; none
are passed. Change it to send the `Authorization` header when the spec names a token
variable, and to pass an explicit timeout.

`WorkerSpec` gains two optional fields:

```python
auth_token_env: str | None = None
"""Name of an environment variable holding the bearer token for this worker.
The token itself is never stored in workers.yaml. Required in practice for
transport='streamable_http'; see the validator below."""

request_timeout: float | None = None
"""Per-request timeout for HTTP transports. A worker across a network link needs
a bound that a local subprocess does not."""
```

A model validator rejects `transport: streamable_http` without `auth_token_env`, so an
unauthenticated networked worker cannot be declared. It is a config error, caught at
`WorkerRegistry.load()`, not a runtime surprise.

**Missing-token behaviour:** if `auth_token_env` names a variable that is not set,
`connect()` fails with a clear error and the worker's `last_error` says which variable
was missing. It does **not** fall back to connecting without auth.

### 5.2 `agent_core` — worker-side serving helper

New module `agent_core/workers/serve.py`:

```python
def run_worker(server, *, default_transport: str = "stdio") -> None:
    """Serve a FastMCP worker over stdio or Streamable HTTP.

    Transport and binding come from the environment so the same binary works
    in both modes:
        PARE_WORKER_TRANSPORT   stdio | http        (default: stdio)
        PARE_WORKER_HOST        bind address        (default: 127.0.0.1)
        PARE_WORKER_PORT        port                (default: 9100)
        PARE_WORKER_TOKEN       bearer token        (required when http)

    Refuses to start in http mode without a token. There is no anonymous mode.
    """
```

Each worker's `main()` becomes `run_worker(build_server())`. Default stays stdio, so
every existing deployment is unaffected.

**An undeclared dependency to fix while here:** `pare-static-mcp`, `pare-frida-mcp` and
`pare-mitm-mcp` all import `agent_core.workers.risk` at runtime but none declares
`agent_core` in its `pyproject.toml`. They work today only because the venv happens to
have it. This design makes the dependency heavier, so it should be declared.

### 5.3 `PARE` — declaration

```yaml
  frida:
    endpoint: http://127.0.0.1:9101/mcp     # SSH tunnel to the laptop
    transport: streamable_http
    auth_token_env: PARE_FRIDA_TOKEN
    request_timeout: 30
    risk_default: low
    autoload: false        # the laptop is not always up
    capability_tags: [mobile, dynamic, android, frida]
```

`autoload: false` matters here: a networked worker's host is not always powered on, and
`/worker load frida` when you sit down at the emulator is the right ergonomic. A dark
laptop then costs one visible `last_error` line rather than a boot delay.

### 5.4 What does not change

Tool code, tier advertisement, the capture layer, the audit log, `WorkerManager`, and
the whole `/worker` command surface. A networked worker is a `WorkerSpec` with a
different transport; everything above `MCPClient` is already transport-agnostic.

## 6. The trust model

This is the part worth getting right, because it is where a networked worker differs
from a subprocess in kind rather than degree.

**What changes.** A stdio worker is a process the daemon launched, on the daemon's
machine, talking over a pipe no one else can reach. A networked worker is a listening
socket. Three properties stop being free: *only the daemon can talk to it* (now
enforced by the token, not by process ancestry), *nobody can impersonate it* (now
enforced by the tunnel, since there is no TLS), and *its binary is the one the operator
installed* (now the remote host's problem).

**What defends each.**

| Property | Defence |
|---|---|
| Only the daemon calls the worker | Bearer token, required; loopback bind by default |
| The worker is not impersonated | Tunnel or trusted segment (D3 non-goal: no TLS) |
| A tampered worker cannot escalate itself | Operator pins (D6) + the session tier high-water mark, which never lets a tool resolve below its highest observed tier |
| A tampered worker cannot inherit approvals | Approvals are generation-keyed; a reconnect bumps the generation and evicts them |
| A compromised worker cannot hide what it did | Every dispatch is audited before and after, including `cancelled` |

Most of that already exists — it was built for the dynamic-loading work, where the
threat was a swapped binary between unload and reload. A remote worker is the same
threat with a longer wire.

**What is explicitly accepted.** Without TLS, anyone who can already read traffic
between daemon and worker can read tool arguments and results — which for the hardware
worker means flash contents. That is accepted because the transport is a tunnel; if
that assumption ever fails, TLS becomes a real requirement rather than a non-goal.

**The bearer token is a shared secret with no rotation story.** Rotating it means
restarting the worker and updating the daemon's environment. Acceptable for a
single-operator lab; explicitly not a design for multiple users.

## 7. Failure modes and operations

A networked worker fails in ways a subprocess does not, and most of the handling
already exists from the dynamic-loading work:

- **Host is down / port closed.** `connect()` fails, `load()` returns
  `ok=False, error_kind="connect_timeout"` or `spawn_failed`, and `/worker list` shows
  the endpoint and the error. The per-worker connect bound means one dark host does not
  delay the others; the cascade fix means it does not cancel their discovery either.
- **Token wrong or missing.** Connect fails with an authentication error. The error
  must name the environment variable, not just report 401 — the most likely cause is a
  variable that was never exported in the daemon's environment.
- **Link drops mid-dispatch.** Surfaces through the existing error path and is audited.
  A dropped link during a *hardware write* is the genuinely dangerous case, and it is a
  reason the hardware worker's write tools must be individually gated and verify after
  writing rather than assuming success — that belongs to the hardware spec, but it is
  motivated here.
- **Latency.** Every tool call now crosses a network hop. Frida hook-event polling is
  the sensitive one; the capture layer already buffers worker-side, so the cost is
  per-poll rather than per-event.

## 8. Testing

`agent_core` already has the fixtures: `streamable_http_stub.py` (a FastMCP worker
served with uvicorn) and `test_conformance_streamable_http.py`. What is new:

- `run_worker` serves stdio when told to, HTTP when told to, and **refuses to start in
  HTTP mode with no token** — the last is the security-relevant one and must fail
  loudly, not warn.
- `MCPClient` sends the `Authorization` header; a worker rejecting a bad token surfaces
  as a clear connect failure rather than a hang.
- `WorkerSpec` rejects `streamable_http` without `auth_token_env` at registry-load time.
- A missing environment variable produces an error naming the variable.
- End to end: `WorkerManager.load()` against a real token-protected HTTP worker on
  loopback — load, list tools, dispatch, unload — extending
  `scripts/live_worker_lifecycle.py`, which currently covers only the stdio path.

## 9. Sequencing

1. **`agent_core`** — client headers and timeout, the two `WorkerSpec` fields and their
   validator, `serve.py`, tests. Ships as v1.9.0; additive.
2. **The three existing workers** — adopt `run_worker`, declare the `agent_core`
   dependency. No behaviour change while they stay stdio.
3. **Move `frida` to the laptop** — the proof. `pare-frida-mcp` runs there over HTTP
   behind an SSH tunnel; `workers.yaml` gains the endpoint and `autoload: false`; the
   adb tunnel disappears. Verified by running an actual attach from the server.
4. **`pare-hardware-mcp`** then has a transport to be born into, and its own spec.

## 10. Risks

- **A worker that refuses to start without a token will bite during development.** The
  mitigation is that stdio remains the default: local development never touches this
  path. Accepted deliberately — the alternative is a disable-auth flag, which is the
  thing that ends up set in production.
- **Frida over a network hop may be slower than useful** for high-frequency hook events.
  Unknown until measured. If it is a problem the answer is worker-side batching, which
  the capture layer already does — but this design does not assume that is enough, and
  step 3 exists partly to find out.
- **Two more `WorkerSpec` fields** is more configuration surface, and `workers.yaml` is
  the trust anchor. The validator is what keeps a networked worker from being declared
  wrongly; it needs a test, not just a docstring.
