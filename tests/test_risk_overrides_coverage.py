"""Guard against risk_overrides pins that silently protect nothing.

The pins in workers.yaml are hand-maintained strings matched by fnmatch against
f"{worker}_{tool}". A typo (e.g. "frida_execute_scrpt") matches no tool and
silently yields no protection — RiskGate only validates the tier, not that the
pattern hits anything. These tests assert every pin matches a real tool and that
the dangerous frida tools resolve to their intended gated tiers against the real
worker contract. (Security-review finding, 2026-05-30.)
"""
import fnmatch

import pytest

from agent_core.workers.registry import WorkerRegistry
from agent_core.workers.risk import RiskGate, resolve_declared_tier


def _frida_tool_targets():
    contract = pytest.importorskip("pare_frida_mcp.contract")
    return {f"frida_{spec.name}" for spec in contract.TOOL_SPECS}


def test_every_pin_matches_at_least_one_real_tool():
    reg = WorkerRegistry.load("workers.yaml")
    overrides = reg.risk_overrides()
    assert overrides, "expected at least the mandatory frida pins"
    targets = _frida_tool_targets()
    for pattern, tier in overrides:
        matched = [t for t in targets if fnmatch.fnmatchcase(t, pattern)]
        assert matched, (
            f"risk_overrides pin {pattern!r} matches no known tool — likely a "
            f"typo that silently disables protection. Known frida targets: {sorted(targets)}"
        )


def test_dangerous_frida_tools_resolve_to_pinned_tiers():
    reg = WorkerRegistry.load("workers.yaml")
    gate = RiskGate(overrides=reg.risk_overrides())
    # Even if a compromised worker advertised these as "low", the pins force the ceiling.
    assert gate.evaluate(worker="frida", tool="execute_script",
                         declared_tier="low").effective_tier == "critical"
    assert gate.evaluate(worker="frida", tool="write_memory",
                         declared_tier="low").effective_tier == "high"


def test_frida_floor_is_low():
    reg = WorkerRegistry.load("workers.yaml")
    assert reg.get("frida").risk_default == "low"


def test_readonly_frida_tools_auto_execute_under_low_floor():
    """With floor=low and honest advertised tiers, metadata/capture reads
    resolve to a non-gated tier; live-memory / behavior-altering tools gate."""
    reg = WorkerRegistry.load("workers.yaml")
    spec = reg.get("frida")
    import pare_frida_mcp.contract as contract
    advertised = {s.name: s.risk_tier for s in contract.TOOL_SPECS}

    def declared(tool):
        return resolve_declared_tier(spec, advertised[tool])[0]

    # Gate fires only on high/critical (risk_pool). These must NOT gate:
    for tool in ("enumerate_processes", "enumerate_applications",
                 "enumerate_modules", "enumerate_exports",
                 "search_capture", "read_capture",
                 "list_devices", "select_device",
                 "java_hook_remove", "attach", "load_script"):
        assert declared(tool) in ("low", "medium"), f"{tool} should auto-execute"

    # These MUST gate:
    for tool in ("read_memory", "java_hook", "write_memory"):
        assert declared(tool) == "high", f"{tool} should be gated"
    assert declared("execute_script") == "critical"
