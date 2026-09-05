"""workers.yaml is the trust anchor: every loadable worker is declared here,
with an operator-set risk floor, before anything can connect it.

The autoload assertion doubles as a pin-version canary. WorkerSpec sets no
model_config, so pydantic's extra="ignore" means an agent_core older than
v1.8.0 drops the key silently and autoloads the worker anyway — a fail-open.
This test turns that into a loud failure.
"""
from pathlib import Path

from agent_core.workers.registry import WorkerRegistry

_WORKERS_YAML = Path(__file__).resolve().parent.parent / "workers.yaml"


def _reg():
    return WorkerRegistry.load(_WORKERS_YAML)


def test_hardware_is_declared_but_not_autoloaded():
    spec = _reg().get("hardware")
    assert spec.autoload is False
    assert spec.risk_default in ("low", "medium", "high", "critical")
    assert spec.capability_tags, "a catalog entry needs tags to be selectable"


def test_live_workers_autoload():
    reg = _reg()
    for name in ("frida", "static", "mitm"):
        assert reg.get(name).autoload is True, (
            f"{name} must still connect at boot; mitm in particular was made "
            f"unconditional deliberately in e9edf9f"
        )


def test_every_declared_worker_has_a_risk_floor():
    for spec in _reg().all():
        assert spec.risk_default, spec.name
