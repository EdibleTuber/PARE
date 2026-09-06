"""Unloading a worker must not cost you its findings.

CaptureLayer.maybe_substitute takes `worker` as a plain string and holds no
pool or client reference, and SearchCapture/ReadCapture are declarative
PareAgent.tools with no `worker` attribute — so remove_worker cannot remove
them. This pins that end to end, in the repo where the store lives.
"""
from unittest.mock import MagicMock

import pytest

from agent_core.tools.executor import ToolExecutor
from pare.agent import PareAgent


class _Agent:
    """Stub agent satisfying every builtin tool's `requires` so
    ToolExecutor.build() doesn't reject them; none of the attributes are
    exercised — no tool in this test is actually run."""
    config = MagicMock()
    allowlist = MagicMock()
    fetcher = MagicMock()
    retrieval = MagicMock()
    websearch = MagicMock()
    learning = MagicMock()


def test_retrieval_tools_are_not_worker_owned():
    from agent_core.capture import ReadCapture, SearchCapture

    for cls in (SearchCapture, ReadCapture):
        assert not hasattr(cls, "worker"), (
            f"{cls.name} would be removed by remove_worker()")
    assert SearchCapture in PareAgent.tools and ReadCapture in PareAgent.tools


def test_remove_worker_leaves_the_capture_tools(tmp_path):
    """The executor-level guarantee: unloading `frida` must not take
    search_capture with it."""
    ex = ToolExecutor.build(_Agent(), [])
    before = set(ex.names())

    from agent_core.workers.tool_factory import make_tool_class
    from agent_core.workers.types import WorkerSpec
    spec = WorkerSpec(name="frida", transport="stdio", risk_default="low",
                      command="/bin/true")
    ex.add(make_tool_class(spec, {"name": "attach", "description": "d",
                                  "inputSchema": {"type": "object", "properties": {}}},
                           MagicMock()))
    ex.remove_worker("frida")
    assert set(ex.names()) == before
