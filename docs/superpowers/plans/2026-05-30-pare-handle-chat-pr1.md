# PARE handle_chat (PR1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make PARE able to hold a tool-using conversation backed by PAL's research vault — implement `handle_chat`, `handle_command`, a `read_vault_doc` tool, system-prompt guidance, and disable the vault-scoped shell builtins.

**Architecture:** Port PAL's proven streaming + tool-loop into `PareAgent.handle_chat` using agent_core's pure-`yield` daemon model (risk gating and operator approval are already transparent via `tool_pool`/the daemon). PAL's research is read over RAG only: the existing `search_vault` builtin for discovery plus a new `read_vault_doc` tool for full bodies. `vault_path` is left at its PARE-owned default so PARE's own state never touches PAL's git repo.

**Tech Stack:** Python 3.12+, `agent_core` (`Agent`, `InferenceClient`, `Conversation`, `Tool`, protocol messages), `pytest` + `pytest-asyncio`, `unittest.mock`.

**Spec:** `docs/superpowers/specs/2026-05-30-pare-handle-chat-design.md`. PR2 (the `workspace_path` + write tools) is deferred to its own design and is out of scope here.

---

## File structure

- `pare/tools/read_vault_doc.py` — **create.** The `ReadVaultDoc` Tool (RAG full-body fetch).
- `pare/tools/__init__.py` — **modify.** Export `ReadVaultDoc`.
- `pare/agent.py` — **modify.** Add `handle_chat`, `handle_command`; register `ReadVaultDoc`; add `disabled_builtins`; add module logger + imports.
- `pare/prompts/system.md` — **modify.** Add vault-usage guidance.
- `tests/test_read_vault_doc.py` — **create.**
- `tests/test_handle_chat.py` — **create.**
- `tests/test_handle_command.py` — **create.**
- `tests/test_disabled_builtins.py` — **create.**
- `tests/test_system_prompt.py` — **create.**

Run all tests with: `cd /home/edible/Projects/PARE && python -m pytest tests/ -q`

---

## Task 1: `read_vault_doc` tool

Fetches a full vault document body over RAG. Mirrors `agent_core/tools/_framework.py:SearchVault`. `search_vault` surfaces a `path` field of the form `"{id}.md"`; this tool accepts that `path`, strips a trailing `.md` to recover the `doc_id`, and calls `ctx.agent.retrieval.get_document(doc_id)` (`agent_core/retrieval.py:49`).

**Files:**
- Create: `pare/tools/read_vault_doc.py`
- Modify: `pare/tools/__init__.py`
- Test: `tests/test_read_vault_doc.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_read_vault_doc.py`:

```python
"""Tests for the read_vault_doc Tool."""
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from pare.tools.read_vault_doc import ReadVaultDoc


@pytest.mark.asyncio
async def test_read_vault_doc_returns_content():
    tool = ReadVaultDoc()
    ctx = MagicMock()
    ctx.agent.retrieval.get_document = AsyncMock(return_value={
        "id": "AI/agents",
        "name": "Agents",
        "summary": "about agents",
        "content": "FULL BODY TEXT",
    })

    result = await tool.run({"path": "AI/agents.md"}, ctx)

    payload = json.loads(result)
    assert payload["status"] == "ok"
    assert payload["content"] == "FULL BODY TEXT"
    assert payload["name"] == "Agents"
    # The ".md" suffix is stripped to recover the doc_id.
    ctx.agent.retrieval.get_document.assert_awaited_once_with("AI/agents")


@pytest.mark.asyncio
async def test_read_vault_doc_not_found_returns_error_string():
    tool = ReadVaultDoc()
    ctx = MagicMock()
    ctx.agent.retrieval.get_document = AsyncMock(side_effect=FileNotFoundError("nope"))
    result = await tool.run({"path": "missing.md"}, ctx)
    payload = json.loads(result)
    assert payload["status"] == "error"
    assert "not found" in payload["reason"].lower()


@pytest.mark.asyncio
async def test_read_vault_doc_traversal_returns_error_string():
    tool = ReadVaultDoc()
    ctx = MagicMock()
    ctx.agent.retrieval.get_document = AsyncMock(side_effect=ValueError("Invalid doc_id"))
    result = await tool.run({"path": "../etc/passwd"}, ctx)
    payload = json.loads(result)
    assert payload["status"] == "error"


@pytest.mark.asyncio
async def test_read_vault_doc_missing_path_returns_error_string():
    tool = ReadVaultDoc()
    ctx = MagicMock()
    result = await tool.run({}, ctx)
    payload = json.loads(result)
    assert payload["status"] == "error"


def test_read_vault_doc_metadata():
    assert ReadVaultDoc.name == "read_vault_doc"
    assert ReadVaultDoc.requires == ("retrieval",)
    assert "path" in ReadVaultDoc.parameters["properties"]
    assert "path" in ReadVaultDoc.parameters.get("required", [])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/edible/Projects/PARE && python -m pytest tests/test_read_vault_doc.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'pare.tools.read_vault_doc'`.

