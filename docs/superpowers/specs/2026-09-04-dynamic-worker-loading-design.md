# Dynamic worker loading — design

**Date:** 2026-09-04
**Status:** v2 — revised after review panel; pending implementation
**Repos:** `agent_core` (v1.8.0), `PARE` (follow-on wiring)

> **v2 changes.** A four-reviewer panel found one blocker and three findings that
> change mechanism rather than wording. Connection teardown is **task**-bound, not
> loop-bound, so unload as originally specified could not work (D7, §6.3). Reload
> could silently *lower* a tool's effective risk tier (§6.4.1). Eviction-timing
> could not close the session-approval race, and the test proposed for it would
> have passed anyway (§6.4.2). The tombstone needed to be a handback trigger, not
> an error string (§9.3). Every `file:line` citation has been re-verified against
> the source; v1's were recalled rather than checked and drifted by 2–25 lines.

## 1. Context

PARE reaches its analysis tools through MCP workers declared in `workers.yaml`.
That registry is read once, at daemon boot, and the resulting tool set is frozen
for the life of the process:

```
workers.yaml
  → WorkerRegistry.load()            pare/agent.py:113
  → MCPClientPool(specs)             pare/agent.py:116
  → RiskAwareToolPool(specs={...})   pare/agent.py:129
  → register_tools()                 pare/agent.py:144
      discover_and_register at :161 in a throwaway asyncio.run loop (:165),
      then close_all() at :162
  → _attach_registries               agent_core/runtime.py:32 (called at :117)
      ToolExecutor.build() at :50 instantiates every class into a dict
```

Adding, removing, or restarting a worker requires restarting the daemon, which
discards the session: live Frida attachments, installed Java hooks, the warm
conversation, and the operator's place in an investigation.

Three workers are declared today — `frida` (`workers.yaml:17`), `static` (`:29`),
`mitm` (`:41`), all stdio, all in-house. Eight `apk_re_agents` Streamable-HTTP
workers are commented out (note at `workers.yaml:10-15`) partly because an
unreachable HTTP worker's failed discovery was observed to cancel its siblings'.
`../pare-hardware-mcp` is an empty directory — the next worker, and the immediate
motivation for this work.

### What already works in our favour

- `ToolExecutor._tools` is a plain mutable dict (`agent_core/tools/executor.py:24`).
- `handle_chat` calls `self.tool_executor.schemas()` fresh every turn
  (`pare/agent.py:214`), so a mutated registry reaches the model on the next
  message with no restart and no client reconnect.
- `MCPClientPool` already lazy-connects per worker and caches per name.
- `make_tool_class` (`agent_core/workers/tool_factory.py:28`) already synthesizes
  `Tool` subclasses from a live `list_tools` result. That code is already runtime
  code; it simply only ever runs once.
- Dispatch across tasks is safe: a client connected in one task can be called
  from many others. Only *teardown* is task-bound (D7).

### What blocks it

- `MCPClientPool._specs` (`client_pool.py:20`) and `RiskAwareToolPool._specs`
  (`risk_pool.py:82`) are private dicts built once, with no add/remove — and they
  are two **distinct objects**, built separately at `pare/agent.py:116` and `:129`.
- Teardown is all-or-nothing: `close_all()` (`client_pool.py:44`) only, no
  per-worker disconnect, and nothing evicts a worker's `_tool_tiers` or
  `_session_approved` entries.
- Boot discovery runs in a throwaway `asyncio.run` loop (`pare/agent.py:165`) and
  must `close_all()` afterwards (`:162`).
- **Connection teardown is task-bound** (D7) — the constraint that shapes the
  whole design.

`RiskGate` overrides are baked at construction (`pare/agent.py:129-133`), but this
is *not* a blocker: under D2 every loadable worker is declared in the same YAML
already parsed at boot, and `RiskGate.evaluate` is stateless over a flat override
list, so the manager never needs to touch it. See §6.4.4.

## 2. Goals

1. Load, unload, and reload a declared worker at runtime, from the operator's CLI,
   without restarting the daemon or losing session state.
2. Reclaim context budget: an unloaded worker's tool schemas leave the per-turn
   request.
3. Keep every existing security property — operator-reviewed risk floors, HITL
   gating, audit — intact **across** load/unload cycles, which is a stronger
   requirement than keeping them intact at a single point in time (§6.4.1).
4. Leave the groundwork for model-initiated, operator-approved loading without
   building it.

## 3. Non-goals

- **Auto-load on capability need.** Silent capability expansion in a tool that
  hooks processes and reads memory is the wrong default.
- **Model-initiated loading.** Groundwork only (§11).
- **Per-channel tool views** and the connection refcounting they imply. The loaded
  set is daemon-wide (D4) — with an expiry date (§11).
- **A worker "are you busy?" contract addition.** Unload is blunt (D6).
- **Re-reading `workers.yaml` at runtime.** The catalog is fixed at boot (§6.4.4).

Note that **in-flight call handling across unload/reload is no longer a non-goal**
— v1 waived it, then claimed containment it did not have. §7 now specifies it.

## 4. Decisions

**D1 — Operator-initiated only.** A `/worker` command drives load/unload/reload.
The model never changes its own toolset. Model-initiated loading is a real
capability-escalation surface, deferred to a later phase and designed so that
phase is additive.

**D2 — `workers.yaml` is the catalog and the trust anchor.** Every worker is
declared there whether or not it connects at boot; a new `autoload` field decides.
Nothing is loadable that the operator has not declared and assigned a
`risk_default` to, ahead of time, in a reviewed file.

*Rejected:* filesystem/entry-point discovery of installed `pare-*-mcp` scripts —
a newly installed worker would arrive with no operator-set risk floor and no pins,
leaving the tier to come from the worker's own wire metadata.

*Rejected:* ad-hoc `/worker load <command>` — an arbitrary-subprocess-launch
primitive inside the agent. `/worker reload <name>` covers the worker-development
loop by re-execing an already-declared worker.

**D3 — The lifecycle lives in `agent_core`.** Almost everything that must become
mutable is framework-owned. More importantly, the invariants at stake — the tier
ratchet, approval eviction, connection ownership — are exactly what
`RiskAwareToolPool` and `MCPClientPool` exist to hold. If PARE reached around
them, those properties would become PARE's problem, and every other agent on the
framework would re-solve them.

**D4 — Daemon-wide, not per-channel.** `tool_executor` is a single instance on the
agent, shared by every channel, matching `mcp_pool` and `tool_pool`. This is a
YAGNI call, not a correctness one, and **it expires when model-initiated loading
lands** (§11) — at that point one channel's model can expand another channel's
toolset with no prompt shown to the second operator.

**D5 — Boot goes through the same `load()` path.** `agent_core` gains an async
startup hook; `Daemon.serve()` awaits `agent.astartup()` before it accepts
connections, and `WorkerManager.load_autoload()` runs there. The throwaway-loop
workaround and its pinning test are deleted, not preserved.

**D6 — Unload is blunt.** `unload <name>` drops the worker's tools from the schema
**and** disconnects the client (for stdio, ending the subprocess). One meaning, no
modes, no `--force`, no refuse-if-busy. The consequence is documented, not guarded
against: unloading `frida` drops any live attachment and its installed hooks. The
`/worker unload` output is the documentation surface and must say so (§9.1).

