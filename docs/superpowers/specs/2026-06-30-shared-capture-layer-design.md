# Shared Capture Layer — Design

- **Date:** 2026-06-30
- **Status:** Approved for implementation planning
- **Repos touched:** `agent_core` (new capability), `PARE` (wiring), `pare-frida-mcp` (teardown)
- **Supersedes (in part):** the capture/snapshot machinery currently living inside `pare-frida-mcp`

## 1. Problem & context

PARE is an agentic reverse-engineering tool: a single persistent daemon talks over stdio to MCP "worker" subprocesses (`frida` today; `ghidra`/network later). Workers produce tool results that can be huge — raw memory dumps, hundreds of enumerated modules, arbitrary `execute_script` output. Those oversized results flood the model's context window.

There are **two distinct amnesia mechanisms**, and they need two distinct defenses:

- **Mechanism 1 — window collapses to empty.** The old `Conversation._truncate` orphan-popping drained the in-memory window to `[]` when a single tool-heavy turn exceeded `history_depth`. **Already fixed** in agent_core `v1.6.2` (`_is_valid_window_start` rewrite); the window never collapses. This design does not revisit it.
- **Mechanism 2 — window stays full but unbounded in size.** `_truncate` caps message *count* (`history_depth`, 50), never per-message *bytes*. Fifty messages each carrying a fat tool result sum to far more than a 32k/64k context window, and the backend degrades. v1.6.2 cannot address this — shrinking individual tool results is exactly what this capture layer is for.

**This layer's job: bound the bytes a tool result contributes to the window, by storing oversized results out-of-band and leaving a small, hard-bounded handle in the conversation — while making those stored results searchable, correlatable across workers, durable per project, and viewable by the operator.**

The capture machinery exists today *inside* `pare-frida-mcp` (a SQLite `CaptureStore`, an in-memory `SnapshotStore`/`@snapshots`, model tools `search_capture`/`read_capture`/`page_capture`, a 4096-byte `_ok` cap, seq-handle minting). Its real job is protecting the *model's* context — an agent-framework concern, not a frida concern. So it moves to agent_core, where every agent (PARE, the sibling agent PAL, future agents) inherits it.

## 2. Goals / non-goals

**Goals**
- Bound per-message bytes in the model window for oversized tool results (close mechanism 2).
- One shared, worker-tagged store per project; searchable and correlatable across workers.
- Durable per-project artifacts that survive daemon restarts ("resume a project").
- Uniform across MCPs: a worker just returns well-formed JSON; it owns no store.
- Zero impact on agents that don't need it (PAL must still boot and run unchanged).

**Non-goals**
- Re-fixing mechanism 1 (done in v1.6.2).
- A full token-accurate window accountant (a first token-aware approximation is in scope; precise per-turn token budgeting is a later refinement).
- Conversation replay on resume (the store is laid out to make it a small later add behind a flag).

## 3. Architecture overview

Every cross-process tool result is materialized PARE-side at exactly one chokepoint: `RiskAwareToolPool.call_tool`'s return (`agent_core/workers/risk_pool.py:165`). That is where capture happens — "at the wire, nearest the model," upstream of where results get stringified (`tool_factory.py:66`) and written into history (`Conversation.add_tool_result`, `conversation.py:58`; PARE's `handle_chat` loop, `pare/agent.py:191-192`).

Capture is implemented as an **injected `CaptureLayer` collaborator**, *not* code inlined into `RiskAwareToolPool`. The risk pool is the audited security chokepoint (risk-evaluate, gate, audit); mixing store writes / JSON parsing / stub-minting into it would couple context-management to the security path. Instead the pool calls the collaborator in one line:

```
result = await self._inner.call_tool(...)
if capture is not None and do_capture:
    result = capture.maybe_substitute(worker, result)   # store + maybe stub
return result
```

The collaborator is constructed in agent_core and handed to the pool. An agent that wants capture (PARE) provides a store; an agent that doesn't (PAL) passes no collaborator and the line is skipped.

### Store vs Substitute — the central split

The design's load-bearing correction: **storing a result and substituting a stub into history are two independent decisions.**

- **STORE** — write the result into the project store. Done for every *substantial* result (a top-level array of objects, or anything over the inline budget), unconditionally, regardless of caller. Preserves correlation/durability.
- **SUBSTITUTE** — replace the result the *model* sees with a compact stub. Done only when (a) the caller is the model dispatch path, and (b) the serialized result exceeds the inline budget.

This split resolves three problems at once: the model-facing byte bound (§5), the operator fast-path (§6), and the small-array round-trip regression (a 3-row array under budget is stored *and* shown inline verbatim — no needless `read_capture`).

