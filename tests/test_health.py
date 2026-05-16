"""Tests for the /health slash command."""
import pytest

from pare.commands.health import Health
from agent_core.protocol.messages import ResponseMessage


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
