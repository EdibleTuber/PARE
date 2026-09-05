# Networked MCP workers — design

**Date:** 2026-09-05
**Status:** v3 — revised after a four-lens review panel; pending review
**Repos:** `agent_core`, a new `pare-worker-kit`, the three existing workers, `PARE`
**Follows:** [`2026-09-04-dynamic-worker-loading-design.md`](2026-09-04-dynamic-worker-loading-design.md)
**Related decision:** [`../2026-09-05-approval-channel-decision.md`](../2026-09-05-approval-channel-decision.md)
**Unblocks:** `pare-hardware-mcp` (a Tigard worker on a Raspberry Pi)

> **v3 changes.** A panel (fact-check, security, framework API, operations) reviewed v2.
> The security lens **endorsed** the no-auth decision, for a better reason than v2 gave,
> and found that two of v2's four "controls transfer intact" claims are false. Three
> code defects block implementation. One decision — putting the serving helper in
> `agent_core` — was wrong and is reversed. Several v2 claims about existing behaviour
> were inaccurate and are corrected in place. Every finding below was verified against
> running code, not inferred.

## 1. Why

PARE's daemon runs on a headless inference server. The hardware it must reach does not:

| Where | What lives there | Why it stays there |
|---|---|---|
| Inference server | PARE daemon, model, `static`, `mitm` | headless; the model is here |
| Laptop | rooted Android emulator | you need to see and touch it |
| Raspberry Pi | Tigard, wired to a target | it goes to the bench, and it has the screen |

Every worker today is `transport: stdio` — a subprocess the daemon spawns. **A daemon
cannot spawn a subprocess on another machine**, so the Pi worker is impossible as
designed, and the emulator is reachable only by tunnelling adb (unauthenticated over
TCP) or `frida-server` back to the server. Running each worker on the machine that owns
its hardware solves all of it with one mechanism.