**D7 — Connections are owned by a per-worker task (new in v2).** anyio requires a
cancel scope be exited in the task that entered it. `MCPClient.connect()`
(`agent_core/workers/client.py:90-108`) enters two nested scopes — `stdio_client`'s
task group and `ClientSession.__aenter__`'s — and `close()` (`:110`) exits both.
`astartup()` runs in the `asyncio.run(daemon.serve())` main task
(`runtime.py:121`); `/worker unload` runs in a per-message task
(`daemon.py:103`, `asyncio.create_task(self._run_handler(...))`). Closing from the
command task therefore raises:

```
RuntimeError: Attempted to exit cancel scope in a different task than it was entered in
```

**Reproduced and measured** against a real stdio MCP stub through
`MCPClientPool` (`mcp` 1.29.1, `anyio` 4.x, Python 3.12.3):

| Operation | Result | Worker process |
|---|---|---|
| `close()` from the connecting task | ok | reaped (`alive=False`) |
| `call_tool()` from another task | ok | — |
| `close()` from another task | **`RuntimeError`** | **`alive=True`** |

The process only dies when the whole event loop tears down at `asyncio.run` exit
— i.e. at daemon shutdown. In a long-running daemon the worker survives the
"unload" for the rest of the process lifetime.

This is why v1's design could not work, and the failure shape is the worst
available: §7's `try/except` around the disconnect **swallows** the `RuntimeError`,
reports `ok=True`, drops the executor entries, and leaves the subprocess alive with
half-torn streams. The next `/worker load frida` spawns a *second*
`pare-frida-mcp` while the first still holds the device attachment. `reload` would
leak a process on every invocation.

The swallowing is not hypothetical — `close_all` already does it today
(`client_pool.py:47-48`, `except BaseException: pass`). In the first repro run,
`close_all()` from a foreign task reported success while anyio printed the
`RuntimeError` to stderr and the child kept running.

So the pool owns a long-lived task per worker that connects, parks, and closes in
its own `finally` (§6.3). Everything else in the design follows from this.

## 5. Architecture

New component: `agent_core/workers/manager.py` → `WorkerManager`, the only thing
that mutates the registries that currently freeze at boot.

```
WorkerManager
  ├─ WorkerRegistry     the catalog — every declared worker, loaded or not
  ├─ RiskAwareToolPool  → MCPClientPool: owner tasks, tiers, approvals, generations
  └─ ToolExecutor       what the model can see

  load(name)    catalog → spawn owner task → list_tools → synthesize → executor
  unload(name)  evict tiers/approvals + remove spec → executor ⊖ tools → stop owner
  reload(name)  unload ∘ load, under one lock, reported as one result
  status()      per-worker: loaded?, tool count, tags, transport, last error
```

PARE's `/worker` command is a thin operator surface over those methods. Nothing in
PARE touches `agent_core` internals.

### Naming

The `workers.yaml` mapping key **is** `WorkerSpec.name` — `registry.py:40` does
`WorkerSpec(name=name, **fields)` — and that same name is the tool prefix in
`make_tool_class` (`tool_factory.py:44`, `f"{worker.name}_{tool_name}"`). One
namespace, no second registry to drift:

```
workers.yaml key  →  WorkerSpec.name  →  tool prefix      →  /worker arg
  frida:          →  "frida"          →  frida_attach     →  /worker load frida
```

Uniqueness is enforced by YAML itself; `WorkerSpec.name_is_valid_identifier`
(`types.py:75-82`) rejects MCP-unsafe names. `/mitm` the command and `mitm` the
worker do not collide — `CommandRegistry` and `ToolExecutor` are separate.

## 6. `agent_core` changes

### 6.1 `WorkerSpec` (`workers/types.py:59`)

```python
autoload: bool = True   # default preserves today's behavior for every existing entry
```

**Forward-compat hazard, security-adjacent.** `WorkerSpec` declares no
`model_config`, so pydantic's default `extra="ignore"` applies. A `workers.yaml`
carrying `autoload: false` read by an *older* `agent_core` does not error — it
silently drops the field and **autoloads the worker anyway**. For D2's trust anchor
that is a fail-open. §12 sequences the repos; the config edit must be sequenced
with them, and §10 pins it with a test that fails loudly on an unbumped pin.

### 6.2 Tool provenance (`workers/tool_factory.py`)

```python
_DynamicTool.worker = worker.name
```

One line, and it is what makes unload safe. Prefix matching would remove
`StaticAnalyze` (`pare/tools/static_analyze.py:20`, name `static_analyze`) when
unloading `static`. With the attribute, removal is
`getattr(t, "worker", None) == name`; builtins and `cls.tools` entries have no
`worker` attribute — verified, no existing Tool class defines one — and are
structurally unremovable.

### 6.3 `MCPClientPool` (`workers/client_pool.py`) — connection ownership

The pool gains a **connection-owner task per worker**. This is D7's fix and the
largest single change in the release.

```python
async def _own(self, name: str) -> None:
    spec = self._specs[name]
    client = MCPClient.from_spec(spec)
    try:
        await client.connect()
        await client.initialize()
        self._clients[name] = client
        self._ready[name].set()
    except BaseException as exc:
        self._errors[name] = exc
        self._ready[name].set()
        raise
    try:
        await self._stop[name].wait()      # parked for the worker's lifetime
    finally:
        self._clients.pop(name, None)
        await client.close()               # same task that entered the scopes
```

Public surface:

```python
add_spec(spec)                 # idempotent; does not connect
remove_spec(name)              # disconnects first
async connect(name)            # spawn owner task, await _ready, raise stored error
async disconnect(name)         # _stop.set(); await owner task (bounded, §7)
is_connected(name) -> bool
spec(name) -> WorkerSpec | None   # single source of truth (§6.4.3)
```

Cancelling `connect()` becomes `owner_task.cancel()` — cancellation delivered
*inside* the owning task, the only safe way to abort a partially-entered anyio
scope. This also gives `close_all()` a correct implementation for the first time.

`_connect_lock` (`client_pool.py:22`) becomes a per-worker
`defaultdict(asyncio.Lock)`. Today one global lock (held at `:27` across
`connect()` at `:31` and `initialize()` at `:32`) serializes every first-connect.
**Do not pop a worker's lock in `remove_spec`** — popping while a waiter holds it
hands the next caller a fresh lock and lets two connects race. The dict is bounded
by the catalog; leave entries.

`MCPClient.connect()` has no timeout at any transport, so §7's per-load timeout is
what keeps a blackholed `streamable_http` endpoint from hanging boot forever.

### 6.4 `RiskAwareToolPool` (`workers/risk_pool.py`)

#### 6.4.1 A tier ratchet — the security fix v1 got backwards

v1 justified evicting `_tool_tiers` on unload with "a reloaded worker starts with
no advertised tiers, so `resolve_declared_tier` applies its floor until the worker
re-advertises," treating the floor as a safe resting place. **It is not.**
`risk.py` returns the *floor* for a missing advertised tier, and says so in its own
docstring: safety for dangerous tools that fail to advertise comes from "mandatory
operator pins + build-time conformance… NOT by a dispatch-time fail-safe."

`workers.yaml:20` sets `frida.risk_default: low`. `risk_overrides`
(`workers.yaml:92-94`) pins only `frida_execute_script` (critical) and
`frida_write_memory` (high). So **`frida_read_memory` and `frida_java_hook` are
protected solely by the wire-advertised tier** cached in `_tool_tiers` — a dict
this design was going to discard and re-learn from whatever binary came back:

1. Boot: `list_tools` sets `("frida","read_memory") → "high"` (`risk_pool.py:102`).
2. `/worker reload frida`.
3. The binary at `workers.yaml:18` is now a different build — a rollback, a dev
   branch, a swapped artifact — advertising `read_memory` with no tier meta.
4. `list_tools` overwrites the entry with `None` (`:102`, unconditional).
5. `resolve_declared_tier(spec, None)` → `("low","floor")` (`risk.py:68`).
6. `frida_read_memory` auto-executes, no prompt, `outcome: "ok"`.

Step 4 does not depend on the eviction at all; the unconditional overwrite in
`list_tools` is sufficient. `risk.py`'s escalate-only rule is monotonic *within one
resolution*, never *across time* for a `(worker, tool)`. A boot-frozen daemon makes
an intra-session downgrade structurally impossible; runtime reload introduces it.

**Fix — a session-scoped high-water mark that lifecycle never evicts:**

```python
self._tier_highwater: dict[tuple[str, str], str] = {}
```

Updated in `list_tools` (max against any prior value), never cleared by
`remove_spec`, and resolved against in `call_tool` as
`max(advertised, highwater)`. A worker that re-advertises lower fails closed at its
previously observed tier. Cheap and independently worthwhile: also pin
`frida_read_memory` and `frida_java_hook` in `workers.yaml` so the two
highest-value tools do not depend on wire metadata at all.

**Related defect:** the comment at `risk_pool.py:113-114` claims the `None` case
"fails safe to 'high' for internal workers." `risk.py:68` contradicts it. That
stale comment is what made v1's reasoning look sound; fix it in the same change.

#### 6.4.2 Generations — the approval fix v1 got incomplete

v1 proposed evicting `_session_approved` on both unload and load. That does not
close the race, because the parked future resolves on an event outside the
manager's control and can land *after* the load completes:

1. Model calls `frida_java_hook` (high). Parks at `risk_pool.py:186`.
2. `/worker reload frida` — unload evicts, load evicts, new subprocess up.
3. Operator answers the still-displayed prompt with `a`.
4. `risk_pool.py:200` adds `("frida","java_hook")` — after both evictions.
5. Next call hits `:123`, finds the entry, no prompt, executes against the fresh
   binary.

The test v1 proposed for this ("approve → unload → resolve → load → assert absent")
replays only the interleaving the fix covers, so it would go green while the leak
stood — the worst kind of test.

**Fix — a per-worker generation counter,** incremented on every `add_spec` and
`remove_spec`:

- capture `gen` in `call_tool` before `_await_operator`;
- at `:200`, record the approval only if the generation is unchanged;
- key `_session_approved` on `(worker, tool, gen)` and compare at `:123`.

Ordering-independent by construction, and it closes §7's reload-mid-approval case
with the same mechanism.

> **Note on v1's use of `frida_execute_script` to illustrate this.** That tool is
> pinned `critical` (`workers.yaml:93`), and both the session-approval read
> (`:123`) and write (`:199`) exclude `critical`. It is the one tool that cannot
> exhibit the race. The mechanism is real; the illustration was wrong.

#### 6.4.3 One spec dict, not two

`MCPClientPool._specs` (`client_pool.py:20`) and `RiskAwareToolPool._specs`
(`risk_pool.py:82`) are distinct objects built separately at `pare/agent.py:116`
and `:129`. Describing `add_spec` as "→ inner" invites an implementation that
writes one and forgets the other, and the drift is asymmetric:

- inner cleared, outer kept → `risk_pool.py:112` finds a live spec, resolves at
  frida's **low** floor, **skips HITL entirely**, then `KeyError`s. A no-approval
  path.
- outer cleared, inner kept → `("high","unknown_worker")`. Safe but noisy.

**Fix:** delete `RiskAwareToolPool._specs`. Add `MCPClientPool.spec(name)` and make
`risk_pool.py:112` read through to it. One source of truth; `add_spec`/`remove_spec`
become genuine one-line delegations. The constructor keeps `specs=` for
compatibility by seeding the inner pool.

#### 6.4.4 Operator pins need no lifecycle handling

`RiskGate(overrides=registry.risk_overrides())` is built once
(`pare/agent.py:129-133`) and `evaluate` is stateless over a flat list keyed on
`f"{worker}_{tool}"` — no per-worker state, nothing to evict. **Pins survive
load/unload/reload correctly and the manager must not touch the gate.**

The hazard is only if the catalog ever becomes re-readable: new specs would flow
into the pool while `RiskGate._overrides` stayed frozen, so an edited `risk_default`
would take effect but an added pin **silently would not**. `WorkerRegistry.add()`
(`registry.py:63`) is an existing spec-mutation door with no overrides counterpart.
Hence §3's non-goal: the catalog is fixed at boot, and if re-reading is ever added
it must rebuild `RiskGate` in the same operation.

### 6.5 `ToolExecutor` (`tools/executor.py`)

```python
def __init__(self, tools, *, agent=None, disabled=frozenset()): ...
def add(self, tool_cls) -> None          # validates `requires`; raises on collision
def add_all(self, tool_classes) -> None  # validate all, then commit — atomic
def remove(self, name) -> bool
def remove_worker(self, name) -> int
def __contains__(self, name) -> bool
```

**The agent is stored at build time, not passed per call.** v1 proposed
`add(tool_cls, agent)` to avoid "a back-pointer the executor doesn't need," but the
executor is already stored *on* the agent (`runtime.py:50`) and every tool it
dispatches receives the agent via `ctx.agent` — the cycle exists regardless.
Per-call `agent` is worse: nothing stops a caller passing a different agent than
`build()` validated against, every future mutator carries the parameter, and it
forces `WorkerManager` to hold an `agent` it otherwise does not need.

**`add_all` gives atomicity where the dict lives.** v1 put the all-or-nothing
rollback in the manager and dismissed it as never-firing. That is true for
`requires` (`make_tool_class` never sets it; `Tool.requires` defaults to `()` at
`base.py:36`) but **false for the collision case** §8.2 introduces — one shadowing
tool among ten is exactly a partial-add.

**`disabled` must be retained.** It is currently a `build()` parameter only
(`:31`) and is not stored, so `add()` could not honour `disabled_builtins`.

**Ordering.** `schemas()` (`:59`) must emit stably: builtins, then declarative
`cls.tools`, then worker tools sorted by `(worker, name)`. Two reasons — a load
must not reshuffle the prompt prefix, and `load_autoload()` runs concurrently (§7),
so registration order is nondeterministic. Sort **inside `schemas()` only**;
`tests/test_tools_executor.py:118` pins `names()` insertion order, which has no
consumer-visible meaning, while `schemas()` order is a prompt-cache prefix.

**Collision handling is version-split.** `add()` refuses (new API, no compat
surface). `build()` **warns** and keeps last-write-wins for 1.8.0 — making it raise
could break an unseen consumer that intentionally shadows a builtin, which is a
2.0.0 change (§6.8). Under D5 the PARE near-miss flows through `add()` anyway, so
practical coverage is identical.

### 6.6 `WorkerManager` (`workers/manager.py`, new)

