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


def test_networked_workers_never_autoload():
    """The property, applied to every worker rather than a hardcoded list.

    A sleeping host does not refuse a connection -- it drops the SYN -- so an
    autoloading networked worker costs its FULL connect timeout at every daemon
    boot. The networked-workers spec makes autoload: false required for them,
    not merely advisable.
    """
    for spec in _reg().all():
        if spec.transport != "stdio":
            assert spec.autoload is False, (
                f"{spec.name} is networked ({spec.transport} -> {spec.endpoint}); "
                f"autoloading it costs its full connect timeout at every boot "
                f"whenever that host is asleep"
            )


def test_stdio_service_workers_autoload():
    """The narrower guard the previous version of this test was really for:
    don't silently switch off a worker that was deliberately made unconditional.

    frida is no longer in this list -- it moved to the laptop and is networked,
    so the test above governs it now. hardware is excluded because it is
    declared-but-unbuilt (see test_hardware_is_declared_but_not_autoloaded).
    """
    reg = _reg()
    for name in ("static", "mitm"):
        spec = reg.get(name)
        assert spec.transport == "stdio", (
            f"{name} is no longer stdio; if it moved to another machine, it "
            f"belongs under test_networked_workers_never_autoload instead"
        )
        assert spec.autoload is True, (
            f"{name} must still connect at boot; mitm in particular was made "
            f"unconditional deliberately in e9edf9f"
        )


def test_every_declared_worker_has_a_risk_floor():
    for spec in _reg().all():
        assert spec.risk_default, spec.name
