# Dynamic worker loading — design

**Date:** 2026-09-04
**Status:** approved, pending implementation
**Repos:** `agent_core` (v1.8.0), `PARE` (follow-on wiring)

## 1. Context

PARE reaches its analysis tools through MCP workers declared in `workers.yaml`.
That registry is read exactly once, at daemon boot, and the resulting tool set is
frozen for the life of the process:

```
workers.yaml
  → WorkerRegistry.load()            pare/agent.py:120
  → MCPClientPool(specs)             pare/agent.py:123
  → RiskAwareToolPool(specs={...})   pare/agent.py:134
  → register_tools()                 pare/agent.py:150
      discover_and_register in a throwaway asyncio.run loop, then close_all()
  → _attach_registries               agent_core/runtime.py:44
      ToolExecutor.build() instantiates every class into a dict
```

Adding, removing, or restarting a worker requires restarting the daemon, which
discards the session: live Frida attachments, installed Java hooks, the warm
conversation, and the operator's place in an investigation.

Three workers are declared today — `frida`, `static`, `mitm`, all stdio, all
in-house. Eight `apk_re_agents` Streamable-HTTP workers are commented out
(`workers.yaml:12`) partly because an unreachable HTTP worker's failed discovery
was observed to cancel its siblings'. `../pare-hardware-mcp` exists as an empty
directory — the next worker, and the immediate motivation for this work.

### What already works in our favour

- `ToolExecutor._tools` is a plain mutable dict (`agent_core/tools/executor.py:24`).
- `handle_chat` calls `self.tool_executor.schemas()` **fresh every turn**
  (`pare/agent.py:238`), so a mutated registry reaches the model on the next
  message with no restart and no client reconnect.
- `MCPClientPool` already lazy-connects per worker and caches per name.
- `make_tool_class` (`agent_core/workers/tool_factory.py:26`) already synthesizes
  `Tool` subclasses from a live `list_tools` result. That code is already runtime
  code; it simply only ever runs once.

### What blocks it

- `MCPClientPool._specs` and `RiskAwareToolPool._specs` are private dicts built
  once, with no add/remove.
- `RiskGate` overrides are baked at construction from the YAML.
- Teardown is all-or-nothing: `close_all()` only, no per-worker disconnect, and
  nothing unregisters tools or evicts a worker's `_tool_tiers` /
  `_session_approved` entries.
- Boot discovery runs in a throwaway `asyncio.run` loop and must `close_all()`
  afterwards, because stdio streams are bound to the loop that opened them
  (pinned by `tests/test_register_tools_reconnect.py`).

## 2. Goals

1. Load, unload, and reload a declared worker at runtime, from the operator's CLI,
   without restarting the daemon or losing session state.
2. Reclaim context budget: an unloaded worker's tool schemas leave the per-turn
   request.
3. Keep every existing security property — operator-reviewed risk floors,
   HITL gating, audit — intact across load/unload cycles.
4. Leave the groundwork for model-initiated, operator-approved loading without
   building it.

## 3. Non-goals

Deliberately out of scope, recorded so the implementation plan does not grow them:

- **Auto-load on capability need.** Silent capability expansion in a tool that
  hooks processes and reads memory is the wrong default.
- **Model-initiated loading.** Groundwork only (§11).
- **Per-channel tool views** and the connection refcounting they imply. The
  loaded set is daemon-wide (§4, D4).
- **In-flight call tracking** across an unload.
- **A worker "are you busy?" contract addition.** Unload is blunt.
- **Auditing lifecycle actions** as `AuditEntry` records (§7).

## 4. Decisions

**D1 — Operator-initiated only.** A `/worker` command drives load/unload/reload.
The model never changes its own toolset. Model-initiated loading is a real
capability-escalation surface and is deferred to a later phase, designed so that
phase is additive.

**D2 — `workers.yaml` is the catalog and the trust anchor.** Every worker is
declared there, whether or not it is connected at boot; a new `autoload` field
decides. Nothing is loadable that the operator has not declared and assigned a
`risk_default` to, ahead of time, in a reviewed file.

*Rejected:* filesystem/entry-point discovery of installed `pare-*-mcp` scripts.
It removes the config step but means a newly installed worker arrives with no
operator-set risk floor and no pins, leaving the tier to come from the worker's
own wire metadata — trusting an artifact that was never reviewed.

