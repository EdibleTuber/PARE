"""Guard against risk_overrides pins that silently protect nothing.

The pins in workers.yaml are hand-maintained strings matched by fnmatch against
f"{worker}_{tool}". A typo (e.g. "frida_execute_scrpt") matches no tool and
silently yields no protection — RiskGate only validates the tier, not that the
pattern hits anything. These tests assert every pin matches a real tool and that
the dangerous tools resolve to their intended gated tiers against the real worker
contracts. (Security-review finding, 2026-05-30.)

Coverage spans every worker whose contract package is installed, not just frida:
a pin typo in the mitm namespace disables protection exactly as silently as one
in frida's. Pins naming a worker that isn't installed here are reported rather
than failed, so a partial checkout doesn't turn into a red suite -- but if
NONE of the pins could be checked against an installed contract, that warning
means reduced coverage, not success, and the run fails outright: a UserWarning
alone does not fail pytest by default (pyproject.toml sets no
filterwarnings), so an all-unchecked run would otherwise look identical to a
fully-verified one.
"""
import fnmatch
import importlib
import warnings
from pathlib import Path

import pytest

from agent_core.workers.registry import WorkerRegistry
from agent_core.workers.risk import RiskGate, resolve_declared_tier

_WORKERS_YAML = Path(__file__).resolve().parent.parent / "workers.yaml"

# worker name (workers.yaml key, and therefore the tool prefix) -> contract module
_CONTRACT_MODULES = {
    "frida": "pare_frida_mcp.contract",
    "static": "pare_static_mcp.contract",
    "mitm": "pare_mitm_mcp.contract",
}


def _tool_targets() -> tuple[set[str], set[str]]:
    """Return (all f"{worker}_{tool}" targets, worker names actually installed)."""
    targets: set[str] = set()
    installed: set[str] = set()
    for worker, module in _CONTRACT_MODULES.items():
        try:
            contract = importlib.import_module(module)
        except ImportError:
            continue
        installed.add(worker)
        targets |= {f"{worker}_{spec.name}" for spec in contract.TOOL_SPECS}
    return targets, installed


def test_every_pin_matches_at_least_one_real_tool():
    reg = WorkerRegistry.load(_WORKERS_YAML)
    overrides = reg.risk_overrides()
    assert overrides, "expected at least the mandatory frida pins"
    targets, installed = _tool_targets()
    assert installed, "no worker contract packages installed — cannot validate pins"

    unchecked = []
    validated = 0
    for pattern, tier in overrides:
        owner = next((w for w in installed if pattern.startswith(f"{w}_")), None)
        if owner is None:
            unchecked.append(pattern)
            continue
        matched = [t for t in targets if fnmatch.fnmatchcase(t, pattern)]
        assert matched, (
            f"risk_overrides pin {pattern!r} matches no known tool — likely a "
            f"typo that silently disables protection. Known {owner} targets: "
            f"{sorted(t for t in targets if t.startswith(f'{owner}_'))}"
        )
        validated += 1
    if unchecked:
        # Warn rather than skip: the pins we *could* check were genuinely
        # verified, and skipping would discard that result.
        warnings.warn(
            "risk_overrides pins not validated (worker contract not installed "
            f"here): {sorted(unchecked)}",
            UserWarning,
            stacklevel=2,
        )
    # `installed` being non-empty only means SOME contract package is
    # importable -- not that it owns any pin. A checkout where the only
    # installed contract is one with no risk_overrides pins (e.g. `static`
    # alone, with every real pin being frida_*/mitm_*) would otherwise leave
    # every pin "unchecked" and still exit green via the warning above. Zero
    # pins actually verified is the exact silently-no-protection failure mode
    # this file exists to catch, so it must fail, not warn.
    assert validated > 0, (
        "no risk_overrides pin could be validated against any installed "
        f"worker contract (installed here: {sorted(installed) or 'none'}) — "
        "every pin was unchecked, so this run proved nothing. Install at "
        "least one pare-*-mcp contract package that owns a pinned tool."
    )


def test_dangerous_frida_tools_resolve_to_pinned_tiers():
    reg = WorkerRegistry.load(_WORKERS_YAML)
    gate = RiskGate(overrides=reg.risk_overrides())
    # Even if a compromised worker advertised these as "low", the pins force the ceiling.
    assert gate.evaluate(worker="frida", tool="execute_script",
                         declared_tier="low").effective_tier == "critical"
    assert gate.evaluate(worker="frida", tool="write_memory",
                         declared_tier="low").effective_tier == "high"


def test_dangerous_mitm_tools_resolve_to_pinned_tiers():
    """mitm is not read-only: inject_request forges traffic, and the rule/replay
    tools alter what the target sees. The pins hold even if a half-wired dev
    build advertises them low (or advertises nothing at all)."""
    pytest.importorskip("pare_mitm_mcp.contract")
    reg = WorkerRegistry.load(_WORKERS_YAML)
    gate = RiskGate(overrides=reg.risk_overrides())
    assert gate.evaluate(worker="mitm", tool="inject_request",
                         declared_tier="low").effective_tier == "critical"
    for tool in ("add_blocking_rule", "add_modification_rule", "replay_flow"):
        assert gate.evaluate(worker="mitm", tool=tool,
                             declared_tier="low").effective_tier == "high", tool


def test_frida_floor_is_low():
    reg = WorkerRegistry.load(_WORKERS_YAML)
    assert reg.get("frida").risk_default == "low"


def test_readonly_frida_tools_auto_execute_under_low_floor():
    """With floor=low and honest advertised tiers, metadata/capture reads
    resolve to a non-gated tier; live-memory / behavior-altering tools gate."""
    reg = WorkerRegistry.load(_WORKERS_YAML)
    spec = reg.get("frida")
    import pare_frida_mcp.contract as contract
    advertised = {s.name: s.risk_tier for s in contract.TOOL_SPECS}

    def declared(tool):
        return resolve_declared_tier(spec, advertised[tool])[0]

    # Gate fires only on high/critical (risk_pool). These must NOT gate:
    # NOTE: search_capture/read_capture/page_capture were removed from the frida
    # MCP in the capture-layer teardown (they moved up into PARE); the readonly
    # set no longer includes them.
    for tool in ("enumerate_processes", "enumerate_applications",
                 "enumerate_modules", "enumerate_exports",
                 "list_devices", "select_device",
                 "java_hook_remove", "attach", "load_script"):
        assert declared(tool) in ("low", "medium"), f"{tool} should auto-execute"

    # These MUST gate:
    for tool in ("read_memory", "java_hook", "write_memory"):
        assert declared(tool) == "high", f"{tool} should be gated"
    assert declared("execute_script") == "critical"
