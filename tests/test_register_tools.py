"""Tests for PareAgent.register_tools() — workers.yaml discovery integration.

Asserts structure (the agent uses agent_core's discovery helpers correctly)
without requiring real apk_re_agents to be running. Live end-to-end
verification is in tests/test_phase3_smoke.py (Task 8).
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pare.agent import PareAgent
from pare.config import PAREConfig


def test_pareagent_has_register_tools_method():
    """The agent exposes the agent_core lifecycle hook."""
    assert callable(getattr(PareAgent, "register_tools", None))


def test_register_tools_runs_discover_and_register(tmp_path):
    """register_tools() invokes agent_core.workers.discover_and_register
    against the configured workers.yaml + the pool from setup()."""
    # Minimal workers.yaml for the test.
    wy = tmp_path / "workers.yaml"
    wy.write_text(
        "workers:\n"
        "  stub:\n"
        "    endpoint: http://127.0.0.1:1/mcp\n"
        "    transport: streamable_http\n"
        "    risk_default: low\n"
    )

    cfg = PAREConfig()
    cfg.workers_yaml_path = str(wy)

    agent = PareAgent()
    agent.config = cfg

    # Stub framework managers normally populated by run_daemon.
    for attr in ("profile", "wisdom", "channels", "learning", "allowlist",
                 "approval_registry", "inference", "retrieval", "websearch",
                 "fetcher"):
        setattr(agent, attr, MagicMock())

    # Patch discover_and_register to return a stub list without hitting
    # the network. The test verifies setup() builds the pool and
    # register_tools() invokes the discovery driver — not the discovery
    # logic itself (which is agent_core's responsibility).
    with patch("pare.agent.discover_and_register", new_callable=AsyncMock) as mock_disc:
        mock_disc.return_value = []
        agent.setup()
        tool_classes = agent.register_tools()
        assert isinstance(tool_classes, list)
        mock_disc.assert_awaited_once()

    # Confirm setup() built a pool.
    assert hasattr(agent, "mcp_pool")
    assert agent.mcp_pool is not None
