# Per-Tool Risk Tier on the MCP Wire — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a worker's per-tool `risk_tier` (already declared in its contract) flow over the MCP wire and become the live gating tier at dispatch — so `risk_default` in `workers.yaml` is a *floor*, not the only signal. Lands as `agent_core@v1.6.0` (purely additive), a one-line emit in `pare-frida-mcp`, and a re-pin + mandatory override pins in PARE.

**Architecture:** The worker advertises each tool's tier in MCP `_meta` under key `agent_core/risk_tier`. `RiskAwareToolPool.list_tools` (the pool's existing pass-through, and the *only* path that sees the live `ListToolsResult`) captures those tiers into an internal `{(worker, tool): tier}` cache. At dispatch, `call_tool` resolves the declared tier via a new pure helper `resolve_declared_tier(spec, advertised)` that returns `max(risk_default_floor, advertised)` — **escalate-only relative to the floor, fail-safe to `high` on a missing/invalid tier for internal workers.** The existing `RiskGate` override-up layer still sits *above* that, fed by mandatory operator pins parsed from a new top-level `risk_overrides:` section in `workers.yaml`. `discovery.py` and `tool_factory.py` stay untouched (the v1.5.0 pool-wrap architecture is preserved).

**Tech Stack:** Python 3.12, asyncio, Pydantic v2, FastMCP (`mcp>=1.27`), the existing `RiskGate`/`AuditLog`/`MCPClientPool`, pytest + pytest-asyncio.

---

## Decisions baked in (from the skeptical-panel review + the chosen sequencing)

These were settled before planning; tasks below assume them. Do not re-litigate during execution.

1. **Capture point = `RiskAwareToolPool.list_tools`**, not `discovery.py`. It already sees the full `ListToolsResult` and is the same object that later dispatches — no threading through `discovery.py`/`tool_factory.py` (both stay frozen, per the v1.5.0 plan).
2. **Resolution rule = `max(floor, advertised)`** (escalate-only relative to the worker-wide `risk_default` floor). A missing/invalid tier resolves to the **floor** (`risk_default`), NOT a dispatch-time fail-safe.
   > **REVISED 2026-05-30 (Option C, decided with Shane):** The original plan fail-safed a missing tier on a `kind="internal"` worker to `max(floor,"high")`. The agent_core regression suite proved this breaks *every* non-advertising internal worker — notably the 8 `apk_re_agents` workers (all `kind="internal"`, none emit `_meta` tiers), which would each demand HITL approval on every call the moment PARE re-pins to v1.6.0. Shane confirmed those workers are a legacy "learning-era" pipeline, not yet exercised by a real PARE run, and chose **Option C**: a missing/invalid tier falls back to the `risk_default` floor (preserving legacy workers). Panel blocking #1's real concern (frida's `execute_script` silently auto-executing) is fully covered by the **mandatory operator pins** (decision #3) + **build-time conformance** (Task A5), not by a dispatch-time fail-safe. The model is now one monotonic rule: `effective = max(floor, advertised_wire_tier, operator_pin)`. Deferred follow-up: add `_meta` tier advertisement to the apk_re_agents workers (they auto-upgrade to full escalation once they do; no agent_core change needed). `spec is None` (dispatch to an *unregistered* worker — distinct from a missing wire tier) still resolves to `"high"`.