## 4. Capture model — JSON shape decides

A worker returns well-formed JSON. The shape determines rows; no opt-in flag, no per-tool declaration:

- top-level **array of objects** → N rows (one per element);
- top-level **object** → 1 row;
- an opaque **blob** → the degenerate one-column row.

**Inference guard rules** (patches that keep real frida/ghidra output from collapsing):

1. **Single-array-value object** — `{"modules":[...500...]}` or `{"result":[...]}` unwraps to that array's rows (otherwise 500 items hide inside one `json_extract('$.modules')` blob and per-item field-filter is unreachable).
2. **Non-object array elements** — `["a","b"]` or `[[addr,bytes],...]` wrap each element as `{"value": elem}`, giving a synthetic column and a valid json-path (otherwise the column union is empty and the table has no columns).
3. **Empty array** — yields `rows: 0` and `preview: []` (never index `[0]`).

## 5. Schema-on-read engine (agent_core)

Store each captured result as JSON verbatim in one text column (`body`). Columns, filters, and the table are computed at read time.

**Storage:** one `captures` table — `seq INTEGER PRIMARY KEY, ts REAL, worker TEXT, tool TEXT, session_id TEXT, launch_ts REAL, ref TEXT, summary TEXT, body TEXT, blob_ref TEXT`, plus FTS5 over the serialized body, plus a promoted `addrs TEXT` column (below). Oversized `body` spills to `<project>/.pare/blobs/<seq>.bin` exactly as the frida store does today.

**Field-filter at any depth:** `json_extract(body, '$.path')`. Literal dotted keys (`"libc.so.6"`, `"com.foo.Bar"`) are quoted per-segment so they are addressable and don't collide with genuine nesting; the real json-path string is surfaced alongside the display label so the model filters on the path it was shown.

> **Known limitation (Plan 1):** the store keeps one `body` per capture — for a rows/array result that `body` is a JSON *array*, so `json_extract(body, '$.name')` returns `NULL` and `field=`-filtering does **not** reach per-element keys; it matches only object-shaped (single-row) captures. Element-key search on array captures goes through full-text `text=` instead (which is depth- and shape-blind). So "field-filter at any depth" holds for object bodies, not array bodies. A follow-up (`json_each` over array bodies, or per-element indexing) can lift this; until then the `field` tool-param description carries the caveat and callers fall back to `text=`.

**Full-text search:** FTS5 over the serialized body — free, depth-blind. **The model/user query string is wrapped as a single quoted FTS5 phrase** (`'"' + text.replace('"','""') + '"'`); without this, `MATCH 'libc.so.6'` raises a syntax error on the `.` (and on the `-`/`:` in versions, IPs, Java classes). One-line fix at the bind site.

**Cross-worker address correlation:** FTS5's tokenizer treats `0x401000` and ghidra's `00401000` as different whole tokens and has no substring match, so the headline "find an address in both a memory dump and a disassembly row" does **not** work through FTS alone. At the capture chokepoint, a cheap regex extracts hex/address-shaped tokens, normalizes them (strip `0x`, lowercase, zero-pad to pointer width), and writes them into the promoted, indexed `addrs` column that `search_capture` matches exactly. Verbatim `body` is untouched.

**Promotion without `ALTER`:** known-hot keys get an **expression index** — `CREATE INDEX ix_k ON captures(json_extract(body,'$.k'))`, which SQLite uses for the matching predicate — rather than a generated column (an `ALTER` on a durable, populated store). The hot-key set is a small per-worker config (frida: `hook`/`url`/`method`; ghidra: `ea`/`name`). A `json_extract` filter that scans more than N rows emits a one-line slow-path log warning.

**Sparse-table display:** column set = union of keys across the result set, nested keys flattened to dotted paths *for display only*, all-null columns dropped. Column order is **deterministic** (promoted/known-hot first in fixed order, then remaining keys sorted) and **capped** (top-K by fill rate, `+M more`) so a heterogeneous multi-worker result doesn't render a wide, mostly-null table. The preview is labeled "row 0 of N; `columns` is the union — not all rows have all columns."

## 6. Capture policy & model-facing substitution

**Store policy:** substantial results (array of objects, or over the inline budget) are always stored; trivial scalar/status returns (small, under budget — e.g. `list_devices` → `"3 devices"`) pass through unstored.

**Substitute policy (model dispatch only):**
- **Fits the inline budget** → the model sees the result inline, verbatim (and it may also be stored).
- **Exceeds it** → a **hard-bounded stub** (~512 B total) enters history; the full payload stays in the store:

```json
{
  "summary": "read_memory: 65536 bytes @ 0x401000",
  "captured": {
    "worker": "frida",
    "ref": "a1b2c3",
    "rows": 1,
    "columns": ["address", "size", "hex (+2 more)"],
    "shape": "1 row; body 65536B (elided)"
  },
  "hint": "read_capture(ref=\"a1b2c3\")"
}
```

The stub shows **shape, not content**. For the dominant single-row/blob case (a memory dump, an `execute_script` object, a 200-module enum serialized as one row), `preview` must **not** re-inline the payload — it carries an elided shape line, the column names (capped, `+M more`), and the `ref`. `preview` is `rows[:1]` run through `bound_text()` at ~256–512 B only when that fits the total stub budget; the serialized stub length is asserted/clamped at construction. This is the fix that actually closes mechanism 2 — without it, N==1 results re-inline their whole body.

**Inline threshold is window-derived, not a fixed 4096.** The threshold is `≈ context_tokens / history_depth`, applied now as `bytes ≈ tokens * 3.5`, so the budget tracks the configured window (32k/64k) rather than a magic constant. A running per-window inline-byte budget (sub-threshold results accumulate toward it, forcing capture once the window is under pressure) is a follow-on refinement, recorded in §11.

## 7. Retrieval surface

Model-facing retrieval tools are **agent_core-provided `Tool` subclasses an agent opts into** (PARE adds them via `register_tools()`/`tools` ClassVar; PAL does not). They read the PARE-side store directly via `requires=('capture_store',)` — no MCP round-trip — and auto-advertise through `schemas()`. They are **never** unconditional builtins (an unconditional `requires=('capture_store',)` builtin makes `ToolExecutor.build` raise for any agent lacking the attribute — PAL would fail to boot).

Tuned for weak local tool-callers (PARE runs gemma-4-26b; Qwen-class models are fragile):

- **`read_capture(ref, offset=0, byte_budget=0)`** — single opaque `ref` param (drop bare `seq` from the model-facing surface; `ref` is globally unique, so no `worker` param). Paged window of one record.
- **`search_capture(contains="", text="", worker="", field="", limit=0, byte_budget=0)`** — `worker` is **optional**, defaulting to all workers. A loose/no-arg call returns a **recent/list** view (`ref` + `summary` + `worker` for the latest captures), so a model that lost the `ref` can re-find it.
- **Dead-ref contract:** a purged/unknown `ref` returns a **structured, model-actionable sentinel** (`{"expired": true, "hint": "use search_capture to find current data"}`), never a raw `ValueError`. Weak models loop or fabricate on exception strings.
- **Hints are exact, copy-pasteable calls** (`read_capture(ref="a1b2c3")`), and the tool **description** (always in-context via `schemas()`) states that captures persist and are found by *searching*, not by remembering refs — because the stub scrolls out of the window after `history_depth` turns while the row persists.

**`/snapshot`** reads the PARE-side store **directly** and renders the sparse table; the cross-boundary `page_capture` call is removed. The same store backs the model-facing tools.

## 8. Project model & store location

A **project is a directory**, like `.git`. The store is `<project>/.pare/capture.db` (+ `<project>/.pare/blobs/`).

**The operator's cwd is authoritative, not the daemon's.** PARE is one persistent daemon that many CLI launches attach to over a socket; the wire messages carry only `channel_id`, no cwd. "Walk up from cwd" would therefore read the *daemon's* fixed launch cwd — collapsing per-project to one store and leaking target B's memory dumps into target A's store. Fix: the **CLI stamps `os.getcwd()` into `ChatMessage`/`CommandMessage`** (alongside the per-launch `channel_id` it already mints); the daemon resolves the project root by git-style walk-up from *that* cwd and caches the store per resolved root.

**Discovery edges:** the walk-up has a **`$HOME` ceiling** — a `.pare/` located exactly at `$HOME` is never auto-created or attached (treated as "outside a project"), so a stray `~/.pare` can't shadow every session. Launched outside any project → an **ephemeral fallback** store keyed per-launch (by `channel_id`) under `XDG_STATE_HOME`/`XDG_RUNTIME_DIR` (not the durable `~/vault`, which would co-mingle unrelated targets and never clean up), with the same hardening, deleted on session end (stale ones swept on startup).

**Framework cleanliness:** agent_core does not hardcode `.pare`. A `project_marker` config field carries it (PARE sets `.pare`; default `None` → no project discovery, the agent uses its existing channel/XDG path). PAL is unaffected.