- [ ] **Step 3: Write minimal implementation**

Create `pare/tools/read_vault_doc.py`:

```python
"""read_vault_doc — fetch a full vault document body over the retrieval service.

Companion to the framework `search_vault` builtin: `search_vault` returns hits
with a `path` field (`"{id}.md"`); this tool takes that `path`, recovers the
`doc_id`, and returns the document's full content. RAG-only — no local filesystem
access, so PARE never needs PAL's vault mounted locally.
"""
from __future__ import annotations

import json
from typing import Any

from agent_core.tools.base import Tool

_MAX_CONTENT_CHARS = 20000


class ReadVaultDoc(Tool):
    name = "read_vault_doc"
    description = (
        "Fetch the full body of a vault document found via search_vault. "
        "Pass the `path` value from a search_vault result. Returns JSON: "
        "{status, name, summary, content}."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "The `path` field from a search_vault result, e.g. 'AI/agents.md'.",
            },
        },
        "required": ["path"],
    }
    requires = ("retrieval",)

    async def run(self, args: dict[str, Any], ctx: Any) -> str:
        path = (args.get("path") or "").strip()
        if not path:
            return json.dumps({"status": "error", "reason": "'path' parameter is required."})
        doc_id = path[:-3] if path.endswith(".md") else path
        try:
            doc = await ctx.agent.retrieval.get_document(doc_id)
        except FileNotFoundError:
            return json.dumps({"status": "error", "path": path,
                               "reason": f"Document not found: {path}"})
        except Exception as exc:
            return json.dumps({"status": "error", "path": path,
                               "reason": f"{type(exc).__name__}: {exc}"})
        content = doc.get("content", "") or ""
        if len(content) > _MAX_CONTENT_CHARS:
            content = content[:_MAX_CONTENT_CHARS] + "\n…[truncated]"
        return json.dumps({
            "status": "ok",
            "name": doc.get("name") or doc_id,
            "summary": doc.get("summary", ""),
            "content": content,
        })
```

Modify `pare/tools/__init__.py` to:

```python
"""PARE tool exports."""
from pare.tools.read_vault_doc import ReadVaultDoc
from pare.tools.static_analyze import StaticAnalyze

__all__ = ["ReadVaultDoc", "StaticAnalyze"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/edible/Projects/PARE && python -m pytest tests/test_read_vault_doc.py -q`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add pare/tools/read_vault_doc.py pare/tools/__init__.py tests/test_read_vault_doc.py
git commit -m "feat(tools): add read_vault_doc (RAG full-body fetch for PAL vault)"
```

---

## Task 2: Register `read_vault_doc` on the agent

**Files:**
- Modify: `pare/agent.py` (imports + `tools` ClassVar)
- Test: `tests/test_read_vault_doc.py` (add registration assertion)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_read_vault_doc.py`:

```python
def test_read_vault_doc_registered_on_agent():
    from pare.agent import PareAgent
    assert ReadVaultDoc in PareAgent.tools
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/edible/Projects/PARE && python -m pytest tests/test_read_vault_doc.py::test_read_vault_doc_registered_on_agent -q`
Expected: FAIL — `assert ReadVaultDoc in [StaticAnalyze]`.

- [ ] **Step 3: Write minimal implementation**

In `pare/agent.py`, change the import line `from pare.tools import StaticAnalyze` to:

```python
from pare.tools import ReadVaultDoc, StaticAnalyze
```

and change the `tools` ClassVar:

