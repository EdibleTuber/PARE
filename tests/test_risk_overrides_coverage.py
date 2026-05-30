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
from agent_core.workers.risk import RiskGate


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