```python
async load(name)      -> WorkerOpResult
async unload(name)    -> WorkerOpResult
async reload(name)    -> WorkerOpResult
async load_autoload() -> list[WorkerOpResult]   # boot path, from astartup()
async close_all()                                # from ashutdown()
status()              -> list[WorkerStatus]
unavailable_reason(name) -> str | None           # advisory only (§8.1, §11)
```

**Constructed in `astartup()`, not `setup()`.** At `setup()` time
(`runtime.py:115`) `agent.tool_executor` does not exist — it is assigned at
`runtime.py:50` inside `_attach_registries`, which runs at `:117`, *after* setup
returns. By `astartup()` every collaborator exists, so the constructor takes the
executor directly: no back-pointer, no late binding, and the ordering constraint is
expressed by where the object is built. PARE's `setup()` pre-assigns
`self.worker_manager = None` as a sentinel so the `/worker` command's
`requires` check passes at `_attach_registries` time — the direct precedent is
`runtime.py:55-58`, which does exactly this for `command_registry`.

**One result type, not two.** `WorkerOpResult(op, name, ok, tool_count, tools,
error, error_kind)`. v1's `reload -> LoadResult` had nowhere to report "the unload
half failed" — precisely the state D7 produces, and reload is the primary
worker-dev loop.

**`error_kind` is a discriminated literal**, not free text:
`"unknown_worker" | "spawn_failed" | "connect_timeout" | "protocol_mismatch" |
"tool_collision" | "list_tools_failed" | "disconnect_timeout"`. `/health`, the
tombstone and (under §11) a model-facing catalog would otherwise string-match prose.

**`unavailable_reason`, not `require`.** `require()` reads like it raises and
inverts truthiness at every call site. It returns a message or `None`, and it is
**advisory only** — enforcement stays at `risk_pool.call_tool`. It must report
"loaded" only *after* `executor.add` completes, not after `add_spec`, or the
executor-bypassing fast paths (`pare/commands/_frida.py:30`,
`pare/commands/mitm.py:72`) can dispatch during the load window with an empty tier
table.

Results are returned, never raised — a failed load must not take down the daemon or
the operator's turn. `last_error` is retained so `/worker list` and `/health` can
show *why* a worker is not loaded; today a worker that fails discovery is silently
absent.

### 6.7 `Agent.astartup()` / `ashutdown()` and `Daemon.serve()`

```python
async def astartup(self) -> None: ...    # default no-op
async def ashutdown(self) -> None: ...   # default no-op
```

**Placement matters more than v1 said.** "Before it binds" has three readings and
the obvious one is the worst:

| Placement | Consequence |
|---|---|
| Top of `serve()` (before `daemon.py:52` unlink) | A stale socket from the previous run stays on disk for the whole load window; `pare-cli` connects to a dead inode |
| After unlink, before `start_unix_server` (`:54`) | No socket during load → CLI gets `FileNotFoundError`, reads as "daemon not running" |
| **`start_serving=False`, await `astartup()`, then serve** | Socket exists immediately, client connects queue in the listen backlog, no request dispatched against a half-populated executor |

```python
server = await asyncio.start_unix_server(..., start_serving=False)
await self.agent.astartup()
async with server:
    await server.serve_forever()
```

The trap in the naive version: `start_unix_server` defaults to
`start_serving=True`, so "bind, then astartup" **already accepts connections** and
spawns `_handle_connection` tasks during startup — a chat turn can land mid-load.

`ashutdown()` is not optional politeness. `pare/agent.py:162` is the only
`close_all()` call site in PARE, and D5 deletes the block containing it; without a
shutdown hook nothing would ever close the pool and stdio workers would be orphaned
on daemon exit. With D7's owner tasks, clean shutdown is nearly free.

**`astartup()` must not raise.** `systemd/pare-daemon.service` is `Type=simple`
with `Restart=on-failure` / `RestartSec=5`, so a raising `astartup` becomes a
5-second restart loop that spawns worker subprocesses each cycle. `load()`'s
no-raise guarantee covers the loads; the consumer's `astartup` must also survive
`WorkerRegistry.load` raising `FileNotFoundError` (`registry.py:32-33`) or
`ValidationError`. §8.6's stale paths make this live, not hypothetical.

### 6.8 Versioning and API hygiene

**1.8.0**, on the condition that `build()` only warns on duplicate names; making it
raise is a 2.0.0 item against 1.0.0's "minor bumps preserve the public contract."
Everything else is additive: new methods, a field with a behaviour-preserving
default, hooks with no-op defaults. Precedent exists for a documented behaviour
change in a minor (1.6.0 changed `risk_default` to a floor).

`worker_contract_version` stays at 1 — no wire fields change; say so explicitly in
the changelog, as every prior worker-touching release has.

Two hygiene items in the same release:

- **`discover_and_register` becomes a divergent second path.** It is public API and
  §6 proposes no change, so after 1.8.0 a consumer using it gets no collision
  refusal, no per-worker locking, no `WorkerOpResult`, no `last_error` — directly
  contradicting D5's "one code path." Reimplement it as a thin wrapper over
  `WorkerManager.load`, or deprecate it in the CHANGELOG. Note
  `tests/test_phase3_smoke.py:13` imports it directly, so removing it outright
  fails collection even though the test is env-gated.
- **Export `WorkerManager` from `agent_core/workers/__init__.py`.**
  `RiskAwareToolPool` and `WorkerRegistry` are not exported today and PARE reaches
  through submodule paths; don't repeat that.
- **Backfill `CHANGELOG.md` 1.7.0–1.7.3.** Its top entry is `[1.6.2]` while
  `pyproject.toml:7` says `1.7.3` — four undocumented releases, including the one
  PARE pins. The 1.8.0 entry cannot say what changed since the pinned version until
  this is fixed.

## 7. Error handling and edge cases

**Load is transactional, timed, and locked.**

```
lock(name):
  add_spec → connect (owner task, timeout) → list_tools (timeout)
           → synthesize → executor.add_all
  on any failure: cancel owner task, remove_spec, record error_kind, ok=False
```

A `WorkerManager`-level `defaultdict(asyncio.Lock)` is held across the whole
load/unload/reload body. §6.3's per-worker connect lock guards only
connect+initialize; the multi-step transaction above is otherwise unguarded, and
concurrent `unload`/`load` of the same worker can interleave so that unload's
`remove_worker` lands after load's `add_all` — connected, tiers learned, tools
invisible, `status()` disagreeing with reality. D4's "one CLI at a time" is an
argument about *operators*, not *tasks*: `load_autoload()`, per-connection dispatch
tasks (`daemon.py:98-106`) and (under §11) model-initiated loads all run
concurrently in one loop. The manager lock also gives `reload` real atomicity
rather than "unload ∘ load".

**Timeouts are per-worker, inside `load()`.** A global timeout around `astartup()`
cannot say *which* worker hung, and cancelling `connect()` from outside its owning
task is exactly D7's hazard. With owner tasks the timeout is `owner_task.cancel()`.
Default ~10s, config-driven. Today's bound is `discovery.py:43`'s 2s `wait_for`
around `list_tools`; losing it without replacement is what makes a hung worker
block boot forever (§6.7).

**`load_autoload()` gathers rather than iterates**, so boot cost is `max()` not
`sum()`. Per-worker locks make it safe; nondeterministic registration order is what
§6.5's sorted `schemas()` neutralises.

