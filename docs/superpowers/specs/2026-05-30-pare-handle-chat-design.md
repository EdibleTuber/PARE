# PARE `handle_chat` + `handle_command` + vault read/write — Design

**Date:** 2026-05-30
**Status:** Approved (pending skeptic-panel review)
**Branch context:** follows the merged PR #5 (`feat/per-tool-risk-tier-wire`)

## Problem

`PareAgent` (`pare/agent.py`) implements `setup` / `register_tools` / `system_prompt`
but not `handle_chat`, so the base class's `raise NotImplementedError` fires on every
chat message. No PARE conversation can complete a turn. In addition, PARE today has:

- **no conversational loop** (the gap above),
- **no command dispatch** (`handle_command` also unimplemented → `/hello`, `/health`,
  and the framework builtins `/help`, `/clear`, `/context` all fail),
- **no operational access to PAL's research vault**, which is the project's core
  hypothesis (an agent backed by PAL's actively-maintained knowledge base is more
  capable than one relying on training data alone), and
- **no home for its own artifacts** — the only write surfaces are the per-channel
  scratchpad and the wisdom pool; there is no general file-write tool, so PARE cannot
  save a script it wrote, dump a tool's output, or keep structured per-target notes.

## Goals

1. Implement `handle_chat`: streaming + tool-use loop, with risk gating and operator
   approval working transparently (already wired by the framework).
2. Implement `handle_command`: delegate to the framework command registry.
3. Wire vault **reads** (PAL's research) and vault **writes** (PARE's own RE artifacts)
   under a single vault root.
4. Degrade gracefully when the vault is sparse or workers are unreachable.

## Non-goals (tracked separately)

- C5 risk-floor lowering (`high`→`medium` for frida).
- Re-enabling the 8 `apk_re_agents` workers.
- agent_core discovery hardening (v1.6.1).
- Bidirectional auto-**push** of the vault between machines (this design commits
  locally; push stays manual).

## Background: what the framework already provides (verified)

- **Risk gating is transparent.** Discovered MCP tools dispatch through
  `tool_pool.call_tool` *inside their own `run()`* (`agent_core/workers/tool_factory.py:61`).
  `handle_chat` calls `self.tool_executor.run(name, args, ctx)` exactly like PAL; gating
  + audit happen automatically.
- **Approval routing is free.** The daemon routes `ToolApprovalResponseMessage` →
  `registry.resolve()` directly in its connection read loop (`agent_core/daemon.py:105`).
  No custom `handle_other` is needed.
- **The daemon emits every *yielded* message** (`_run_handler`, `agent_core/daemon.py:138`),
  so PARE can use a clean pure-`yield` style rather than PAL's legacy direct-`writer.write`.
- **`self.inference`, `self.tool_executor`, `self.command_registry`, `self.retrieval`,
  `self.prompt_builder`** are all populated by `run_daemon` before `setup()`.
  `self.retrieval` is a `RetrievalClient(base_url=config.inference_url,
  collection_id=config.collection_id)`.
- **The seven read-only shell builtins** (`cat/head/tail/ls/grep/find/read_lines`) are
  registered and scoped to `config.vault_path`. PARE does not disable them.
- **`search_vault`** is a registered builtin (`requires=("retrieval",)`) calling
  `ctx.agent.retrieval.search(query)`. The inference manager
  (`~/Projects/inference_server`) indexes a collection with `id="vault"` and exposes
  `/collections/{id}/search`; PARE's default `collection_id` is already `"vault"`.

## Architecture

### 1. `handle_chat` (Approach A — port PAL's proven loop, pure-`yield`)

Mirrors `pal/agent.py:406`, minus PAL-only machinery (researcher, learning scanner,
batch inference) and using pure-`yield` instead of direct `writer.write`.

```
async def handle_chat(self, msg, ctx) -> AsyncIterator[object]:
    from agent_core.inference import StreamEnd, ToolCall
    conv = ctx.conversation
    conv.add_user(msg.text)
    mode = self.decide_mode(conv)                 # "on" | "off" | "auto"
    messages = conv.get_messages_for_api(system_prompt=self.system_prompt(ctx))
    schemas  = self.tool_executor.schemas()
    MAX_TOOL_ROUNDS = 50
    MAX_TOKENS = 4096                              # runaway-loop stopgap (matches PAL)

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
                    tool_calls = item; break
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

**Why pure-`yield` is safe with the approval flow.** While `handle_chat` (running as a
daemon-spawned task) awaits `self.tool_executor.run(...)` for a high/critical tool, the
`RiskAwareToolPool` emits a `ToolApprovalRequestMessage` via `ctx.emit` and awaits a
registry future. The daemon's connection read loop runs concurrently, reads the operator's
`ToolApprovalResponseMessage`, and resolves the future — exactly as in PAL. The yielded
`ToolProgressMessage`/`StreamChunkMessage` and the `ctx.emit`-sent approval request both
write to the same `StreamWriter` on the same event loop; each write is atomic, so there is
no corruption, only interleaving — acceptable and identical to PAL's behavior.

### 2. `handle_command`

```
async def handle_command(self, msg, ctx) -> AsyncIterator[object]:
    async for out in self.command_registry.dispatch(msg.name, msg.args, ctx):
        yield out