PARE's side is already built for this: `WorkerSpec` accepts `transport:
streamable_http` with an `endpoint`, `MCPClient.from_spec` dispatches on it, and
`agent_core` has a live Streamable-HTTP conformance fixture. What is missing is
worker-side serving, three bug fixes, and an honest account of what a remote worker
changes.

## 2. Goals

1. A worker serves over Streamable HTTP or stdio, chosen at launch, without its tool
   code changing.
2. The trust boundary is stated explicitly and matches the rest of this ecosystem.
3. A worker on a small machine installs a small dependency set.
4. When a networked worker is unreachable, the operator can tell **which machine** and
   **why** from `/worker list`.
5. `loaded` means *connected*, not *was connected once*.
6. Moving `pare-frida-mcp` to the laptop is the proof.

## 3. Non-goals

- **Application-level authentication.** See §6.
- **TLS between daemon and worker.** The tailnet provides transport encryption.
- **Service discovery.** Endpoints are declared by hand.
- **A runtime-re-readable catalog.** Changing a transport requires a daemon restart (§5.5).
- **Making stdio workers remote.** stdio stays the default; `static` stays on the server.
- **Solving large-payload transfer.** Flash dumps are the hardware spec's problem, but
  §7.3 records why it cannot be assumed solved.

## 4. Decisions

**D1 — The worker chooses its transport at launch; the daemon learns it from
`workers.yaml`.** One binary serves either way.

**D2 — The network is the trust boundary.** No tokens, no TLS between daemon and
worker. The deployment sits on a Tailscale tailnet the operator controls, and
ArcticBase — already in this ecosystem, already holding approval documents — states the
same posture. §6 records what this costs and what would invalidate it.

**D3 — Bind to a named interface, never a wildcard.** With no application auth the bind
address is the access control. Checked with `ipaddress.ip_address(host).is_unspecified`,
**not** a string comparison against `"0.0.0.0"` — verified on this host, `'0'`, `'0x0'`,
`'00.0.0.0'`, `'::'` and `'::0'` all resolve to a wildcard bind, and
`net.ipv6.bindv6only=0` means `::` accepts IPv4 too. `PARE_WORKER_HOST` additionally
accepts an **interface name** (`tailscale0`), resolved at startup, because a container
or a freshly-booted Pi genuinely cannot name its address in a unit file. That removes
the only honest reason to want a wildcard, which is what makes the refusal defensible
rather than stubborn.

**D4 — Networked in-house workers stay `kind: internal`.** They advertise per-tool wire
tiers that escalate above the floor. `external_mcp` is for third-party workers.

**D5 — Destructive tools get operator pins, from day one.** The tier a worker
self-reports is what a tampered worker would misreport; pins are the only link not
under its control. **Currently unmet:** `workers.yaml` declares `hardware` with
`risk_default: medium` and *zero* pins. That must be fixed before the hardware worker
ships, not after.

**D6 (REVERSED from v2) — the serving helper goes in a new `pare-worker-kit`, not in
`agent_core`.** v2 argued the workers "already import `agent_core.workers.risk`" so the
dependency was free. Measured, it is not: that import loads **21 agent_core modules**,
including `MCPClientPool`, `WorkerManager`, `RiskAwareToolPool` and the daemon's shell
tool, to obtain one string. Declaring `agent_core` in each worker's `pyproject.toml`
would additionally install `trafilatura`, `markitdown[pdf,docx,pptx,xlsx]`, `rich` and
`prompt-toolkit` — on a Raspberry Pi, for a constant and a forty-line wrapper.

The direction was also backwards. `agent_core/workers/__init__.py` states its own
boundary as the *client* side: transport, enforcement, lifecycle. `run_worker` is the
*server* side. Putting it there means the machine being protected from the worker ships
the code the worker runs.

`pare-worker-kit` depends on `mcp` alone and exports `RISK_TIER_META_KEY` and
`run_worker`.

**Amended during implementation:** v3 said `agent_core` would *re-export* the
constant from the kit, "so the constant keeps one definition". That does not
survive contact with the deployment. `agent_core` declares its dependencies as
git URLs, so re-exporting would make the generic framework depend on a
PARE-named package and impose a publish ordering on every release — and more
importantly, the daemon and the Pi worker are separately installed packages on
**different machines** that never share a Python environment. A shared import
cannot guarantee agreement across that gap; version skew between two installed
packages is exactly as possible with a re-export as with two literals.

`RISK_TIER_META_KEY` is a **wire** constant, so both sides state it and a
**bidirectional guard test** enforces agreement: each package's suite asserts
the other's literal matches, skipping when the other is not installed. Neither
package depends on the other. Note the failure mode this guards is silent and
safe-directioned — if the strings drift, advertised tiers stop being read and
every tool resolves at its floor — which is precisely why it needs a test
rather than trust.

Note also that the three workers already imported `agent_core.workers.risk`
**without declaring `agent_core` as a dependency at all**; they worked only
because they shared PARE's venv. The split fixes an undeclared dependency as
well as a heavy one.

**D7 — Liveness is checked, not assumed.** `loaded` currently means "load succeeded and
nothing has told us otherwise" — there is no heartbeat anywhere in
`agent_core/workers/`, `is_loaded()` is pure bookkeeping, and `unavailable_reason()`
returns `None` whenever it is true. That was free under stdio, where the daemon owned
the process. It is not free now: a sleeping laptop leaves `/worker list` reporting
`loaded` with a stale tool count indefinitely. Networked workers get a periodic cheap
probe and a distinguishable `unreachable` state.

**D8 — The bench screen is a peer approval channel.** Recorded separately in
[`../2026-09-05-approval-channel-decision.md`](../2026-09-05-approval-channel-decision.md).
It binds D1 of the ArcticBase spec, not this one, but it is why that spec is a
prerequisite for the hardware worker rather than an enhancement.

## 5. Architecture

### 5.1 `agent_core` — three defects that block implementation

All three were found by driving the real code against an unreachable endpoint.

**(a) `MCPClient.close()` leaks on the HTTP failure path** — `client.py:110-116`. If
`_session.__aexit__` raises, `_transport_ctx.__aexit__` never runs. For HTTP,
`connect()` never touches the network (the client is lazy), so an unreachable endpoint
raises inside `initialize()`, and the `httpx.AsyncClient` is never closed; the transport
asyncgen is finalised later by the GC in an arbitrary task. Measured: three failed
loads, three leaked clients, each with a `RuntimeError: Attempted to exit cancel scope
in a different task` from GC finalisation.

Note the trap: that message is the signature of the owner-task bug the lifecycle work
fixed, and here it has a completely different cause. Anyone debugging it will look in
the wrong place.

This is exactly the path `autoload: false` makes routine. Fix is `try/finally` so the
transport context always exits, plus a regression test asserting no live
`httpx.AsyncClient` remains after a failed connect.

**(b) One timeout field cannot bound what v2 claimed.** Verified:
`streamablehttp_client` builds `httpx.Timeout(timeout, read=sse_read_timeout)`. So
`timeout` covers connect/write/pool while the **response read** is governed by
`sse_read_timeout`, default **300s**. v2's §8 assertion — "a slow worker produces a
bounded failure rather than a hang" — cannot pass as written.

`WorkerSpec` therefore gains **two** fields, not one:

```python
connect_timeout: float | None = None   # -> the pool's per-connect bound
read_timeout: float | None = None      # -> sse_read_timeout
```

Both are per-spec so a tailnet worker can have a longer bound than `static`. v2's single
`request_timeout: 30` example was also dead configuration: the pool's hard-coded
`DEFAULT_CONNECT_TIMEOUT = 10.0` fires first and the operator's 30 is silently ignored.
`WorkerManager._load_body` passes `spec.connect_timeout or self._connect_timeout`.

**Migration note:** `streamablehttp_client` is `@deprecated` in mcp 1.29.1; the
replacement `streamable_http_client` takes an `httpx.AsyncClient` instead of
`timeout`/`headers`/`auth`. Constructing the client in `MCPClient.connect()` and calling
the new entry point makes the timeout split, and any future header change, local and
version-stable — and should be done in the same pass rather than twice.

**(c) An unreachable worker reports `spawn_failed` and a memory address.** Measured
`last_error`:

```
ConnectionError: connecting to worker 'frida' failed
  (cancelled internally: CancelledError('Cancelled via cancel scope 0x7e671b0f3a70'))