```python
    tools = [StaticAnalyze, ReadVaultDoc]  # add Tool subclasses here
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/edible/Projects/PARE && python -m pytest tests/test_read_vault_doc.py -q`
Expected: PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
git add pare/agent.py
git commit -m "feat(agent): register read_vault_doc tool"
```

---

## Task 3: Disable the vault-scoped shell builtins

Under the RAG-only read design, `vault_path` is PARE's **private state dir**, not a research corpus. The framework shell builtins (`cat/head/tail/ls/grep/find/read_lines`) are scoped to `vault_path`, so exposing them lets the model grep PARE's own profile/wisdom files — noise. Disable them via `disabled_builtins`. `search_vault` and the new `read_vault_doc` stay enabled.

**Files:**
- Modify: `pare/agent.py` (add `disabled_builtins` ClassVar)
- Test: `tests/test_disabled_builtins.py`

- [ ] **Step 1: Confirm the exact builtin names**

Run: `grep -n "name = " /home/edible/Projects/agent_core/agent_core/tools/_shell.py`
Expected: confirms the seven names `cat`, `head`, `tail`, `ls`, `grep`, `find`, `read_lines`. If any differ, use the actual values in Steps 2–3.

- [ ] **Step 2: Write the failing test**

Create `tests/test_disabled_builtins.py`:

```python
"""PARE disables the vault-scoped shell builtins (vault_path is PARE's private
state dir under the RAG-only read design, not a research corpus)."""
from pare.agent import PareAgent


def test_shell_builtins_disabled():
    expected = {"cat", "head", "tail", "ls", "grep", "find", "read_lines"}
    assert expected.issubset(PareAgent.disabled_builtins)


def test_search_vault_not_disabled():
    # PAL research access must stay enabled.
    assert "search_vault" not in PareAgent.disabled_builtins
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd /home/edible/Projects/PARE && python -m pytest tests/test_disabled_builtins.py -q`
Expected: FAIL — `disabled_builtins` is the empty `frozenset()` inherited from `Agent`.

- [ ] **Step 4: Write minimal implementation**

In `pare/agent.py`, add this ClassVar to `PareAgent` (just after `commands = [...]`):

```python
    # vault_path is PARE's private state dir (RAG-only reads of PAL's vault),
    # so the framework shell builtins — scoped to vault_path — would only let
    # the model grep PARE's own state. Disable them; PAL research goes through
    # search_vault + read_vault_doc. Workspace-scoped reads return in PR2.
    disabled_builtins = frozenset({
        "cat", "head", "tail", "ls", "grep", "find", "read_lines",
    })
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd /home/edible/Projects/PARE && python -m pytest tests/test_disabled_builtins.py -q`
Expected: PASS (2 passed).

- [ ] **Step 6: Commit**

```bash
git add pare/agent.py tests/test_disabled_builtins.py
git commit -m "feat(agent): disable vault-scoped shell builtins (RAG-only reads)"
```

---

## Task 4: `handle_command`

**Files:**
- Modify: `pare/agent.py` (add `handle_command` + imports)
- Test: `tests/test_handle_command.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_handle_command.py`:

```python
"""handle_command delegates to the framework command registry."""
from unittest.mock import MagicMock

import pytest

from pare.agent import PareAgent
from agent_core.protocol import ResponseMessage


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/edible/Projects/PARE && python -m pytest tests/test_handle_command.py -q`
Expected: FAIL — base `handle_command` raises `NotImplementedError`.

- [ ] **Step 3: Write minimal implementation**

In `pare/agent.py`, ensure these imports exist near the top (add what's missing):

```python
import json
import logging
from typing import AsyncIterator

from agent_core.protocol import (
    ChatMessage,
    CommandMessage,
    ErrorMessage,
    ResponseMessage,
    StreamChunkMessage,
    ToolProgressMessage,
)

logger = logging.getLogger(__name__)
```

Add this method to `PareAgent`:

```python
    async def handle_command(
        self, msg: CommandMessage, ctx: HandlerContext,
    ) -> AsyncIterator[object]:
        """Delegate to the framework command registry (serves /hello, /health,
        and the builtins /help, /clear, /context)."""
        async for out in self.command_registry.dispatch(msg.name, msg.args, ctx):
            yield out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/edible/Projects/PARE && python -m pytest tests/test_handle_command.py -q`
Expected: PASS (1 passed).

- [ ] **Step 5: Commit**

```bash
git add pare/agent.py tests/test_handle_command.py
git commit -m "feat(agent): implement handle_command (registry delegation)"
```

---

## Task 5: `handle_chat`

Port PAL's loop (`pal/agent.py:406`), pure-`yield`. Risk gating + approval are transparent via `tool_executor.run` → `tool_pool`; the daemon emits each yielded message.

**Files:**
- Modify: `pare/agent.py` (add `handle_chat`)
- Test: `tests/test_handle_chat.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_handle_chat.py`:

```python
"""handle_chat: streaming, tool-loop, loop-cap, and error paths."""
from unittest.mock import AsyncMock, MagicMock