**Unload ordering is: evict, remove spec, remove tools, *then* disconnect.** Every
step before the disconnect is unconditional and cannot hang, so a wedged teardown
can never leave a half-unloaded worker whose tools are gone but whose specs and
standing approvals remain. The disconnect is bounded by `asyncio.wait_for`; on
timeout, hard-kill the recorded child pid and report `disconnect_timeout`. A frida
worker holding a live attachment in a native thread may not honour stdin EOF
promptly, and an orphaned `pare-frida-mcp` that keeps its attachment while
`/worker load frida` spawns a second one is the same duplicated-process failure as
D7, arrived at from the other direction.

**In-flight calls.** For **unload**, `remove_spec` makes `_ensure_connected` raise
`KeyError` (`client_pool.py:26`), caught at `risk_pool.py:208`. For **reload**, v1's
containment claim was false: `remove_spec` + `add_spec` restores the key, so an
approval granted before the reload proceeds against the *new* subprocess, audited
against the *old* tier table. The generation check (§6.4.2) covers it — before
`return None` at `risk_pool.py:201`, a changed generation returns
`_ErrorResult("<worker> was reloaded while approval was pending; re-issue the
call")` and emits an audit entry.

**Cancellation must be audited, not swallowed.** Disconnecting mid-dispatch tears
the awaiting task down as `asyncio.CancelledError` — a `BaseException`. Every guard
on that path is `Exception`-only: `risk_pool.py:208`, `tool_factory.py:62`, and
`executor.py:54` explicitly re-raises. So an unload during a live dispatch kills the
turn and leaves **no record that the dispatch happened** — and for this toolset that
dispatch may have injected a script or installed a hook. `Outcome` already has
`"cancelled"` (`types.py:110`) and nothing in the codebase has ever emitted it.

```python
except asyncio.CancelledError:
    self._emit(worker, tool, snapshot, declared, effective,
               int((time.monotonic() - start) * 1000),
               "cancelled", gate_override, "worker disconnected mid-dispatch",
               tier_source)
    raise
```

`WorkerManager.load` must likewise let `CancelledError` propagate — a bare
`except Exception` does; do not copy `discovery.py:44`'s tuple (§8.3).

**`close_all` is a second lifecycle path.** `client_pool.py:44-50` clears
`_clients` but not `_specs`; `risk_pool.py:105` proxies through and touches neither
`_tool_tiers` nor `_session_approved`. So after `close_all` the next call lazily
reconnects — fresh subprocesses — with **every session approval intact**, exactly
the invariant unload protects. Bump all generations in
`RiskAwareToolPool.close_all` (two lines), or narrow it to shutdown-only and route
every other teardown through the manager.

**Idempotency.** `load` on a loaded worker and `unload` on an unloaded one succeed
with an informative message. Either on an unknown name returns
`error_kind="unknown_worker"` and names the valid ones.

**Captured evidence survives, with one caveat.** `CaptureLayer.maybe_substitute`
(`capture/layer.py:45`) takes `worker` as a **string** (`:63`) and holds no
worker/pool/client reference; `CaptureStore` is opened per project with no worker
coupling. Verified: nothing in the capture path breaks on unload, and
`SearchCapture`/`ReadCapture` are declarative `PareAgent.tools` with no `worker`
attribute, so `remove_worker` cannot remove them. **But** `CaptureRecord` carries
`session_id` and `launch_ts`, and `launch_ts` is set once at `pare/agent.py:117`
and never refreshed. After a reload, `search_capture` returns pre-reload records
that look current and carry now-dead `session_id`s. Findings survive; **live-state
captures — sessions, hook events — go stale on reload.** Stamp the worker
generation on captures, and say so in `/worker`'s output and the prompt paragraph.