```

Activates `/hello`, `/health`, and the framework builtins.

### 3. Vault access — single root, read-everything / write-`projects/`

- **Root:** `vault_path = ~/pal-vault-prod` (PARE default, overridable via `PARE_VAULT_PATH`).
  The vault is a live git repo (`github.com/EdibleTuber/vault`, branch `main`, 2,796
  notes, `_index.md` present). A `projects/` subdir already exists and is the established
  home for project notes.
- **Reads (whole vault):**
  - shell builtins (`cat/grep/find/ls/head/tail/read_lines`) for literal navigation and
    full-body reads of local files;
  - `search_vault` (collection `"vault"`) for semantic discovery of PAL's research.
  - No `read_vault_doc` tool is added: `cat` already reads full bodies from the local copy;
    `search_vault` covers semantic discovery.
- **Writes (`projects/` subtree only):** new lean PARE tools save scripts, tool-output
  dumps, and per-target notes under `projects/<target>/…`.

### 4. New write tools (PARE-specific, `pare/tools/`)

Lean RE-focused set (reads already covered by builtins):

- `write_file(path, content)` — create/overwrite.
- `replace_in_file(path, old, new)` — targeted edit.
- `delete_file(path)` — remove.

**Path safety.** Each resolves `path` against `vault_path` using the framework's
`resolve_safe` (prevents escaping the vault), then enforces that the resolved path is
under `vault_path/projects/`. Any path outside `projects/` is rejected with a tool-error
string (PARE must not clobber PAL's curated research articles).

**Auto-commit.** After a successful write/replace/delete, the tool commits the changed
path(s) to the vault git repo via a small helper (`git -C <vault_path> add <path> &&
git commit -m "pare: …"`). This keeps the working tree clean so a later `git pull`
doesn't fight uncommitted changes. **Push stays manual** (non-goal). Commit failures are
surfaced as tool-error strings, not raised — a failed commit must not crash the turn.

**Risk gating note.** These tools execute in-process via `tool_executor.run`, *not*
through `tool_pool.call_tool`, so they are **not** risk-gated. This is acceptable: the
blast radius is confined to PARE's own `projects/` subtree (the path guard prevents
touching research articles or escaping the vault). Recorded here explicitly so it is a
conscious decision, not an oversight.

### 5. System prompt (`pare/prompts/system.md`)

Add guidance so the model actually exercises the vault:

- PARE's role: a reverse-engineering lab agent.
- PAL's research vault is available read-only: start at `_index.md`; use `grep`/`find`
  for literal lookups and `search_vault` for semantic discovery; **consult the vault for
  prior research before answering from training data alone** (the core hypothesis).
- PARE's own workspace is `projects/<target>/…`: save scripts, tool outputs, and notes
  there via the write tools; do not write outside `projects/`.

### 6. Config (`pare/config.py`)

- `vault_path` default → `~/pal-vault-prod` (was `~/vault`); still `PARE_VAULT_PATH`-overridable.
- `collection_id` stays `"vault"` (already matches the server's collection).
- Document that `PARE_INFERENCE_URL` must point at the inference manager's LAN URL that
  exposes both `/v1/chat/completions` and `/collections/{id}/search` (manager `PORT`,
  default 8080 in the repo config — confirm the deployed port).

## Error handling

- Whole `handle_chat` loop wrapped in `try/except` → `yield ErrorMessage` + `logger.exception`.
- Tool-level failures (denied/timeout/failed dispatch from `tool_pool`, or write-tool path
  guard / git-commit failures) return error **strings** that feed back to the model as
  normal tool results, so the turn continues rather than crashing.

## Testing

- **`handle_chat`** (stub `InferenceClient` with scripted `stream`/`complete`, fake
  `tool_executor`): assert yielded-message sequence for (a) streaming text turn,
  (b) one tool round → text, (c) loop-cap, (d) exception → `ErrorMessage`.
- **`handle_command`**: dispatch yields command output.
- **Write tools**: (a) reject `../` escape and any non-`projects/` path; (b) successful
  `write_file` creates the file and commits (temp git repo fixture); (c) `replace_in_file`
  and `delete_file` happy paths; (d) git-commit failure surfaces as a tool-error string,
  not an exception.

## Operational checklist (not code)

- Add `_channels/` to the vault `.gitignore` (currently untracked-but-not-ignored, so
  PARE's per-channel scratchpad would otherwise be committed into the shared vault).
- Confirm the deployed inference-manager port for `PARE_INFERENCE_URL`.
- (Done) PAL's vault content is present locally at `~/pal-vault-prod`.

## Risks / open questions for review

1. Does pure-`yield` truly preserve the approval round-trip under the daemon's task model?
   (Argued above; the skeptic panel should pressure-test the concurrency claim.)
2. Is `vault_path/projects/` write-scoping robust against symlink / `..` / absolute-path
   tricks once `resolve_safe` is composed with the `projects/` prefix check?
3. Auto-committing on every write into a repo that is concurrently `git pull`-ed from
   another machine — any failure modes (index lock, mid-pull commit, detached states)?
4. Is `search_vault` actually reachable from PARE's host, or should the design tolerate it
   being down (it already returns an error string, fed back to the model)?
```
