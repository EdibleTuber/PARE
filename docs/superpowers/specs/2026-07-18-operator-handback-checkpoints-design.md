# Operator-handback checkpoints: make the loop obey

**Date:** 2026-07-18
**Status:** approved (design); revised after design-panel review
**Related:** `pare/repeat_guard.py`, `pare/agent.py` (`handle_chat`), `[[project_re_methodology_and_repeat_guard]]`

## Motivation

Two live runs (2026-07-17) showed that gemma-4-26b **ignores prose** that would
prevent its failures — cross-referenced from channel history + the audit log:

- **Spin (run #2, channel `215134`).** The model emitted `static_grep_smali(pattern="OMTG_DATAST_001_SQLite")`
  **24×**; the repeat-guard short-circuited 18 (only **6** hit the backend, audit-
  confirmed) and returned **22** `[repeat-guard]` nudges. The model **ignored all
  22** and kept re-emitting to the ~50-call cap. The guard is a backend-protection
  *floor*, not a loop-terminator.
- **Silent wrong commit (run #1, channel `214816`).** `grep OMTG_DATAST_001_SQLite`
  matches **five** near-duplicate classes (`_SQLite`, `_SQLite_Not_Encrypted`,
  `_SQLite_Encrypted`, `_SQLITE_Encrypted`, `_SQLiteEncrypted`). The model silently
  committed to `_SQLite_Encrypted` — the wrong variant — inspected it, then reached
  `frida_java_hook` on it, and the operator approved the wrong-class hook before
  noticing.

Both are the same root cause: **the model won't obey the prose that prevents these
failures.** The fix must be *mechanical* and sit where the model can't skip it.

## Decisions (from brainstorming)

- **Intervention = hand control back to the operator** (co-pilot model), not
  constrain-and-continue autonomously.
- **Disambiguation fires at commit-time** (when the model digs into / hooks one
  class), not on every multi-match search.

## Key architectural insight

"Hand back to the operator" needs **no mid-turn pause and no agent_core change.**
`handle_chat` is a yield-generator; its existing text-completion exits already do
exactly `conv.add_assistant(...); yield ResponseMessage(...); return` (agent.py
~218-221, ~262-266). The operator's next message starts a fresh `handle_chat`
against the persisted `ctx.conversation`. So the handback is just **early
turn-termination with a structured question** — PARE stops the tool loop, yields
one `ResponseMessage`, and returns. The operator's reply *is* the next turn; the
model resumes with everything it found still in context.

agent_core's HITL is an *approval* future (approve/deny/justify) bound to tool
dispatch — not a general "ask a free-form question" primitive. We deliberately do
**not** extend it. agent_core stays pinned @v1.7.3; all changes are PARE-side.

## Panel review (fixes folded in)

A 4-lens code-grounded panel returned **proceed-with-fixes**. Three must-fixes
were verified against the code and would have broken or silently disabled the
design; all are incorporated below. (The chair also confirmed the tool arg is
`cls`, not `class` — `contract.py` — so no rename.) The design below is the
post-review version.

## Design

All PARE-side, in/near `handle_chat`'s tool loop. Keep logic in pure helpers so
`handle_chat` stays thin and the seams are unit-testable.

### 0. Handback = settle pending, then terminate

**A handback must not leave dangling tool_calls.** `handle_chat` records the whole
batch with `conv.add_assistant_tool_calls([...all tcs...])` *before* the per-tc
loop, and adds a `tool` result per call *inside* it. Returning mid-loop would leave
the tripping call and any later siblings with no `tool` message — an invalid
OpenAI-compatible sequence that 400s on the *next* turn. So every handback goes
through:

```python
def _handback(conv, tool_calls, done_ids, question):
    # satisfy EVERY tool_call id in the batch that has no result yet
    for tc in tool_calls:
        if tc.id not in done_ids:
            conv.add_tool_result(tc.id, "[handed back to operator — call not executed]")
    conv.add_assistant(question)
    return ResponseMessage(text=question)   # caller does: yield ...; return
```

`done_ids` is the set of tool_call ids already given a real result this round. The
question text always includes what happened, the concrete context (candidate list /
repeated call + its result), and an explicit ask — and is recorded in the
conversation so the *next* turn has it.

### 1. Trigger 1 — spin → operator

Today `RepeatGuard.should_run()` returns `False` on a spun signature and the loop
feeds a `blocked()` nudge the model ignores. Change: on the **first hard-block** of
a signature, hand back instead of nudging on forever.

- Add `RepeatGuard.tripped(name, arguments) -> bool`: `True` the first time a
  signature crosses a hard-block threshold **and** hasn't already handed back this
  turn (track handed-back signatures in the guard; only the first spin ends the
  turn).
- **Poll-tool exemption.** The guard hard-blocks on two paths: the call-count
  ceiling (verbatim spin — the 24× grep) *and* the result-aware streak (same result
  3×). Poll tools (`frida_read_hook_events`, `list_sessions`) legitimately return
  identical-empty results while the operator triggers the app, and the guard
  docstring already promises "legitimate re-polls are NOT penalized." So Trigger 1
  is **scoped out** for a `_POLL_TOOLS` set — those still get only the existing
  `blocked()` nudge, never a handback.
- On trip (non-poll), build the question from what the guard knows — the spun call,
  repeat count, last result summary — plus the candidate list if a recent
  name-search has one (§2 machinery):

  > "I've re-run `static_grep_smali(pattern="OMTG_DATAST_001_SQLite")` 6× with the
  > same result and I'm stuck. That search matched these classes:
  > `OMTG_DATAST_001_SQLite`, `_SQLite_Not_Encrypted`, `_SQLite_Encrypted`, … .
  > Which should I dig into, or how would you like me to proceed?"

### 2. Trigger 2 — commit-time disambiguation → operator

Track recent **name-searches** and their matched classes; when the model commits to
one of ≥2 *near-duplicate* name-matched classes, block that call and hand back.

- **Commit tools** (`_COMMIT_TOOLS`, prefixed, centralized as a constant with a
  comment to keep it current): `static_list_methods`, `static_decompile_method`,
  and **`frida_java_hook`** — the hook is the actual damaging commit (run #1), and
  intercepting it surfaces the candidate *list* the approval prompt alone doesn't.
- **Track name-searches without relying on a ref.** After a `static_grep_smali`
  dispatch, extract candidate class names **directly from the result**:
  - If the result is raw JSON with `rows` (the common small-result case — no ref),
    parse it.
  - Else if it's a capture stub with a `ref`, `capture_store.get(ref)` and parse the
    full body (the large/spilled path).
  - Candidate names come from the **referenced-type token** in each row's
    `insn`/`match` field (a smali type ref `Lsg/vp/.../OMTG_DATAST_001_SQLite;`),
    **not** the row's `class` column — grep sets `class` to the *enclosing* class of
    the matching instruction, so a dispatcher that instantiates all five variants
    would collapse the column to itself and drop the variants. Normalize smali→dotted
    and collect the distinct class-type references whose **simple name contains the
    search pattern**. Store the parsed candidate set for the turn.
- **Detect commit + near-duplicate check.** On a `_COMMIT_TOOLS` call, *before*
  yielding its `ToolProgressMessage` or dispatching it:
  1. Normalize the incoming `cls` arg to dotted form (the model may pass smali or
     dotted).
  2. Look up the most-recent turn candidate set containing `cls`.
  3. Apply the **near-duplicate gate** (below). If `cls` is in a set of ≥2
     near-duplicates *and* that set is not already resolved (§ cross-turn state):
     block the call and hand back listing the candidates. Else run normally.

  > "About to dig into `OMTG_DATAST_001_SQLite_Encrypted`, but the search matched 5
  > near-identical classes: `_SQLite`, `_SQLite_Not_Encrypted`, `_SQLite_Encrypted`,
  > `_SQLITE_Encrypted`, `_SQLiteEncrypted`. Which is the target?"

### Near-duplicate gate (the non-overfit trigger)

Merely "≥2 classes whose names contain the pattern" over-fires: `grep User` →
`UserManager` / `UserActivity` / `UserRepository` are unrelated, and nagging there
interrupts a correct workflow. The candidates must be near-duplicates **of each
other**:

- all candidate simple names **share a common prefix** that *contains the search
  pattern*, **and**
- the shared stem is **most of each name** — each candidate is `stem + short variant
  suffix` (e.g. `OMTG_DATAST_001_SQLite` + `_Encrypted`). Operationally: the search
  pattern length is a large fraction of each candidate's simple-name length (the
  operator searched ~the whole class name), *or* the candidates' pairwise common
  prefix is ≥ most of the shorter name.

`OMTG_DATAST_001_SQLite` (pattern ≈ whole name, 5 variants sharing the stem) arms;
`User` / `Payment` (short fragment of longer, semantically different names) does
not. This rule is general (no SQLite/OMTG special-case) and **must be proven by a
fixture test** on both shapes (see Testing).

### Cross-turn resolution state (must-fix: no ping-pong)

The candidate set and any resolution marker are **per-turn** (a fresh `RepeatGuard`
and name-search tracking are created each `handle_chat`). But the operator's answer
is a *new* turn, and gemma re-enumerates compulsively — so on the answer turn it
re-greps, the same 5-class set reappears, and a per-turn marker would let Trigger 2
fire **again**, forever.

Fix: persist resolution on **agent/channel state**, mirroring the existing
`self.last_usage[channel_id]` pattern:

- `self._disambig_resolved: dict[str, set[frozenset[str]]]` (channel_id → set of
  resolved candidate-name frozensets).
- When a disambiguation handback fires, add the normalized candidate-name frozenset
  to the channel's resolved set.
- Trigger 2 skips (runs the commit normally) when `cls`'s candidate set matches a
  resolved frozenset — even after a re-grep produces a fresh ref. Keyed by the
  **class-name set**, not the ref.

## Control flow (tool loop, per round)

```
conv.add_assistant_tool_calls([... all tcs ...])
done_ids = set()
for tc in tool_calls:
    # Trigger 2: BEFORE progress/dispatch — commit to an ambiguous near-dup set?
    if tc.name in _COMMIT_TOOLS:
        cset = _ambiguous_commit(tc, name_searches, capture_store,
                                 resolved=self._disambig_resolved[ctx.channel_id])
        if cset:
            q = _disambig_question(tc, cset)
            yield _handback(conv, tool_calls, done_ids, q)
            self._disambig_resolved[ctx.channel_id].add(frozenset(cset)); return
    yield ToolProgressMessage(tool=tc.name, arguments=tc.arguments)
    if guard.should_run(tc.name, tc.arguments):
        result = await tool_executor.run(tc.name, tc.arguments, ctx)
        result = guard.record(tc.name, tc.arguments, result)
        if tc.name == "static_grep_smali":
            _note_name_search(tc, result, name_searches, capture_store)
    else:
        if tc.name not in _POLL_TOOLS and guard.tripped(tc.name, tc.arguments):
            q = _spin_question(tc, guard, name_searches)
            yield _handback(conv, tool_calls, done_ids, q); return
        result = guard.blocked(tc.name, tc.arguments)
    conv.add_tool_result(tc.id, result); done_ids.add(tc.id)
# ... existing inference call ...
```

## Edge cases

- **No recent name-search when a spin trips.** Trigger 1 hands back omitting the
  candidate list (reports the stuck call + result only).
- **Name-search matched exactly 1 class / 0 near-dups.** No arming; commit runs.
- **Broad instruction-grep (`SQLiteDatabase`, 800 rows).** Referenced-type names
  don't near-duplicate → not armed.
- **`grep User` → unrelated classes.** Fails the near-duplicate gate → not armed.
- **Capture spilled to blob.** `get(ref)` restores the full body; parse defensively,
  cap the shown list (first 10, "+N more").
- **Multiple tool calls in one round.** `_handback` settles *all* batch ids; later
  calls after the firing one are dropped (operator now driving) — safe because every
  id gets a synthetic result.
- **Operator's answer turn.** Model re-issues the disambiguated commit (possibly
  after a re-grep); the resolved frozenset short-circuits Trigger 2 so it runs.
- **Poll flow (`read_hook_events` while operator triggers).** Exempt from Trigger 1
  → only the existing nudge, never a handback.

## Non-goals

- No agent_core change; no mid-turn pause primitive.
- No `pare-static-mcp` change (classes parsed from the result / read from the capture
  store PARE already holds).
- Not constraining/continuing the model autonomously (rejected for operator handback).
- Not changing `MAX_TOOL_ROUNDS`.

## Testing

Pure helpers (unit):

- `RepeatGuard.tripped()` — first hard-block returns True once per signature/turn;
  poll-tool signatures never trip a handback.
- `_candidate_classes(result_or_capture, pattern)` — extracts distinct
  referenced-type class names from a grep result's `insn`/`match` fields (NOT the
  `class` column); normalizes smali→dotted. Table-test with **real OMTG capture
  JSON** proving all five `_SQLite*` variants surface, and the dispatcher-references-
  all-variants layout.
