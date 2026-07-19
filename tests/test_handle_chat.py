"""handle_chat: streaming, tool-loop, loop-cap, and error paths."""
import itertools
import json
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
    # setup() isn't run in these unit tests; stub the attr it would create.
    agent._disambig_resolved = {}
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
    """Genuine round-cap scenario: arguments change every round, so
    RepeatGuard never trips (each signature is new) and the spin handback
    never fires — the coarse MAX_TOOL_ROUNDS backstop is what has to end
    the turn. (A verbatim-repeat scenario is now covered by the spin
    handback tests instead — see test_spin_hands_back_wellformed.)"""
    agent = _make_agent(mode="on")
    counter = itertools.count()

    async def _complete(*_a, **_k):
        i = next(counter)
        call = ToolCall(id=f"t{i}", name="search_vault", arguments={"query": str(i)})
        return CompletionResult(type="tool_calls", tool_calls=[call], usage=None)

    agent.inference.complete = _complete
    agent.tool_executor.run = AsyncMock(return_value="r")
    ctx = _ctx()
    msg = MagicMock(); msg.text = "hi"

    out = [m async for m in agent.handle_chat(msg, ctx)]

    assert isinstance(out[-1], ResponseMessage)
    assert "limit" in out[-1].text.lower()


@pytest.mark.asyncio
async def test_repeat_guard_short_circuits_a_spinning_tool_call():
    """When the model re-issues the SAME call returning the SAME result, the
    guard stops hitting the backend after the hard limit — and now (with the
    spin handback wired in) the turn ends by handing back to the operator
    rather than grinding on to the round cap. See test_spin_hands_back_wellformed
    for the well-formedness assertion on this same scenario."""
    agent = _make_agent(mode="on")
    call = ToolCall(id="t1", name="static_grep_smali", arguments={"pattern": "X"})
    # Model keeps asking for the identical call forever; tool always returns "0 matches".
    agent.inference.complete = AsyncMock(return_value=CompletionResult(
        type="tool_calls", tool_calls=[call], usage=None))
    agent.tool_executor.run = AsyncMock(return_value="0 matches")
    ctx = _ctx()
    msg = MagicMock(); msg.text = "hi"

    out = [m async for m in agent.handle_chat(msg, ctx)]

    # The turn ends with the operator handback question, not the loop-cap...
    assert isinstance(out[-1], ResponseMessage)
    assert "stuck" in out[-1].text.lower()
    # ...and the backend was invoked only up to the guard's hard limit (3),
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


# --- Operator-handback checkpoints (spin trigger + commit-time disambiguation) ---

def _msg(text):
    msg = MagicMock()
    msg.text = text
    return msg


def _assert_toolcalls_paired(msgs):
    """Every assistant message carrying `tool_calls` must be followed by a
    `tool` message for each of its ids — a dangling id makes the next API
    request invalid."""
    for i, m in enumerate(msgs):
        if m.get("role") != "assistant" or not m.get("tool_calls"):
            continue
        ids = {tc["id"] for tc in m["tool_calls"]}
        seen = set()
        j = i + 1
        while j < len(msgs) and msgs[j].get("role") == "tool":
            seen.add(msgs[j]["tool_call_id"])
            j += 1
        missing = ids - seen
        assert not missing, f"tool_calls {missing} at index {i} have no tool result"


# Two near-duplicate classes referenced by one grep result — the smali tokens
# are scanned by candidate_classes() regardless of which JSON field holds them.
_GREP_RESULT_2VARIANTS = json.dumps({"rows": [
    {"match": "Lsg/vp/owasp_mobile/OMTG_Android/OMTG_DATAST_001_SQLite_Encrypted;->test()V"},
    {"match": "Lsg/vp/owasp_mobile/OMTG_Android/OMTG_DATAST_001_SQLite_Not_Encrypted;->test()V"},
]})

# A single candidate — nothing to disambiguate.
_GREP_RESULT_1VARIANT = json.dumps({"rows": [
    {"match": "Lsg/vp/owasp_mobile/OMTG_Android/OMTG_DATAST_001_SQLite_Encrypted;->test()V"},
]})


@pytest.mark.asyncio
async def test_spin_hands_back_wellformed():
    """Trigger 1 (spin): once RepeatGuard.tripped fires for a non-poll tool,
    the turn ends with an operator question instead of grinding to the
    round cap — and the message list stays well-formed (every tool_calls id
    settled before the handback text is appended)."""
    agent = _make_agent(mode="on")
    call = ToolCall(id="t1", name="static_grep_smali", arguments={"pattern": "X"})
    agent.inference.complete = AsyncMock(return_value=CompletionResult(
        type="tool_calls", tool_calls=[call], usage=None))
    agent.tool_executor.run = AsyncMock(return_value='{"rows": []}')
    ctx = _ctx()

    out = [m async for m in agent.handle_chat(_msg("hi"), ctx)]

    assert isinstance(out[-1], ResponseMessage) and "stuck" in out[-1].text.lower()
    msgs = ctx.conversation.get_messages_for_api(system_prompt="S")
    _assert_toolcalls_paired(msgs)