*Rejected:* ad-hoc `/worker load <command>`. An arbitrary-subprocess-launch
primitive inside the agent. `/worker reload <name>` covers the worker-development
loop by re-execing an already-declared worker.

**D3 — The lifecycle lives in `agent_core`.** Almost everything that must become
mutable is framework-owned: `client_pool.py`, `risk_pool.py`, `risk.py`,
`types.py`, `tools/executor.py`. More importantly, the invariants at stake —
evicting session approvals so a reloaded worker cannot inherit them, keeping a
vanished worker from corrupting dispatch — are exactly what `RiskAwareToolPool`
exists to hold. If PARE reached around it, that property would become PARE's
problem, and every other agent on the framework would re-solve it.

**D4 — Daemon-wide, not per-channel.** `tool_executor` is a single instance on
the agent, shared by every channel, matching `mcp_pool` and `tool_pool`. The
operator runs one `pare-cli` at a time in practice. This is a YAGNI call, not a
correctness one: if ArcticBase brings concurrent per-project sessions,
`ToolExecutor.schemas()` is the single choke point that would need a channel
dimension, and all mutation already flows through one place.

**D5 — Boot goes through the same `load()` path.** `agent_core` gains an async
startup hook; `Daemon.serve()` awaits `agent.astartup()` before it binds, and
`WorkerManager.load_autoload()` runs there. Discovery then happens in the serving
loop — the loop that will actually use the connections — which is precisely what
the throwaway-loop workaround exists to work around. The hack and its pinning
test are **deleted**, not preserved. One code path for boot and runtime means the
two cannot drift.

**D6 — Unload is blunt.** `unload <name>` drops the worker's tools from the
schema **and** disconnects the client (for stdio, ending the subprocess). One
meaning, no modes, no `--force`, no refuse-if-busy. The consequence is
documented, not guarded against: unloading `frida` drops any live attachment and
its installed hooks; `/worker load frida` yields a fresh process to re-attach
from.

## 5. Architecture

New component: `agent_core/workers/manager.py` → `WorkerManager`. It owns the
worker lifecycle and is the only thing that mutates the three registries that
currently freeze at boot.

```
WorkerManager
  ├─ WorkerRegistry     the catalog — every declared worker, loaded or not
  ├─ RiskAwareToolPool  → MCPClientPool: connections, tiers, approvals
  └─ ToolExecutor       what the model can see

  load(name)    catalog → connect → list_tools → synthesize Tools → executor
  unload(name)  executor ⊖ worker's tools → disconnect → evict tiers + approvals
  reload(name)  unload ∘ load
  status()      per-worker: loaded?, tool count, tags, transport, last error
```

PARE's `/worker` command is a thin operator surface over those four methods.
Nothing in PARE touches `agent_core` internals.

### Naming

The `workers.yaml` mapping key **is** `WorkerSpec.name` — `registry.py:44` does
`WorkerSpec(name=name, **fields)` — and that same name is the tool prefix in
`make_tool_class` (`f"{worker.name}_{tool_name}"`). One namespace, no second
registry to drift:

```
workers.yaml key  →  WorkerSpec.name  →  tool prefix      →  /worker arg
  frida:          →  "frida"          →  frida_attach     →  /worker load frida
  static:         →  "static"         →  static_load_apk  →  /worker load static
```

Uniqueness is enforced by YAML itself; `WorkerSpec.name_is_valid_identifier`
(`types.py`) already rejects MCP-unsafe names. `/mitm` the command and `mitm` the
worker do not collide — `CommandRegistry` and `ToolExecutor` are separate
registries.

## 6. `agent_core` changes

### 6.1 `WorkerSpec` (`workers/types.py`)

```python
autoload: bool = True   # default preserves today's behavior for every existing entry
```

### 6.2 Tool provenance (`workers/tool_factory.py`)

```python
_DynamicTool.worker = worker.name
```

One line, and it is what makes unload safe. Without it, "remove this worker's
tools" means string-matching the `{worker}_` prefix, which would happily remove a
declarative tool that shares the prefix. With it, removal is
`getattr(t, "worker", None) == name`; builtins and `cls.tools` entries have no
`worker` attribute and are structurally unremovable.

### 6.3 `MCPClientPool` (`workers/client_pool.py`)

