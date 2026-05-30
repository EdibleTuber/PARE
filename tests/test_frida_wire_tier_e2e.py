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