@pytest.mark.asyncio
async def test_ambiguous_commit_not_dispatched():
    """Trigger 2 (commit-time disambiguation): a prior grep surfaced two
    near-duplicate classes; when the model commits to one of them, the
    commit tool must NOT run — the turn hands back with the choice instead.

    Uses mode="off" (not the brief's mode="on") so the first turn goes
    through inference.stream() (-> grep) and the follow-up round goes
    through inference.complete() (-> commit) — matching the "first
    inference: grep; second: commit" comment. mode="on" would route BOTH
    calls through inference.complete(), so the stream()-stubbed grep would
    never run and the single-item complete() side_effect would fire on the
    very first turn with no prior grep to disambiguate against.
    """
    agent = _make_agent(mode="off")
    grep = ToolCall(id="g", name="static_grep_smali", arguments={"pattern": "OMTG_DATAST_001_SQLite"})
    commit = ToolCall(id="c", name="static_decompile_method",
                      arguments={"cls": "sg.vp.owasp_mobile.OMTG_Android.OMTG_DATAST_001_SQLite_Encrypted"})
    agent.inference.stream = MagicMock(return_value=_Stream([[grep]]))
    agent.inference.complete = AsyncMock(side_effect=[
        CompletionResult(type="tool_calls", tool_calls=[commit], usage=None),
    ])
    agent.tool_executor.run = AsyncMock(return_value=_GREP_RESULT_2VARIANTS)
    ctx = _ctx()

    out = [m async for m in agent.handle_chat(_msg("hi"), ctx)]

    dispatched = [c.args[0] for c in agent.tool_executor.run.await_args_list]
    assert "static_decompile_method" not in dispatched
    assert isinstance(out[-1], ResponseMessage) and "Not_Encrypted" in out[-1].text
    msgs = ctx.conversation.get_messages_for_api(system_prompt="S")
    _assert_toolcalls_paired(msgs)


@pytest.mark.asyncio
async def test_unambiguous_commit_runs():
    """A grep that surfaces exactly one candidate class is not ambiguous —
    committing to it must dispatch normally, no handback."""
    agent = _make_agent(mode="off")
    grep = ToolCall(id="g", name="static_grep_smali", arguments={"pattern": "OMTG_DATAST_001_SQLite"})
    commit = ToolCall(id="c", name="static_decompile_method",
                      arguments={"cls": "sg.vp.owasp_mobile.OMTG_Android.OMTG_DATAST_001_SQLite_Encrypted"})
    agent.inference.stream = MagicMock(return_value=_Stream([[grep]]))
    agent.inference.complete = AsyncMock(side_effect=[
        CompletionResult(type="tool_calls", tool_calls=[commit], usage=None),
        CompletionResult(type="text", content="decompiled it", usage=None),
    ])
    agent.tool_executor.run = AsyncMock(return_value=_GREP_RESULT_1VARIANT)
    ctx = _ctx()

    out = [m async for m in agent.handle_chat(_msg("hi"), ctx)]

    dispatched = [c.args[0] for c in agent.tool_executor.run.await_args_list]
    assert dispatched == ["static_grep_smali", "static_decompile_method"]
    assert isinstance(out[-1], ResponseMessage) and out[-1].text == "decompiled it"
    msgs = ctx.conversation.get_messages_for_api(system_prompt="S")
    _assert_toolcalls_paired(msgs)


@pytest.mark.asyncio
async def test_poll_spin_does_not_handback():
    """A poll tool (frida_read_hook_events / list_sessions) that keeps
    returning the same result is exempt from the spin handback — legitimate
    re-polls must not be penalized into an early stop (see repeat_guard.py).
    The backend still stops being hit once the guard's hard limit kicks in,
    but the turn runs to the round cap instead of handing back early."""
    agent = _make_agent(mode="on")
    call = ToolCall(id="t1", name="list_sessions", arguments={})
    agent.inference.complete = AsyncMock(return_value=CompletionResult(
        type="tool_calls", tool_calls=[call], usage=None))
    agent.tool_executor.run = AsyncMock(return_value="no sessions")
    ctx = _ctx()

    out = [m async for m in agent.handle_chat(_msg("hi"), ctx)]

    assert isinstance(out[-1], ResponseMessage)
    assert "stuck" not in out[-1].text.lower()
    assert "limit" in out[-1].text.lower()
    assert agent.tool_executor.run.await_count <= 3
    msgs = ctx.conversation.get_messages_for_api(system_prompt="S")
    _assert_toolcalls_paired(msgs)
