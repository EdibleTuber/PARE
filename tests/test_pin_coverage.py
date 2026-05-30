"""Guard: the operator pins in the REAL workers.yaml actually fire.

A risk_overrides pattern is a hand-maintained fnmatch string. RiskGate
validates the tier but NOT that a pattern matches any real tool, so a typo
(e.g. `frida_execute_scrpt`) silently yields zero protection. These tests
pin the shipped config: if a dangerous-tool pin is typo'd, removed, or
weakened, the assertion fails. (Security-review follow-up, 2026-05-30.)
"""
from pathlib import Path

from agent_core.workers.registry import WorkerRegistry
from agent_core.workers.risk import RiskGate

_WORKERS_YAML = Path(__file__).resolve().parent.parent / "workers.yaml"


def _gate():
    reg = WorkerRegistry.load(_WORKERS_YAML)
    return RiskGate(overrides=reg.risk_overrides())


def test_execute_script_pin_forces_critical_even_if_worker_lies_low():
    # If this fails, the frida_execute_script pin is typo'd/missing — arbitrary
    # JS in the target could auto-execute on a lying/buggy worker.
    decision = _gate().evaluate(worker="frida", tool="execute_script", declared_tier="low")
    assert decision.effective_tier == "critical", (
        "frida_execute_script operator pin is not firing — check workers.yaml risk_overrides"
    )


def test_write_memory_pin_forces_at_least_high_even_if_worker_lies_low():
    decision = _gate().evaluate(worker="frida", tool="write_memory", declared_tier="low")
    assert decision.effective_tier == "high", (
        "frida_write_memory operator pin is not firing — check workers.yaml risk_overrides"
    )
