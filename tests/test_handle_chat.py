"""handle_chat: streaming, tool-loop, loop-cap, and error paths."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_core.capture import CaptureStore
from agent_core.conversation import Conversation
from agent_core.inference import CompletionResult, StreamEnd, ToolCall, Usage
from agent_core.protocol import (
    ErrorMessage, ResponseMessage, StreamChunkMessage, ToolProgressMessage,
)
from pare.agent import PareAgent


def _make_agent(mode="off"):
    """A PareAgent with the framework-populated attrs stubbed for unit testing."""
    agent = PareAgent()
    agent.decide_mode = lambda conv: mode
    agent.system_prompt = lambda ctx: "SYSTEM"
    agent.tool_executor = MagicMock()
    agent.tool_executor.schemas = MagicMock(return_value=[])
    agent.inference = MagicMock()
    # Stub the capture store manager so _bind_store works without full setup().
    agent._capture_stores = MagicMock()
    agent._capture_stores.resolve.return_value = CaptureStore.open_memory()
    return agent


def _ctx():
    ctx = MagicMock()
    # Conversation is a dataclass whose first field `history_depth` is required.
    ctx.conversation = Conversation(history_depth=50)
    ctx.channel_id = "test"
    return ctx


class _Stream:
    """Async-iterable returning the given items in order."""
    def __init__(self, items):
        self._items = items

    def __aiter__(self):
        async def gen():
            for it in self._items:
                yield it
        return gen()


@pytest.mark.asyncio
async def test_streaming_text_turn():
    agent = _make_agent(mode="off")
    usage = Usage(prompt_tokens=1, completion_tokens=2, total_tokens=3)
    agent.inference.stream = MagicMock(
        return_value=_Stream(["Hello", " world", StreamEnd(finish_reason="stop",
                                                            chunks_yielded=2, usage=usage)]))
    ctx = _ctx()
    msg = MagicMock(); msg.text = "hi"

    out = [m async for m in agent.handle_chat(msg, ctx)]

    assert [type(m) for m in out] == [StreamChunkMessage, StreamChunkMessage, ResponseMessage]
    assert out[-1].text == "Hello world"
    assert agent.last_usage["test"] is usage


@pytest.mark.asyncio
async def test_tool_round_then_text():
    agent = _make_agent(mode="off")
    call = ToolCall(id="t1", name="search_vault", arguments={"query": "x"})
    agent.inference.stream = MagicMock(return_value=_Stream([[call]]))
    agent.tool_executor.run = AsyncMock(return_value="search-result")
    agent.inference.complete = AsyncMock(return_value=CompletionResult(
        type="text", content="final answer", usage=None))
    ctx = _ctx()
    msg = MagicMock(); msg.text = "hi"

    out = [m async for m in agent.handle_chat(msg, ctx)]

    assert [type(m) for m in out] == [ToolProgressMessage, ResponseMessage]
    assert out[0].tool == "search_vault"
    assert out[1].text == "final answer"
    agent.tool_executor.run.assert_awaited_once_with("search_vault", {"query": "x"}, ctx)


@pytest.mark.asyncio
async def test_loop_cap_emits_cap_message():
    agent = _make_agent(mode="on")
    call = ToolCall(id="t1", name="search_vault", arguments={})
    agent.inference.complete = AsyncMock(return_value=CompletionResult(
        type="tool_calls", tool_calls=[call], usage=None))
    agent.tool_executor.run = AsyncMock(return_value="r")
    ctx = _ctx()
    msg = MagicMock(); msg.text = "hi"

    out = [m async for m in agent.handle_chat(msg, ctx)]

    assert isinstance(out[-1], ResponseMessage)
    assert "limit" in out[-1].text.lower()


@pytest.mark.asyncio
async def test_repeat_guard_short_circuits_a_spinning_tool_call():
    """When the model re-issues the SAME call returning the SAME result, the
    guard stops hitting the backend after the hard limit — the loop-cap no
    longer has to absorb the whole spin."""
    agent = _make_agent(mode="on")
    call = ToolCall(id="t1", name="static_grep_smali", arguments={"pattern": "X"})
    # Model keeps asking for the identical call forever; tool always returns "0 matches".
    agent.inference.complete = AsyncMock(return_value=CompletionResult(
        type="tool_calls", tool_calls=[call], usage=None))
    agent.tool_executor.run = AsyncMock(return_value="0 matches")
    ctx = _ctx()
    msg = MagicMock(); msg.text = "hi"

    out = [m async for m in agent.handle_chat(msg, ctx)]

    # Loop still terminates via the cap message...
    assert isinstance(out[-1], ResponseMessage)
    # ...but the backend was invoked only up to the guard's hard limit (3),
    # not once per round (50) — the guard, not the blanket cap, did the stopping.
    assert agent.tool_executor.run.await_count <= 3
    # The model was told to change approach.
    assert any(getattr(m, "arguments", None) == {"pattern": "X"} for m in out
               if isinstance(m, ToolProgressMessage))


@pytest.mark.asyncio
async def test_exception_yields_error_message():
    agent = _make_agent(mode="off")
    agent.inference.stream = MagicMock(side_effect=RuntimeError("boom"))
    ctx = _ctx()
    msg = MagicMock(); msg.text = "hi"

    out = [m async for m in agent.handle_chat(msg, ctx)]

    assert any(isinstance(m, ErrorMessage) for m in out)
    assert "boom" in out[-1].error
