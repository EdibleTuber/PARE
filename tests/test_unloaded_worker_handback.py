"""A turn must not burn its round budget on an unloaded worker.

RepeatGuard cannot save us here: different frida_* tools are different guard
signatures, and POLL_TOOLS deliberately exempts frida_read_hook_events from
handback because polling is supposed to repeat.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_core.protocol import ChatMessage, ResponseMessage
from pare.agent import PareAgent

pytestmark = pytest.mark.asyncio


class _Call:
    def __init__(self, name, args=None, id_=None):
        self.id = id_ or f"c-{name}"
        self.name = name
        self.arguments = args or {}


class _Completion:
    def __init__(self, tool_calls):
        self.type = "tool_calls"
        self.tool_calls = tool_calls
        self.content = None
        self.reasoning = None
        self.usage = None


def _agent(tool_call_script):
    """An agent whose model emits `tool_call_script` batches, one per round."""
    agent = PareAgent()
    agent.config = MagicMock(context_window_tokens=8000, history_depth=10)
    agent._disambig_resolved = {}
    agent.worker_manager = MagicMock()
    agent.worker_manager.worker_of = lambda n: n.split("_", 1)[0] if "_" in n else None
    agent.worker_manager.unavailable_reason = lambda w: (
        f"worker {w!r} is not loaded — run /worker load {w}." if w == "frida" else None)

    agent.tool_executor = MagicMock()
    agent.tool_executor.schemas.return_value = []
    agent.tool_executor.run = AsyncMock(return_value="Unknown tool")
    agent.inference = MagicMock()
    agent.inference.complete = AsyncMock(
        side_effect=[_Completion(b) for b in tool_call_script[1:]])
    agent.record_usage = MagicMock()
    agent.decide_mode = MagicMock(return_value="on")
    agent.system_prompt = MagicMock(return_value="")
    agent._first_batch = tool_call_script[0]
    return agent


async def _drive(agent, monkeypatch):
    """Run one turn, returning every yielded message."""
    conv = MagicMock()
    conv.get_messages_for_api.return_value = []
    ctx = MagicMock(conversation=conv, channel_id="t", cwd=None)
    agent.inference.complete = AsyncMock(
        side_effect=[_Completion(agent._first_batch)] + list(
            agent.inference.complete.side_effect))
    monkeypatch.setattr(agent, "_bind_store", lambda c: __import__("contextlib").nullcontext())
    return [m async for m in agent.handle_chat(ChatMessage(text="go"), ctx)], conv


async def test_three_distinct_unloaded_tools_hand_back_fast(monkeypatch):
    """Not 50 rounds. RepeatGuard never trips on distinct signatures."""
    script = [[_Call("frida_attach")], [_Call("frida_list_devices")],
              [_Call("frida_enumerate_processes")]] + [[_Call("frida_attach")]] * 50
    agent = _agent(script)
    msgs, conv = await _drive(agent, monkeypatch)
    text = "\n".join(m.text for m in msgs if isinstance(m, ResponseMessage))
    assert "not loaded" in text
    assert agent.inference.complete.await_count <= 3, (
        f"handback took {agent.inference.complete.await_count} rounds")


async def test_poll_tool_still_hands_back(monkeypatch):
    """frida_read_hook_events is in POLL_TOOLS, exempt from the spin handback —
    so without this trigger an unloaded frida would poll to the round cap."""
    script = [[_Call("frida_read_hook_events")]] * 51
    agent = _agent(script)
    msgs, conv = await _drive(agent, monkeypatch)
    text = "\n".join(m.text for m in msgs if isinstance(m, ResponseMessage))
    assert "not loaded" in text
    assert agent.inference.complete.await_count <= 3


async def test_every_tool_call_id_is_settled(monkeypatch):
    """A dangling tool_call id with no matching result makes the NEXT turn's
    API request invalid."""
    batch = [_Call("frida_attach", id_="a"), _Call("frida_detach", id_="b")]
    script = [batch, batch, batch]
    agent = _agent(script)
    _msgs, conv = await _drive(agent, monkeypatch)
    settled = {call.args[0] for call in conv.add_tool_result.call_args_list}
    assert {"a", "b"} <= settled


async def test_loaded_worker_is_unaffected(monkeypatch):
    """The trigger must not fire for a worker that is loaded."""
    script = [[_Call("static_grep_smali")], [_Call("static_grep_smali")]]
    agent = _agent(script)
    msgs, conv = await _drive(agent, monkeypatch)
    text = "\n".join(m.text for m in msgs if isinstance(m, ResponseMessage))
    assert "not loaded" not in text
