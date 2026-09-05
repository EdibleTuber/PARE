# Networked MCP workers — design

**Date:** 2026-09-05
**Status:** v2 — revised after the trust-boundary decision; pending review
**Repos:** `agent_core`, `pare-frida-mcp`, `pare-static-mcp`, `pare-mitm-mcp`, `PARE`
**Follows:** [`2026-09-04-dynamic-worker-loading-design.md`](2026-09-04-dynamic-worker-loading-design.md)
**Unblocks:** `pare-hardware-mcp` (a Tigard worker on a Raspberry Pi), and the
ArcticBase interaction surface its autonomous mode depends on.

> **v2 changes.** v1 required a bearer token on every networked worker. That is
> dropped. The operator runs a Tailscale node and does not intend to reach this
> deployment from outside the LAN, and ArcticBase — already part of this ecosystem —
> is explicitly "single-user, no auth, designed for LAN / Tailscale". Inventing a
> second, stricter posture for workers would have been inconsistent without being
> meaningfully safer. §6 now records the boundary, what it costs, and what would make
> it wrong.

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
designed, and the emulator is reachable only by tunnelling adb and frida back to the
server.

The alternative is to run each worker on the machine that owns its hardware and have
the daemon connect over the network. PARE's side is already built for this:
`WorkerSpec` accepts `transport: streamable_http` with an `endpoint`,
`MCPClient.from_spec` dispatches on it, and `agent_core` has a live Streamable-HTTP
conformance fixture. The eight commented-out `apk_re_agents` entries in `workers.yaml`
are exactly this shape. What is missing is worker-side serving.

### The tunnel alternative, and why it loses

The frida worker could stay on the server and reach the emulator by forwarding adb
(5037) or `frida-server` (27042). It works, but adb over TCP is unauthenticated, it
needs a tunnel per machine per protocol, and it does nothing for the Pi — which still
cannot host a stdio worker. Running the worker next to its hardware solves all three
with one mechanism.

## 2. Goals

1. A worker serves over Streamable HTTP or stdio, chosen at launch, with no change to
   the tools it exposes.
2. The trust boundary is **stated explicitly** and is the same one the rest of this
   ecosystem already uses.
3. `workers.yaml` gains networked entries without becoming a place secrets live.
4. Moving `pare-frida-mcp` to the laptop is the proof, and removes the adb-tunnel
   question entirely.
5. The risk model is unchanged in meaning and unweakened in practice by the worker
   being remote.

## 3. Non-goals

- **Application-level authentication.** See §6. The network is the boundary, matching
  ArcticBase's stated posture for the same deployment.
- **TLS between daemon and worker.** Same reasoning; the tailnet already provides
  transport encryption between nodes.
- **Service discovery.** Endpoints are declared in `workers.yaml` by hand, like
  everything else.
- **A runtime-re-readable catalog.** Changing a worker's transport requires a daemon
  restart — see §5.4. Deliberately out of scope.
- **Making stdio workers remote.** stdio is unchanged and stays the default. `static`
  has no reason to leave the server.

## 4. Decisions

**D1 — The worker chooses its transport at launch; the daemon learns it from
`workers.yaml`.** One binary serves either way. A worker can run as stdio for local
development and HTTP in deployment without the code paths diverging.

**D2 — The network is the trust boundary.** No bearer tokens, no TLS between daemon
and worker. The deployment sits on a Tailscale tailnet and a LAN the operator
controls, and ArcticBase — already in this ecosystem, already holding approval
documents — makes the same assumption explicitly. A second, stricter posture for
workers alone would add token management without changing who can actually reach the
port. §6 records what this costs and what would invalidate it.

**D3 — Bind to a specific interface, never `0.0.0.0`.** With no application auth, the
bind address *is* the access control, which makes it more important rather than less.
`run_worker` defaults to `127.0.0.1`; reaching a worker from another host requires the
operator to name an interface — the tailnet address, typically — as a deliberate act.
A worker must never default to every interface it has.

**D4 — Networked in-house workers stay `kind: internal`.** They advertise per-tool wire
tiers and those escalate above the floor, exactly as for a stdio worker.
`external_mcp` (floor-only) is for third-party workers and would discard tier
information we control.

**D5 — Destructive tools on networked workers get operator pins, from day one.** The
tier a worker self-reports is exactly what a tampered worker would misreport, and pins
in `workers.yaml` are the only link in that chain not under the worker's control. This
matters more, not less, without app auth. It is the same conclusion the mitm audit
reached the hard way.

**D6 — A shared serving helper in `agent_core`, not four copies.** All four workers'
`main()` functions are already near-identical, and all four already import
`agent_core.workers.risk`. One helper standardises the environment-variable names and
the bind default, so a new worker cannot inherit a bad posture by copying an old one
carelessly.

## 5. Architecture

### 5.1 `agent_core` — client side

`MCPClient.connect()` currently calls `streamablehttp_client(self.endpoint)`. The
underlying function accepts `headers`, `timeout`, `sse_read_timeout` and `auth`; none
are passed. Only one is needed now:

```python
request_timeout: float | None = None   # new WorkerSpec field
"""Per-request timeout for HTTP transports. A worker across a network link needs a
bound that a local subprocess does not. None uses the SDK default."""
```

`connect()` passes it through. The `headers` parameter stays unused — if the boundary
assumption ever changes (§6), adding an `Authorization` header is a three-line change
against a parameter the SDK already accepts, not a redesign.

### 5.2 `agent_core` — worker-side serving helper

New module `agent_core/workers/serve.py`:

```python
def run_worker(server, *, default_transport: str = "stdio") -> None:
    """Serve a FastMCP worker over stdio or Streamable HTTP.

    Transport and binding come from the environment, so one binary works both ways:
        PARE_WORKER_TRANSPORT   stdio | http   (default: stdio)
        PARE_WORKER_HOST        bind address   (default: 127.0.0.1)
        PARE_WORKER_PORT        port           (default: 9100)

    The host default is loopback deliberately: with no application-level auth, the
    bind address is the access control. Binding to a routable interface must be an
    explicit act, and `0.0.0.0` is rejected rather than merely discouraged.
    """
```

Each worker's `main()` becomes `run_worker(build_server())`. Default stays stdio, so
every existing deployment is unaffected.

**Rejecting `0.0.0.0` outright** is a judgement call worth stating: it is the value
someone reaches for when a tunnel is not working, and under D2 it is the difference
between "reachable from the tailnet" and "reachable from the coffee shop". If an
operator genuinely needs every interface they can bind the specific addresses.

**An undeclared dependency to fix while here:** `pare-static-mcp`, `pare-frida-mcp` and
`pare-mitm-mcp` all import `agent_core.workers.risk` at runtime, and none declares
`agent_core` in its `pyproject.toml`. They work today only because the venv happens to
have it. This design leans on that import harder, so it should be declared.

### 5.3 `PARE` — declaration

```yaml
  frida:
    endpoint: http://100.x.y.z:9101/mcp     # laptop's tailnet address
    transport: streamable_http
    request_timeout: 30
    risk_default: low
    autoload: false        # the laptop is not always up
    capability_tags: [mobile, dynamic, android, frida]
```

`autoload: false` matters here: a networked worker's host is not always powered on, and
`/worker load frida` when you sit down at the emulator is the right ergonomic. A dark
laptop then costs one visible `last_error` line rather than a boot delay.

### 5.4 Switching a worker between transports

**At launch: supported.** Set `PARE_WORKER_TRANSPORT` and start the worker either way.

**At runtime on a live daemon: not supported, deliberately.** Changing a declared
worker's transport means editing `workers.yaml`, and the catalog is fixed at boot. The
reason is specific and load-bearing: re-reading the YAML would flow new specs into the
pool while `RiskGate`'s operator pins stayed frozen at boot values — so an edited
`risk_default` would take effect while an added *pin* silently would not. That is a
fail-open on the trust anchor, and re-reading therefore requires rebuilding `RiskGate`
in the same operation.

So: change transport, restart the daemon. `/worker reload` re-execs a worker; it does
not re-read its declaration.

**Declaring the same worker twice** (`frida_local` stdio, `frida_laptop` HTTP) is not a
workaround. The worker name is the tool prefix, so tools would become
`frida_laptop_java_hook` — breaking `pare/handback.py`'s hardcoded `frida_*` constants
and showing the model two competing frida toolsets.

### 5.5 What does not change

Tool code, tier advertisement, the capture layer, the audit log, `WorkerManager`, and
the entire `/worker` command surface. A networked worker is a `WorkerSpec` with a
different transport; everything above `MCPClient` is already transport-agnostic.

## 6. The trust boundary

**The boundary is the tailnet and the operator's LAN.** Anything that can route to a
worker's port can call any tool it exposes, including tools that write flash or drive
JTAG. There is no application-level authentication and no transport encryption between
daemon and worker beyond what the tailnet provides.

This is a deliberate choice, and it is the same one ArcticBase already makes for the
same deployment — it holds approval documents and states plainly that it is
"single-user, no auth, designed for LAN / Tailscale". Adding tokens to workers alone
would have produced two postures to maintain and one to get wrong, without changing who
can reach the port.

### What still defends what

App auth would have protected against an *unauthorised caller*. Nothing here does. The
remaining controls address a different threat — a worker that is reachable but
**untrustworthy** — and they are unchanged by this design:

| Threat | Defence |
|---|---|
| A tampered worker under-reports a tool's tier | Operator pins (D5), which the worker cannot influence |
| A worker re-advertises a lower tier after a reload | Session tier high-water mark, never evicted |
| A restarted worker inherits a standing approval | Approvals are generation-keyed; a reconnect bumps the generation |
| A dispatch happens without a record | Every call audited before and after, including cancellation |