**Concurrency:** `PRAGMA busy_timeout` (~5000 ms) on connect, and a per-store advisory lock file in `.pare/` taken on open, so a second daemon (env-override socket) fails loudly with a clear message instead of silently racing FTS writes on the same db.

## 9. Lifecycle, retention & security

- **Durable, not channel-scoped.** The project store is an artifact; clearing a conversation wipes only the throwaway channel, never the store.
- **Retention** by size + age, plus an explicit purge command. Rows carry `worker` + `session_id` + `launch_ts`, so retention can be **reachability-aware**: never evict a row whose `ref` is still live in the active channel window or a resumable transcript, and never evict the current launch; prefer age-based eviction of prior launches. On resume, a genuinely-missing stub is rewritten to a tombstone.
- **Blob-aware purge.** Today's `delete_by_source` is row-only ("does not remove blob files"). Since the large dumps are exactly what spill to `.bin`, row-only delete would orphan sensitive blobs and the size cap could never reclaim. Purge/retention unlink each deleted row's `blob_ref` (`missing_ok=True`), count blob bytes toward the size cap, and wrap row-delete + blob-unlink so a crash can't leave one without the other.
- **On-disk hardening.** Port the frida store's posture into the agent_core store constructor: create `.pare/` and `blobs/` `mode=0o700` (umask-guarded), `chmod 0o600` the db and every spilled blob. agent_core's existing writer uses umask defaults (world-readable under `umask 022`) — a regression for raw dumps/keys/PII.
- **Never committed.** On store creation, write `<project>/.pare/.gitignore` containing `*`, so nothing under `.pare/` is ever staged regardless of the repo's root `.gitignore`, with zero operator action.

## 10. Migration (sequenced, lockstep)

1. **agent_core** gains the `CaptureLayer`, store engine, and base retrieval tools (a release; PARE bumps its pin).
2. **PARE** wires the collaborator into its `tool_pool`, registers the retrieval tools, repoints `/snapshot` and the operator `_EnumView` commands at the PARE-side store, and threads cwd through the protocol.
3. **pare-frida-mcp** sheds its store: remove `CaptureStore`/`SnapshotStore`/`@snapshots`, `search_capture`/`read_capture`/`page_capture`, `snapshot_key`, the 4096-byte cap, and seq-handle minting in `read_memory`/`execute_script`. Tools just return full JSON.

Steps 2 and 3 are **lockstep**: until PARE captures on the wire, frida must keep returning real envelopes; the moment frida sheds `page_capture`/handles, PARE must already be serving the store — otherwise the model (or `/snapshot`) gets a handle pointing at a store that no longer exists. Any on-disk frida capture data from before the cutover is orphaned by design (documented, not migrated).

## 11. Deferred / recorded risks

- **Token-accurate window budgeting.** The window-derived threshold uses a `bytes ≈ tokens * 3.5` approximation and a per-message bound. A running per-window inline-byte accountant (forcing capture under accumulated pressure even for sub-threshold results) is the next refinement.
- **Conversation resume.** Captures always resume; a `--resume`/`--continue` flag to also replay the project's last conversation is a later phase. The store and project layout are designed so it's an additive change (a stable project channel), not a redesign.
- **Hot-key promotion policy.** The per-worker hot-key config is static at first; dynamically promoting a newly-hot ghidra key on an already-populated store (via a new expression index) is supported by the engine but not yet automated.

## 12. Decision ledger (for traceability)

| # | Decision |
|---|----------|
| 1 | Unified capture: JSON shape decides rows; inference guards for single-array-value / non-object / empty. |
| 2 | Schema-on-read engine in agent_core; FTS phrase-escaping; normalized `addrs` column; expression indexes. |
| 3 | Capture at the wire via an injected `CaptureLayer` collaborator, not inlined into `RiskAwareToolPool`. |
| 4 | Single worker-tagged store per project. |
| 5 | **Store ≠ Substitute**: always store substantial results; substitute a stub only for the model and only over budget. |
| 6 | Hard-bounded stub (~512 B), shape-not-content for single-row/blob; window-derived inline threshold. |
| 7 | Retrieval tools opt-in (PARE adds, PAL doesn't); `project_marker` parametrized; dead-ref sentinel; `ref`-first ergonomics. |
| 8 | Project = directory; cwd threaded on the wire; `$HOME` ceiling; per-launch XDG fallback; `busy_timeout` + lockfile. |
| 9 | Durable store; reachability-aware + blob-aware retention; `0o700/0o600`; auto `.pare/.gitignore`. |
| 10 | `/snapshot` and operator `_EnumView` read the PARE-side store directly. |
| 11 | Lockstep migration across the three repos. |