```

The endpoint never appears; the real `httpcore.ConnectError` is buried in a collapsed
`BaseExceptionGroup`; and `manager.py:249-251` classifies anything without "version" in
its text as `spawn_failed` — for a worker that was never spawned. Fix: add
`ErrorKind = "unreachable"`, carry `self.endpoint` into the error message, and unwrap
`ExceptionGroup` to surface the innermost cause. `/worker list`'s error footer already
prints the full text, so a good message reaches the operator unchanged.

### 5.2 `pare-worker-kit` — a new, deliberately tiny package

Dependencies: `mcp` only.

```python
RISK_TIER_META_KEY = "agent_core/risk_tier"   # wire constant; guard-tested both ways

def run_worker(server, *, default_transport="stdio", env_prefix="AGENT_WORKER_") -> None:
    """Serve a FastMCP worker over stdio or Streamable HTTP.

        {prefix}TRANSPORT   stdio | http     (default: stdio)
        {prefix}HOST        interface name or address (default: 127.0.0.1)
        {prefix}PORT        port             (required when http)
    """
```

Four implementation details the review surfaced, each of which would otherwise be found
the hard way:

- **The transport strings differ.** `WorkerSpec.transport` is `streamable_http`
  (underscore); `FastMCP.run()` wants `streamable-http` (hyphen). `run_worker` must
  translate or raise `ValueError: Unknown transport`.
- **There are two different `FastMCP` classes.** The workers use
  `mcp.server.fastmcp.FastMCP`, whose `run()` takes host/port only from the constructor
  — so `run_worker` must set `server.settings.host`/`.port` before calling it. The
  conformance fixture uses the standalone `fastmcp` package, whose `run()` accepts them
  as kwargs. Branch deliberately; do not let fixture and production diverge.
- **Port has no safe default.** v2 defaulted to 9100 while its own example used 9101.
  Two workers on one laptop would both take 9100 and the second dies inside uvicorn, not
  with a `run_worker` message. Port is required in http mode.
- **DNS-rebinding protection is off by default.** The MCP SDK ships
  `TransportSecurityMiddleware` and disables it when no settings are passed, which is
  what FastMCP does. `run_worker` is already computing host and port, so passing
  `TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=[...])`
  is one line. Defence in depth rather than a hole being patched — and exactly the kind
  of posture D6's shared-helper argument exists to standardise.

### 5.3 `PARE` — declaration

```yaml
  frida:
    endpoint: http://100.x.y.z:9101/mcp     # laptop's tailnet address
    transport: streamable_http
    connect_timeout: 20
    read_timeout: 60
    risk_default: low
    autoload: false        # REQUIRED for networked workers, see below
    capability_tags: [mobile, dynamic, android, frida]
