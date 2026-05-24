"""Phase 3 end-to-end smoke: PARE → MCP-direct apk_re_agents agents.

Requires apk_re_agents to be running (docker compose up) at the
endpoints listed in workers.yaml (default: localhost:9000-9007).

Enable with:
    PARE_PHASE3_SMOKE=1 pytest tests/test_phase3_smoke.py -v
"""
import os

import pytest

from agent_core.workers import MCPClientPool, discover_and_register
from agent_core.workers.registry import WorkerRegistry


pytestmark = pytest.mark.skipif(
    os.getenv("PARE_PHASE3_SMOKE") != "1",
    reason="set PARE_PHASE3_SMOKE=1 (with apk_re_agents docker compose up) to run",
)


@pytest.mark.asyncio
async def test_discovers_all_apk_re_agents_workers():
    """discover_and_register against the live apk_re_agents stack returns at
    least one tool per worker (8 workers, so at least 8 tools)."""
    registry = WorkerRegistry.load("workers.yaml")
    pool = MCPClientPool(registry.all())
    try:
        specs = registry.all()
        tool_classes = await discover_and_register(specs, pool)
        names_by_worker: dict[str, list[str]] = {}
        for cls in tool_classes:
            worker_name = cls.name.split("_", 1)[0]
            names_by_worker.setdefault(worker_name, []).append(cls.name)

        # Every workers.yaml entry should produce at least one tool.
        for spec in specs:
            assert spec.name in names_by_worker, (
                f"worker {spec.name!r} produced no tools"
            )
    finally:
        await pool.close_all()


@pytest.mark.asyncio
async def test_unpacker_run_jadx_is_registered():
    """The unpacker agent's run_jadx tool is registered as unpacker_run_jadx."""
    registry = WorkerRegistry.load("workers.yaml")
    pool = MCPClientPool(registry.all())
    try:
        tool_classes = await discover_and_register(registry.all(), pool)
        names = {cls.name for cls in tool_classes}
        assert "unpacker_run_jadx" in names, (
            f"expected unpacker_run_jadx in registered tools, got: {sorted(names)}"
        )
    finally:
        await pool.close_all()
