from unittest.mock import MagicMock

from pare.agent import PareAgent
from pare.config import PAREConfig


_WORKERS_YAML = (
    "workers:\n"
    "  stub:\n"
    "    endpoint: http://127.0.0.1:1/mcp\n"
    "    transport: streamable_http\n"
    "    risk_default: low\n"
    "  mitm:\n"
    "    command: pare-mitm-mcp\n"
    "    transport: stdio\n"
    "    risk_default: low\n"
)


def _setup_agent(tmp_path, enable_mitm):
    """Build a real PareAgent and drive its actual setup() — the gate under
    test lives there (specs = [s for s in specs if s.name != "mitm"]), so the
    test must exercise that code path rather than a hand-copied reimplementation
    of it (a prior version of this test did the latter and would not have
    caught a regression in the real filter)."""
    wy = tmp_path / "workers.yaml"
    wy.write_text(_WORKERS_YAML)

    cfg = PAREConfig()
    cfg.workers_yaml_path = str(wy)
    cfg.audit_dir = tmp_path
    cfg.enable_mitm = enable_mitm

    agent = PareAgent()
    agent.config = cfg
    # Stub framework managers normally populated by run_daemon (see
    # test_register_tools.py). setup() only builds the pool/gate — it does
    # not connect (MCPClientPool connects lazily on first call) — so this is
    # safe without any worker process actually existing.
    for attr in ("profile", "wisdom", "channels", "learning", "allowlist",
                 "approval_registry", "tool_approval_registry", "inference",
                 "retrieval", "websearch", "fetcher"):
        setattr(agent, attr, MagicMock())

    agent.setup()
    return agent


def test_mitm_excluded_when_disabled(tmp_path):
    agent = _setup_agent(tmp_path, enable_mitm=False)
    names = {s.name for s in agent._worker_specs}
    assert "mitm" not in names and "stub" in names


def test_mitm_included_when_enabled(tmp_path):
    agent = _setup_agent(tmp_path, enable_mitm=True)
    names = {s.name for s in agent._worker_specs}
    assert "mitm" in names


def test_config_defaults_mitm_off():
    assert PAREConfig().enable_mitm is False
