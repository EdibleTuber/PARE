"""register_tools must close the worker pool after discovery.

Discovery runs in a throwaway event loop (`asyncio.run` inside register_tools).
MCPClientPool lazy-connects stdio workers on first use (during discovery) and
caches the client. If that connection is left open, the daemon's separate
serving loop reuses a client whose stdio streams are bound to the now-closed
discovery loop, and the first dispatched call dies with `ClosedResourceError`
(observed live: an approved `frida_list_devices` failed this way).

Closing the pool at the end of discovery drops those connections so the serving
loop reconnects lazily in its own loop. This test pins that behavior.
"""
from unittest.mock import AsyncMock, MagicMock

import pare.agent as agent_mod
from pare.agent import PareAgent
from pare.config import PAREConfig


def test_register_tools_closes_pool_after_discovery(monkeypatch):
    async def _fake_discover(specs, pool):
        return []

    monkeypatch.setattr(agent_mod, "discover_and_register", _fake_discover)

    agent = PareAgent()
    agent.config = PAREConfig()   # register_tools reads config.enable_apk_re_agents
    agent._worker_specs = []
    agent.tool_pool = MagicMock()
    agent.tool_pool.close_all = AsyncMock()

    result = agent.register_tools()

    assert result == []
    agent.tool_pool.close_all.assert_awaited_once()