- `_near_duplicate(candidates, pattern)` — arms on the OMTG 5-variant set; does
  **not** arm on `User`→{UserManager,UserActivity,UserRepository} or
  `Payment`→{PaymentActivity,PaymentService}. This is the anti-overfit gate; it is a
  required test.
- Class-name normalization is symmetric (incoming `cls` and candidates canonicalize
  to the same form).
- **Schema-assertion test** against the real `tool_executor.schemas()`: the commit
  tools exist and take a `cls` arg; fails loudly if a rename/prefix drift disarms the
  trigger.

`handle_chat` integration (mirror `tests/test_handle_chat.py`, `MagicMock` store):

- Spin (non-poll) → turn ends with a `ResponseMessage` naming the stuck call; the
  message list is **well-formed** (every `tool_calls` id has a following `tool`
  result — guards must-fix #1).
- Commit to an ambiguous near-dup class → the commit tool is **not** dispatched;
  turn ends with the candidate question; batch ids all settled.
- Commit to an unambiguous class → runs normally, no handback.
- **Answer-turn resume**: after a disambiguation handback, a follow-up turn that
  re-greps then re-commits to a class in the same set → commit **runs**, no second
  handback (guards must-fix #2).
- Poll spin (`read_hook_events` empty ×4) → nudge only, **no** handback (guards the
  watch-while-fiddle flow).

Behavioral (manual, operator): re-run the OMTG-SQLite prompt; PARE hands back with
the 5-class list instead of spinning or silently picking `_Encrypted`; after the
operator answers "the plain one," it proceeds without re-prompting.