```python
add_spec(spec)          # idempotent
remove_spec(name)       # disconnects if connected
async disconnect(name)  # per-worker; close_all() stays for shutdown
is_connected(name) -> bool
```

`_connect_lock` (line 26, held across `connect()` + `initialize()` at line 30)
becomes a per-worker `defaultdict(asyncio.Lock)`. Today one global lock serializes
every first-connect, so a worker that hangs on connect blocks every other
worker's first use.

### 6.4 `RiskAwareToolPool` (`workers/risk_pool.py`)

```python
add_spec(spec)     # → inner; evict stale approvals for this worker (see §7)
remove_spec(name)  # → inner, then evict:
                   #   _tool_tiers:       drop all (worker, *) keys
                   #   _session_approved: drop all (worker, *) keys
```

Eviction is the security-relevant behaviour: a reloaded worker starts with no
advertised tiers (so `resolve_declared_tier` applies its floor until the worker
re-advertises) and no session approvals.

### 6.5 `ToolExecutor` (`tools/executor.py`)

```python
add(tool_cls, agent)        # same `requires` validation build() runs
remove(name) -> bool
remove_worker(name) -> int  # count removed, by provenance
has(name) -> bool
```

`add` takes `agent` so the `requires` check is identical to boot; the alternative
is a back-pointer on the executor it does not otherwise need.

`schemas()` must emit in a **stable order**: builtins, then declarative
`cls.tools`, then worker tools sorted by `(worker, name)`. Dict insertion order
means load/unload would otherwise reshuffle the tool block between turns, which
invalidates the prompt-cache prefix on a local inference server every turn after
a load.

`add` **refuses** to overwrite an existing name, surfacing a load error naming
both tools (see §8.2).

### 6.6 `WorkerManager` (`workers/manager.py`, new)

```python
async load(name)      -> LoadResult(name, ok, tool_count, tools, error)
async unload(name)    -> UnloadResult(name, ok, tools_removed, was_connected, error)
async reload(name)    -> LoadResult
async load_autoload() -> list[LoadResult]   # boot path, called from astartup()
status()              -> list[WorkerStatus] # name, loaded, tool_count, transport,
                                            # risk_default, capability_tags, last_error
require(name)         -> str | None         # operator/model-facing message if not loaded
```

Results are returned, never raised — for the same reason `discover_and_register`
logs-and-continues today: a failed load must not take down the daemon or the
operator's turn. `last_error` is retained so `/worker list` and `/health` can show
*why* a worker is not loaded. Today a worker that fails discovery is silently
absent.

### 6.7 `Agent.astartup()` (`agent.py`, `daemon.py`)

```python
async def astartup(self) -> None: ...   # default no-op
```

Awaited by `Daemon.serve()` before it binds. Agents that do not override it are
unaffected — the backward-compatibility guarantee for anything else on the
framework.

## 7. Error handling and edge cases

**Load is transactional.** The pool keys on spec, so the spec must be added before
`list_tools` can run:

```
add_spec → connect → initialize → list_tools → synthesize → executor.add(*)
        └── on any failure: disconnect, remove_spec, record last_error, ok=False
```

Failure modes all land in `LoadResult.error`: `FileNotFoundError` from the stdio
spawn (`client.py:102`), connection refused for `streamable_http`, protocol
mismatch at `initialize`, `list_tools` timeout. None raise into the operator's
turn.

All-or-nothing on the executor add is cheap: `make_tool_class` never sets
`requires`, and `Tool.requires` defaults to `()` (`tools/base.py:35`), so the
`requires` validation cannot realistically fail for a worker tool. Rollback exists
for tidiness, not because it will fire.

**Session-approval race.** `risk_pool.py:199-200` adds `(worker, tool)` to
`_session_approved` *after* the operator's approval future resolves. Unload evicts
that set, but a call already parked in `_await_operator` re-adds its entry when the
operator answers:

```
model calls frida_execute_script  → parks on approval future
operator: /worker unload frida    → evicts _session_approved
operator answers the prompt "a"   → risk_pool.py:200 re-adds ("frida","execute_script")
operator: /worker load frida      → fresh binary, silently pre-approved
```

Fix: evict on **both** unload and load, so "a freshly loaded worker has no
standing approvals" holds regardless of ordering.

