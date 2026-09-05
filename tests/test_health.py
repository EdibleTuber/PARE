"""Tests for the /health slash command."""
from unittest.mock import MagicMock

import pytest

from pare.commands.health import Health
from agent_core.protocol.messages import ResponseMessage


def _cfg():
    """A MagicMock config carrying the fields /health reads."""
    cfg = MagicMock()
    cfg.inference_url = "http://example.invalid:11434"
    cfg.model = "gemma-test"
    cfg.vault_path = "/tmp/nowhere"
    cfg.apk_re_agents_url = "http://127.0.0.1:8000"
    return cfg


async def _collect(cmd, raw_args, ctx) -> str:
    """Run a command and join the `.text` of every message it yields."""
    return "\n".join([m.text async for m in cmd.run(raw_args, ctx)])


@pytest.mark.asyncio
async def test_health_returns_status_lines():
    """The /health command returns daemon status info."""
    # Mock context with a minimal agent shape.
    class FakeConfig:
        inference_url = "http://example.invalid:11434"
        model = "gemma-test"
        vault_path = "/tmp/nowhere"
        apk_re_agents_url = "http://127.0.0.1:8000"

    class FakeAgent:
        config = FakeConfig()
        name = "pare"

    class FakeCtx:
        agent = FakeAgent()

    cmd = Health()
    result = cmd.run("", FakeCtx())

    # Since run() is async and yields ResponseMessage, collect the output
    messages = []
    async for msg in result:
        messages.append(msg)

    assert len(messages) == 1
    output = messages[0].text
    assert "pare" in output
    assert "inference" in output.lower()
    assert "apk_re_agents" in output.lower() or "apk-re-agents" in output.lower()
    assert "gemma-test" in output


def test_health_survives_an_agent_without_a_manager():
    """/health must never be the thing that crashes."""
    import asyncio

    agent = type("A", (), {"config": _cfg(), "name": "pare"})()
    out = asyncio.run(_collect(Health(), "", type("C", (), {"agent": agent})()))
    assert "agent: pare" in out


def test_health_lists_workers_when_a_manager_is_present():
    from agent_core.workers.manager import WorkerStatus
    import asyncio

    agent = MagicMock()
    agent.config = _cfg()
    agent.name = "pare"
    agent.worker_manager.status.return_value = [
        WorkerStatus("frida", True, 19, "stdio", "low", [], True),
        WorkerStatus("hardware", False, 0, "stdio", "medium", [], False),
    ]
    out = asyncio.run(_collect(Health(), "", type("C", (), {"agent": agent})()))
    assert "frida(19)" in out and "hardware" in out
