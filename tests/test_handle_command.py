"""handle_command delegates to the framework command registry."""
from unittest.mock import MagicMock

import pytest

from agent_core.capture import CaptureStore
from agent_core.protocol import ResponseMessage
from pare.agent import PareAgent


class _FakeRegistry:
    def __init__(self, outputs):
        self._outputs = outputs
        self.calls = []

    async def dispatch(self, name, args, ctx):
        self.calls.append((name, args))
        for out in self._outputs:
            yield out


@pytest.mark.asyncio
async def test_handle_command_passes_through_registry_output():
    agent = PareAgent()
    # Stub the capture store manager so _bind_store works without full setup().
    agent._capture_stores = MagicMock()
    agent._capture_stores.resolve.return_value = CaptureStore.open_memory()
    agent.command_registry = _FakeRegistry([ResponseMessage(text="pong")])
    ctx = MagicMock()
    msg = MagicMock(name="cmd", args="")
    msg.name = "ping"
    msg.args = ""

    collected = [out async for out in agent.handle_command(msg, ctx)]

    assert len(collected) == 1
    assert isinstance(collected[0], ResponseMessage)
    assert collected[0].text == "pong"
    assert agent.command_registry.calls == [("ping", "")]
