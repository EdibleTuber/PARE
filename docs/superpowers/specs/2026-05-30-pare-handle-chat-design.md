# PARE `handle_chat` + `handle_command` + PAL-vault reads — Design

**Date:** 2026-05-30
**Status:** Approved (revised after skeptic-panel review)
**Branch context:** follows the merged PR #5 (`feat/per-tool-risk-tier-wire`)

## Problem

`PareAgent` (`pare/agent.py`) implements `setup` / `register_tools` / `system_prompt`
but not `handle_chat`, so the base class's `raise NotImplementedError` fires on every
chat message. No PARE conversation can complete a turn. PARE also has no command dispatch
(`handle_command` unimplemented → `/hello`, `/health`, `/help`, `/clear`, `/context` all
fail) and no operational access to PAL's research vault — the project's core hypothesis
(an agent backed by PAL's actively-maintained knowledge base is more capable than one
relying on training data alone).

## Scope: this design is split into two PRs

This spec **fully specifies PR 1** and scopes **PR 2** as a deferred follow-up that gets
its own design pass.

- **PR 1 — conversational agent + PAL knowledge (this spec):** `handle_chat` +
  `handle_command` + read access to PAL's research via RAG (`search_vault` +
  a new `read_vault_doc` tool) + system-prompt guidance. This is the validated, low-risk
  half and is sufficient to test the core hypothesis end-to-end.