3. **Operator override pins are MANDATORY, not optional.** PARE parses a top-level `risk_overrides:` list from `workers.yaml` into `RiskGate(overrides=...)` and ships default pins `frida_execute_script → critical` and `frida_write_memory → high`. This is the authoritative ceiling that survives a lying/buggy worker. (Panel blocking #2 — `RiskGate(overrides=[])` left the worker self-asserting its own ceiling.)
4. **Wire channel = `_meta`** under key `agent_core/risk_tier`, **not** `annotations`. The MCP spec says `ToolAnnotations` are non-authoritative hints clients must not make decisions on; a security-load-bearing value belongs in `_meta`. (Panel wire-mechanics finding.)
5. **Frida `risk_default: high` during rollout; lower to `medium` ONLY after the e2e test (Task C3) is green. Never `low`.** (Panel sequencing finding; the chosen option explicitly said "medium, not low".)
6. **Audit provenance:** add a nullable `tier_source` field to `AuditEntry` so a `low`-tier dispatch is forensically distinguishable as `wire` vs `floor` vs `fallback_safe` vs `override`. (Panel forensic-honesty finding.)
7. **`kind="external_mcp"` is out of scope.** Its "raise one tier" behavior is unimplemented docstring today; the fallback is justified on its own merit (workers that don't advertise tiers use `risk_default`), not as "preserving external_mcp behavior". (Panel minor finding — don't cite behavior that doesn't exist.)

---

## File Structure

### Repo: `agent_core` (`~/Projects/agent_core`) — lands as `v1.6.0`

**Modify:**
- `agent_core/workers/risk.py` — add `RISK_TIER_META_KEY` constant + `resolve_declared_tier(spec, advertised)` pure helper.
- `agent_core/workers/risk_pool.py` — `list_tools` captures advertised tiers into `self._tool_tiers`; `call_tool` resolves the declared tier via the helper + records `tier_source`; `_emit` writes `tier_source`.
- `agent_core/workers/types.py` — add nullable `tier_source` field to `AuditEntry`.
- `agent_core/workers/registry.py` — `WorkerRegistry.load` also parses top-level `risk_overrides:`; new `risk_overrides()` accessor.
- `agent_core/workers/conformance.py` — `assert_stdio_conformance` (and the streamable_http sibling) assert each live tool advertises a valid `risk_tier` in `_meta`.

**Create:**
- `agent_core/tests/workers/test_resolve_declared_tier.py`
- `agent_core/tests/workers/test_risk_pool_wire_tier.py`
- `agent_core/tests/workers/test_registry_risk_overrides.py`

**Do NOT touch:** `discovery.py`, `tool_factory.py`, `client_pool.py`, `audit.py`, `tool_approval.py`.

### Repo: `pare-frida-mcp` (`~/Projects/pare-frida-mcp`)

**Modify:**
- `src/pare_frida_mcp/server.py` — `add_tool(..., meta={RISK_TIER_META_KEY: spec.risk_tier})`.
- `pyproject.toml` — bump the `mcp`/agent_core test dep if needed for the live conformance import.

**Create:**
- `tests/integration/test_wire_risk_tier.py` — stand up `build_server()` over stdio, run agent_core's live conformance, assert tiers ride the wire.

### Repo: `PARE` (`~/Projects/PARE`)

**Modify:**
- `pyproject.toml` — re-pin `agent_core` to `v1.6.0`.
- `pare/agent.py` — build `RiskGate` from `registry.risk_overrides()` instead of `[]`.
- `workers.yaml` — add the `frida` stdio worker (`risk_default: high`) + top-level `risk_overrides:` pins.
- `.env` — created from `.env.example`.

**Create:**
- `tests/test_frida_wire_tier_e2e.py` — prove an advertised tier is captured and used at dispatch (not the `risk_default`).

---

## Phase A — agent_core `v1.6.0`

> Work in `~/Projects/agent_core` on a branch off clean `main` (currently at `v1.5.1`). Use the repo's own `.venv`.

- [ ] **Step 0: Branch**

```bash
cd ~/Projects/agent_core
git checkout main && git pull
git checkout -b feat/per-tool-risk-tier-on-wire
.venv/bin/pytest -q   # baseline green before changes
```

### Task A1: `resolve_declared_tier` pure helper + wire-key constant

**Files:**
- Modify: `agent_core/workers/risk.py`
- Test: `agent_core/tests/workers/test_resolve_declared_tier.py`

- [ ] **Step 1: Write the failing test**

```python
# agent_core/tests/workers/test_resolve_declared_tier.py
import pytest
from agent_core.workers.risk import resolve_declared_tier, RISK_TIER_META_KEY
from agent_core.workers.types import WorkerSpec


def _spec(risk_default="medium", kind="internal"):
    return WorkerSpec(
        name="frida", transport="stdio", command="x",
        risk_default=risk_default, kind=kind,
    )


def test_meta_key_is_namespaced():
    assert RISK_TIER_META_KEY == "agent_core/risk_tier"


def test_advertised_escalates_above_floor():
    # floor=medium, advertised=critical -> critical
    out = resolve_declared_tier(_spec("medium"), "critical")
    assert out == ("critical", "wire")


def test_floor_dominates_when_advertised_is_lower():
    # floor=high, advertised=low -> high (escalate-only relative to floor)
    out = resolve_declared_tier(_spec("high"), "low")
    assert out == ("high", "floor")


def test_missing_tier_internal_is_failsafe_high():
    # internal worker, no advertised tier -> max(floor, "high")
    out = resolve_declared_tier(_spec("low"), None)
    assert out == ("high", "fallback_safe")


def test_invalid_tier_internal_is_failsafe_high():
    out = resolve_declared_tier(_spec("medium"), "lowww")
    assert out == ("high", "fallback_safe")


def test_external_mcp_uses_risk_default_unchanged():
    out = resolve_declared_tier(_spec("medium", kind="external_mcp"), "low")
    assert out == ("medium", "floor")


def test_unknown_worker_spec_none_is_high():
    out = resolve_declared_tier(None, "low")
    assert out == ("high", "unknown_worker")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest agent_core/tests/workers/test_resolve_declared_tier.py -v`
Expected: FAIL — `ImportError: cannot import name 'resolve_declared_tier'`.

- [ ] **Step 3: Implement the helper**

Add to `agent_core/workers/risk.py` (after the `_TIER_RANK` definition):

```python
from typing import Tuple
from agent_core.workers.types import WorkerSpec

RISK_TIER_META_KEY = "agent_core/risk_tier"

_VALID_TIERS = set(_TIER_RANK)


def _max_tier(a: RiskTier, b: RiskTier) -> RiskTier:
    return a if _TIER_RANK[a] >= _TIER_RANK[b] else b


def resolve_declared_tier(
    spec: WorkerSpec | None, advertised: str | None
) -> Tuple[RiskTier, str]:
    """Resolve the declared tier for a tool call from the worker-wide floor
    and the per-tool tier advertised over the wire.

    Returns (tier, tier_source) where tier_source is one of:
      "wire"          advertised tier escalated above the floor
      "floor"         floor dominated (advertised <= floor, or external_mcp)
      "fallback_safe" internal worker advertised no/invalid tier -> max(floor, high)
      "unknown_worker" spec is None -> "high"

    Rules:
      - Unknown worker (spec is None): "high" (fail safe-ish; matches prior behavior).
      - external_mcp: floor only (per-tool wire tiers are not honored; out of scope).
      - internal + valid advertised: max(floor, advertised). source="wire" if it
        escalated above the floor, else "floor".
      - internal + missing/invalid advertised: max(floor, "high"). source="fallback_safe".
    """
    if spec is None:
        return ("high", "unknown_worker")

    floor: RiskTier = spec.risk_default
    if spec.kind == "external_mcp":
        return (floor, "floor")

    if advertised in _VALID_TIERS:
        resolved = _max_tier(floor, advertised)  # type: ignore[arg-type]
        source = "wire" if _TIER_RANK[advertised] > _TIER_RANK[floor] else "floor"  # type: ignore[index]
        return (resolved, source)

    # internal worker advertised nothing usable: fail safe, never below "high"
    return (_max_tier(floor, "high"), "fallback_safe")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest agent_core/tests/workers/test_resolve_declared_tier.py -v`
Expected: PASS (7 passed).

- [ ] **Step 5: Commit**

```bash
git add agent_core/workers/risk.py agent_core/tests/workers/test_resolve_declared_tier.py
git commit -m "feat(workers): resolve_declared_tier — floor + escalate-only wire tier (fail-safe)"
```

### Task A2: `AuditEntry.tier_source` field

**Files:**
- Modify: `agent_core/workers/types.py`
- Test: covered via Task A3's risk_pool test (this step is just the additive field + a direct field test)

- [ ] **Step 1: Write the failing test**

```python
# append to agent_core/tests/workers/test_resolve_declared_tier.py
from agent_core.workers.types import AuditEntry


def test_audit_entry_accepts_tier_source():
    e = AuditEntry(
        request_id="r", worker="w", tool="t", args={},
        declared_tier="high", effective_tier="high", outcome="ok",
        latency_ms=1, session_guid="s", worker_contract_version=1,
        tier_source="wire",
    )
    assert e.tier_source == "wire"


def test_audit_entry_tier_source_defaults_none():
    e = AuditEntry(
        request_id="r", worker="w", tool="t", args={},
        declared_tier="high", effective_tier="high", outcome="ok",
        latency_ms=1, session_guid="s", worker_contract_version=1,
    )
    assert e.tier_source is None
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest agent_core/tests/workers/test_resolve_declared_tier.py -k tier_source -v`
Expected: FAIL — `ValidationError: unexpected keyword 'tier_source'` (Pydantic forbids unknown fields).

- [ ] **Step 3: Add the field**

In `agent_core/workers/types.py`, inside `class AuditEntry`, after `detail: str | None = None`:

```python
    tier_source: str | None = None
    """Provenance of declared_tier: "wire" | "floor" | "fallback_safe" |
    "unknown_worker" | None (pre-v1.6 entries). Forensic honesty: lets an
    auditor tell a low-tier dispatch advertised-low apart from a floor default."""
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest agent_core/tests/workers/test_resolve_declared_tier.py -k tier_source -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add agent_core/workers/types.py agent_core/tests/workers/test_resolve_declared_tier.py
git commit -m "feat(workers): add nullable AuditEntry.tier_source provenance field"
```

### Task A3: Pool captures wire tiers + resolves declared tier at dispatch

**Files:**
- Modify: `agent_core/workers/risk_pool.py`
- Test: `agent_core/tests/workers/test_risk_pool_wire_tier.py`

- [ ] **Step 1: Write the failing test**

```python
# agent_core/tests/workers/test_risk_pool_wire_tier.py
import pytest
from agent_core.workers.risk import RiskGate, RISK_TIER_META_KEY
from agent_core.workers.risk_pool import RiskAwareToolPool
from agent_core.workers.tool_approval import ToolApprovalRegistry
from agent_core.workers.types import WorkerSpec


class _Tool:
    def __init__(self, name, tier=None):
        self.name = name
        self.meta = {RISK_TIER_META_KEY: tier} if tier is not None else None


class _ListResult:
    def __init__(self, tools):
        self.tools = tools


class _Ok:
    isError = False
    content = []


class _FakeInner:
    """Stand-in MCPClientPool: records call_tool, serves canned list_tools."""
    def __init__(self, tools):
        self._tools = tools
        self.calls = []

    async def list_tools(self, worker):
        return _ListResult(self._tools)

    async def call_tool(self, worker, tool, arguments):
        self.calls.append((worker, tool))
        return _Ok()

    async def close_all(self):
        pass


class _Audit:
    def __init__(self):
        self.entries = []

    def append(self, entry):
        self.entries.append(entry)


def _pool(inner, spec, overrides=None):
    return RiskAwareToolPool(
        inner=inner,
        specs={spec.name: spec},
        risk_gate=RiskGate(overrides=overrides or []),
        approval_registry=ToolApprovalRegistry(),
        audit_log=_Audit(),
    )


@pytest.mark.asyncio
async def test_low_advertised_with_low_floor_auto_executes_and_records_source():
    spec = WorkerSpec(name="frida", transport="stdio", command="x", risk_default="low")
    inner = _FakeInner([_Tool("list_devices", "low")])
    pool = _pool(inner, spec)
    await pool.list_tools("frida")            # populate cache
    res = await pool.call_tool("frida", "list_devices", {})
    assert inner.calls == [("frida", "list_devices")]   # auto-executed (no prompt)
    assert pool._audit.entries[-1].declared_tier == "low"
    assert pool._audit.entries[-1].tier_source == "floor"


@pytest.mark.asyncio
async def test_critical_advertised_blocks_without_approval_channel():
    # low floor, but execute_script advertises critical -> declared critical ->
    # requires approval; with no send channel it must NOT auto-execute.
    spec = WorkerSpec(name="frida", transport="stdio", command="x", risk_default="low")
    inner = _FakeInner([_Tool("execute_script", "critical")])
    pool = _pool(inner, spec)
    await pool.list_tools("frida")
    res = await pool.call_tool("frida", "execute_script", {"source": "x"})
    assert inner.calls == []                  # blocked, never dispatched
    assert getattr(res, "isError", False) is True
    assert pool._audit.entries[-1].declared_tier == "critical"
    assert pool._audit.entries[-1].tier_source == "wire"


@pytest.mark.asyncio
async def test_missing_tier_is_failsafe_high_even_with_low_floor():
    spec = WorkerSpec(name="frida", transport="stdio", command="x", risk_default="low")
    inner = _FakeInner([_Tool("untagged_tool", None)])   # advertises no tier
    pool = _pool(inner, spec)
    await pool.list_tools("frida")
    res = await pool.call_tool("frida", "untagged_tool", {})
    assert inner.calls == []                  # high -> blocked, fail-safe
    assert pool._audit.entries[-1].declared_tier == "high"
    assert pool._audit.entries[-1].tier_source == "fallback_safe"


@pytest.mark.asyncio
async def test_call_before_discovery_is_failsafe_high():
    # cache never populated (no list_tools call) -> treat as missing -> high
    spec = WorkerSpec(name="frida", transport="stdio", command="x", risk_default="low")
    inner = _FakeInner([_Tool("list_devices", "low")])
    pool = _pool(inner, spec)
    res = await pool.call_tool("frida", "list_devices", {})
    assert inner.calls == []
    assert pool._audit.entries[-1].tier_source == "fallback_safe"
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest agent_core/tests/workers/test_risk_pool_wire_tier.py -v`
Expected: FAIL — declared tier still comes from `spec.risk_default`; cache attribute and `tier_source` audit field unset.

- [ ] **Step 3: Implement capture + resolution**

In `agent_core/workers/risk_pool.py`:

(a) Import the helper and constant at top:

```python
from agent_core.workers.risk import RiskGate, RISK_TIER_META_KEY, resolve_declared_tier
```

(b) In `__init__`, add the cache (after `self._session_approved = set()`):

```python
        self._tool_tiers: dict[tuple[str, str], str] = {}
```

(c) Replace `list_tools` to capture tiers (keyed by bare tool name) before returning:

```python
    async def list_tools(self, worker: str):
        result = await self._inner.list_tools(worker)
        for tool in getattr(result, "tools", []) or []:
            meta = getattr(tool, "meta", None) or {}
            tier = meta.get(RISK_TIER_META_KEY) if isinstance(meta, dict) else None
            self._tool_tiers[(worker, tool.name)] = tier
        return result
```

(d) In `call_tool`, replace the declared-tier line. Change:

```python
        spec = self._specs.get(worker)
        declared = spec.risk_default if spec else "high"  # unknown worker -> fail safe-ish
        decision = self._gate.evaluate(worker=worker, tool=tool, declared_tier=declared)
```

to:

```python
        spec = self._specs.get(worker)
        # A tool we never saw in discovery is treated as "no advertised tier"
        # (None) -> resolve_declared_tier fails safe to "high" for internal workers.
        advertised = self._tool_tiers.get((worker, tool))
        declared, tier_source = resolve_declared_tier(spec, advertised)
        decision = self._gate.evaluate(worker=worker, tool=tool, declared_tier=declared)
```

(e) Thread `tier_source` through to the audit emit. In `call_tool`, change both the
`_await_operator(...)` blocked-path emit and the final `_execute_and_audit(...)` call to
pass `tier_source`. The simplest non-invasive route: store it on a local and pass it into
`_execute_and_audit`, and into `_await_operator` (which calls `_emit` on deny/timeout).

Update `_execute_and_audit` signature and its two `_emit` calls to accept and forward
`tier_source`; update `_await_operator` likewise; and update `_emit`:

```python
    def _emit(self, worker, tool, snapshot, declared, effective, latency_ms,
              outcome, override_reason, detail, tier_source=None):
        self._audit.append(AuditEntry(
            request_id=uuid.uuid4().hex,
            worker=worker, tool=tool, args=snapshot,
            declared_tier=declared, effective_tier=effective,
            override_reason=override_reason, detail=detail, outcome=outcome,
            latency_ms=latency_ms, session_guid="pending",
            worker_contract_version=1, tier_source=tier_source,
        ))
```

> Implementer note: `_await_operator`, `_execute_and_audit`, and `_emit` are all private
> to `RiskAwareToolPool`. Add `tier_source` as a trailing keyword arg with a `None` default
> on each so any caller not yet updated keeps working, then thread the real value from
> `call_tool`. Run the existing `test_risk_pool.py` after — signatures stay backward-compatible.

- [ ] **Step 4: Run to verify pass (new + existing pool tests)**

Run: `.venv/bin/pytest agent_core/tests/workers/test_risk_pool_wire_tier.py agent_core/tests/workers/test_risk_pool.py -v`
Expected: PASS — new file 4 passed; existing `test_risk_pool.py` still all green.

- [ ] **Step 5: Commit**

```bash
git add agent_core/workers/risk_pool.py agent_core/tests/workers/test_risk_pool_wire_tier.py
git commit -m "feat(workers): RiskAwareToolPool captures wire risk_tier, resolves declared tier fail-safe"
```

### Task A4: `WorkerRegistry` parses top-level `risk_overrides:`

**Files:**
- Modify: `agent_core/workers/registry.py`
- Test: `agent_core/tests/workers/test_registry_risk_overrides.py`

- [ ] **Step 1: Write the failing test**

```python
# agent_core/tests/workers/test_registry_risk_overrides.py
from agent_core.workers.registry import WorkerRegistry


def _write(tmp_path, text):
    p = tmp_path / "workers.yaml"
    p.write_text(text)
    return p


def test_risk_overrides_parsed_as_pattern_tier_pairs(tmp_path):
    path = _write(tmp_path, """
workers:
  frida:
    command: x
    transport: stdio
    risk_default: high
risk_overrides:
  - ["frida_execute_script", "critical"]
  - ["frida_write_memory", "high"]
""")
    reg = WorkerRegistry.load(path)
    assert reg.risk_overrides() == [
        ("frida_execute_script", "critical"),
        ("frida_write_memory", "high"),
    ]


def test_risk_overrides_absent_returns_empty(tmp_path):
    path = _write(tmp_path, """
workers:
  frida:
    command: x
    transport: stdio
    risk_default: high
""")
    reg = WorkerRegistry.load(path)
    assert reg.risk_overrides() == []
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest agent_core/tests/workers/test_registry_risk_overrides.py -v`
Expected: FAIL — `AttributeError: 'WorkerRegistry' object has no attribute 'risk_overrides'`.

- [ ] **Step 3: Implement parse + accessor**

In `agent_core/workers/registry.py`:

```python
    def __init__(self) -> None:
        self._workers: dict[str, WorkerSpec] = {}
        self._risk_overrides: list[tuple[str, str]] = []
```

In `load`, after the worker loop and before `return reg`:

```python
        for pair in data.get("risk_overrides", []) or []:
            if not (isinstance(pair, (list, tuple)) and len(pair) == 2):
                raise ValueError(
                    f"risk_overrides entry must be a [pattern, tier] pair, got {pair!r}"
                )
            reg._risk_overrides.append((str(pair[0]), str(pair[1])))
```

Add the accessor:

```python
    def risk_overrides(self) -> list[tuple[str, str]]:
        """Top-level [pattern, tier] override pairs for RiskGate (override-up only)."""
        return list(self._risk_overrides)
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest agent_core/tests/workers/test_registry_risk_overrides.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add agent_core/workers/registry.py agent_core/tests/workers/test_registry_risk_overrides.py
git commit -m "feat(workers): WorkerRegistry parses top-level risk_overrides into pattern/tier pairs"
```

### Task A5: Live stdio conformance asserts tier on the wire

**Files:**
- Modify: `agent_core/workers/conformance.py`
- Test: extend the existing conformance test module (mirror its current pattern)

- [ ] **Step 1: Read the current live-conformance helper**

Run: `.venv/bin/python -c "import inspect; from agent_core.workers import conformance as c; print(inspect.getsource(c.assert_stdio_conformance))"`
Expected: shows it checks `tool.name` and `inputSchema` only — note the exact tool-iteration shape so the new assertion matches it.

- [ ] **Step 2: Write the failing test**

Add to the existing conformance test file (the one that exercises `assert_stdio_conformance`). If a live stdio fixture already exists, reuse it; otherwise model on `tests/integration/test_stdio_handshake.py`:

```python
import pytest


@pytest.mark.asyncio
async def test_stdio_conformance_requires_risk_tier_on_wire(spec_missing_tier):
    # A worker whose tools carry NO _meta risk_tier must fail conformance.
    # `spec_missing_tier` is a WorkerSpec (transport="stdio") pointing at a
    # throwaway stdio server built WITHOUT meta= on add_tool.
    # assert_stdio_conformance is async — it MUST be awaited or it never raises.
    with pytest.raises(AssertionError, match="risk_tier"):
        await assert_stdio_conformance(spec_missing_tier)
```

> Note: `assert_stdio_conformance(spec)` takes a `WorkerSpec` and spawns the subprocess
> itself. The fixture must expose a stdio server (a tiny script that registers a tool with
> no `meta=`) as a `WorkerSpec(transport="stdio", command=...)`. If building a separate
> throwaway command is heavy, an equivalent unit test against the per-tool loop logic
> (feed it a fake `tool` object with `.meta = None`) covers the same assertion.

- [ ] **Step 3: Run to verify failure**

Run: `.venv/bin/pytest agent_core/tests/workers/ -k risk_tier_on_wire -v`
Expected: FAIL — current `assert_stdio_conformance` does not look at `_meta`, so no `AssertionError` is raised.

- [ ] **Step 4: Add the assertion**

In `assert_stdio_conformance` (and the streamable_http sibling), inside the per-tool loop, after the existing name/inputSchema asserts:

```python
        meta = getattr(tool, "meta", None) or {}
        tier = meta.get(RISK_TIER_META_KEY) if isinstance(meta, dict) else None
        assert tier in {"low", "medium", "high", "critical"}, (
            f"tool {tool.name!r} must advertise a valid {RISK_TIER_META_KEY} "
            f"in _meta over the wire, got {tier!r}"
        )
```

Add the import at the top of `conformance.py`:

```python
from agent_core.workers.risk import RISK_TIER_META_KEY
```

- [ ] **Step 5: Run to verify pass + full agent_core suite**

Run: `.venv/bin/pytest agent_core/tests/ -q`
Expected: PASS — the new conformance test green; the full suite green (no signature regressions).

- [ ] **Step 6: Commit**

```bash
git add agent_core/workers/conformance.py agent_core/tests/
git commit -m "feat(workers): live conformance asserts per-tool risk_tier rides _meta on the wire"
```

### Task A6: Release `v1.6.0`

- [ ] **Step 1: Update CHANGELOG / version**

Bump the version in `agent_core`'s `pyproject.toml` (or wherever the project version lives — match how `v1.5.1` was set) to `1.6.0`. Add a CHANGELOG entry:

```
## v1.6.0
feat(workers): per-tool risk_tier now flows over the MCP wire (_meta key
"agent_core/risk_tier"). RiskAwareToolPool captures it during list_tools and
resolves the declared tier as max(risk_default_floor, advertised), fail-safe
to "high" for internal workers that advertise no/invalid tier. risk_default
is now a FLOOR, not the sole signal. WorkerRegistry parses top-level
risk_overrides:. AuditEntry gains tier_source provenance. BEHAVIOR CHANGE for
kind="internal" workers that advertise _meta tiers — other ecosystem agents
should review before pinning. external_mcp + non-advertising workers unchanged.
```

- [ ] **Step 2: Full suite + merge + tag**

```bash
cd ~/Projects/agent_core
.venv/bin/pytest -q
git add -A && git commit -m "chore(release): v1.6.0 — per-tool risk_tier on the wire"
git checkout main && git merge --no-ff feat/per-tool-risk-tier-on-wire
git tag v1.6.0
git push origin main --tags
```

Expected: full suite green; tag `v1.6.0` pushed.

---

## Phase B — `pare-frida-mcp` emits the tier

> Work in `~/Projects/pare-frida-mcp`. It is editable-installed into PARE's venv, so use `/home/edible/Projects/PARE/.venv/bin/python` for cross-repo conformance, or the worker's own dev install.

### Task B1: `server.py` emits `_meta` risk_tier

**Files:**
- Modify: `src/pare_frida_mcp/server.py`
- Test: `tests/integration/test_wire_risk_tier.py`

- [ ] **Step 1: Write the failing test (live stdio round-trip)**

```python
# tests/integration/test_wire_risk_tier.py
import pytest
from pare_frida_mcp.server import build_server
from pare_frida_mcp.contract import TOOL_SPECS
from agent_core.workers.risk import RISK_TIER_META_KEY


@pytest.mark.asyncio
async def test_every_tool_advertises_its_contract_tier_in_meta():
    server = build_server()
    tools = await server.list_tools()          # FastMCP in-process tool list
    by_name = {t.name: t for t in tools}
    expected = {s.name: s.risk_tier for s in TOOL_SPECS}
    for name, tier in expected.items():
        meta = getattr(by_name[name], "meta", None) or {}
        assert meta.get(RISK_TIER_META_KEY) == tier, (
            f"{name} should advertise {tier} in _meta, got {meta!r}"
        )
```

> **VERIFIED (mcp 1.27.1):** `await server.list_tools()` returns a `list[mcp.types.Tool]`.
> Each tool exposes its `_meta` payload on the Python attribute `tool.meta` (pydantic field
> `meta`, wire alias `_meta`). So `meta = (await server.list_tools())[i].meta` yields the dict
> `{"agent_core/risk_tier": "<tier>"}`. Use `server.list_tools()`, read `.meta`.

- [ ] **Step 2: Run to verify failure**

Run: `cd ~/Projects/pare-frida-mcp && /home/edible/Projects/PARE/.venv/bin/pytest tests/integration/test_wire_risk_tier.py -v`
Expected: FAIL — `server.py` passes no `meta`, so `meta.get(...)` is `None`.

- [ ] **Step 3: Emit the tier**

In `src/pare_frida_mcp/server.py`, change the registration loop:

```python
from agent_core.workers.risk import RISK_TIER_META_KEY

def build_server() -> FastMCP:
    server = FastMCP("pare-frida-mcp")
    for spec in TOOL_SPECS:
        handler = getattr(tools_mod, spec.name, None)
        if handler is None:
            handler = _stub_for(spec.name)
        server.add_tool(
            handler,
            name=spec.name,
            description=spec.description,
            meta={RISK_TIER_META_KEY: spec.risk_tier},
        )
    return server
```

> **VERIFIED (mcp 1.27.1):** `FastMCP.add_tool` accepts `meta: dict[str, Any] | None`
> (forwarded to `Tool.from_function(..., meta=...)`). `FastMCP.list_tools` serializes it as
> `MCPTool(..., _meta=info.meta)`, and the value round-trips through the wire alias back to
> `tool.meta` on the client. The `meta={RISK_TIER_META_KEY: spec.risk_tier}` call below is correct as written.

- [ ] **Step 4: Run to verify pass**

Run: `/home/edible/Projects/PARE/.venv/bin/pytest tests/integration/test_wire_risk_tier.py -v`
Expected: PASS (1 passed; every tool's tier matches its contract).

- [ ] **Step 5: Commit**

```bash
cd ~/Projects/pare-frida-mcp
git add src/pare_frida_mcp/server.py tests/integration/test_wire_risk_tier.py
git commit -m "feat(server): emit per-tool risk_tier in _meta for agent_core risk gating"
```

### Task B2: Run agent_core's live stdio conformance against the worker

**Files:**
- Test: extend `tests/integration/test_wire_risk_tier.py`

- [ ] **Step 1: Write the test**

```python
# append to tests/integration/test_wire_risk_tier.py
from agent_core.workers.conformance import assert_stdio_conformance
from agent_core.workers.types import WorkerSpec


@pytest.mark.asyncio
async def test_worker_passes_live_stdio_conformance():
    # The real worker, spawned over stdio, must satisfy agent_core's wire
    # conformance — including the new risk_tier-on-_meta assertion (Task A5).
    # assert_stdio_conformance spawns the subprocess itself via the WorkerSpec.
    spec = WorkerSpec(
        name="frida",
        transport="stdio",
        command="pare-frida-mcp",   # console-script entry point of this package
        risk_default="high",
    )
    await assert_stdio_conformance(spec)   # raises AssertionError on any gap
```

> **VERIFIED (agent_core v1.5.1 + Task A5):** `assert_stdio_conformance(spec: WorkerSpec)`
> takes a `WorkerSpec` with `transport="stdio"` and a `command`; it builds `MCPClient.from_spec(spec)`,
> spawns the subprocess, runs the handshake, calls `client.list_tools()`, and iterates
> `result.tools` (each an `mcp.types.Tool` with `.name`, `.inputSchema`, `.meta`). It does **not**
> take a server object — pass the spec. The `command` must resolve on `PATH` (the package's
> `pare-frida-mcp` console script); ensure the venv running pytest has the package installed.

- [ ] **Step 2: Run**

Run: `/home/edible/Projects/PARE/.venv/bin/pytest tests/integration/test_wire_risk_tier.py -v`
Expected: PASS — worker conforms, tiers present on the wire.

- [ ] **Step 3: Full worker suite + commit**

```bash
make test
git add tests/integration/test_wire_risk_tier.py
git commit -m "test(integration): assert worker passes agent_core live wire conformance"
```

---

## Phase C — PARE wiring

> Work in `~/Projects/PARE`. `agent_core` is a git pin; `pare-frida-mcp` is already editable-installed.

### Task C1: Re-pin agent_core to v1.6.0

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Bump the pin**

In `pyproject.toml`, change:

```
"agent_core @ git+https://github.com/EdibleTuber/agent_core.git@v1.5.1",
```

to `@v1.6.0`.

- [ ] **Step 2: Reinstall + baseline**

```bash
cd ~/Projects/PARE
.venv/bin/pip install -e ".[dev]" --upgrade
.venv/bin/python -c "import agent_core; from agent_core.workers.risk import resolve_declared_tier; print('v1.6.0 OK')"
.venv/bin/pytest tests/ -q
```

Expected: import OK; existing PARE suite still `17 passed, 3 skipped` (no regressions from the bump).

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml
git commit -m "chore(deps): bump agent_core pin to v1.6.0 (per-tool wire risk tiers)"
```

### Task C2: Build RiskGate from `risk_overrides`

**Files:**
- Modify: `pare/agent.py:45-56`
- Test: `tests/test_risk_overrides_wired.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_risk_overrides_wired.py
from agent_core.workers.registry import WorkerRegistry


def test_pare_passes_registry_overrides_into_riskgate(tmp_path, monkeypatch):
    wy = tmp_path / "workers.yaml"
    wy.write_text("""
workers:
  frida:
    command: x
    transport: stdio
    risk_default: high
risk_overrides:
  - ["frida_execute_script", "critical"]
""")
    reg = WorkerRegistry.load(wy)
    # The gate built from these overrides must escalate a low declared tier.
    from agent_core.workers.risk import RiskGate
    gate = RiskGate(overrides=reg.risk_overrides())
    decision = gate.evaluate(worker="frida", tool="execute_script", declared_tier="low")
    assert decision.effective_tier == "critical"
```

- [ ] **Step 2: Run to verify it passes at the unit level, then assert the wiring gap**

Run: `.venv/bin/pytest tests/test_risk_overrides_wired.py -v`
Expected: PASS (this validates the mechanism). The real change is in `agent.py` — verify the
gap by reading `pare/agent.py:52`: it currently constructs `RiskGate(overrides=[])`.

- [ ] **Step 3: Wire it in `pare/agent.py`**

Change the `setup` block (currently lines 45-56):

```python
        registry = WorkerRegistry.load(self.config.workers_yaml_path)
        specs = registry.all()
        self._worker_specs = specs
        self.mcp_pool = MCPClientPool(specs)
        self.tool_pool = RiskAwareToolPool(
            inner=self.mcp_pool,
            specs={s.name: s for s in specs},
            risk_gate=RiskGate(overrides=registry.risk_overrides()),
            approval_registry=self.tool_approval_registry,
            audit_log=AuditLog(self.config.audit_dir),
            send_message=None,
        )
```

(Only the `risk_gate=` line changes: `overrides=[]` → `overrides=registry.risk_overrides()`.)

- [ ] **Step 4: Run PARE suite**

Run: `.venv/bin/pytest tests/ -q`
Expected: `18 passed, 3 skipped` (new test + existing).

- [ ] **Step 5: Commit**

```bash
git add pare/agent.py tests/test_risk_overrides_wired.py
git commit -m "feat(agent): build RiskGate from workers.yaml risk_overrides (mandatory operator pins)"
```

### Task C3: End-to-end — advertised tier captured & used at dispatch

**Files:**
- Test: `tests/test_frida_wire_tier_e2e.py`

This is the gate that must be green before Task C5 lowers the floor.

- [ ] **Step 1: Write the e2e test**

```python
# tests/test_frida_wire_tier_e2e.py
import pytest
from agent_core.workers.risk import RiskGate, RISK_TIER_META_KEY, resolve_declared_tier
from agent_core.workers.risk_pool import RiskAwareToolPool
from agent_core.workers.tool_approval import ToolApprovalRegistry
from agent_core.workers.types import WorkerSpec


class _Tool:
    def __init__(self, name, tier):
        self.name = name
        self.meta = {RISK_TIER_META_KEY: tier}


class _ListResult:
    def __init__(self, tools): self.tools = tools


class _Ok:
    isError = False
    content = []


class _Inner:
    def __init__(self, tools): self._t = tools; self.calls = []
    async def list_tools(self, w): return _ListResult(self._t)
    async def call_tool(self, w, t, a): self.calls.append((w, t)); return _Ok()
    async def close_all(self): pass


class _Audit:
    def __init__(self): self.entries = []
    def append(self, e): self.entries.append(e)


@pytest.mark.asyncio
async def test_advertised_critical_gates_even_with_low_floor_and_records_wire_source():
    """Floor=low, but execute_script advertises critical over the wire ->
    declared critical -> blocked without approval. Proves the wire tier
    (not risk_default) drives the decision."""
    spec = WorkerSpec(name="frida", transport="stdio", command="x", risk_default="low")
    inner = _Inner([_Tool("execute_script", "critical"), _Tool("list_devices", "low")])
    pool = RiskAwareToolPool(
        inner=inner, specs={"frida": spec},
        risk_gate=RiskGate(overrides=[]),
        approval_registry=ToolApprovalRegistry(),
        audit_log=_Audit(),
    )
    await pool.list_tools("frida")
    blocked = await pool.call_tool("frida", "execute_script", {"source": "x"})
    assert ("frida", "execute_script") not in inner.calls
    assert pool._audit.entries[-1].declared_tier == "critical"
    assert pool._audit.entries[-1].tier_source == "wire"


@pytest.mark.asyncio
async def test_operator_pin_forces_critical_even_if_worker_lies():
    """A compromised worker advertises execute_script as low; the operator
    pin frida_execute_script->critical must still gate it."""
    spec = WorkerSpec(name="frida", transport="stdio", command="x", risk_default="low")
    inner = _Inner([_Tool("execute_script", "low")])   # worker lies
    pool = RiskAwareToolPool(
        inner=inner, specs={"frida": spec},
        risk_gate=RiskGate(overrides=[("frida_execute_script", "critical")]),
        approval_registry=ToolApprovalRegistry(),
        audit_log=_Audit(),
    )
    await pool.list_tools("frida")
    blocked = await pool.call_tool("frida", "execute_script", {"source": "x"})
    assert ("frida", "execute_script") not in inner.calls
    assert pool._audit.entries[-1].effective_tier == "critical"
    assert pool._audit.entries[-1].override_reason is not None
```

- [ ] **Step 2: Run**

Run: `.venv/bin/pytest tests/test_frida_wire_tier_e2e.py -v`
Expected: PASS (2 passed) — wire tier drives gating; operator pin overrides a lying worker.

- [ ] **Step 3: Commit**

```bash
git add tests/test_frida_wire_tier_e2e.py
git commit -m "test(e2e): advertised wire tier + operator pin both gate frida dispatch"
```

### Task C4: Register the frida worker (floor = high during rollout)

**Files:**
- Modify: `workers.yaml`
- Create: `.env`

- [ ] **Step 1: Add the worker + mandatory pins to `workers.yaml`**

Append under `workers:` and add a top-level `risk_overrides:` block:

```yaml
  frida:
    command: pare-frida-mcp
    transport: stdio
    risk_default: high          # FLOOR during rollout — lower to medium only after Task C3 is green
    capability_tags: [mobile, dynamic, android, frida]

# Operator policy: authoritative ceiling above whatever the worker self-declares.
# These survive a buggy/compromised worker that under-reports its tiers.
risk_overrides:
  - ["frida_execute_script", "critical"]
  - ["frida_write_memory", "high"]
```

- [ ] **Step 2: Create `.env`**

```bash
cd ~/Projects/PARE
cp .env.example .env
```

Then review `.env` — confirm `PARE_INFERENCE_URL`, `PARE_MODEL`, `PARE_VAULT_PATH`, `PARE_COLLECTION_ID` match this host's lab setup. (Defaults assume `192.168.1.14:11434`.)

- [ ] **Step 3: Daemon boot smoke (manual)**

In one terminal: `.venv/bin/python -m pare`
In another: `.venv/bin/python -m agent_core.adapters.cli`

Confirm in the daemon log that the `frida` worker is discovered and its tools register
(`registered tool frida_list_devices from worker frida`, etc.). Then ask the agent to do
something benign (e.g. list frida devices). Expect:
- `frida_list_devices` (advertised low, floor high) → prompts `[y/n/j/a]` (floor dominates during rollout).
- A request that would call `frida_execute_script` → prompts and, on the justification path, is `critical`.

> No device needs to be attached for discovery + the gating prompt; tool execution may
> return a "no device" error, which is fine — the point here is that gating fires.

- [ ] **Step 4: Commit (workers.yaml only — never commit `.env`)**

```bash
git add workers.yaml
git commit -m "feat(workers): register in-house frida stdio worker + mandatory risk_override pins"
```

Confirm `.env` is git-ignored (`git status` must not list it; `.gitignore` should cover it — add it if not).

### Task C5: Lower the floor to `medium` (GATED on Task C3 green + C4 smoke)

> **DEFERRED by Shane (2026-05-30): keep the frida floor at `high` for now** — every frida tool
> prompts until the wire tiers prove out in real use. Do NOT lower the floor yet.
> When revisiting: the security review flagged that at `medium`, a worker advertising `low` gets
> auto-execute for `attach`/`load_script`/`java_hook`/`read_memory` (only `execute_script`+`write_memory`
> are pinned). Re-audit those and consider pinning `attach`/`load_script` before lowering. Never set `low`.

> Do this ONLY after Task C3 passes and the Task C4 smoke confirmed gating fires. Never set `low`.

**Files:**
- Modify: `workers.yaml`

- [ ] **Step 1: Lower the floor**

Change the frida worker's `risk_default: high` → `risk_default: medium`.

Effect: advertised `low`/`medium` tools (device/process enum, capture reads) auto-execute
(audited, no prompt); advertised `high` (`write_memory`) and `critical` (`execute_script`)
self-gate from the contract; the operator pins still force those two regardless; a
missing/mis-tagged tier still fails safe to `high`.

- [ ] **Step 2: Re-run the smoke**

Repeat Task C4 Step 3. Expect `frida_list_devices` to now auto-execute (no prompt), while
`execute_script`/`write_memory` still prompt.

- [ ] **Step 3: Commit**

```bash
git add workers.yaml
git commit -m "chore(workers): lower frida risk_default floor to medium after wire-tier verification"
```

---

## Self-Review

**Spec coverage** (each panel must-fix → task):
- Fail-OPEN fallback (blocking #1) → Task A1 `resolve_declared_tier` (`max(floor, advertised)`, fail-safe high) + Task A3 tests `test_missing_tier_is_failsafe_high`, `test_call_before_discovery_is_failsafe_high`.
- Empty override layer (blocking #2) → Task A4 (`risk_overrides` parse) + Task C2 (wire into RiskGate) + Task C4 (mandatory pins) + Task C3 `test_operator_pin_forces_critical_even_if_worker_lies`.
- Plumbing gap (discovery drops `_meta`) → Task A3 captures in `list_tools` (discovery/tool_factory untouched).
- `_meta` not `annotations` → Tasks A1/A5/B1 all use `RISK_TIER_META_KEY` in `_meta`.
- Conformance green while wire empty → Task A5 (live conformance asserts tier) + Task B2 (worker runs it).
- Audit provenance → Task A2 (`tier_source` field) + Task A3 (threaded through `_emit`).
- Sequencing (don't drop to low until proven) → Task C4 floor=high → Task C5 gated lower to medium.
- external_mcp not preserved-behavior → Task A1 treats external_mcp as floor-only, documented; no fabricated bump.

**Placeholder scan:** No "TBD"/"handle edge cases"/"similar to Task N" — every code step shows real code. The three formerly-soft API shapes were **VERIFIED against the installed `mcp` 1.27.1 + agent_core v1.5.1** (2026-05-29) and the callouts now state confirmed facts: (1) `FastMCP.add_tool(meta=...)` exists and forwards to `Tool.from_function`; (2) `FastMCP.list_tools()` serializes it as `_meta` and it round-trips to `tool.meta` on the client; (3) `assert_stdio_conformance(spec: WorkerSpec)` spawns the subprocess itself — Task B2 was corrected from passing a server object to passing a `WorkerSpec`, and the A5 failing-test was corrected to `await` the async helper.

**Type consistency:** `RISK_TIER_META_KEY` (= `"agent_core/risk_tier"`), `resolve_declared_tier(spec, advertised) -> (RiskTier, str)`, `WorkerRegistry.risk_overrides() -> list[tuple[str, str]]`, `AuditEntry.tier_source: str | None`, and `RiskGate(overrides=...)` are used identically across all tasks and repos.
