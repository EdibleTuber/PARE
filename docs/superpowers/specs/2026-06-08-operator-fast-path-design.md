# PARE operator fast path — direct slash-command tool invocation alongside the agent — Design

**Date:** 2026-06-08
**Status:** Approved (brainstorm), pending written-spec review
**Branch context:** follows the merged PR #5 (`feat/per-tool-risk-tier-wire`) and the frida-worker launcher fix (`workers.yaml` `frida.command` pinned to the venv path)

## Problem

Every interaction with a worker tool today routes through the LLM. To enumerate
devices, list installed packages, or attach to a process — all deterministic,
mechanical operations — the operator must ask the agent, pay an LLM round-trip,
and wait. That is slow, costs tokens, and puts a non-deterministic decision-maker
in front of operations that have exactly one correct behavior.

Meanwhile the operations that genuinely *benefit* from an agent — interpreting
results, authoring and iterating Frida scripts, deciding what to hook — get the
same undifferentiated treatment.

This design splits PARE's surface into two front-ends over the **same** worker:

- a **fast path** — operator-driven slash commands for mechanical ops, no LLM in
  the loop, so they are instant, free, and deterministic; and
- an **agent path** — the LLM in the loop for reasoning-shaped work.

The operator drives setup and observation; the agent does the thinking. Both read
and write the same shared state, so work done on one path is visible to the other.

## Key insight: the worker owns the state, not the agent

Live `frida.Session` objects live in the worker process's `SessionManager`
(`pare_frida_mcp/core/sessions.py`), reached through the module-level `MANAGER`
in `pare_frida_mcp/tools.py`. The agent never *holds* a session — it holds a
handle and calls the worker. Verified: `MANAGER` is module-level, so it persists
for the worker process's lifetime and is shared across every call into that
process, regardless of who triggered the call.

Consequence: "operator attaches via a slash command, then the agent scripts
against that session" does **not** require the operator to reach into agent state
or hand anything off. It requires only that the slash-command dispatcher and the
LLM tool-caller funnel through the **same MCP client connection** PARE core
already owns to the frida stdio worker. The slash command bypasses *the LLM as
decision-maker* — nothing else. One worker process, one `SessionManager`, two
callers.

This is why **no daemon and no transport change are needed.** The currently
-disabled Streamable-HTTP multi-client route (see `workers.yaml`) is the heavier
alternative and is explicitly **out of scope** (see Non-goals).

## Scope

This spec covers the full operator-fast-path design. Implementation splits across
two repos:

- **PARE core (the bulk):** a slash-command dispatcher in `handle_command` that
  invokes worker tools directly through the MCP client the agent already uses;
  dual rendering (human view + persisted record); the "self-approved-but-audited"
  risk path; and a convention that the agent queries live session state at the
  start of every reasoning turn.
- **frida worker (`pare-frida-mcp`, small):** one new tool, `list_sessions`, plus
  audit-actor plumbing. Tracked as a cross-repo dependency below.

## Non-goals (tracked separately)

- Promoting the worker to a multi-client daemon / Streamable-HTTP transport.
  stdio + PARE-core multiplexing is sufficient; the daemon is the escape hatch if
  long-running streamed scripts ever make single-request serialization painful.
- Generalizing the fast path to the non-frida (HTTP `apk_re_agents`) workers.
  Those are disabled today; revisit when they come back.
- Any change to how captured *data* is persisted/searched. The persist-then-search
  model and the three-shapes output policy stand as-is; this design reuses them.

## Design

### 1. Two callers, one worker connection

PARE core already holds a single MCP client connection to the frida stdio worker
for the agent's benefit. We add a second caller onto that same connection: a
slash-command dispatcher in the CLI / `handle_command`. Both the LLM tool-call
path and the slash-command path resolve to the same `tool_pool` / worker process.

**Concurrency constraint (known, accepted):** stdio is one-request-at-a-time, so
PARE core must serialize the two callers. If the agent is mid-call, an operator
command queues behind it, and vice versa. This is fine for the quick ops on the
fast path. The daemon route (Non-goals) is the escape hatch if streamed,
long-running scripts ever make this painful.

### 2. The split — which commands live on the fast path

| Fast path (operator, no LLM) | Agent path (LLM in loop) |
|---|---|
| `/devices` — list devices | interpret enumeration / capture results |
| `/select <device_id>` — pick device | author & iterate Frida scripts |
| `/attach <target>` — attach (stateful) | decide what to hook |
| `/detach <handle>` — tear down (stateful) | reason over diffs across snapshots |
| `/ps` — enumerate processes | anything reasoning-shaped |
| `/apps` — enumerate applications | |
| `/sessions` — list live sessions (see §3) | |

Mechanical reads are stateless and trivially safe on the fast path. Stateful setup
(`/attach`, `/detach`, `/select`) is also on the fast path and works because the
state lives in the worker (Key Insight). Script authoring/execution as a *creative*
act stays agent-side; operator-initiated `execute_script` is still *possible* as a
slash command (see §5) but is not the primary fast-path workflow.