- **PR 2 — project workspace (deferred, design later):** a `workspace_path`
  (defaulting to the daemon's cwd) plus write/read tools scoped to it, so PARE can save
  scripts, tool outputs, and per-target notes. See "PR 2 — deferred" below.

### Why the split (skeptic-panel outcome)

A 4-lens adversarial review (concurrency / security / integration / scope) found the
`handle_chat` loop **correct and ship-ready** — the concurrency lens could not break the
central control-flow claim — but found the original write-tool design rested on a false
premise (it would have written into PAL's curated, git-tracked vault). Pointing
`vault_path` at the shared PAL repo was the root cause: it also relocated PARE's own
state (profile/wisdom/learning/channels, all keyed off `config.vault_path` in
`runtime.py:90-99`) into PAL's repo. The redesign **decouples three roots** so none of
those failure modes can occur:

| Concern | Root | Mechanism |
|---|---|---|
| **PAL research** (read) | the inference server's `vault` collection | `search_vault` (semantic) + `read_vault_doc` (full bodies). No local-FS coupling. |
| **PARE's own state** (profile/wisdom/learning/channels) | `vault_path` — stays PARE-owned (default unchanged) | framework managers, isolated from PAL's repo |
| **Project artifacts** (read+write) | `workspace_path` (PR 2) | PARE workspace tools scoped to the daemon's cwd |

## Non-goals (tracked separately)

- The PR 2 workspace + write tools (deferred; own design).
- C5 risk-floor lowering, re-enabling `apk_re_agents`, agent_core discovery hardening.
- Auto-commit of any vault/workspace (PR 2 question; PR 1 writes nothing to disk).

## Background: what the framework already provides (verified against code)

- **Risk gating is transparent.** Discovered MCP tools dispatch through
  `tool_pool.call_tool` *inside their own `run()`* (`agent_core/workers/tool_factory.py:61`).
  `handle_chat` calls `self.tool_executor.run(name, args, ctx)`; gating + audit happen
  automatically.
- **Approval routing is free.** The daemon routes `ToolApprovalResponseMessage` →
  `registry.resolve()` in its connection read loop (`agent_core/daemon.py:106-107`).
  No custom `handle_other` is needed.
- **Pure-`yield` preserves the approval round-trip (panel-confirmed).** `handle_chat` runs
  as an asyncio task (`daemon.py:96-99`); the per-connection read loop runs concurrently on
  the same event loop and resolves the approval future synchronously
  (`tool_approval.py:66`) while `handle_chat` is parked on `await future`
  (`risk_pool.py:144`). `encode_message` frames each message as one atomic NDJSON line, so
  concurrent `yield`-writes and `ctx.emit`-writes interleave without corruption. No lock,
  no deadlock — identical to PAL.
- **`self.inference`, `self.tool_executor`, `self.command_registry`, `self.retrieval`,
  `self.prompt_builder`** are populated by `run_daemon` before `setup()`.
  `self.retrieval` is `RetrievalClient(base_url=config.inference_url,
  collection_id=config.collection_id)`; `config.inference_url` (default
  `192.168.1.14:11434`) is the inference **manager** proxy, which serves both
  `/v1/chat/completions` and `/collections/{id}/search` on the same port (panel-confirmed —
  no port split).
- **`search_vault`** is a registered builtin (`requires=("retrieval",)`) calling
  `ctx.agent.retrieval.search(query)`. `collection_id` defaults to `"vault"` (inherited
  from `BaseConfig`) and already matches the server's collection id.

## Architecture

### 1. `handle_chat` (port of PAL's proven loop, pure-`yield`)

Mirrors `pal/agent.py:406`, minus PAL-only machinery (researcher, learning scanner, batch
inference) and using pure-`yield` instead of direct `writer.write`.

```
async def handle_chat(self, msg, ctx) -> AsyncIterator[object]:
    from agent_core.inference import StreamEnd, ToolCall
    conv = ctx.conversation
    conv.add_user(msg.text)
    mode = self.decide_mode(conv)                 # "on" | "off"  (decide_mode never returns "auto")
    messages = conv.get_messages_for_api(system_prompt=self.system_prompt(ctx))
    schemas  = self.tool_executor.schemas()
    MAX_TOOL_ROUNDS = 50
    MAX_TOKENS = 4096                             # runaway-loop stopgap (matches PAL)

    try:
        tool_calls = None
        if mode == "on":
            completion = await self.inference.complete(messages, tools=schemas,
                                                       reasoning=mode, max_tokens=MAX_TOKENS)
            self.record_usage(ctx.channel_id, completion.usage)
            if completion.type == "text":
                conv.add_assistant(completion.content or "")
                yield ResponseMessage(text=completion.content or "",
                                      reasoning=completion.reasoning or "")
                return
            tool_calls = completion.tool_calls
        else:
            full = []
            async for item in self.inference.stream(messages, tools=schemas,
                                                    reasoning=mode, max_tokens=MAX_TOKENS):
                if isinstance(item, list):
                    tool_calls = item; break       # NOTE: usage for this streamed segment is not
                                                   # recorded (stream() omits StreamEnd on the
                                                   # tool-call path) — faithful port of PAL's gap;
                                                   # the follow-up complete() repopulates last_usage.
                if isinstance(item, StreamEnd):
                    self.record_usage(ctx.channel_id, item.usage); break
                yield StreamChunkMessage(token=item); full.append(item)
            if tool_calls is None:
                conv.add_assistant("".join(full))
                yield ResponseMessage(text="".join(full))
                return

        for _round in range(MAX_TOOL_ROUNDS):
            conv.add_assistant_tool_calls([
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)}}
                for tc in tool_calls])
            for tc in tool_calls:
                yield ToolProgressMessage(tool=tc.name, arguments=tc.arguments)
                result = await self.tool_executor.run(tc.name, tc.arguments, ctx)
                conv.add_tool_result(tc.id, result)
            messages = conv.get_messages_for_api(system_prompt=self.system_prompt(ctx))
            completion = await self.inference.complete(messages, tools=schemas,
                                                       reasoning=mode, max_tokens=MAX_TOKENS)
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

Known carried-over limitation (document, do not fix here): `StreamEnd.finish_reason` is
not inspected, so a `max_tokens`-truncated text turn surfaces as a complete answer. This
is PAL's deferred inference-safety gap; tracked there, not in scope for PR 1.

### 2. `handle_command`

```
async def handle_command(self, msg, ctx) -> AsyncIterator[object]:
    async for out in self.command_registry.dispatch(msg.name, msg.args, ctx):
        yield out
```

Activates `/hello`, `/health`, and the framework builtins.

### 3. PAL research access — RAG only (no local-FS coupling)

PARE reaches PAL's 2,796 notes through the inference server's `vault` collection, so
`vault_path` never points at PAL's repo and PARE's own state stays isolated:

- **`search_vault`** (existing builtin) — semantic discovery; returns hits with
  `id / name / summary / score`.
- **`read_vault_doc`** (new tool, §4) — fetches the full body of a hit by `doc_id`.

The framework's read-only shell builtins (`cat/grep/find/ls/head/tail/read_lines`) are
scoped to `config.vault_path`, which under this design is PARE's **private state dir** (not
the research corpus). They are therefore **disabled** in PR 1 (added to
`disabled_builtins`) to avoid the model wasting calls grepping PARE's own profile/wisdom
files. (Workspace-scoped read tools return in PR 2.)

### 4. New tool: `read_vault_doc` (`pare/tools/`)

- Signature: `read_vault_doc(path: str) -> str`. Takes the `path` field a
  `search_vault` hit returns (`"{id}.md"`), strips the trailing `.md` to recover the
  `doc_id`, calls `ctx.agent.retrieval.get_document(doc_id)` (`agent_core/retrieval.py:49`),
  and returns the document's `content` (with `name`/`summary` header).
  `requires = ("retrieval",)`. (Using `path` keeps it consistent with what `search_vault`
  actually emits — see `agent_core/tools/_framework.py:SearchVault`.)
- `get_document` already rejects path-traversal `doc_id`s and raises `FileNotFoundError`
  for unknown ids; the tool catches both and returns an error string (never raises).
- Registered in `PareAgent.tools`.
- *Design note:* this mirrors `SearchVault` and is a natural agent_core builtin. Keeping it
  PARE-local for PR 1; upstreaming it next to `SearchVault` is a reasonable follow-up
  (see should-consider).

### 5. System prompt (`pare/prompts/system.md`)

Add guidance so the model actually exercises PAL's knowledge:

- PARE's role: a reverse-engineering lab agent.
- PAL's research is available via `search_vault` (semantic discovery) then `read_vault_doc`
  (full content) — **consult it for prior research before answering from training data
  alone** (the core hypothesis).
- (PR 2 will add workspace guidance.)

### 6. Config (`pare/config.py`)

- **`vault_path` is left at its PARE-owned default** (no change) — it holds PARE's own
  state, isolated from PAL's git repo. (`vault_path` is inherited from `BaseConfig`; this
  design does **not** re-point it.)
- `collection_id` stays `"vault"` (inherited; already matches the server).
- Document that `PARE_INFERENCE_URL` must point at the inference **manager** proxy
  (default `192.168.1.14:11434`), which serves both `/v1/chat/completions` and
  `/collections/{id}/search`.
- `workspace_path` is introduced in **PR 2**, not here.

## Error handling

- The whole `handle_chat` loop is wrapped in `try/except` → `yield ErrorMessage` +
  `logger.exception`.
- Tool-level failures (denied/timeout/failed MCP dispatch from `tool_pool`,
  `read_vault_doc` not-found) return error **strings** that feed back to the model as
  normal tool results, so the turn continues rather than crashing (panel-confirmed:
  `tool_executor.run` always returns `str`).

## Testing

- **`handle_chat`** (stub `InferenceClient` with scripted `stream`/`complete`, fake
  `tool_executor`): assert the yielded-message sequence for (a) streaming text turn,
  (b) one tool round → text, (c) loop-cap → cap `ResponseMessage`, (d) exception →
  `ErrorMessage`.
- **`handle_command`**: dispatch yields command output.
- **`read_vault_doc`**: (a) happy path returns content via a fake `retrieval`;
  (b) `FileNotFoundError` → error string; (c) traversal `doc_id` → error string.

## Operational checklist (not code)

- **Verify the `vault` collection is populated and reindexed on the inference host.** The
  collection's `source_dir` (`collections.json`, `id="vault"`) must point at the host's
  populated copy of the vault, and an embeddings reindex must have run. Confirm
  `POST /collections/vault/search` returns nonempty hits for a known term **before**
  relying on `search_vault`. (The repo-local sample `collections.json` lists
  `source_dir=/home/edible/vault`; the deployed inference host's path may differ — verify
  there, not on the PARE host.)

## PR 2 — deferred (own design pass)

The project workspace. To be brainstormed separately; the skeptic panel's relevant
findings are captured here as inputs so they aren't lost:

- **Separate root:** add `workspace_path` config, default `Path.cwd()` (the daemon's launch
  dir — Claude-Code-like), overridable via `PARE_WORKSPACE_PATH`. Decide the systemd case
  (daemon cwd vs explicit setting; possibly per-channel).
- **Write tools** (`write_file` / `replace_in_file` / `delete_file`, scoped to
  `workspace_path`): must `mkdir(parents=True, exist_ok=True)` on write (PAL `wiki.py:58`
  does this; the read-only builtins do not). Path guard must use `Path.is_relative_to`
  against the **resolved** real path (not `str.startswith`, which lets a sibling
  `workspace-export/` through), composed with `resolve_safe`.
- **Git:** if the workspace is a git repo and auto-commit is wanted, **reuse
  `agent_core.git_helpers.make_commit_callback`** (it already does `git add -- <path>`,
  `gpgsign=false`, and a no-op-skip) rather than hand-rolling; resolve its swallow-failure
  vs surface-as-tool-error behavior. Forbid `git add .`/`-A`. Consider commit-per-turn over
  commit-per-write, and a restore-on-failure path. Or defer git entirely.
- **Gating:** these write tools run in-process (not via `tool_pool`), so they are not
  risk-gated; with a dedicated workspace root the blast radius is the user's own per-target
  dir, which is acceptable — but reconsider gating `delete_file`.

## Should-consider (from the panel; non-blocking)

- Upstream `read_vault_doc` into `agent_core` next to `SearchVault` so PAL benefits too.
- Track the streamed-segment usage-loss and the `finish_reason` truncation alongside PAL's
  inference-safety plan rather than fixing them per-agent.

## Panel disposition (what was validated vs dismissed)

- **Confirmed sound:** the entire chat/command control flow; pure-`yield` approval
  round-trip; `search_vault` URL/port; the lean-tool decision; conversation tool-loop API.
- **Resolved by this redesign:** clobbering PAL's `projects/`, `_channels`/state pollution,
  the `vault_path` relocation, git-helper reinvention, `write_file` mkdir — all moved to
  PR 2 or eliminated by decoupling the three roots.
- **Remaining for PR 1:** verify the search index is populated/reindexed (operational).
```