import pytest

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
    # complete() always returns more tool calls -> loop runs to the cap.
    agent.inference.complete = AsyncMock(return_value=CompletionResult(
        type="tool_calls", tool_calls=[call], usage=None))
    agent.tool_executor.run = AsyncMock(return_value="r")
    ctx = _ctx()
    msg = MagicMock(); msg.text = "hi"

    out = [m async for m in agent.handle_chat(msg, ctx)]

    assert isinstance(out[-1], ResponseMessage)
    assert "limit" in out[-1].text.lower()


@pytest.mark.asyncio
async def test_exception_yields_error_message():
    agent = _make_agent(mode="off")
    agent.inference.stream = MagicMock(side_effect=RuntimeError("boom"))
    ctx = _ctx()
    msg = MagicMock(); msg.text = "hi"

    out = [m async for m in agent.handle_chat(msg, ctx)]

    assert any(isinstance(m, ErrorMessage) for m in out)
    assert "boom" in out[-1].error
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/edible/Projects/PARE && python -m pytest tests/test_handle_chat.py -q`
Expected: FAIL — base `handle_chat` raises `NotImplementedError`.

- [ ] **Step 3: Write minimal implementation**

Add this method to `PareAgent` (uses the imports added in Task 4):

```python
    async def handle_chat(
        self, msg: ChatMessage, ctx: HandlerContext,
    ) -> AsyncIterator[object]:
        """Stream a reply, running tools when the model calls them.

        Pure-yield: the daemon emits every yielded message. Risk gating and
        operator approval happen transparently inside tool_executor.run ->
        tool_pool.call_tool (the daemon's read loop resolves the approval
        future while we're parked on the await). Ported from pal/agent.py.
        """
        from agent_core.inference import StreamEnd

        conv = ctx.conversation
        conv.add_user(msg.text)
        mode = self.decide_mode(conv)            # "on" | "off" (never "auto")
        messages = conv.get_messages_for_api(system_prompt=self.system_prompt(ctx))
        schemas = self.tool_executor.schemas()
        MAX_TOOL_ROUNDS = 50
        MAX_TOKENS = 4096                        # runaway-loop stopgap (matches PAL)

        try:
            tool_calls = None
            if mode == "on":
                completion = await self.inference.complete(
                    messages, tools=schemas, reasoning=mode, max_tokens=MAX_TOKENS)
                self.record_usage(ctx.channel_id, completion.usage)
                if completion.type == "text":
                    conv.add_assistant(completion.content or "")
                    yield ResponseMessage(text=completion.content or "",
                                          reasoning=completion.reasoning or "")
                    return
                tool_calls = completion.tool_calls
            else:
                full: list[str] = []
                async for item in self.inference.stream(
                    messages, tools=schemas, reasoning=mode, max_tokens=MAX_TOKENS):
                    if isinstance(item, list):
                        tool_calls = item
                        break  # NOTE: usage for this streamed segment is not recorded
                               # (stream() omits StreamEnd on the tool-call path) — the
                               # follow-up complete() repopulates last_usage. Matches PAL.
                    if isinstance(item, StreamEnd):
                        self.record_usage(ctx.channel_id, item.usage)
                        break
                    yield StreamChunkMessage(token=item)
                    full.append(item)
                if tool_calls is None:
                    conv.add_assistant("".join(full))
                    yield ResponseMessage(text="".join(full))
                    return

            for _round in range(MAX_TOOL_ROUNDS):
                conv.add_assistant_tool_calls([
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)}}
                    for tc in tool_calls
                ])
                for tc in tool_calls:
                    yield ToolProgressMessage(tool=tc.name, arguments=tc.arguments)
                    result = await self.tool_executor.run(tc.name, tc.arguments, ctx)
                    conv.add_tool_result(tc.id, result)
                messages = conv.get_messages_for_api(system_prompt=self.system_prompt(ctx))
                completion = await self.inference.complete(
                    messages, tools=schemas, reasoning=mode, max_tokens=MAX_TOKENS)
                self.record_usage(ctx.channel_id, completion.usage)
                if completion.type == "text":
                    conv.add_assistant(completion.content or "")
                    yield ResponseMessage(text=completion.content or "",
                                          reasoning=completion.reasoning or "")
                    return
                tool_calls = completion.tool_calls

            cap = "Reached the tool-call limit for this turn. Here's what I have so far."
            conv.add_assistant(cap)
            yield ResponseMessage(text=cap)
        except Exception as exc:
            logger.exception("Chat error: %s", exc)
            yield ErrorMessage(error=f"Chat error: {exc}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/edible/Projects/PARE && python -m pytest tests/test_handle_chat.py -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add pare/agent.py tests/test_handle_chat.py
git commit -m "feat(agent): implement handle_chat (streaming + tool loop, pure-yield)"
```

---

## Task 6: System-prompt vault guidance

**Files:**
- Modify: `pare/prompts/system.md`
- Test: `tests/test_system_prompt.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_system_prompt.py`:

```python
"""system_prompt embeds the base prompt incl. vault-usage guidance."""
from unittest.mock import MagicMock

from pare.agent import PareAgent


def test_system_prompt_includes_vault_guidance():
    agent = PareAgent()
    pb = MagicMock()
    pb.render_profile.return_value = ""
    pb.render_wisdom.return_value = ""
    pb.render_scratchpad.return_value = ""
    pb.render_commands_catalog.return_value = ""
    agent.prompt_builder = pb
    ctx = MagicMock(); ctx.channel_id = "c"

    prompt = agent.system_prompt(ctx)

    assert "search_vault" in prompt
    assert "read_vault_doc" in prompt
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/edible/Projects/PARE && python -m pytest tests/test_system_prompt.py -q`
Expected: FAIL — current `prompts/system.md` does not mention the vault tools.

- [ ] **Step 3: Write minimal implementation**

Append to `pare/prompts/system.md` (create the file with this content if it does not exist; otherwise add this section):

```markdown
## Using PAL's research vault

You have access to a large, actively-maintained research vault built by a sibling
agent (PAL). Prefer it over answering from training data alone:

- Use `search_vault` to find relevant notes by meaning (semantic search). It returns
  hits with a `path`, `name`, `summary`, and `score`.
- Use `read_vault_doc` with a hit's `path` to read that note's full body.
- When a question touches prior research, search the vault first, then cite what you
  found. If the vault has nothing relevant, say so and proceed from general knowledge.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/edible/Projects/PARE && python -m pytest tests/test_system_prompt.py -q`
Expected: PASS (1 passed).

- [ ] **Step 5: Commit**

```bash
git add pare/prompts/system.md tests/test_system_prompt.py
git commit -m "feat(prompt): guide PARE to consult PAL's vault via search_vault/read_vault_doc"
```

---

## Task 7: Full suite + regression check

- [ ] **Step 1: Run the whole test suite**

Run: `cd /home/edible/Projects/PARE && python -m pytest tests/ -q`
Expected: all tests pass. The pre-existing suite does NOT break: `test_smoke.py:24`
asserts `StaticAnalyze in PareAgent.tools` (membership — still true after adding
`ReadVaultDoc`), and `test_register_tools.py` asserts `register_tools` is callable and
invokes discovery (no tool-count or builtin assertions). If anything unexpected fails,
fix it before continuing.

- [ ] **Step 2: Commit any regression fixes**

```bash
git add -A
git commit -m "test: update registration expectations for PR1 tool/builtin changes"
```

---

## Operational checklist (post-merge, not code)

- Verify the inference host's `vault` collection is populated and reindexed, and that
  `POST /collections/vault/search` returns nonempty hits for a known term — otherwise
  `search_vault` returns empty and the hypothesis can't be exercised. (Spec §Operational.)
- Smoke-test a live turn: start the daemon (`python -m pare`), connect (`pare-cli`), ask a
  question whose answer is in the vault, and confirm the model calls `search_vault` →
  `read_vault_doc` and that a high/critical worker tool still prompts for approval.

## Out of scope (PR2, deferred)

`workspace_path` (= daemon cwd), `write_file`/`replace_in_file`/`delete_file`, auto-commit
(reuse `agent_core.git_helpers.make_commit_callback`), `mkdir -p`, and the
`Path.is_relative_to` write-scope guard. These get their own design pass.
