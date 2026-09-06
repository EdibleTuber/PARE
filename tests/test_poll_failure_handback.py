"""A degrading link must not poll silently to the round cap.

`frida_read_hook_events` is in POLL_TOOLS, which the spin guard deliberately
exempts because polling is supposed to repeat. Combined with an unbounded
call, that made the ONE tool the system prompt tells the model to hammer the
one tool with no backstop when its link goes bad.

The worker here is LOADED -- this is not the unloaded-worker handback. The
link is degraded, which is the case that produced nothing but a wedged turn.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_core.protocol import ChatMessage, ResponseMessage
from pare.agent import PareAgent

pytestmark = pytest.mark.asyncio


class _Call:
    def __init__(self, name, args=None, id_=None):
        self.id = id_ or f"c-{name}-{id(self)}"
        self.name = name
        self.arguments = args or {}


class _Completion:
    def __init__(self, tool_calls):
        self.type = "tool_calls"
        self.tool_calls = tool_calls
        self.content = None
        self.reasoning = None
        self.usage = None


def _agent(results):
    """An agent whose frida_read_hook_events returns `results` in order."""
    agent = PareAgent()
    agent.config = MagicMock(context_window_tokens=8000, history_depth=10)
    agent._disambig_resolved = {}
    agent.worker_manager = MagicMock()
    agent.worker_manager.worker_of = lambda n: n.split("_", 1)[0] if "_" in n else None
    agent.worker_manager.unavailable_reason = lambda w: None   # LOADED
    agent.tool_executor = MagicMock()
    agent.tool_executor.schemas.return_value = []
    agent.tool_executor.run = AsyncMock(side_effect=list(results))
    agent.inference = MagicMock()
    agent.record_usage = MagicMock()
    agent.decide_mode = MagicMock(return_value="on")
    agent.system_prompt = MagicMock(return_value="")
    return agent


async def _drive(agent, rounds, monkeypatch):
    conv = MagicMock()
    conv.get_messages_for_api.return_value = []
    ctx = MagicMock(conversation=conv, channel_id="t", cwd=None)
    agent.inference.complete = AsyncMock(
        side_effect=[_Completion([_Call("frida_read_hook_events")])
                     for _ in range(rounds)])
    monkeypatch.setattr(agent, "_bind_store",
                        lambda c: __import__("contextlib").nullcontext())
    msgs = [m async for m in agent.handle_chat(ChatMessage(text="go"), ctx)]
    return "\n".join(m.text for m in msgs if isinstance(m, ResponseMessage))


FAIL = "frida_read_hook_events call failed: ConnectError: All connection attempts failed"
EMPTY = '{"events": [], "buffered_remaining": 0}'


async def test_three_consecutive_failed_polls_hand_back(monkeypatch):
    text = await _drive(_agent([FAIL] * 60), 60, monkeypatch)
    assert "stopped polling" in text.lower()
    assert "/worker list" in text, "the useful next action is checking the link"
    assert "ConnectError" in text, "the operator needs the actual error"


async def test_it_hands_back_fast_rather_than_burning_the_budget(monkeypatch):
    agent = _agent([FAIL] * 60)
    await _drive(agent, 60, monkeypatch)
    assert agent.inference.complete.await_count <= 4, (
        f"took {agent.inference.complete.await_count} rounds to notice a dead link")


async def test_empty_polls_never_hand_back(monkeypatch):
    """The distinction the trigger rests on. An empty poll is the NORMAL
    result and may repeat all turn; handing back on it would break the
    workflow POLL_TOOLS exists to allow."""
    agent = _agent([EMPTY] * 12)
    text = await _drive(agent, 12, monkeypatch)
    assert "stopped polling" not in text.lower()


async def test_a_success_between_failures_resets_the_count(monkeypatch):
    """The trigger is CONSECUTIVE failure. A flaky link that recovers is not
    a wedged session, and re-prompting on it would train the operator to
    ignore the handback."""
    agent = _agent([FAIL, FAIL, EMPTY, FAIL, FAIL, EMPTY] * 4)
    text = await _drive(agent, 24, monkeypatch)
    assert "stopped polling" not in text.lower()


async def test_the_isError_shape_also_counts(monkeypatch):
    """agent_core renders a worker-side error differently from a transport
    failure; both mean the poll is not working."""
    agent = _agent(["frida_read_hook_events returned an error: session gone"] * 60)
    text = await _drive(agent, 60, monkeypatch)
    assert "stopped polling" in text.lower()