Those exist because the dynamic-loading work assumed a binary could be swapped between
unload and reload. A networked worker is the same threat with a longer wire, so the
controls transfer intact.

### What this costs, stated plainly

- Anything on the tailnet can drive the hardware worker — including writing flash.
- Anything on the tailnet can read tool arguments and results in transit within a
  node, which for the hardware worker means flash contents.
- A compromised device on the tailnet is a compromised lab. There is no second layer.

### What would make this decision wrong

Recorded so it is revisitable rather than forgotten. Any of these should trigger a
re-read of this section:

1. **The Pi leaves the trusted network** — taken to a bench on someone else's wifi, or
   onto a client site, while wired to a target.
2. **Another person or device joins the tailnet**, so "single user" stops being true.
3. **A worker gains a tool that can damage something irreplaceable** — a target that
   cannot be re-flashed, or a device that is not yours.
4. **The daemon becomes reachable from outside the LAN**, whether deliberately or by a
   misconfigured exit node.

The re-entry cost is deliberately low: `streamablehttp_client` already accepts
`headers`, FastMCP already ships a `BearerAuthProvider`, and `WorkerSpec` would need
one field naming an environment variable. That is an afternoon, not a redesign — which
is precisely why deferring it now is reasonable rather than negligent.

### One consequence for the ArcticBase work

If HITL approvals are later routed through ArcticBase, the approval channel inherits
this boundary: anything that can POST to ArcticBase can approve a `critical` operation.
Under D2 that is consistent rather than a hole — the same devices are trusted either
way. It is recorded here so the ArcticBase spec makes that inheritance a stated choice
rather than an accident.

There is also a simpler option worth carrying into that spec, on plumbing grounds
rather than security: let ArcticBase **render** the decision document — target, offset,
byte count, pre-image — and let the answer come back over the daemon's own socket. PARE
then never has to poll ArcticBase for a response.

## 7. Failure modes and operations

Most of this already exists from the dynamic-loading work:

- **Host down / port closed.** `connect()` fails, `load()` returns `ok=False` with an
  `error_kind`, and `/worker list` shows the endpoint and the error. The per-worker
  connect bound means one dark host does not delay the others; the cascade fix means it
  does not cancel their discovery either.
- **Link drops mid-dispatch.** Surfaces through the existing error path and is audited.
  A dropped link during a *hardware write* is the genuinely dangerous case, and is a
  reason the hardware worker's write tools must verify after writing rather than
  assuming success — that belongs to the hardware spec, but it is motivated here.
- **Latency.** Every call now crosses a network hop. Frida hook-event polling is the
  sensitive one; the capture layer already buffers worker-side, so the cost is per-poll
  rather than per-event.
- **Wrong bind address.** The most likely misconfiguration is a worker bound to
  loopback on the Pi while the daemon dials its tailnet address. It presents as a clean
  connection refusal with the endpoint in `last_error`, which is the right failure.

## 8. Testing

`agent_core` already has the fixtures: `streamable_http_stub.py` (a FastMCP worker
served with uvicorn) and `test_conformance_streamable_http.py`. New:

- `run_worker` serves stdio when told to and HTTP when told to, from one server object.
- `run_worker` **rejects `0.0.0.0`** — the security-relevant assertion in this design,
  and it must fail loudly rather than warn.
- The bind default is loopback when `PARE_WORKER_HOST` is unset.
- `WorkerSpec.request_timeout` reaches `streamablehttp_client`, and a slow worker
  produces a bounded failure rather than a hang.
- End to end: `WorkerManager.load()` against a real HTTP worker on loopback — load,
  list tools, dispatch, unload — extending `scripts/live_worker_lifecycle.py`, which
  currently covers only the stdio path.

## 9. Sequencing

1. **`agent_core`** — `request_timeout` plumbed through the client, `serve.py`, tests.
   Ships as v1.9.0; additive.
2. **The three existing workers** — adopt `run_worker`, declare the `agent_core`
   dependency. No behaviour change while they stay stdio.
3. **Move `frida` to the laptop** — the proof. It runs there over HTTP on the tailnet;
   `workers.yaml` gains the endpoint and `autoload: false`; the adb tunnel disappears.
   Verified by a real attach driven from the server.
4. **`pare-hardware-mcp`** then has a transport to be born into, and its own spec.

## 10. Risks

- **Frida over a network hop may be too slow** for high-frequency hook events. Unknown
  until measured. If it is a problem the answer is worker-side batching, which the
  capture layer already does — but this design does not assume that is enough, and step
  3 exists partly to find out.
- **The boundary decision ages.** §6 lists four triggers precisely because this is the
  kind of assumption that stays true until one day it quietly does not. The cheap
  re-entry path is documented so revisiting it is a small task rather than an argument.
- **`0.0.0.0` rejection will annoy someone at some point**, most likely while debugging
  a tunnel at a bench. That is the moment it is protecting against, so it stays — but
  it should fail with a message that names the tailnet address as the thing to bind
  instead, rather than just refusing.