**Lifecycle actions are audited in v1 (changed from v1's deferral).** v1 argued the
record could wait because an operator typing `/worker load` is already an
intentional act. That conflates *authorization* with *record*: intentionality is why
a lifecycle action needs no approval hop, not why it needs no row. Every row in the
log today is intentional too. More decisively, a tool dispatch is a data-plane
action while load/unload is a **control-plane** change to the enforcement
configuration itself — the first thing an audit log exists for. Without those rows
the log is not self-consistent: given §6.4.1, an auditor sees
`frida_read_memory / high / hitl_approved` and later `frida_read_memory / low / ok`
with nothing between explaining it, and `tier_source` — added precisely so an
auditor can tell an advertised-low dispatch from a floor default
(`types.py:129`) — reads `"floor"` in both the innocent and the downgraded case.
`logger.info` cannot substitute: different retention, not append-only, not
per-project, and carrying no `session_guid`, so the streams cannot be joined.

The claimed distortion is small. `Outcome` is an extensible `Literal` that has
already grown; add `"worker_loaded"` / `"worker_unloaded"`, make `tool` nullable,
and pass `args={"action": "reload", "transport": ..., "tool_count": ...}`. Carry
the resolved `command` path plus the binary's mtime/size and an artifact swap
(§6.4.1 step 3) becomes visible for about three more lines.

## 8. Defects found during design

### 8.1 Dispatch to an unloaded worker costs a pointless approval round-trip

`pare/commands/_frida.py:30` (`WORKER = "frida"` at `:12`) is the chokepoint for
`/devices /ps /apps /sessions /select /attach /detach`; `pare/commands/mitm.py:72`
dispatches `capture_health`. Both bypass the executor and call the pool by
hard-coded name. These are the **only** two direct `tool_pool.call_tool` sites in
PARE; everything else goes through `ToolExecutor.run`, which the tombstone covers.

After an unload: `risk.py:63-64` returns `("high","unknown_worker")` when the spec
is absent, and `risk_pool.py:122` gates `high` on approval. So the operator gets an
**approval prompt for a call that cannot succeed**, approves, and only then hits
`KeyError` at `client_pool.py:26` → `risk_pool.py:213` → an audit row for a
dispatch to a worker that does not exist.

The code path is pre-existing — reachable by commenting a worker out of
`workers.yaml` while the hard-coded name remains — but never hit in practice
because all three commanded workers are always declared. Unload makes it routine.

**Fix:** `manager.unavailable_reason(name)` short-circuits before dispatch.
`_frida.call` checks it once and returns its error-shaped dict — but note the
*existing* dict is `{"error": True, "summary": f"{tool} call failed"}`
(`_frida.py:32`); the implementer must substitute the unavailable message into
`summary`, not reuse that literal. All six render sites read `summary` with the
error branch first, so no per-command changes are needed there.

**`/mitm status` does need a code change** — v1 wrongly claimed it "checks the same
way." `pare/commands/mitm.py:72-73` has **no `isError` check** and no `try`:

```python
result = await ctx.agent.tool_pool.call_tool("mitm", "capture_health", {}, ctx=ctx)
payload = json.loads(_result_text(result))
```

`_ErrorResult`'s block is `type = "text"`, so `_result_text` hands `json.loads`
plain failure text and `JSONDecodeError` escapes the command, caught only by
`daemon.py:146` and surfaced as a raw `ErrorMessage`. This is a **pre-existing bug
independent of this feature** — it fires today whenever the mitm daemon is down —
so guard it on its own merits: check `isError` and wrap the `json.loads`.

**Related:** `mitm` stays `autoload: true`. Commit `e9edf9f` deliberately made it
unconditional; `hardware` is the only pre-declared unloaded entry.

### 8.2 A worker tool can silently shadow a declarative tool

`ToolExecutor.build` does `instances[tool_cls.name] = tool_cls()`
(`tools/executor.py:45`), last-write-wins, silent, and `_attach_registries` orders
`declared + dynamic` (`runtime.py:49`) with dedup by *class identity*, not name — so
a worker tool overwrites a declarative one with no error and no log.

PARE has a live near-miss: `StaticAnalyze.name = "static_analyze"`
(`pare/tools/static_analyze.py:20`) while the `static` worker prefixes `static_*`.
If `pare-static-mcp` ever ships a tool named `analyze`, it silently replaces
`StaticAnalyze`. It does not today — the worker's ten tools are `load_apk`,
`find_symbol`, `grep_smali`, `list_methods`, `decompile_method`, `read_manifest`,
`callers_of`, `extract_strings`, `paths_between`, `reachable_sinks` (pinned by
`pare-static-mcp/tests/unit/test_contract.py`). Latent, not active.

**Fix:** `add()` refuses and reports `error_kind="tool_collision"`; `build()` warns
(§6.5, §6.8). Note the blast radius: with `add_all` atomicity, one colliding name
fails the *entire* worker load, including at boot. That is the right default —
half a worker is worse than none — but it must be stated and tested, and the
`WorkerOpResult` must name the colliding pair so the operator can act.

### 8.3 Discovery cascade — revised hypothesis, still unverified

`workers.yaml:10-15` records that an unreachable HTTP worker's "botched
cancellation" cancelled sibling discovery, including `frida`.

v1 guessed the global `_connect_lock`. **Two reviewers independently proposed a
better hypothesis, and the lock theory is likely wrong** — `async with` at
`client_pool.py:27` releases it correctly on every path. The stronger candidate is
`discovery.py:44`:

```python
except (asyncio.TimeoutError, asyncio.CancelledError, Exception) as exc:
```

`asyncio.TimeoutError is TimeoutError` and is an `Exception` subclass on 3.11+, so
it is redundant; the only meaningful member is **`CancelledError`, which should
never be swallowed.** A cancellation aimed at the discovery task is absorbed and the
loop marches on to the next worker in a task that is already cancelling — every
subsequent `await` re-raises immediately, which looks exactly like "one dead worker
killed its siblings."

This remains **unverified**. Point the verification test at this line, not the
lock. The per-worker lock change stands on its own merits (a hanging connect
blocking every other worker's first use is worth fixing regardless), but
re-enabling the eight `apk_re_agents` entries should not be assumed to follow from
it.

### 8.4 Stale `command:` paths (config, not code)

`workers.yaml:18,30,42` point at `/home/edible/Projects/PARE/.venv/bin/...`, which
does not exist on this machine (the repo is `/mnt/secondary/projects/PARE`). Today
that is a silently-skipped worker. After this change it is a `spawn_failed` in
`last_error`, visible in `/worker list`. Fix the paths in the PARE-side work,
tracked separately from the feature.

### 8.7 The `mcp` dependency is under-constrained — a fresh install is broken

`agent_core`, `pare-static-mcp` and `pare-frida-mcp` all declare `mcp>=1.27.0`
with **no upper bound**. `mcp` 2.x renamed `streamablehttp_client` →
`streamable_http_client`, so a fresh `pip install` resolves to 2.1.1 and
`agent_core/workers/client.py:35` fails at import — taking every worker test with
it (17 collection errors across the two repos, observed).

Standalone `fastmcp` (needed only by `agent_core`'s stub fixtures) is worse: 2.12+
imports `IdentityAssertionParams`, which no `mcp` 1.x has, and 4.x requires `mcp`
2.x — while its own metadata claims `mcp<2.0.0,>=1.12.4`, so pip's resolver cannot
see the conflict. The working combination is **`mcp` 1.29.1 + `fastmcp` 2.11.3**.

Pin `mcp>=1.27,<2` in all three `pyproject.toml` files and `fastmcp>=2.11,<2.12`
in `agent_core`'s dev extra. Independent of this feature, but it blocks anyone
setting up a test environment, and this design's plans both start with "run the
suite."

(Note for implementers: downgrading `fastmcp` across a major version leaves stale
modules behind — `pip uninstall` then reinstall, or you get import errors that
name modules the installed version never had.)

## 9. PARE changes

### 9.1 The `/worker` command

`pare/commands/worker.py`, shaped like `Mitm` (plain `ResponseMessage`, subcommand
dispatch), with `requires = ("worker_manager",)` so a `setup()` that forgets the
sentinel fails at boot rather than at first invocation.

```
/worker                → list
/worker list
/worker tools <name>
/worker load <name>
/worker unload <name>
/worker reload <name>
```

`/worker list` renders through `render_table` (`pare/commands/_snapshot_render.py:14`),
the house renderer used by `/devices`, `/ps`, `/apps`, `/sessions`, `/snapshot` — it
clips at 100 chars, which matters because the `last_error` column will carry full
`FileNotFoundError` paths (§8.4 guarantees three of those today).

`/worker tools <name>` exists because after `/worker load frida → 19 tools` the
operator otherwise has no way to see *which* nineteen. It is provenance data the
manager already holds, and §11 makes the same view the model's catalog.

`/worker unload` output must name what it destroyed — D6 makes the consequence
"documented, not guarded against," and this output *is* that documentation:

```
unloaded frida — 19 tools removed, client disconnected.
2 live sessions and their installed hooks are gone; captures remain searchable
but session ids are stale.
```

(Query `list_sessions` before the disconnect for the count.)

**Context-reprocessing warning.** §6.5's stable ordering prevents *gratuitous*
prefix invalidation, but removing a worker's tools changes the prompt prefix by
construction, so the first turn after any load/unload reprocesses the whole
conversation on a local inference server. On a long session that is a multi-second
stall the operator will misread as a hang. Say it in the command output.

### 9.2 Pool construction — the easy thing to get wrong

`pare/agent.py:116` currently passes **all** specs to `MCPClientPool`. If that line
is left alone, everything looks right at boot (`add_spec` is a harmless no-op) and
then the first dispatch after an `unload` **lazily re-spawns the worker you just
unloaded**, silently defeating the feature. §8.1's `KeyError` behaviour depends on
the pool holding *loaded* specs, not *declared* ones.

`setup()` therefore builds `MCPClientPool([])` and `RiskAwareToolPool(specs={})`,
with the manager owning all population. §10 pins it with an unload → dispatch → *no
reconnect* test.

`setup()` also pre-assigns `self.worker_manager = None` (§6.6). `register_tools()`
loses the `asyncio.run` / `close_all` block entirely and returns `[StaticAnalyze]`
when `config.enable_apk_re_agents` is set, `[]` otherwise. `astartup()` constructs
the manager and calls `load_autoload()`; `ashutdown()` calls `close_all()` — on both
the worker pool and `CaptureStoreManager` (`pare/capture_store.py:72`, likewise
never called today).

### 9.3 The tombstone must be a handback trigger, not an error string

When the model calls a tool from an unloaded worker, `ToolExecutor.run` returns
`Unknown tool: frida_attach` (`tools/executor.py:51`). The conversation still holds
successful `frida_*` calls from before the unload, so this is a plausible mistake
for a local model — and v1's plan to return a friendlier string is **not enough**:

- *Same tool, same args*: the tombstone is identical each time, so `RepeatGuard`
  (`pare/repeat_guard.py`, `_HARD_AFTER = 3` at `:29`) blocks after 3 and hands
  back — but with `spin_question`'s "I've re-run this 4× and I'm stuck" rather than
  "frida is not loaded," after 4 wasted inference round-trips.
- *Different frida tools, or different args*: every call is a distinct guard
  signature. The guard **never** fires; the loop runs all `MAX_TOOL_ROUNDS = 50`
  (`pare/agent.py:220`), each a full-context `complete()`, ending in "Reached the
  tool-call limit."
- *Worst case — poll tools*: `pare/agent.py:310` reads
  `if tc.name not in POLL_TOOLS and guard.tripped(...)`, and
  `POLL_TOOLS = {"frida_read_hook_events", "frida_list_sessions"}`
  (`pare/handback.py:18`) is **deliberately exempt from handback** because polling
  is supposed to repeat. `pare/prompts/system.md` explicitly instructs the model to
  poll `read_hook_events` repeatedly. So with `frida` unloaded, the one mechanism
  that would return control to the operator is switched off by design, and the turn
  burns all 50 rounds with no escalation.

**Fix:** make the unloaded-worker case a first-class handback trigger alongside the
two at `pare/agent.py:289` (commit-time disambiguation) and `:310` (spin). Before
dispatch, test `manager.unavailable_reason(worker_of(tc.name))`; on the second
distinct unloaded-worker call in a turn, call `_settle_and_handback`
(`pare/agent.py:268`) with "frida is not loaded — run `/worker load frida`, or tell
me how to proceed without it." That is the affordance §11 needs, and it must settle
every unanswered `tc.id` in the batch or the next turn's request is invalid.

### 9.4 Handback constants

`COMMIT_TOOLS` / `NAME_SEARCH_TOOLS` / `POLL_TOOLS` (`pare/handback.py:16-18`) are
membership sets over `tc.name`; unloading cannot corrupt them, and
`tests/test_handback_schema.py` resolves them against the installed worker package
contracts rather than the live executor, so it is unaffected. Two consequences to
record:

- Unloading `static` removes `static_grep_smali`, the sole `NAME_SEARCH_TOOLS`
  member. `name_searches` then stays empty for the turn, so commit-time
  disambiguation **silently stops firing** — including for `frida_java_hook`, a
  `COMMIT_TOOLS` member that survives the unload. The operator loses a checkpoint
  with no signal; `/worker unload static` should say so.
- The constants hardcode `{worker}_` prefixes. §10 adds a test that every prefix is
  a declared key in `workers.yaml`, so a worker rename cannot silently disarm a
  trigger — a bug class this feature makes much easier to hit.

### 9.5 Prompt and `/health`

`pare/prompts/system.md` gains a short paragraph: the toolset can change
mid-session; a tool vanishing is an operator decision, not a failure; if a needed
capability is not loaded, say so rather than working around it. Without that last
clause a model that loses `static_*` mid-investigation will try to reconstruct the
answer from `frida`.

**The prompt change is static text only.** `tests/test_system_prompt.py` builds a
bare `PareAgent()` with no `setup()`, so rendering live worker state into
`system_prompt` would break all 13 assertions — and would also let round *N* of a
turn claim "frida is unloaded" while the round-0 `schemas` snapshot still advertises
19 `frida_*` tools (§10). If live state is ever rendered, `schemas` must be
refreshed per round alongside it.

`/health` gains `workers: frida(19), static(10) · unloaded: hardware`, read through
`getattr(ctx.agent, "worker_manager", None)` so `/health` never crashes on a
partially-constructed agent.

## 10. Testing strategy

`tests/workers/conftest.py:12` exposes a `stdio_stub_spec(name, tier)` factory
building a `WorkerSpec` that launches `tests/workers/fixtures/stdio_stub.py` as a
real subprocess module via `sys.executable`, advertising `noop_low` and
`risky_high` with wire tiers. Load/unload/reload can be exercised end-to-end.

### `agent_core` — new `tests/workers/test_manager.py`

| Test | Asserts |
|---|---|
| **cross-task teardown (D7)** | `load` in one task, `unload` in **another** task — succeeds, subprocess reaped. *No existing `test_client_pool.py` test can catch this: every one connects and closes inside the same coroutine.* |
| `load` registers | N tools in a real `ToolExecutor`, named `{worker}_{tool}` |
| `unload` removes only its own | builtins and declarative tools untouched, via provenance not prefix |
| `unload` disconnects | `is_connected` False; process reaped; no lazy reconnect on next dispatch |
| `reload` is a fresh process | new pid; tools re-registered |
| failed load leaves no residue | bad `command` → `ok=False`, `error_kind="spawn_failed"`, absent from pool specs, zero tools |
| collision refused | colliding tool → whole load fails, `error_kind="tool_collision"`, names the pair, existing tool unchanged |
| stable schema order | `schemas()` prefix byte-identical across an unrelated load/unload |
| concurrent load/unload | same worker, both orders, under the manager lock — `status()` matches reality |
| **`load()` timeout** | one `streamable_http` spec on a closed port → `astartup()` completes and the socket binds (today's only bound is `discovery.py:43`) |

**Security tests, each pinning a §6.4 mechanism:**

- **tier ratchet**: stub advertises `high`, reload with a stub advertising nothing →
  next dispatch still resolves `high`, not the `low` floor.
- **generation / approval**: approve `a` → reload → let the parked future resolve →
  assert the next call **prompts again**. v1's version of this test would pass
  against the broken design; this one does not.
- **reload-mid-approval**: approval granted before reload → dispatch refused with
  "reloaded while approval was pending," audit row emitted.
- **`CancelledError` audit**: unload mid-dispatch → an `AuditEntry` with
  `outcome="cancelled"` exists (the first thing in the codebase to emit it).
- **`close_all` approvals**: `close_all` then dispatch → prompts again.
- **lifecycle rows**: load/unload emit `worker_loaded`/`worker_unloaded` entries.

**`astartup`/`ashutdown`:** `serve()` accepts no connection before `astartup`
completes (`start_serving=False`); an agent not overriding either is unaffected;
`astartup` does not raise when a spec's command is missing. Note `Daemon.serve()`
has **zero test coverage today** — `start_serving=False` is what makes it testable.

### PARE

**New `tests/test_worker_command.py`:** `/worker list` with mixed loaded/unloaded
and a `last_error` row; `tools`; load/unload/reload; unknown name; idempotent
repeats.

**New handback tests (§9.3), the highest-value PARE tests here:**
- three different `frida_*` tools against an unloaded frida → handback within ≤2
  rounds, **not 50**;
- repeated `frida_read_hook_events` against an unloaded frida → handback fires
  **despite `POLL_TOOLS` membership**;
- every `tc.id` in the settled batch has a matching tool result.

**New:** `unavailable_reason` in `_frida.call` and `/mitm status` — assert the
operator sees "not loaded" **and that no approval prompt is emitted**. The absence
of the prompt is the real assertion (§8.1).

**New:** after `/worker unload frida`, `search_capture(worker="frida")` still
returns pre-unload refs and `read_capture` resolves their bodies (§7's claim, pinned
where the store lives).

**New:** every `{COMMIT,NAME_SEARCH,POLL}_TOOLS` prefix is a declared key in
`workers.yaml` (§9.4).

**New:** `WorkerRegistry.load(workers.yaml)` yields `autoload is False` for
`hardware` — fails loudly if the `agent_core` pin is unbumped, where pydantic would
otherwise drop the field silently (§6.1).

### Existing tests that change

| File | Why | Action |
|---|---|---|
| `tests/test_register_tools.py` | patches `pare.agent.discover_and_register` at `:50`/`:122`; D5 removes that import (`pare/agent.py:34`), so `patch` raises `AttributeError`. 3 cases | Rewrite: `register_tools()` returns `[StaticAnalyze]`/`[]` with no discovery; `astartup()` drives `load_autoload()` |
| `tests/test_register_tools_reconnect.py` | pins `close_all()` after boot discovery; its docstring says it exists *because* discovery ran in a throwaway loop. Under D5 that loop is gone | **Delete** — in **plan 2 (PARE)**, not plan 1. v1 filed it under the `agent_core` half; it is a PARE test asserting on `PareAgent.register_tools`. Delete the 10-line rationale docstring at `pare/agent.py:149-158` with it |
| `test_frida_command_helper.py`, `test_frida_views_commands.py`, `test_frida_actions_commands.py`, `test_mitm_commands.py` | all build fakes as `type("A", (), {"tool_pool": pool})()`; an unconditional `ctx.agent.worker_manager` raises `AttributeError`. ~22 cases | Guard with `getattr(ctx.agent, "worker_manager", None)` — also correct production behaviour — **and** add `worker_manager` to the four fakes |
| `tests/test_health.py` | `FakeAgent` has only `config`/`name`; §9.5's worker line would `AttributeError` | `getattr`-guard in `health.py` |
| `tests/test_system_prompt.py` | bare `PareAgent()`, no `worker_manager` | Safe **iff** §9.5's prompt change stays static text; add a test that `system_prompt()` works with no manager |
| `tests/test_phase3_smoke.py:13` | imports `discover_and_register` from `agent_core.workers`; ImportError at collection is not skipped by the env gate | Keep the symbol (§6.8) |
| `agent_core/tests/test_tools_executor.py:118` | pins `names()` insertion order | Unaffected — sort inside `schemas()` only (§6.5) |

**Replacing the deleted reconnect test** needs two assertions, not one — its real
subject was *dispatch failing because a client outlived its context*: (a) no MCP
connection exists before `astartup()`, and (b) a tool dispatched **after**
`astartup()`, from a handler task, succeeds against a real stdio stub. (b) is the
one that would have caught the original live failure, and it doubles as the D7
regression test.

### Worker-side churn: none

No wire fields change, no conformance changes. `pare-frida-mcp` (19 tools),
`pare-static-mcp` (10) and `pare-mitm-mcp` need no edits; `pare-hardware-mcp` only
has to be a normal worker. (`pare-mitm-mcp` has no checkout on this machine, so
this is asserted from its README, not verified.)

## 11. Groundwork for model-initiated loading

| It needs | This design gives |
|---|---|
| a catalog of unloaded workers | `manager.status()` — name, tags, transport, `risk_default`, `last_error`, `error_kind` |
| a selection signal | `capability_tags` (`types.py:66`), currently read by nothing |
| a request trigger | the tombstone handback (§9.3) |
| an approval hop | `ToolApprovalRegistry` + `ToolApprovalRequestMessage`, already driving `risk_pool._await_operator` |
| a pre-reviewed risk floor | `risk_default`, set by the operator before the model ever asks (D2) |

That phase is then: render the unloaded catalog into the system prompt, add one
`request_worker(name, reason)` tool, route it through the existing approval
registry, and call `manager.load()` on approval.

**Three preconditions that must be recorded now, because they are assumptions this
phase inherits rather than problems it creates:**

1. **D2's "pre-reviewed risk floor" is `low` for all three declared workers**
   (`workers.yaml:20,32,44`). The real danger classification comes from wire
   metadata plus two pins. So D2's rejection of filesystem discovery — "leaving the
   tier to come from the worker's own wire metadata" — describes the current
   configuration uncomfortably well. At load time the manager already holds the
   `list_tools` result and the override list: **warn when a worker advertises a
   tool above its `risk_default` with no covering pin**, and under model-initiated
   loading, refuse. That converts §11's stated foundation into an enforced one.
2. **D4's YAGNI expires here.** Daemon-wide is fine when only the operator loads.
   Once the model can request, an approval granted in channel A expands the toolset
   for channel B, whose operator saw no prompt — and `ToolApprovalRegistry` has no
   channel or authorization dimension (`_resolve_send` at `risk_pool.py:148` just
   uses whichever `ctx.emit` is attached). Per-channel scoping is a **precondition**
   of this phase, not a later refactor.
3. **`unavailable_reason` stays advisory.** It is the natural place a
   `request_worker` tool would check state, at which point a cheap "is it loaded"
   becomes a TOCTOU check. Enforcement stays at `risk_pool.call_tool`.

Two seams named rather than pre-built: the approval hop for loads (a shared helper
extracted when there is a second caller — the manager already holds the risk pool,
which holds the registry), and per-channel views. For ArcticBase: **all mutation
flows through `WorkerManager`**, and `ToolExecutor.schemas()` is the single choke
point that would need a channel dimension.

## 12. Sequencing

Following the capture-layer precedent
(`2026-06-30-shared-capture-layer-design.md` → `2026-06-30-capture-layer-agent-core.md`
→ `2026-07-02-capture-layer-pare-wiring.md`), one design, two plans:

1. **`agent_core` dynamic worker lifecycle** — §6, §7, §10's `agent_core` half,
   plus the CHANGELOG backfill (§6.8). Ships as v1.8.0. The owner-task rework
   (§6.3) is the first task; everything else depends on it.
2. **PARE wiring** — §9, the §8.1 and §8.4 fixes, §10's PARE half including the
   four fast-path test updates and both register-tools test changes. Bumps the pin
   at `pyproject.toml:11` (currently `v1.7.3`).

**The `workers.yaml` edit belongs in plan 2, after the pin bump.** Landing
`autoload: false` against the unbumped pin fails open and silently (§6.1).

## 13. Risks

- **Two-repo change.** PARE cannot land until `agent_core` v1.8.0 is tagged.
- **The owner-task rework touches the connection path every worker uses.** It is
  the right fix and it is not a small one; a regression there breaks all worker
  dispatch, not just lifecycle. `tests/workers/` conformance suites are the
  backstop, and the cross-task test is new coverage for a gap that exists today.
- **`astartup`/`ashutdown` widen the change** beyond the worker package into
  `agent.py`, `daemon.py` and `runtime.py`, in a `serve()` method with no existing
  test coverage. Mitigated by no-op defaults, a BC test, and `start_serving=False`
  making `serve()` testable at all.
- **§8.3 may not reproduce.** If the cascade cannot be reproduced against
  `discovery.py:44`, re-enabling the eight `apk_re_agents` entries should not be
  assumed to follow.
- **The tier ratchet is new enforcement.** A worker legitimately *lowering* a
  tool's tier between builds now requires a daemon restart. That is the intended
  trade — a downgrade should not be silent — but it will surprise someone during
  worker development, so `/worker reload` should report when a re-advertised tier
  was clamped upward.