**In-flight calls during unload.** Unload removes from the executor first, then
disconnects, with the disconnect wrapped in `try/except`, so even a messy teardown
leaves a consistent view. A call already in flight dies and surfaces through the
existing containment at `risk_pool.py:209`.

**Idempotency.** `load` on a loaded worker and `unload` on an unloaded one both
succeed with an informative message. Either on a name absent from the catalog is an
error naming the valid ones.

**Captured evidence survives.** Results from an unloaded worker remain in the
project capture store, reachable via `search_capture` / `read_capture`. Unloading
drops the *tools*, not the findings — the property that makes context-budget
unloading safe.

**Lifecycle auditing is deferred.** `AuditEntry` (`workers/types.py`) is shaped
around a tool dispatch (tool, args, declared/effective tier); a load/unload does
not fit it without distortion. For v1 these go to `logger.info`. A
model-requested load must be audited properly — an `AuditEntry` variant, designed
when that case exists (§11).

## 8. Defects found during design

### 8.1 Dispatch to an unloaded worker costs a pointless approval round-trip

Several PARE commands bypass the executor and call the pool by hard-coded worker
name: `pare/commands/_frida.py:30` (`WORKER = "frida"`, the chokepoint for
`/devices /ps /apps /sessions /select /attach /detach`) and
`pare/commands/mitm.py:73` (`"mitm"`, `capture_health`).

The failure is **contained** — `risk_pool.py:208` wraps the dispatch in
`except Exception` and returns an `_ErrorResult`, and `_frida.py:31` checks
`isError` — so there is no unhandled exception. But `risk.py:63-64` returns
`("high", "unknown_worker")` when the spec is absent, and `risk_pool.py:127` gates
`high` on operator approval. So after an unload:

1. `/devices` → `tool_pool.call_tool("frida", "list_devices", ...)`
2. spec is `None` → tier `high` → **operator gets an approval prompt**
3. operator approves
4. `_ensure_connected` raises `KeyError: no worker named 'frida' in this pool`
5. caught at `risk_pool.py:209` → `list_devices call failed`
6. an audit entry is written for a dispatch to a worker that does not exist

The code path is pre-existing — reachable today by commenting a worker out of
`workers.yaml` while the hard-coded name remains — but is never hit in practice
because all three commanded workers are always declared. Unload makes it routine.

**Fix:** `manager.require(name)` short-circuits before dispatch. `_frida.call`
checks it once and returns its existing error-shaped dict, so all seven fast-path
commands render the message through their existing `data.get("error")` branch with
no per-command changes; `/mitm status` checks the same way. The executor tombstone
(§9) is the same guard worded for the model.

**Related:** `mitm` stays `autoload: true`. Commit `e9edf9f` deliberately made it
unconditional; `hardware` is the only pre-declared unloaded entry.

### 8.2 A worker tool can silently shadow a declarative tool

`ToolExecutor.build` does `instances[tool_cls.name] = tool_cls()`
(`tools/executor.py:44`), last-write-wins and silent, and `_attach_registries`
orders `declared + dynamic` (`runtime.py:48`) — so a worker tool **overwrites** a
declarative one with no error and no log.

PARE has a live near-miss: `StaticAnalyze.name = "static_analyze"`
(`pare/tools/static_analyze.py:20`) while the `static` worker prefixes its tools
`static_*`. If `pare-static-mcp` ever ships a tool named `analyze`, it silently
replaces `StaticAnalyze`. It does not today — its `TOOL_SPECS` are `load_apk`,
`find_symbol`, `grep_smali`, `list_methods`, `decompile_method`, `callers_of`,
`extract_strings`, `paths_between`, `reachable_sinks` — so this is latent.

**Fix:** `ToolExecutor.add` refuses to overwrite, surfacing a load error naming
both. Converts a silent shadow into a legible failure at both boot and runtime.

### 8.3 Discovery cascade — unverified, verify before fixing

`workers.yaml:12` records that an unreachable HTTP worker's "botched cancellation"
cancelled sibling workers' discovery, including `frida`. **This diagnosis has not
been verified against the code.** What has been confirmed is only that
`client_pool.py:26` uses a single global `_connect_lock` held across `connect()` +
`initialize()` (line 30), and that `discovery.py:44` wraps each `list_tools` in a
2s `wait_for`. That is consistent with the symptom but is not proof of the
mechanism.

