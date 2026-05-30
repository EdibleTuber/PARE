from agent_core.workers.registry import WorkerRegistry


def test_pare_passes_registry_overrides_into_riskgate(tmp_path):
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
