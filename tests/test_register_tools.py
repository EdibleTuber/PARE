"""PareAgent's worker wiring after the move to agent_core's WorkerManager.

register_tools() no longer discovers anything: discovery moved into
astartup(), where it runs in the serving loop and in a task that outlives the
connections it opens. What remains here is the declarative half.
"""
from unittest.mock import MagicMock

import pytest

from pare.agent import PareAgent
from pare.config import PAREConfig
from pare.tools import StaticAnalyze

_FRAMEWORK_ATTRS = ("profile", "wisdom", "channels", "learning", "allowlist",
                    "approval_registry", "tool_approval_registry", "inference",
                    "retrieval", "websearch", "fetcher")


def _agent(tmp_path, **cfg_overrides):
    wy = tmp_path / "workers.yaml"
    wy.write_text(
        "workers:\n"
        "  stub:\n"
        "    command: /bin/true\n"
        "    transport: stdio\n"
        "    risk_default: low\n"
        "  later:\n"
        "    command: /bin/true\n"
        "    transport: stdio\n"
        "    risk_default: low\n"
        "    autoload: false\n"
    )
    cfg = PAREConfig()
    cfg.workers_yaml_path = str(wy)
    cfg.audit_dir = tmp_path
    for k, v in cfg_overrides.items():
        setattr(cfg, k, v)
    agent = PareAgent()
    agent.config = cfg
    for attr in _FRAMEWORK_ATTRS:
        setattr(agent, attr, MagicMock())
    return agent


def test_setup_leaves_the_pool_empty(tmp_path):
    """The pool must hold LOADED specs, not DECLARED ones.

    If setup() seeds every declared spec, the first dispatch after an unload
    lazily reconnects the worker you just unloaded — silently undoing it.
    """
    agent = _agent(tmp_path)
    agent.setup()
    assert agent.mcp_pool.names() == []
    assert agent.worker_manager is None, "sentinel until astartup builds it"


def test_setup_loads_the_registry_without_connecting(tmp_path):
    agent = _agent(tmp_path)
    agent.setup()
    assert {s.name for s in agent.worker_registry.all()} == {"stub", "later"}


@pytest.mark.parametrize("enabled", [False, True])
def test_register_tools_is_declarative_only(tmp_path, enabled):
    """No discovery, no asyncio.run, no close_all — just the gated tool."""
    agent = _agent(tmp_path, enable_apk_re_agents=enabled)
    agent.setup()
    classes = agent.register_tools()
    assert classes == ([StaticAnalyze] if enabled else [])
    assert (agent.apk_re_agents_client is not None) is enabled


async def test_astartup_builds_the_manager_and_autoloads(tmp_path):
    agent = _agent(tmp_path)
    agent.setup()
    agent.tool_executor = MagicMock()
    with pytest.MonkeyPatch.context() as mp:
        loaded = []

        async def fake_autoload(self):
            loaded.append(True)
            return []
        mp.setattr("agent_core.workers.manager.WorkerManager.load_autoload",
                   fake_autoload)
        await agent.astartup()
    assert agent.worker_manager is not None
    assert loaded == [True]


async def test_astartup_survives_a_worker_that_cannot_spawn(tmp_path):
    """A missing binary must be a last_error in /worker list, not a boot crash —
    systemd Restart=on-failure would respawn the daemon every 5s."""
    agent = _agent(tmp_path)
    agent.setup()
    agent.tool_executor = MagicMock()
    agent.tool_executor.add_all = MagicMock()
    await agent.astartup()          # /bin/true exits immediately: load fails
    statuses = {s.name: s for s in agent.worker_manager.status()}
    assert statuses["stub"].loaded is False
    assert statuses["stub"].last_error
    assert statuses["later"].loaded is False, "autoload: false must be skipped"


async def test_astartup_starts_liveness_and_ashutdown_stops_it(tmp_path):
    """A probe loop nobody starts is not a liveness story, and one nobody
    stops logs spurious 'unreachable' warnings for a shutdown the operator
    asked for."""
    agent = _agent(tmp_path)
    agent.setup()
    agent.tool_executor = MagicMock()
    agent.tool_executor.add_all = MagicMock()
    await agent.astartup()
    assert agent.worker_manager._liveness_task is not None or \
        agent.worker_manager._liveness_interval <= 0
    await agent.ashutdown()
    assert agent.worker_manager._liveness_task is None