The plan treats this as a **verification step, not a fix**: a test loading one
reachable stdio stub alongside one `streamable_http` spec pointed at a closed
port, in either order, asserting the reachable one registers regardless. If it
passes before the per-worker lock change, the `workers.yaml:12` comment describes
something else, and that is a finding to record rather than a failing test to
chase.

### 8.4 Stale `command:` paths (config, not code)

`workers.yaml` points at `/home/edible/Projects/PARE/.venv/bin/...`, which does not
exist on this machine (the repo is at `/mnt/secondary/projects/PARE`). Today that
is a silently-skipped worker. After this change, `/worker load frida` returns
`FileNotFoundError` in `last_error` and `/worker list` shows it. Fix the paths as
part of the PARE-side work, tracked separately from the feature.

## 9. PARE changes

**`pare/commands/worker.py`** — one command, four subcommands, shaped like `/mitm`
(plain-text `ResponseMessage`, no new protocol messages):

```
/worker                → list
/worker list
/worker load <name>
/worker unload <name>
/worker reload <name>
```

`/worker list` is the catalog view — the thing model-initiated loading eventually
reads:

```
  frida      loaded     14 tools   stdio   low       mobile, dynamic, android, frida
  static     loaded      7 tools   stdio   low       static, apk, android
  mitm       loaded      4 tools   stdio   low       traffic, https, mitm, dynamic
  hardware   unloaded    –         stdio   medium    hardware, uart, jtag
```

with a `last_error` column when a load failed.

**`workers.yaml`** — `autoload:` on each entry (`True` by default, so an unchanged
file behaves exactly as today), plus a pre-declared `hardware` entry with
`autoload: false`.

**Tombstone on unload.** When the model calls a tool from an unloaded worker,
`ToolExecutor.run` returns the generic `Unknown tool: frida_attach`
(`tools/executor.py:50`). Since the conversation history still contains successful
`frida_*` calls from before the unload, that is a plausible mistake for a local
model, and the generic message gives it nothing to act on. The manager leaves a
tombstone so the executor can answer:

> `worker 'frida' is not loaded — its tools are unavailable this session. Ask the operator to run /worker load frida.`

It turns a dead end into a legible handback, and it is precisely the affordance
model-initiated loading needs (§11).

**`pare/agent.py`** — `setup()` constructs the `WorkerManager`. `register_tools()`
no longer discovers: it returns `[StaticAnalyze]` when
`config.enable_apk_re_agents` is set and `[]` otherwise, losing the
`asyncio.run` / `close_all()` block entirely (D5). `astartup()` calls
`manager.load_autoload()` and logs a one-line summary.

**`pare/prompts/system.md`** — a short paragraph: the available toolset can change
mid-session; a tool vanishing is an operator decision, not a failure; if a needed
capability is not loaded, say so rather than working around it. The last clause
matters — without it, a model that loses `static_*` mid-investigation will try to
reconstruct the answer from `frida` instead of saying what it needs.

**`/health`** — add `workers: frida(14), static(7) · unloaded: hardware`.

## 10. Testing strategy

`agent_core` already has the fixture that makes this testable for real rather than
with mocks: `tests/workers/conftest.py:12` exposes a `stdio_stub_spec(name, tier)`
factory building a `WorkerSpec` that launches
`tests/workers/fixtures/stdio_stub.py` as an actual subprocess via
`sys.executable`. Load/unload/reload can be exercised end-to-end — real spawn,
real MCP handshake, real teardown — against two toy tools (`noop_low`,
`risky_high`) that already advertise wire risk tiers.

### `agent_core` — new `tests/workers/test_manager.py`

| Test | Asserts |
|---|---|
| `load` registers | N tools appear in a real `ToolExecutor`, named `{worker}_{tool}` |
| `unload` removes only its own | worker's tools gone; builtins and declarative tools untouched, via the `worker` provenance attribute, not prefix matching |
| `unload` disconnects | `pool.is_connected(name)` is False; subprocess reaped |
| `reload` is a fresh process | new pid / new session; tools re-registered |
| failed load leaves no residue | bad `command` → `ok=False`, `last_error` set, name absent from pool specs, zero tools added |
| collision refused | stub tool colliding with a pre-registered name → load fails naming both; existing tool unchanged |
| stable schema order | `schemas()` prefix byte-identical across an unrelated load/unload |