```

`autoload: false` is **required**, not ergonomic, until the connect bound is per-spec
everywhere: a sleeping laptop does not refuse a connection, it silently drops the SYN,
so every autoloading networked worker costs its full connect timeout at every daemon
boot.

### 5.4 What v2 said does not change, and does

v2's §5.5 claimed the audit log is unaffected. It is not:

- **`_artifact_args` is stdio-gated** (`manager.py:44-50`). For an HTTP worker the
  `worker_loaded` row records `resolved_command`, `command_mtime` and `command_size` as
  `None`. The artifact-swap attestation — the dynamic-loading branch's own stated
  central threat — silently does not exist for exactly the workers this design creates.
  Partial fix, cheap: `_own` already calls `initialize()` and discards the result;
  capture `serverInfo.name`/`.version` and record them. Not equivalent to an mtime, but
  it turns nothing into a value that changes when the remote build changes.
- **`worker_contract_version=1` is hardcoded** into every audit row
  (`risk_pool.py:257,471`) rather than read from the connected worker, so version skew
  is invisible at connect time *and* unrecoverable afterwards. The same
  `initialize()` result should populate it.
- **v2's "every call audited before and after" overstates it.** There is one terminal
  audit row per call, whatever the outcome, including cancellation. The guarantee — no
  silent unaudited dispatch — holds; the mechanism is one row, not two.

### 5.5 Switching a worker between transports

**At launch: supported.** **At runtime: no, deliberately.** The catalog is fixed at
boot. Re-reading `workers.yaml` would flow new specs into the pool while `RiskGate`'s
operator pins stayed frozen at boot values — an edited `risk_default` would take effect
while an added *pin* silently would not. That is a fail-open on the trust anchor, and
`registry.py`'s own module docstring documents the live-registry behaviour that makes it
so. Change transport, restart the daemon.

**Declaring the same worker twice is not a workaround.** The worker name is the tool
prefix, so `frida_laptop` yields `frida_laptop_java_hook`, breaking
`pare/handback.py`'s hardcoded `frida_*` constants and showing the model two competing
frida toolsets.

### 5.6 `/worker reload` means something different for HTTP — and the copy is now wrong

For stdio, reload re-execs the worker; that is the worker-development loop it is
described as. For HTTP it is a client-side disconnect and reconnect that does **not**
restart the remote process — code changes on the laptop still need a manual restart
there.

The security consequences survive: `remove_spec`/`add_spec` still bump the generation,
so approvals still evict and the high-water mark still holds. But two operator-facing
strings become false and both are safety-relevant, because they claim state was
destroyed when it was not:

- `/worker unload`'s "any live attachments, sessions and installed hooks for this worker
  are gone" — false for HTTP; the remote frida session survives the disconnect.
- `disconnect_timeout`'s "its process may still be running" — sends the operator hunting
  for a process on the wrong machine.

Both must become transport-aware.

## 6. The trust boundary

**The boundary is the tailnet and the operator's LAN.** Anything that can route to a
worker's port can call any tool it exposes. No application authentication, no
daemon-to-worker TLS beyond what the tailnet provides. This matches ArcticBase, which
holds approval documents under the same assumption.

### Why this holds, more precisely than v2 argued

The security review's endorsement rests on something v2 did not say: **the risk gate
never derived authority from the worker.** `resolve_declared_tier` takes the worker's
word only to *escalate*; `RiskGate.evaluate` is override-up only; the floor and the pins
come from `workers.yaml`, which the worker cannot touch. A tampered worker and a
wire-tampering attacker are the same adversary to that code, and moving the wire from a
pipe to a socket does not change it.

### Two of v2's four "controls transfer intact" claims were false

**Generation-keyed approvals are a property of local process ownership.** `_bump` has
exactly three call sites, all driven by `WorkerManager` — the *daemon* acting. Under
stdio the kernel enforces the invariant: the daemon spawned the child and holds its
pipe, so the process cannot be replaced without a reload. Over HTTP a Pi can reboot or a
worker be restarted by hand, and the daemon never learns. A `scope: session` approval
then survives onto a **different process**, dispatching with no prompt and an audit row
reading `hitl_approved / session-approved`.

*Required:* bump the generation on any transport error for non-stdio workers — fail
closed on link loss; record `serverInfo` at load and treat a change as a generation
boundary; and forbid FastMCP's `stateless_http`, which removes the session ids that are
currently the only thing surfacing a swap.

**The audit log stops being complete** — a third threat class, neither "unauthorised
caller" nor "untrustworthy worker". Under stdio, PARE's log was a total record of
everything that worker did, because PARE's pipe was its only input. Over HTTP it records
only what *this daemon* dispatched. After a bricked target, the operator cannot
establish whether PARE did it — the question an audit log exists to answer.

*Required:* worker-side request logging in `run_worker` (peer address, tool, timestamp,
argument hash). Cheap, worker-local, restores the property.

### One threat that is new because the worker is remote

Tool **names, descriptions and schemas** cross the wire and land in the model's context.
Whoever holds a worker's port does not merely gain the tools that worker exposes — they
gain an authoring channel into an agent that also holds `frida_execute_script` (pinned
`critical`) and `mitm_inject_request` on workers they cannot reach. That is lateral
movement via the model, and under stdio it did not exist, because descriptions came from
a binary the daemon resolved and stat'd.

Pinning worker identity across generations (above) is the same mechanism that addresses
this.

### What this costs, and what would make it wrong

Anything on the tailnet can drive the hardware worker, read tool arguments and results
in transit within a node, and there is no second layer. Revisit if: a **worker port**
becomes reachable beyond the tailnet (`tailscale serve`/`funnel`, an exit node, subnet
routing, UPnP on a bench router — note v2 named the daemon here, which is the *safest*
node); a worker is left bound to a routable address after debugging; another person or
device joins the tailnet; a tool gains the ability to damage something irreplaceable; or
a second daemon starts sharing a worker, since `_session_approved` and `_tier_highwater`
are per-process and a later daemon starts with an empty high-water table.

Note also that the exfiltration surface is broader than v2's "flash contents": `mitm`
holds intercepted session cookies and bearer tokens, `frida` reads process memory.

**The cheaper control that fits this deployment better than tokens:** Tailscale ACLs
restricting worker ports to the daemon node's tag, plus a startup check that Funnel is
off. No token management, and it makes D2 rest on something enforced rather than on a
bind string.

## 7. Operations

### 7.1 Who starts the workers

Nothing in v2 answered this. Each networked worker needs a systemd unit on its host
(`Restart=on-failure`, `After=tailscaled.service`), and the Pi's needs to survive a
power cycle. The unit is part of this design's deliverable, not an exercise for the
operator.

### 7.2 The `POLL_TOOLS` trap

The sharpest finding in the review, and it is invisible from either side alone.

`pare/handback.py:18` puts `frida_read_hook_events` in `POLL_TOOLS`, and
`pare/agent.py:412` reads `if tc.name not in POLL_TOOLS and guard.tripped(...)` — a
deliberate, correct exemption of that tool from the spin handback, because polling is
supposed to repeat. Meanwhile `client_pool.py:440` awaits `client.call_tool()` with no
bound, and `ClientSession` is constructed without `read_timeout_seconds` (verified:
default `None`), so the only backstop is the SDK's 300s `sse_read_timeout`.

So the one tool the system prompt tells the model to hammer becomes the one where a
degraded link is invisible for up to five minutes per call, **and** the one tool whose
handback is switched off. Slow polling becomes a silently wedged session.

*Required before frida moves:* the `read_timeout` field from §5.1(b), set low for this
worker, **and** a handback trigger that fires on N consecutive timeouts or errors on a
`POLL_TOOL` — distinct from N consecutive empty-but-successful polls, which are normal.

### 7.3 Large payloads are not solved, and must not be assumed solved

v2 said "the capture layer already buffers worker-side". **That is wrong.**
`CaptureLayer` runs *daemon-side* and substitutes a stub for oversized results
(`maybe_substitute`, called after the result has already crossed the network). The
worker-side buffering that makes frida polling cheap is `pare-frida-mcp`'s own
`SessionManager.read_events()` (`since_seq`/`limit`/`buffered_remaining`). Right
conclusion, wrong component — an implementer tuning latency would read the wrong file.

For the hardware worker this matters: a multi-megabyte flash dump is one
`CallToolResult` over one HTTP response, and no chunking or streaming story exists.
Suggestively, `pare-frida-mcp/config.py` defines `capture_dir`, `blob_threshold` and
`max_disk_per_session` that are **read nowhere** — someone anticipated this and stopped.
The hardware spec must decide whether large artifacts spool worker-side (and how the
operator reaches them, given the daemon's capture store is on the server) or are chunked
over MCP. This spec's job is only to refuse to let that be assumed.

### 7.4 Diagnosis

Beyond §5.1(c): `WorkerStatus` carries `transport` but not `endpoint`, so for a healthy
networked worker there is no way to see which host you are talking to without reading
`workers.yaml`. And `/health` — the command an operator reflexively types first — is
strictly less informative than `/worker list`: no `last_error`, no transport, no
endpoint. Both are small additions and both matter more once three machines are
involved.

### 7.5 The bench workflow

`pare-cli` talks over a **Unix domain socket** — local IPC only. The operator at the Pi
cannot talk to PARE from the Pi; they SSH into the inference server and run `pare-cli`
there. The Pi hosts a *worker* and (per D8) an *approval surface*; it is not where the
conversation happens. v2's deployment table implied otherwise and should not.

## 8. Testing

- `run_worker` serves stdio and http from one server object; **rejects any wildcard
  bind** via `is_unspecified`, including `::` and `0`; resolves an interface name.
- After a **failed** HTTP connect, no live `httpx.AsyncClient` remains and
  `_transport_ctx is None` — §5.1(a)'s regression test.
- `read_timeout` genuinely bounds a slow tool call; `connect_timeout` genuinely bounds a
  dial to a black hole. Two separate tests, because they are two parameters.
- An unreachable endpoint yields `error_kind="unreachable"` and a `last_error`
  containing the endpoint.
- A generation bump occurs on transport error for a non-stdio worker.
- Liveness: a worker whose host vanishes transitions out of `loaded` without a dispatch.
- End to end against a real HTTP worker on loopback. **Note:**
  `scripts/live_worker_lifecycle.py` lives in **PARE**, not `agent_core`, and asserts on
  `_owner_pid` and process reaping — meaningless for HTTP. Its extension is step 3 work
  and needs transport-branched assertions, not an appended case.

## 9. Sequencing

1. **`agent_core` bug fixes** — §5.1(a) close leak, (b) timeout split and the
   `streamable_http_client` migration, (c) `unreachable` error kind. Plus the §6
   generation-on-transport-error rule, `serverInfo` capture, and the §5.4 audit
   corrections. Ships as v1.9.0.
2. **`pare-worker-kit`** — new package; bidirectional guard test on the constant.
3. **The three existing workers** — adopt `run_worker`, depend on the kit rather than
   `agent_core`. No behaviour change while they stay stdio.
4. **Liveness (D7) and the operator-facing corrections** — §5.6 transport-aware copy,
   §7.4 endpoint column, §7.2's poll-tool handback trigger.
5. **Move `frida` to the laptop** — the proof, with a systemd unit. Verified by a real
   attach driven from the server, and by measuring whether hook-event polling over the
   tailnet is usable.
6. **`pare-hardware-mcp`** then has a transport, a liveness story, and a resolved
   large-payload question to be born into.

## 10. Risks

- **Frida over a network hop may be too slow** for hook-event polling. Genuinely
  unknown; step 5 measures it. §7.2 is what stops "too slow" becoming "silently stuck".
- **The boundary decision ages.** §6 lists the triggers because this is an assumption
  that stays true until it quietly does not.
- **A new package is a new thing to version.** `pare-worker-kit` is justified by the Pi's
  install weight, but it adds a release to keep in step, and the guard test only
  fires in an environment where both packages are installed — which the Pi is
  not. CI must run at least one job with both.
- **Wildcard rejection will annoy someone at a bench.** That is the moment it protects
  against. The interface-name path (D3) is what keeps it from being merely obstructive.
