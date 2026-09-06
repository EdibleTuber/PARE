"""ashutdown must never leave subprocesses or capture stores dangling.

Two failure modes fixed here:
  - worker_manager.close_all() suppresses plain Exception internally but not
    CancelledError; if that propagates, the capture stores below it in
    ashutdown's body never ran.
  - astartup can raise before self.worker_manager is ever assigned (e.g.
    WorkerManager() rejecting a misconfigured pool) -- worker_manager stays
    None, and the mcp_pool it would have wrapped was never closed at all.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from pare.agent import PareAgent

pytestmark = pytest.mark.asyncio


async def test_ashutdown_closes_capture_stores_even_if_worker_close_raises_cancelled():
    agent = PareAgent()
    agent.worker_manager = MagicMock()
    # ashutdown stops the liveness probe before closing connections, so a
    # probe cannot fire against a worker being torn down and log a spurious
    # "unreachable" for a shutdown the operator asked for.
    agent.worker_manager.stop_liveness = AsyncMock()
    agent.worker_manager.close_all = AsyncMock(side_effect=asyncio.CancelledError())
    agent._capture_stores = MagicMock()

    with pytest.raises(asyncio.CancelledError):
        await agent.ashutdown()

    agent._capture_stores.close_all.assert_called_once()


async def test_ashutdown_closes_mcp_pool_directly_when_worker_manager_never_built():
    agent = PareAgent()
    agent.worker_manager = None
    agent.mcp_pool = MagicMock()
    agent.mcp_pool.close_all = AsyncMock()
    agent._capture_stores = MagicMock()

    await agent.ashutdown()

    agent.mcp_pool.close_all.assert_awaited_once()
    agent._capture_stores.close_all.assert_called_once()
