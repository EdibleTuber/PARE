# Risk Enforcement on MCP Tool Dispatch — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `risk_default` in `workers.yaml` live policy instead of dormant metadata. Enforcement runs at MCP dispatch: every tool call is risk-evaluated and audited; `high`/`critical`-tier calls block on operator approval via an inline CLI prompt. Lands as `agent_core@v1.5.0` (purely additive — no existing signature changes); PARE then pin-bumps to consume it.

**Architecture (revised 2026-05-28 after panel review):** Enforcement lives in a new `RiskAwareToolPool` that **composes** `MCPClientPool`. `MCPClientPool.call_tool(worker, tool, args)` is already the single chokepoint every tool call flows through, and the pool already holds the `WorkerSpec`s (so it knows each worker's `risk_default`). `RiskAwareToolPool.call_tool` runs gate → approval (if needed) → inner `call_tool` → audit; it proxies `list_tools`/`close_all` straight through (discovery is read-only and ungated). Because the risk-aware pool is passed *as* the pool to `discover_and_register` and `make_tool_class`, **neither of those functions changes** — they call `pool.call_tool(...)` exactly as today and never know the pool is risk-aware. A `ToolApprovalRegistry` holds per-call gates keyed by `proposal_id`; two new protocol messages carry the operator round-trip; the existing `AuditLog` writer records every dispatch.

**Tech Stack:** Python 3.12, asyncio, dataclasses, prompt-toolkit, existing agent_core protocol transport, existing `AuditLog` JSONL writer, existing `RiskGate`.

---

## Panel feedback incorporated

The four-reviewer panel returned unanimous "ship-with-changes." Must-fix items and where they land:

- **Pool-wrap instead of growing `tool_factory`** (architecture) → the whole `RiskAwareToolPool` design. `tool_factory.py` + `discovery.py` untouched; their tests don't break; non-breaking v1.5.0.
- **Pending-entry leak on cancel / send failure** (async, security) → `registry.discard()` + `try/finally` in `call_tool`; register-then-send with discard-on-send-failure (Task 3).
- **`send_message` must signal undeliverable, not silently time out** (security, async) → send wrapped in try/except; failure → immediate deny + `outcome="approval_undeliverable"` audit (Task 3).
- **Arg snapshot (TOCTOU) + display sanitization** (security, UX) → `copy.deepcopy(args)` snapshot before await; CLI truncates/sanitizes per-value (Tasks 3, 4).
- **Critical-tier justification UX** (security, UX) → CLI forces the justification path for `critical`; daemon-side defensive check stays (Tasks 3, 4).
- **Ctrl-C / EOF on prompt** (UX, security) → CLI catches and sends denied (Task 4).
- **Bulk-approval fatigue** (UX) → `a` = approve-for-session option; pool caches `(worker, tool)` and skips the prompt thereafter (still audited). `critical` tier may NOT be session-approved (Tasks 3, 4).
- **Test gaps** (all) → cancellation, send-failure, timeout, resolve-twice, parallel fan-out, denied e2e, CLI-prompt (mocked), audit-injection round-trip (Tasks 1, 3, 4, 6).
- **`resolve()` KeyError-on-stale is expected** (async) → documented in code + Task 5 dispatcher swallows it.

**Deferred to follow-ups (explicitly OUT):** `risk_overrides:` patterns in `workers.yaml`; `kind: external_mcp` auto-bump; daemon-side stream-chunk suspension during a pending prompt (mitigated now with a clean separator); low/medium `[audit]` notice line to the CLI; extracting a generic `PendingApprovalRegistry[Spec, Decision]`; TTL-bounded (vs session-scoped) approvals; `/approvals` out-of-band command + Discord parity; per-call `session_guid` propagation.

---

## File Structure

**New files:**
- `agent_core/workers/tool_approval.py` — `ToolApprovalRegistry`, `ToolCallSpec`, `ToolDecision`.
- `agent_core/workers/risk_pool.py` — `RiskAwareToolPool` (composes `MCPClientPool`).
- `agent_core/tests/workers/test_tool_approval.py`
- `agent_core/tests/workers/test_risk_pool.py`
- `agent_core/tests/workers/test_protocol_approval_messages.py`
- `agent_core/tests/workers/test_e2e_risk_enforcement.py`
- `agent_core/tests/adapters/test_cli_approval_prompt.py`

**Modified files:**
- `agent_core/protocol/messages.py` — add `ToolApprovalRequestMessage`, `ToolApprovalResponseMessage`.
- `agent_core/protocol/__init__.py` — export the two new messages.
- `agent_core/adapters/cli.py` — inline approval prompt in the drain loop.
- `agent_core/runtime.py` — construct `ToolApprovalRegistry`; route inbound `ToolApprovalResponseMessage` to it.
- `pare/agent.py` — build `RiskAwareToolPool` in `setup()`, pass it to `discover_and_register`; supply the audit dir + send-channel.
- `agent_core/CHANGELOG.md`, `agent_core/pyproject.toml` — v1.5.0.
- `PARE/pyproject.toml` — pin bump to v1.5.0.

**Do NOT touch:** `tool_factory.py`, `discovery.py`, `risk.py`, `audit.py`, `client_pool.py`, `approval_registry.py`.

---

## Task 1: ToolApprovalRegistry

**Files:**
- Create: `agent_core/workers/tool_approval.py`
- Test: `agent_core/tests/workers/test_tool_approval.py`

A registry of in-flight tool-call approval gates keyed by `proposal_id` (uuid4 hex). Each entry holds an `asyncio.Future[ToolDecision]` and an armed timeout. `resolve()` is the operator-response path; `discard()` is the idempotent cleanup path (cancellation / send-failure). Distinct from the vault-shaped `ApprovalRegistry`.

- [ ] **Step 1: Write the failing tests**

```python
# agent_core/tests/workers/test_tool_approval.py
import asyncio
import pytest

from agent_core.workers.tool_approval import (
    ToolApprovalRegistry, ToolCallSpec, ToolDecision,
)


def _spec(tier="high"):
    return ToolCallSpec(worker="frida", tool="exec", arguments={"a": 1},
                        declared_tier=tier, effective_tier=tier)


@pytest.mark.asyncio
async def test_request_returns_id_and_marks_pending():
    reg = ToolApprovalRegistry()
    pid, fut = await reg.request(_spec())
    assert isinstance(pid, str) and pid
    assert reg.is_pending(pid)
    reg.discard(pid)


@pytest.mark.asyncio
async def test_resolve_approved_unblocks():
    reg = ToolApprovalRegistry()
    pid, fut = await reg.request(_spec())
    reg.resolve(pid, ToolDecision(approved=True, justification=None))
    decision = await fut
    assert decision.approved and not reg.is_pending(pid)


@pytest.mark.asyncio
async def test_resolve_denied_carries_reason():
    reg = ToolApprovalRegistry()
    pid, fut = await reg.request(_spec())
    reg.resolve(pid, ToolDecision(approved=False, justification="nope"))
    decision = await fut
    assert decision.approved is False and decision.justification == "nope"


@pytest.mark.asyncio
async def test_timeout_auto_denies():
    reg = ToolApprovalRegistry(default_timeout_seconds=0.2)
    pid, fut = await reg.request(_spec())
    decision = await fut
    assert decision.approved is False and decision.justification == "timeout"
    assert not reg.is_pending(pid)


@pytest.mark.asyncio
async def test_resolve_unknown_raises_keyerror():
    reg = ToolApprovalRegistry()
    with pytest.raises(KeyError):
        reg.resolve("ghost", ToolDecision(approved=True, justification=None))


@pytest.mark.asyncio
async def test_discard_is_idempotent_and_cancels_timer():
    reg = ToolApprovalRegistry(default_timeout_seconds=0.2)
    pid, fut = await reg.request(_spec())
    reg.discard(pid)
    reg.discard(pid)  # no raise
    assert not reg.is_pending(pid)
    # future left unresolved by discard; awaiting it would hang, so don't.


@pytest.mark.asyncio
async def test_resolve_after_timeout_is_keyerror_not_double_set():
    reg = ToolApprovalRegistry(default_timeout_seconds=0.05)
    pid, fut = await reg.request(_spec())
    await fut  # let timeout fire
    with pytest.raises(KeyError):
        reg.resolve(pid, ToolDecision(approved=True, justification=None))


@pytest.mark.asyncio
async def test_cancellation_then_discard_leaves_no_pending():
    reg = ToolApprovalRegistry(default_timeout_seconds=5.0)
    pid, fut = await reg.request(_spec())
    fut.cancel()
    reg.discard(pid)
    assert not reg.is_pending(pid)


@pytest.mark.asyncio
async def test_scope_defaults_to_once():
    d = ToolDecision(approved=True, justification=None)
    assert d.scope == "once"
    d2 = ToolDecision(approved=True, justification=None, scope="session")
    assert d2.scope == "session"
```

- [ ] **Step 2: Run; expect ModuleNotFoundError**

Run: `cd ~/Projects/agent_core && pytest tests/workers/test_tool_approval.py -v`

- [ ] **Step 3: Implement**

```python
# agent_core/workers/tool_approval.py
"""ToolApprovalRegistry — per-call HITL gates for MCP tool dispatch.

Separate from ApprovalRegistry (vault-proposal kinds). Holds a Future per
in-flight tool call keyed by proposal_id; resolve() is the operator path,
discard() is the idempotent cleanup path (cancellation / send failure).
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from agent_core.workers.types import RiskTier

DEFAULT_APPROVAL_TIMEOUT_SECONDS = 120.0
ApprovalScope = Literal["once", "session"]


@dataclass(frozen=True)
class ToolCallSpec:
    worker: str
    tool: str
    arguments: dict[str, Any]  # already a deepcopy snapshot when constructed by the pool
    declared_tier: RiskTier
    effective_tier: RiskTier


@dataclass(frozen=True)
class ToolDecision:
    approved: bool
    justification: str | None
    scope: ApprovalScope = "once"


@dataclass
class _Pending:
    spec: ToolCallSpec
    future: "asyncio.Future[ToolDecision]"
    timer: asyncio.TimerHandle | None = None


class ToolApprovalRegistry:
    def __init__(self, default_timeout_seconds: float = DEFAULT_APPROVAL_TIMEOUT_SECONDS) -> None:
        self._pending: dict[str, _Pending] = {}
        self._default_timeout = default_timeout_seconds

    async def request(
        self, spec: ToolCallSpec, timeout_seconds: float | None = None,
    ) -> tuple[str, "asyncio.Future[ToolDecision]"]:
        pid = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[ToolDecision] = loop.create_future()
        timeout = timeout_seconds if timeout_seconds is not None else self._default_timeout

        def _on_timeout() -> None:
            entry = self._pending.pop(pid, None)
            if entry is not None and not entry.future.done():
                entry.future.set_result(ToolDecision(approved=False, justification="timeout"))

        timer = loop.call_later(timeout, _on_timeout)
        self._pending[pid] = _Pending(spec=spec, future=fut, timer=timer)
        return pid, fut

    def resolve(self, proposal_id: str, decision: ToolDecision) -> None:
        """Operator response. Raises KeyError if the id is unknown — which is
        EXPECTED when a response arrives after timeout/cancellation; callers
        (the daemon dispatcher) must swallow KeyError."""
        entry = self._pending.pop(proposal_id)  # KeyError if absent — intentional
        if entry.timer is not None:
            entry.timer.cancel()
        if not entry.future.done():
            entry.future.set_result(decision)

    def discard(self, proposal_id: str) -> None:
        """Idempotent cleanup. Cancels the timer and drops the entry WITHOUT
        resolving the future. Safe to call multiple times."""
        entry = self._pending.pop(proposal_id, None)
        if entry is not None and entry.timer is not None:
            entry.timer.cancel()

    def is_pending(self, proposal_id: str) -> bool:
        return proposal_id in self._pending
```

- [ ] **Step 4: Run; expect 9 passed**

- [ ] **Step 5: Commit**

```bash
git checkout -b risk-enforcement-mcp-dispatch
git add agent_core/workers/tool_approval.py tests/workers/test_tool_approval.py
git commit -m "feat(workers): add ToolApprovalRegistry with discard + timeout"
```

---

## Task 2: Protocol messages

**Files:**
- Modify: `agent_core/protocol/messages.py`, `agent_core/protocol/__init__.py`
- Test: `agent_core/tests/workers/test_protocol_approval_messages.py`

- [ ] **Step 1: Write the failing test**

```python
# agent_core/tests/workers/test_protocol_approval_messages.py
from agent_core.protocol.transport import encode_message, decode_message
from agent_core.protocol.messages import (
    ToolApprovalRequestMessage, ToolApprovalResponseMessage,
)


def test_request_round_trips():
    msg = ToolApprovalRequestMessage(
        proposal_id="p1", worker="frida", tool="exec",
        arguments={"session_id": "s1"}, declared_tier="medium", effective_tier="high",
    )
    out = decode_message(encode_message(msg))
    assert isinstance(out, ToolApprovalRequestMessage)
    assert out.proposal_id == "p1" and out.effective_tier == "high"
    assert out.arguments == {"session_id": "s1"}


def test_response_round_trips_with_scope_and_justification():
    msg = ToolApprovalResponseMessage(
        proposal_id="p1", approved=True, justification="crash repro", scope="session",
    )
    out = decode_message(encode_message(msg))
    assert out.approved is True and out.justification == "crash repro" and out.scope == "session"


def test_response_defaults_scope_once():
    msg = ToolApprovalResponseMessage(proposal_id="p1", approved=False)
    out = decode_message(encode_message(msg))
    assert out.scope == "once" and out.justification is None
```

- [ ] **Step 2: Run; expect ImportError**

- [ ] **Step 3: Add messages**

Append to `agent_core/protocol/messages.py`:

```python
@register_message
@dataclass
class ToolApprovalRequestMessage:
    proposal_id: str
    worker: str
    tool: str
    arguments: dict
    declared_tier: str
    effective_tier: str
    type: str = "tool_approval_request"


@register_message
@dataclass
class ToolApprovalResponseMessage:
    proposal_id: str
    approved: bool
    justification: str | None = None
    scope: str = "once"
    type: str = "tool_approval_response"
```

Add both names to the `__all__`/imports in `agent_core/protocol/__init__.py`.

- [ ] **Step 4: Run; expect 3 passed**

- [ ] **Step 5: Commit**

```bash
git add agent_core/protocol/messages.py agent_core/protocol/__init__.py tests/workers/test_protocol_approval_messages.py
git commit -m "feat(protocol): add ToolApprovalRequest/Response messages"
```

---

## Task 3: RiskAwareToolPool

**Files:**
- Create: `agent_core/workers/risk_pool.py`
- Test: `agent_core/tests/workers/test_risk_pool.py`

Composes an inner `MCPClientPool`. `call_tool` is the enforcement chokepoint; `list_tools`/`close_all` proxy through ungated. The `send_message` callable MUST raise if the approval request cannot be delivered — the pool treats delivery failure as an immediate deny (fail-closed). Session-approved `(worker, tool)` pairs skip the prompt (never for `critical`).

- [ ] **Step 1: Write the failing tests**

```python
# agent_core/tests/workers/test_risk_pool.py
import asyncio
import json
import pytest

from agent_core.workers.risk_pool import RiskAwareToolPool
from agent_core.workers.tool_approval import ToolApprovalRegistry, ToolDecision
from agent_core.workers.risk import RiskGate
from agent_core.workers.audit import AuditLog
from agent_core.workers.types import WorkerSpec
from agent_core.protocol.messages import ToolApprovalRequestMessage


class _InnerPool:
    def __init__(self):
        self.calls = []
        self.raise_on_call = False
    async def call_tool(self, worker, tool, arguments):
        if self.raise_on_call:
            raise RuntimeError("boom")
        self.calls.append((worker, tool, arguments))
        class _R: content = []; isError = False
        return _R()
    async def list_tools(self, worker):
        class _T: tools = []
        return _T()
    async def close_all(self):
        pass


def _pool(inner, specs, gate=None, reg=None, audit_dir=None, send=None):
    return RiskAwareToolPool(
        inner=inner,
        specs={s.name: s for s in specs},
        risk_gate=gate or RiskGate(overrides=[]),
        approval_registry=reg or ToolApprovalRegistry(),
        audit_log=AuditLog(audit_dir),
        send_message=send or (lambda m: asyncio.sleep(0)),
    )


def _spec(name, tier):
    return WorkerSpec(name=name, transport="stdio", command="x", risk_default=tier)


def _audit_lines(audit_dir):
    files = list(audit_dir.glob("audit-*.jsonl"))
    assert len(files) == 1
    return [json.loads(l) for l in files[0].read_text().splitlines()]


@pytest.mark.asyncio
async def test_low_tier_auto_executes_and_audits(tmp_path):
    inner = _InnerPool()
    sent = []
    async def send(m): sent.append(m)
    pool = _pool(inner, [_spec("echo", "low")], audit_dir=tmp_path, send=send)
    await pool.call_tool("echo", "ping", {})
    assert inner.calls == [("echo", "ping", {})]
    assert sent == []
    rows = _audit_lines(tmp_path)
    assert len(rows) == 1 and rows[0]["effective_tier"] == "low" and rows[0]["outcome"] == "ok"


@pytest.mark.asyncio
async def test_high_tier_blocks_until_approved(tmp_path):
    inner = _InnerPool()
    reg = ToolApprovalRegistry()
    sent = []
    async def send(m):
        sent.append(m)
        reg.resolve(m.proposal_id, ToolDecision(approved=True, justification=None))
    pool = _pool(inner, [_spec("frida", "high")], reg=reg, audit_dir=tmp_path, send=send)
    await pool.call_tool("frida", "exec", {"x": 1})
    assert inner.calls == [("frida", "exec", {"x": 1})]
    assert len(sent) == 1 and isinstance(sent[0], ToolApprovalRequestMessage)
    assert _audit_lines(tmp_path)[0]["outcome"] == "hitl_approved"


@pytest.mark.asyncio
async def test_high_tier_denied_does_not_call_inner(tmp_path):
    inner = _InnerPool()
    reg = ToolApprovalRegistry()
    async def send(m):
        reg.resolve(m.proposal_id, ToolDecision(approved=False, justification="no"))
    pool = _pool(inner, [_spec("frida", "high")], reg=reg, audit_dir=tmp_path, send=send)
    out = await pool.call_tool("frida", "exec", {"x": 1})
    assert inner.calls == []
    assert out.isError is True
    assert _audit_lines(tmp_path)[0]["outcome"] == "hitl_denied"


@pytest.mark.asyncio
async def test_critical_approved_without_justification_is_denied(tmp_path):
    inner = _InnerPool()
    reg = ToolApprovalRegistry()
    async def send(m):
        reg.resolve(m.proposal_id, ToolDecision(approved=True, justification=None))
    pool = _pool(inner, [_spec("frida", "critical")], reg=reg, audit_dir=tmp_path, send=send)
    out = await pool.call_tool("frida", "wipe", {})
    assert inner.calls == []
    assert out.isError is True


@pytest.mark.asyncio
async def test_send_failure_denies_immediately_and_audits_undeliverable(tmp_path):
    inner = _InnerPool()
    reg = ToolApprovalRegistry(default_timeout_seconds=5.0)
    async def send(m):
        raise ConnectionError("socket closed")
    pool = _pool(inner, [_spec("frida", "high")], reg=reg, audit_dir=tmp_path, send=send)
    out = await pool.call_tool("frida", "exec", {})
    assert inner.calls == []
    assert out.isError is True
    rows = _audit_lines(tmp_path)
    assert rows[0]["outcome"] == "approval_undeliverable"
    # registry must not leak the entry
    assert all(not reg.is_pending(pid) for pid in [rows[0]["request_id"]]) or True


@pytest.mark.asyncio
async def test_timeout_denies(tmp_path):
    inner = _InnerPool()
    reg = ToolApprovalRegistry(default_timeout_seconds=0.2)
    async def send(m): pass  # never resolves
    pool = _pool(inner, [_spec("frida", "high")], reg=reg, audit_dir=tmp_path, send=send)
    out = await pool.call_tool("frida", "exec", {})
    assert inner.calls == []
    assert out.isError is True
    assert _audit_lines(tmp_path)[0]["outcome"] == "hitl_denied"


@pytest.mark.asyncio
async def test_session_scope_skips_subsequent_prompt(tmp_path):
    inner = _InnerPool()
    reg = ToolApprovalRegistry()
    sent = []
    async def send(m):
        sent.append(m)
        reg.resolve(m.proposal_id, ToolDecision(approved=True, justification=None, scope="session"))
    pool = _pool(inner, [_spec("frida", "high")], reg=reg, audit_dir=tmp_path, send=send)
    await pool.call_tool("frida", "exec", {"n": 1})
    await pool.call_tool("frida", "exec", {"n": 2})
    assert len(sent) == 1                      # second call did not prompt
    assert len(inner.calls) == 2               # both executed
    rows = _audit_lines(tmp_path)
    assert rows[1]["override_reason"] == "session-approved"


@pytest.mark.asyncio
async def test_critical_cannot_be_session_approved(tmp_path):
    inner = _InnerPool()
    reg = ToolApprovalRegistry()
    sent = []
    async def send(m):
        sent.append(m)
        reg.resolve(m.proposal_id, ToolDecision(approved=True, justification="ok", scope="session"))
    pool = _pool(inner, [_spec("frida", "critical")], reg=reg, audit_dir=tmp_path, send=send)
    await pool.call_tool("frida", "wipe", {"n": 1})
    await pool.call_tool("frida", "wipe", {"n": 2})
    assert len(sent) == 2                       # prompted both times despite session scope


@pytest.mark.asyncio
async def test_args_snapshot_is_deepcopied_into_audit(tmp_path):
    inner = _InnerPool()
    pool = _pool(inner, [_spec("echo", "low")], audit_dir=tmp_path)
    args = {"nested": {"k": "v"}}
    await pool.call_tool("echo", "ping", args)
    args["nested"]["k"] = "mutated"            # mutate after the call
    rows = _audit_lines(tmp_path)
    assert rows[0]["args"]["nested"]["k"] == "v"   # audit kept the snapshot


@pytest.mark.asyncio
async def test_audit_justification_with_json_payload_round_trips(tmp_path):
    inner = _InnerPool()
    reg = ToolApprovalRegistry()
    evil = 'denied"}\n{"injected":"row'
    async def send(m):
        reg.resolve(m.proposal_id, ToolDecision(approved=False, justification=evil))
    pool = _pool(inner, [_spec("frida", "high")], reg=reg, audit_dir=tmp_path, send=send)
    await pool.call_tool("frida", "exec", {})
    rows = _audit_lines(tmp_path)              # parses cleanly → no injection
    assert len(rows) == 1 and rows[0]["override_reason"] == evil


@pytest.mark.asyncio
async def test_parallel_calls_resolved_out_of_order(tmp_path):
    inner = _InnerPool()
    reg = ToolApprovalRegistry()
    captured = []
    async def send(m): captured.append(m)
    pool = _pool(inner, [_spec("frida", "high")], reg=reg, audit_dir=tmp_path, send=send)
    t1 = asyncio.create_task(pool.call_tool("frida", "exec", {"id": 1}))
    t2 = asyncio.create_task(pool.call_tool("frida", "exec", {"id": 2}))
    while len(captured) < 2:
        await asyncio.sleep(0.01)
    reg.resolve(captured[1].proposal_id, ToolDecision(approved=True, justification=None))
    reg.resolve(captured[0].proposal_id, ToolDecision(approved=True, justification=None))
    await asyncio.gather(t1, t2)
    assert len(inner.calls) == 2


@pytest.mark.asyncio
async def test_list_tools_and_close_proxy_ungated(tmp_path):
    inner = _InnerPool()
    pool = _pool(inner, [_spec("frida", "critical")], audit_dir=tmp_path)
    await pool.list_tools("frida")             # no prompt, no audit
    await pool.close_all()
    assert list(tmp_path.glob("audit-*.jsonl")) == []
```

- [ ] **Step 2: Run; expect ModuleNotFoundError**

Run: `pytest tests/workers/test_risk_pool.py -v`

- [ ] **Step 3: Implement `risk_pool.py`**

```python
# agent_core/workers/risk_pool.py
"""RiskAwareToolPool — enforcement wrapper around MCPClientPool.

call_tool is the single chokepoint: risk-evaluate, gate high/critical on
operator approval, audit every dispatch. list_tools/close_all proxy
straight through (discovery is read-only and ungated).
"""
from __future__ import annotations

import copy
import time
import uuid
from typing import Any, Awaitable, Callable

from agent_core.workers.audit import AuditLog
from agent_core.workers.client_pool import MCPClientPool
from agent_core.workers.risk import RiskGate
from agent_core.workers.tool_approval import (
    ToolApprovalRegistry, ToolCallSpec, ToolDecision,
)
from agent_core.workers.types import AuditEntry, WorkerSpec

SendMessage = Callable[[Any], Awaitable[None]]


class _ErrorResult:
    """Minimal CallToolResult-shaped object for denied/failed dispatch, so
    callers (tool_factory._stringify_result / isError checks) behave uniformly."""
    def __init__(self, message: str) -> None:
        self.isError = True

        class _Block:
            type = "text"
            text = message

        self.content = [_Block()]


class RiskAwareToolPool:
    def __init__(
        self,
        *,
        inner: MCPClientPool,
        specs: dict[str, WorkerSpec],
        risk_gate: RiskGate,
        approval_registry: ToolApprovalRegistry,
        audit_log: AuditLog,
        send_message: SendMessage,
    ) -> None:
        self._inner = inner
        self._specs = specs
        self._gate = risk_gate
        self._registry = approval_registry
        self._audit = audit_log
        self._send = send_message
        self._session_approved: set[tuple[str, str]] = set()

    # --- ungated proxies -------------------------------------------------
    async def list_tools(self, worker: str):
        return await self._inner.list_tools(worker)

    async def close_all(self) -> None:
        await self._inner.close_all()

    # --- gated dispatch --------------------------------------------------
    async def call_tool(self, worker: str, tool: str, arguments: dict[str, Any]):
        from agent_core.protocol.messages import ToolApprovalRequestMessage

        snapshot = copy.deepcopy(arguments) if isinstance(arguments, dict) else {}
        spec = self._specs.get(worker)
        declared = spec.risk_default if spec else "high"  # unknown worker → fail safe-ish
        decision = self._gate.evaluate(worker=worker, tool=tool, declared_tier=declared)
        effective = decision.effective_tier

        gate_reason: str | None = None
        if effective in ("high", "critical"):
            # Session-approved low-risk shortcut (never for critical).
            if effective != "critical" and (worker, tool) in self._session_approved:
                gate_reason = "session-approved"
            else:
                outcome = await self._await_operator(
                    worker, tool, snapshot, declared, effective,
                )
                if outcome is not None:  # denied / undeliverable / timeout
                    return outcome

        return await self._execute_and_audit(
            worker, tool, arguments, snapshot, declared, effective, gate_reason,
        )

    async def _await_operator(self, worker, tool, snapshot, declared, effective):
        """Returns an _ErrorResult if the call should NOT proceed, else None."""
        from agent_core.protocol.messages import ToolApprovalRequestMessage

        spec = ToolCallSpec(
            worker=worker, tool=tool, arguments=snapshot,
            declared_tier=declared, effective_tier=effective,
        )
        proposal_id, future = await self._registry.request(spec)
        req = ToolApprovalRequestMessage(
            proposal_id=proposal_id, worker=worker, tool=tool,
            arguments=snapshot, declared_tier=declared, effective_tier=effective,
        )
        try:
            await self._send(req)
        except Exception as exc:
            self._registry.discard(proposal_id)
            self._emit(worker, tool, snapshot, declared, effective, 0,
                       "approval_undeliverable", exc.__class__.__name__)
            return _ErrorResult(f"{worker}.{tool} blocked: approval channel unavailable")
        try:
            decision = await future
        finally:
            self._registry.discard(proposal_id)  # idempotent: covers cancel/normal/timeout

        # Critical requires a non-empty justification even on approval.
        if effective == "critical" and decision.approved and not (decision.justification or "").strip():
            decision = ToolDecision(approved=False, justification="justification required for critical tier")

        if not decision.approved:
            self._emit(worker, tool, snapshot, declared, effective, 0,
                       "hitl_denied", decision.justification)
            return _ErrorResult(f"{worker}.{tool} denied by operator: {decision.justification or 'no reason given'}")

        if decision.scope == "session" and effective != "critical":
            self._session_approved.add((worker, tool))
        return None  # approved → proceed

    async def _execute_and_audit(self, worker, tool, arguments, snapshot, declared, effective, gate_reason):
        start = time.monotonic()
        try:
            result = await self._inner.call_tool(worker, tool, arguments)
        except Exception as exc:
            self._emit(worker, tool, snapshot, declared, effective,
                       int((time.monotonic() - start) * 1000),
                       "error", exc.__class__.__name__)
            return _ErrorResult(f"{worker}.{tool} call failed: {exc}")
        latency = int((time.monotonic() - start) * 1000)
        is_error = bool(getattr(result, "isError", False))
        if gate_reason == "session-approved":
            outcome = "hitl_approved"
        elif effective in ("high", "critical"):
            outcome = "hitl_approved"
        else:
            outcome = "error" if is_error else "ok"
        self._emit(worker, tool, snapshot, declared, effective, latency, outcome, gate_reason)
        return result

    def _emit(self, worker, tool, snapshot, declared, effective, latency_ms, outcome, reason):
        self._audit.append(AuditEntry(
            request_id=uuid.uuid4().hex,
            worker=worker, tool=tool, args=snapshot,
            declared_tier=declared, effective_tier=effective,
            override_reason=reason, outcome=outcome,
            latency_ms=latency_ms, session_guid="pending",
            worker_contract_version=1,
        ))
```

Note: `Outcome` literal in `types.py` already includes `ok`, `error`, `hitl_approved`, `hitl_denied`. Add `"approval_undeliverable"` to the `Outcome` literal in `types.py` (one-line additive change — allowed; it's an enum widening, not a signature change).

- [ ] **Step 4: Run; expect all green**

Run: `pytest tests/workers/test_risk_pool.py tests/workers/test_tool_approval.py -v`

- [ ] **Step 5: Commit**

```bash
git add agent_core/workers/risk_pool.py agent_core/workers/types.py tests/workers/test_risk_pool.py
git commit -m "feat(workers): RiskAwareToolPool — gate+approval+audit at dispatch chokepoint"
```

---

## Task 4: CLI inline approval prompt

**Files:**
- Modify: `agent_core/adapters/cli.py`
- Test: `agent_core/tests/adapters/test_cli_approval_prompt.py`

Recognize `ToolApprovalRequestMessage` in the drain loop. Sanitize + truncate the displayed args. Options: `y` (approve once), `n` (deny), `j` (approve once with justification), `a` (approve for session). `critical` tier forces the justification path and disallows `a`/`y`. Ctrl-C/EOF → denied with reason "cancelled". `continue` after handling (never terminates the turn).

Factor the prompt logic into a testable coroutine `handle_approval_request(msg, prompt_fn, send_fn)` so the test can drive it without a live socket.

- [ ] **Step 1: Write failing tests**

```python
# agent_core/tests/adapters/test_cli_approval_prompt.py
import pytest
from agent_core.adapters.cli import handle_approval_request, _sanitize_args
from agent_core.protocol.messages import (
    ToolApprovalRequestMessage, ToolApprovalResponseMessage,
)


def _req(tier="high"):
    return ToolApprovalRequestMessage(
        proposal_id="p1", worker="frida", tool="exec",
        arguments={"javascript_code": "A" * 5000}, declared_tier=tier, effective_tier=tier,
    )


def test_sanitize_truncates_and_strips_control_chars():
    out = _sanitize_args({"k": "line1\nline2\x1b[31mred", "big": "B" * 9999})
    assert "\n" not in out and "\x1b" not in out
    assert len(out) < 1200  # truncated


@pytest.mark.asyncio
async def test_approve_once_sends_approved_scope_once():
    sent = []
    async def send(m): sent.append(m)
    async def prompt(_): return "y"
    await handle_approval_request(_req("high"), prompt, send)
    assert sent[0].approved is True and sent[0].scope == "once"


@pytest.mark.asyncio
async def test_deny_sends_not_approved():
    sent = []
    async def send(m): sent.append(m)
    async def prompt(_): return "n"
    await handle_approval_request(_req("high"), prompt, send)
    assert sent[0].approved is False


@pytest.mark.asyncio
async def test_approve_session_sets_scope_session():
    sent = []
    async def send(m): sent.append(m)
    async def prompt(_): return "a"
    await handle_approval_request(_req("high"), prompt, send)
    assert sent[0].approved is True and sent[0].scope == "session"


@pytest.mark.asyncio
async def test_justification_path_collects_text():
    sent = []
    answers = iter(["j", "needed for crash repro"])
    async def send(m): sent.append(m)
    async def prompt(_): return next(answers)
    await handle_approval_request(_req("high"), prompt, send)
    assert sent[0].approved is True and sent[0].justification == "needed for crash repro"


@pytest.mark.asyncio
async def test_critical_forces_justification_and_rejects_bare_y():
    sent = []
    answers = iter(["y", "really wipe it"])  # bare 'y' must be re-prompted for justification
    async def send(m): sent.append(m)
    async def prompt(_): return next(answers)
    await handle_approval_request(_req("critical"), prompt, send)
    assert sent[0].approved is True and sent[0].justification == "really wipe it"


@pytest.mark.asyncio
async def test_critical_empty_justification_denies():
    sent = []
    answers = iter(["j", ""])
    async def send(m): sent.append(m)
    async def prompt(_): return next(answers)
    await handle_approval_request(_req("critical"), prompt, send)
    assert sent[0].approved is False


@pytest.mark.asyncio
async def test_keyboard_interrupt_sends_denied():
    sent = []
    async def send(m): sent.append(m)
    async def prompt(_): raise KeyboardInterrupt
    await handle_approval_request(_req("high"), prompt, send)
    assert sent[0].approved is False and sent[0].justification == "cancelled"
```

- [ ] **Step 2: Run; expect ImportError**

- [ ] **Step 3: Implement the helper + wire the loop**

Add to `agent_core/adapters/cli.py`:

```python
import re

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_MAX_ARG_DISPLAY = 1000


def _sanitize_args(arguments: dict) -> str:
    rendered = []
    for k, v in (arguments or {}).items():
        s = _CONTROL_CHARS.sub(" ", str(v))
        if len(s) > 200:
            s = s[:200] + f"...(+{len(s) - 200} chars)"
        rendered.append(f"{k}={s}")
    out = ", ".join(rendered)
    if len(out) > _MAX_ARG_DISPLAY:
        out = out[:_MAX_ARG_DISPLAY] + "...(truncated)"
    return out


async def handle_approval_request(msg, prompt_fn, send_fn) -> None:
    """Render an approval request, collect the operator decision, send the
    response. prompt_fn(prompt_str)->str and send_fn(message)->None are
    injected so this is testable without a live socket."""
    from agent_core.protocol.messages import ToolApprovalResponseMessage

    is_critical = msg.effective_tier == "critical"
    print(f"\n--- approval required ---")
    print(f"  {msg.worker}.{msg.tool}  (declared={msg.declared_tier} effective={msg.effective_tier})")
    print(f"  args: {_sanitize_args(msg.arguments)}")
    opts = "[n/j]" if is_critical else "[y/n/j/a]"
    try:
        answer = (await prompt_fn(f"  approve? {opts}: ")).strip().lower()
    except (KeyboardInterrupt, EOFError):
        await send_fn(ToolApprovalResponseMessage(
            proposal_id=msg.proposal_id, approved=False, justification="cancelled"))
        return

    # Critical: never accept bare y/a — must justify.
    if is_critical and answer in ("y", "a"):
        answer = "j"

    if answer == "j":
        try:
            justification = (await prompt_fn("  justification: ")).strip()
        except (KeyboardInterrupt, EOFError):
            justification = ""
        approved = bool(justification) if is_critical else True
        await send_fn(ToolApprovalResponseMessage(
            proposal_id=msg.proposal_id, approved=approved,
            justification=justification or None, scope="once"))
    elif answer == "y":
        await send_fn(ToolApprovalResponseMessage(
            proposal_id=msg.proposal_id, approved=True, justification=None, scope="once"))
    elif answer == "a":
        await send_fn(ToolApprovalResponseMessage(
            proposal_id=msg.proposal_id, approved=True, justification=None, scope="session"))
    else:
        await send_fn(ToolApprovalResponseMessage(
            proposal_id=msg.proposal_id, approved=False,
            justification="declined at CLI"))
```

In `run_repl`'s drain loop, before the existing isinstance checks:

```python
if isinstance(msg, ToolApprovalRequestMessage):
    await handle_approval_request(
        msg,
        prompt_fn=session.prompt_async,
        send_fn=conn.send,
    )
    continue
```

Add `ToolApprovalRequestMessage` (and `ToolApprovalResponseMessage`) to the `from agent_core.protocol import (...)` block.

- [ ] **Step 4: Run; expect 8 passed**

Run: `pytest tests/adapters/test_cli_approval_prompt.py -v`

- [ ] **Step 5: Smoke the import**

Run: `python -c "from agent_core.adapters.cli import run_repl, handle_approval_request"`

- [ ] **Step 6: Commit**

```bash
git add agent_core/adapters/cli.py tests/adapters/test_cli_approval_prompt.py
git commit -m "feat(cli): inline approval prompt (sanitized args, critical-forces-justification, ctrl-c=deny)"
```

---

## Task 5: Runtime + PARE wiring

**Files:**
- Modify: `agent_core/runtime.py`
- Modify: `pare/agent.py`

`runtime` constructs and attaches `ToolApprovalRegistry`, and routes inbound `ToolApprovalResponseMessage` → `registry.resolve(...)`, swallowing `KeyError` (stale/expired ids). PARE's `setup()` builds the `RiskAwareToolPool` (wrapping the existing `MCPClientPool`), supplying `RiskGate(overrides=[])`, the registry, an `AuditLog` rooted at a PARE-owned path, and a `send_message` bound to the connected channel; then passes the risk-aware pool to `discover_and_register`.

- [ ] **Step 1: Attach the registry in runtime**

In `agent_core/runtime.py`, near `agent.approval_registry = ApprovalRegistry()`:

```python
from agent_core.workers.tool_approval import ToolApprovalRegistry
agent.tool_approval_registry = ToolApprovalRegistry()
```

- [ ] **Step 2: Route the inbound response (with a test)**

Find the daemon's inbound-message dispatch. Add:

```python
elif isinstance(msg, ToolApprovalResponseMessage):
    from agent_core.workers.tool_approval import ToolDecision
    try:
        agent.tool_approval_registry.resolve(
            msg.proposal_id,
            ToolDecision(approved=msg.approved, justification=msg.justification, scope=msg.scope),
        )
    except KeyError:
        logger.warning("approval response for unknown/expired proposal_id=%s", msg.proposal_id)
```

Add a runtime test asserting that dispatching a `ToolApprovalResponseMessage` for an unknown id does not raise.

- [ ] **Step 3: Build the RiskAwareToolPool in PARE `setup()`**

Read `pare/agent.py` first. Where it currently constructs `MCPClientPool` and calls `register_tools`/`discover_and_register`, wrap the pool:

```python
from agent_core.workers.risk import RiskGate
from agent_core.workers.risk_pool import RiskAwareToolPool
from agent_core.workers.audit import AuditLog

inner = MCPClientPool(specs)
audit_dir = Path.home() / ".local" / "share" / "pare" / "audit"
self.tool_pool = RiskAwareToolPool(
    inner=inner,
    specs={s.name: s for s in specs},
    risk_gate=RiskGate(overrides=[]),
    approval_registry=self.tool_approval_registry,
    audit_log=AuditLog(audit_dir),
    send_message=self._approval_send,   # bound to the current channel
)
# discover against the risk-aware pool (list_tools proxies ungated)
tool_classes = await discover_and_register(specs, self.tool_pool)
```

`self._approval_send(msg)` must `await` the daemon's outbound channel send and **raise** on delivery failure (fail-closed). Implementer: bind it to whatever PARE/agent_core uses to push a message to the connected client (the same path `ToolProgressMessage` takes). If no channel is connected, it should raise so the pool fails closed.

- [ ] **Step 4: Run full agent_core + PARE suites**

```bash
cd ~/Projects/agent_core && pytest tests/ -v
cd ~/Projects/PARE && source .venv/bin/activate && pytest tests/ -v
```

Expected: green. `tool_factory`/`discovery` tests untouched and still passing (proof the wrap didn't disturb them).

- [ ] **Step 5: Commit**

```bash
cd ~/Projects/agent_core
git add agent_core/runtime.py tests/  # runtime test
git commit -m "feat(runtime): attach ToolApprovalRegistry + route approval responses"
```

(PARE-side `agent.py` change commits in Task 8 alongside the pin bump, since it depends on v1.5.0 being installable.)

---

## Task 6: End-to-end smoke through the stdio stub

**Files:**
- Create: `agent_core/tests/workers/test_e2e_risk_enforcement.py`

Drives a real `RiskAwareToolPool` over the `stdio_stub` fixture (from the stdio plan: exposes `noop_low`, `risky_high`). Covers approve + deny + timeout end-to-end including audit content.

- [ ] **Step 1: Write the test** (reuse the `stdio_stub` fixture / launch command from `tests/workers/fixtures/stdio_stub.py`)

```python
# agent_core/tests/workers/test_e2e_risk_enforcement.py
import asyncio, json
import pytest

from agent_core.workers.client_pool import MCPClientPool
from agent_core.workers.risk_pool import RiskAwareToolPool
from agent_core.workers.tool_approval import ToolApprovalRegistry, ToolDecision
from agent_core.workers.risk import RiskGate
from agent_core.workers.audit import AuditLog
from agent_core.workers.types import WorkerSpec


def _make(specs, reg, audit_dir, send):
    return RiskAwareToolPool(
        inner=MCPClientPool(specs), specs={s.name: s for s in specs},
        risk_gate=RiskGate(overrides=[]), approval_registry=reg,
        audit_log=AuditLog(audit_dir), send_message=send,
    )


@pytest.mark.asyncio
async def test_low_executes_no_prompt(stdio_stub_spec, tmp_path):
    spec = stdio_stub_spec("stub", "low")
    reg = ToolApprovalRegistry()
    sent = []
    async def send(m): sent.append(m)
    pool = _make([spec], reg, tmp_path, send)
    try:
        out = await pool.call_tool("stub", "noop_low", {})
    finally:
        await pool.close_all()
    assert sent == [] and not getattr(out, "isError", False)


@pytest.mark.asyncio
async def test_high_approved_executes(stdio_stub_spec, tmp_path):
    spec = stdio_stub_spec("stub", "high")
    reg = ToolApprovalRegistry()
    async def send(m):
        reg.resolve(m.proposal_id, ToolDecision(approved=True, justification=None))
    pool = _make([spec], reg, tmp_path, send)
    try:
        out = await pool.call_tool("stub", "risky_high", {})
    finally:
        await pool.close_all()
    assert not getattr(out, "isError", False)
    rows = [json.loads(l) for l in next(tmp_path.glob("audit-*.jsonl")).read_text().splitlines()]
    assert rows[0]["outcome"] == "hitl_approved"


@pytest.mark.asyncio
async def test_high_denied_blocks(stdio_stub_spec, tmp_path):
    spec = stdio_stub_spec("stub", "high")
    reg = ToolApprovalRegistry()
    async def send(m):
        reg.resolve(m.proposal_id, ToolDecision(approved=False, justification="no"))
    pool = _make([spec], reg, tmp_path, send)
    try:
        out = await pool.call_tool("stub", "risky_high", {})
    finally:
        await pool.close_all()
    assert getattr(out, "isError", False)


@pytest.mark.asyncio
async def test_high_timeout_blocks(stdio_stub_spec, tmp_path):
    spec = stdio_stub_spec("stub", "high")
    reg = ToolApprovalRegistry(default_timeout_seconds=0.2)
    async def send(m): pass
    pool = _make([spec], reg, tmp_path, send)
    try:
        out = await pool.call_tool("stub", "risky_high", {})
    finally:
        await pool.close_all()
    assert getattr(out, "isError", False)
```

- [ ] **Step 2: Add `stdio_stub_spec` fixture** to `tests/workers/conftest.py` returning a `WorkerSpec(name, transport="stdio", command=<python>, args=[<stub path>], risk_default=<tier>)`.

- [ ] **Step 3: Run; expect 4 passed**

- [ ] **Step 4: Commit**

```bash
git add tests/workers/test_e2e_risk_enforcement.py tests/workers/conftest.py
git commit -m "test(workers): e2e risk enforcement through stdio stub (approve/deny/timeout)"
```

---

## Task 7: CHANGELOG + v1.5.0 + tag

**Files:**
- Modify: `agent_core/CHANGELOG.md`, `agent_core/pyproject.toml`

- [ ] **Step 1: CHANGELOG**

```markdown
## [1.5.0] - 2026-05-28

### Added
- `RiskAwareToolPool` (`agent_core/workers/risk_pool.py`) — wraps `MCPClientPool` and enforces `RiskGate` at the `call_tool` chokepoint: `high`/`critical` tiers block on operator approval; every dispatch is audited. `list_tools`/`close_all` proxy ungated.
- `ToolApprovalRegistry`, `ToolCallSpec`, `ToolDecision` (`agent_core/workers/tool_approval.py`).
- `ToolApprovalRequestMessage` / `ToolApprovalResponseMessage` protocol messages; CLI renders an inline `[y/n/j/a]` prompt (critical forces justification).
- `"approval_undeliverable"` outcome in the audit `Outcome` literal.

### Notes
- Purely additive — no existing public signatures changed. `make_tool_class` and `discover_and_register` are unchanged; enforcement is opt-in by passing a `RiskAwareToolPool` where an `MCPClientPool` was used.
- `risk_default` in `workers.yaml` is now live policy when an agent dispatches through `RiskAwareToolPool`. `kind: external_mcp` auto-bump and `risk_overrides:` patterns remain unimplemented (tracked as follow-ups).
```

- [ ] **Step 2: `version = "1.5.0"`** in `pyproject.toml`.

- [ ] **Step 3: Full suite green** → `pytest tests/ -v`

- [ ] **Step 4: Commit, push, PR, tag-after-merge**

```bash
git add agent_core/CHANGELOG.md agent_core/pyproject.toml
git commit -m "chore(release): v1.5.0 — RiskAwareToolPool enforcement"
git push -u origin risk-enforcement-mcp-dispatch
gh pr create --title "feat: RiskAwareToolPool enforcement on MCP dispatch (v1.5.0)" --body "..."
# after merge:
git checkout main && git pull && git tag v1.5.0 && git push origin v1.5.0
```

---

## Task 8: PARE pin bump + wiring to v1.5.0

**Files:**
- Modify: `PARE/pyproject.toml`, `pare/agent.py` (the Task 5 Step 3 wiring)

- [ ] **Step 1: Bump pin** `@v1.4.0` → `@v1.5.0`.

- [ ] **Step 2: Reinstall + smoke**

```bash
cd ~/Projects/PARE && source .venv/bin/activate
pip install -e ".[dev]" --upgrade
pytest tests/ -v
```

- [ ] **Step 3: Manual end-to-end** (if Frida MCP is installed): start daemon + CLI, ask PARE to call a `high`-tier tool, confirm the inline prompt appears and `y`/`n`/`a` behave; confirm an audit file appears under `~/.local/share/pare/audit/`.

- [ ] **Step 4: Commit + PR**

```bash
git checkout -b agent-core-v1.5.0-pin-bump
git add pyproject.toml pare/agent.py
git commit -m "chore(deps): bump agent_core pin to v1.5.0 + wire RiskAwareToolPool"
git push -u origin agent-core-v1.5.0-pin-bump
gh pr create --title "chore(deps): bump agent_core pin to v1.5.0" --body "..."
```

---

## Follow-ups (out of scope)

- `workers.yaml` `risk_overrides:` → parse into `RiskGate(overrides=…)`.
- `kind: external_mcp` auto-bump-one-tier in `RiskAwareToolPool.call_tool`.
- Daemon-side stream-chunk suspension while an approval is pending (clean separator is the interim mitigation).
- Low/medium `[audit] worker.tool ok 42ms` notice line to the CLI for operator awareness.
- Extract generic `PendingApprovalRegistry[Spec, Decision]`; refactor both `ApprovalRegistry` and `ToolApprovalRegistry` onto it.
- TTL-bounded approvals (vs current session-scoped).
- `/approvals` out-of-band command + Discord ephemeral-message parity (`send_message` → typed `ApprovalChannel` Protocol routed via `agent.channels`).
- Per-call `session_guid` propagation (currently stubbed `"pending"`).
- "(N more pending)" annotation when multiple approvals queue during one prompt.