**The approval race gets its own test** against `RiskAwareToolPool`, replaying §7's
sequence: approve → unload → let the parked future resolve → load → assert
`("frida","execute_script") not in _session_approved`. This is the one most worth
pinning, because it is silent when it regresses.

**The cascade gets a verification test, not a fix test** — see §8.3.

**`astartup`:** `Daemon.serve()` awaits it before binding; an agent not overriding
it is unaffected.

**Deletion, with cause:** `tests/test_register_tools_reconnect.py` is removed. It
exists solely to pin `close_all()` after boot discovery, and its own docstring
explains why — discovery ran in a throwaway loop. Under D5 that loop is gone, so
the test pins a workaround that no longer exists. It is replaced by a test
asserting worker connections are opened during `astartup`, i.e. in the serving
loop, which is the property the old test protected by proxy.

### PARE — new `tests/test_worker_command.py`

`/worker list` shape with mixed loaded/unloaded and a `last_error` row;
`load`/`unload`/`reload` happy paths; unknown name; idempotent repeats. Plus a
`require()` test for `_frida.call` and `/mitm status` asserting the short-circuit
message **and that no approval prompt is emitted** — the §8.1 fix, where the
absence of the prompt is the real assertion.

`tests/test_commands_metadata.py` parametrizes over `PareAgent.commands` (line 18),
so adding `Worker` to that list is covered automatically.

### Worker-side churn: none

No wire fields change, no conformance changes. `pare-frida-mcp`,
`pare-static-mcp` and `pare-mitm-mcp` need no edits; `pare-hardware-mcp` only has
to be a normal worker.

## 11. Groundwork for model-initiated loading

Everything a later model-initiated phase needs is produced here as a by-product:

| It needs | This design gives |
|---|---|
| a catalog of unloaded workers | `manager.status()` — name, tags, transport, `risk_default`, `last_error` |
| a selection signal | `capability_tags` in `workers.yaml`, currently read by nothing |
| a request trigger | the unloaded-worker tombstone (§9) |
| an approval hop | `ToolApprovalRegistry` + `ToolApprovalRequestMessage`, already driving `risk_pool.py:_await_operator` |
| a pre-reviewed risk floor | `risk_default`, set by the operator before the model ever asks (D2) |

That phase is then: render the unloaded catalog into the system prompt, add one
`request_worker(name, reason)` tool, route it through the existing approval
registry, and call `manager.load()` on approval.

Two seams named rather than pre-built:

- **The approval hop for loads.** `_await_operator` is private and reachable only
  through `call_tool`. A load request needs approval without a tool dispatch,
  meaning a small shared helper — extracted when there is a second caller, not
  speculatively. `WorkerManager` will already hold the risk pool, which holds the
  registry, so it is a parameter addition, not a rewiring.
- **`AuditEntry` for lifecycle actions** (§7). A model-requested load must be
  audited; an operator typing `/worker load` at a terminal is already an
  intentional act.

For ArcticBase: **all mutation flows through `WorkerManager`.** If per-channel
views become real, `ToolExecutor.schemas()` is the single choke point needing a
channel dimension, and nothing in PARE reaches around the manager to add or remove
a tool.

## 12. Sequencing

Following the capture-layer precedent in this repo
(`2026-06-30-shared-capture-layer-design.md` feeding
`2026-06-30-capture-layer-agent-core.md` then
`2026-07-02-capture-layer-pare-wiring.md`), this one design produces two plans:

1. **`agent_core` dynamic worker lifecycle** — §6, §7, §10's `agent_core` half.
   Ships as v1.8.0.
2. **PARE wiring** — §9, the §8.1 and §8.4 fixes, §10's PARE half. Bumps the
   `agent_core` pin in `pyproject.toml` (currently `v1.7.3`).

## 13. Risks

- **Two-repo change.** PARE cannot land until `agent_core` v1.8.0 is tagged. The
  plans are sequenced accordingly.
- **`astartup` widens the `agent_core` change** beyond the worker package, into
  `agent.py`, `daemon.py` and `runtime.py`. Mitigated by the default no-op and a
  backward-compatibility test.
- **§8.3 may not reproduce.** If the cascade cannot be reproduced, the per-worker
  lock change still stands on its own merits (a hanging connect blocking every
  other worker's first use is independently worth fixing), but re-enabling the
  eight `apk_re_agents` entries should not be assumed to follow from it.