### 3. New worker capability — `list_sessions` (cross-repo dependency)

The one genuine capability gap. The worker exposes `list_devices` but no
`list_sessions` (verified against the 16-tool surface).

- **Returns:** for each session — handle, target, pid, and a **real liveness
  check** (probe the session, e.g. via `frida.Session` detached-state / a cheap
  RPC), not merely a registry flag. A session can be dead in the registry after a
  USB drop; the agent must not be told a dead session is alive.
- **Contract:** live session state is something the agent **queries**, never
  something it remembers. The agent is expected to call `list_sessions` at the
  **start of every reasoning turn** that will act on a session, rather than
  inferring "what's attached" from conversation history.
- **Why this matters:** the operator and the LLM are now both agents-of-change
  over mutable session state. Captured *data* is immutable (snapshots), so the
  existing persist-then-search model already handles it. Session *liveness* is
  mutable — if the operator attaches/detaches/swaps targets, or a USB hiccup kills
  a session between agent turns, the agent's inferred belief goes stale and it will
  author a script against a session that no longer exists. `list_sessions` as an
  always-fresh read is the fix, and it fits PARE's persist-then-search instinct:
  read truth, don't trust memory.

### 4. Dual output shape

Every slash command produces **two artifacts from one invocation**:

1. a **human-rendered view** to the operator's terminal — fast, readable, the
   point of the fast path; and
2. a **persisted record** in the shared store (snapshot / session store) so the
   agent reads it later via search/read.

This is the existing three-shapes output policy applied to operator-initiated
calls: control output renders inline to the human; snapshot-shaped output persists
to `@snapshots`; the agent consumes the persisted form. The operator never waits
on the agent, and the agent never misses what the operator did.

### 5. Risk + audit — self-approved-but-audited

Risk tiers continue to govern both paths. The difference is *who approves* and
*whether a prompt fires*:

- **Agent-initiated** calls gate exactly as today: high/critical tiers route a
  `ToolApprovalRequest` to the operator (`write_memory` = high,
  `execute_script` = critical), resolved via the daemon's approval routing.
- **Operator-initiated** slash commands are **self-approved-but-audited**: the
  operator typing the command *is* the approval, so no second prompt fires — but
  the risk tier still governs, meaning the call is recorded as a real approval
  event with `actor=operator` in the same audit trail.

The sharp edge, named deliberately: the agent can suggest *"run `/execute_script`
with this payload."* By running it you consciously approve something that would
otherwise be a critical gate. That is **not** gate-laundering — you are a
deliberate human reading the payload — but the design treats "operator ran it" as
a logged approval event, **not** as an ungated action that silently skipped policy.
The risk tier decides what counts as a consequential, audited approval; it just
does not *prompt* when the operator is already the actor.

### 6. What changes where

**frida worker (`pare-frida-mcp`):**
- New `list_sessions` tool (handle, target, pid, real liveness probe).
- Audit-actor plumbing so an operator-initiated call can be tagged `actor=operator`.

**PARE core (the bulk):**
- A slash-command dispatcher in `handle_command` that maps the §2 fast-path
  commands to worker tool calls over the MCP client the agent already uses.
- Dual rendering (§4): human view to terminal + persisted record to the store.
- A "self-approved-but-audited" invocation path (§5): a variant of the gated
  `tool_executor.run` that records operator-as-approver and skips the prompt while
  still emitting the audit event.
- A system-prompt / loop convention that the agent calls `list_sessions` at the
  start of any session-acting turn.

## Background: what the framework already provides

Carried from the `2026-05-30-pare-handle-chat-design.md` spec; **re-verify against
current `agent_core` before implementation**:

- Discovered MCP tools dispatch through `tool_pool.call_tool` inside their own
  `run()` (`agent_core/workers/tool_factory.py:61`); `handle_chat` calls
  `self.tool_executor.run(name, args, ctx)` and gating + audit happen automatically.
- The daemon routes `ToolApprovalResponseMessage` → `registry.resolve()` in its
  connection read loop (`agent_core/daemon.py:106-107`).
- `handle_command` is the established dispatch point for slash commands
  (`/hello`, `/health`, `/help`, `/clear`, `/context`) — the natural home for the
  fast-path dispatcher.

## Open items to verify during planning

1. Does `tool_executor.run` expose (or can it cheaply gain) an
   "operator-approved, skip-prompt, still-audit" mode, or does the fast path need
   a sibling entry point? (§5)
2. Is the agent's MCP client session safely shareable by a second synchronous
   caller, or does PARE core need an explicit serialization queue in front of it?
   (§1 concurrency constraint)
3. Exact liveness-probe mechanism for `list_sessions` that is cheap and does not
   perturb the target. (§3)
4. Whether `/snapshot` (already a planned PARE command) should be unified with this
   dispatcher or remain separate.

## Cross-repo dependency

The `list_sessions` tool and audit-actor tagging land in the `pare-frida-mcp`
repo (`src/pare_frida_mcp/`), not PARE core. Sequence that worker change first (or
in parallel) so PARE core has the capability to call.
