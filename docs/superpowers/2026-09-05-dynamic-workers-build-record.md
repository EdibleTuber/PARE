# Dynamic worker loading — build record (phase 1: `agent_core` v1.8.0)

**Date:** 2026-09-05
**Spec:** [`specs/2026-09-04-dynamic-worker-loading-design.md`](specs/2026-09-04-dynamic-worker-loading-design.md)
**Plan:** [`plans/2026-09-05-dynamic-workers-agent-core.md`](plans/2026-09-05-dynamic-workers-agent-core.md)
**Carried follow-ups:** [`2026-09-05-agent-core-1.8.0-followups.md`](2026-09-05-agent-core-1.8.0-followups.md)
**Outcome:** `agent_core` v1.8.0 — 17 commits, 852 passed / 2 skipped. PARE green against it at 165 / 3.

This is the paper trail: what was decided, what was found, and what caught it.
It exists to be read *after* the fact, so it records the failures and the reasoning,
not a summary of the feature. For what the feature does, read the spec.

---

## 1. How the work was structured

Design → review panel → spec → two plans → per-task execution with a review gate on
each → one whole-branch review at the end.

- **Design phase.** A four-reviewer panel (fact-check, security, framework API, PARE
  integration) read the v1 spec. It returned one blocker and three findings that
  changed mechanism rather than wording. The spec was rewritten as v2 (`af2e082`).
- **Execution.** Nine tasks, each with a fresh implementer, a task-scoped review, and
  a fix loop where needed. Six fix rounds ran across the branch.
- **Final gate.** One whole-branch review, then one consolidated fix wave, then a
  scoped re-review, then a residual pass.

## 2. Commit trail

### `agent_core`, branch `feat/dynamic-worker-lifecycle`, tag `v1.8.0` → `b62ee1a`

| # | Commit | What |
|---|---|---|
| 1 | `0cc4d59` | pin `mcp<2` and `fastmcp<2.12` |
| 2 | `0958adf` | own each MCP connection in a dedicated task |
| 3 | `274b2d0` | close on cancelled connect, lock disconnect, normalize `CancelledError` |
| 4 | `a4f107d` | stamp worker provenance on synthesized tools |
| 5 | `fc3e570` | make `ToolExecutor` mutable at runtime |
| 6 | `3c2d88b` | hold risk invariants across worker reloads |
| 7 | `dfcc5b8` | close review Criticals in risk_pool lifecycle invariants |
| 8 | `c78adb6` | stop high-water escalation bypassing the `external_mcp` floor |
| 9 | `f0b0d33` | add `WorkerSpec.autoload`; stop swallowing cancellation |
| 10 | `fec6240` | add `WorkerManager` for runtime load/unload/reload |
| 11 | `c903bff` | manager rollback/atomicity + true cancellation propagation |
| 12 | `3bee6a8` | shield `_reap`'s join, fix `_cancel_owner` cleanup leak |
| 13 | `2cc11c8` | export `WorkerManager`, `WorkerRegistry`, `RiskAwareToolPool` |
| 14 | `4c8c0b1` | add `astartup`/`ashutdown` hooks around `serve()` |
| 15 | `3d8e6ec` | release 1.8.0 (incl. CHANGELOG backfill for 1.7.0–1.7.3) |
| 16 | `dee8eb6` | close the final-review findings |
| 17 | `b62ee1a` | bound `connect()`'s error-path reap, seed the floor ratchet, guard the kill |

### PARE, branch `feat/dynamic-worker-loading`

| # | Commit | What |
|---|---|---|
| 1 | `e874cd9` | v1 design |
| 2 | `af2e082` | v2 design after the review panel |
| 3 | `d7a7f75` | correct the mitm worker's tool count and risk profile |
| 4 | `054d970` | pin mitm's traffic-altering tools, widen pin coverage |
| 5 | `977c39b` | split the design into two plans |
| 6 | `bf21926` | correct the `start_serving=False` socket claim |
| 7 | `63d8b88` | widen `test_frida_wire_tier_e2e`'s inner-pool fake for 1.8.0 |
| 8 | `41f82fe` | record carried follow-ups |

## 3. Defects found, and what caught each

Ordered by severity. The right-hand column is the point of this table: different
review stages catch structurally different classes of defect.

| Defect | Caught by |
|---|---|
| MCP client teardown is **task**-bound, not loop-bound — unload could not work, and swallowed its own `RuntimeError` while leaking the subprocess | Design-phase panel, reproduced empirically |
| Reload could silently **lower** a tool's effective tier — `frida_read_memory`/`frida_java_hook` are protected only by the wire tier, and the floor is `low` | Design-phase panel (security lens) |
| `close_all` used `or` instead of a set union, so once any worker reloaded, every never-reloaded worker kept its session approvals across a full teardown | Task review, by executing the code |
| `_max_tier` hashed a possibly-unhashable advertised tier, raising inside the `list_tools` loop — a hostile worker ordering its listing dropped every later tool to the floor | Task review, by executing the code |
| A cancelled load left a registered spec, an emptied tier table and a live published client — a tool needing approval dispatched at the floor with an honest-looking audit row | **Whole-branch review** (crosses three files) |
| The spec-mandated hard-kill was never implemented, and `_reap` popped `_owners` before awaiting, so a timed-out unload left an unreachable orphan | **Whole-branch review** |
| `close_all`'s reap was unbounded and was the only reaper for the orphans above | **Whole-branch review** |
| The fix wave introduced a **worse** regression: `close_all` hung forever where it previously returned | Scoped re-review of the fix wave |
| `RiskAwareToolPool`'s widened `inner` contract broke PARE's test double | **Running the consumer's suite in the release task** |
| A vacuous test — baseline captured from an emptied `BUILTIN_TOOLS`, so `remove_worker` could have been `_tools.clear()` | Task review, prompted to look for that shape |

**The lesson worth keeping:** eleven of these were defects in code the *plan* supplied,
not implementer error. Tests specified in the same plan share its author, so a shared
misconception passes both. This is now recorded as a global working rule.

## 4. What each review stage was actually worth

- **Task-scoped review** caught per-file correctness — and nothing that crossed files.
- **Whole-branch review** caught all three Criticals, every one of them assembled from
  behaviour that is defensible in each file and wrong across files. None was reachable
  from a single task's diff.
- **Scoped re-review of fixes** caught a regression worse than the bug being fixed.
  Fix diffs need reviewing as much as feature diffs.
- **The consumer's test suite** caught the one break the library's own 836 tests could
  not, because it lived in the contract, not the code.
- **Adversarial framing mattered.** Reviewers told to reason from the tests approved
  code that reviewers told to construct attack sequences rejected. Several proved
  findings by executing the code or reconstructing the pre-fix tree.

## 5. Rulings

Decisions taken during execution without checking in, with rationale and stated cost
if wrong. Recorded in full so they can be second-guessed later.

| # | Ruling | Cost if wrong |
|---|---|---|
| 1 | Branch in the checkout, not a worktree — the venv installs `agent_core` editable from that exact path | None; still isolated |
| 2 | Batch Tasks 3+4 | Larger review surface |
| 3 | **Split Task 8** — the plan had a circular dependency (Task 6 needs a field Task 8 adds; Task 8's exports need Task 6's module) | None; same code, compilable order |
| 4 | Task 4 needs no `runtime.py` change | A dead change to flag |
| 5 | Leave a weak `pytest.raises` tuple standing | Wouldn't notice a changed exception type |
| 6 | Normalize `CancelledError` at source rather than compensating downstream | A caller wanting SDK cancellation sees `ConnectionError` |
| 7 | **Elevate a Minor** the reviewer lacked context to weight | Larger fix diff |
| 8 | **Park the externally-cancelled-owner leak** | *Wrong — see below* |
| 9 | Bundle two Minors into an existing round | Larger re-review |
| 10 | Manager uses public proxies, not `_pool._inner` | Two delegating methods |
| 11 | **Elevate three Minors**, each contradicting a constraint the brief itself stated | Larger fix diff |
| 12 | Batch 8b's exports into Task 7 | Reviewed beside a daemon change |
| 13 | **Correct the spec**, not just the brief, on the AF_UNIX claim | None; verified twice |
| 14 | Bundle a one-line assertion into the release task | Trivial |
| 15 | **Stay 1.8.0, not 2.0.0**, for the `inner` contract tightening | A consumer with a custom inner pool hits `AttributeError` with only a CHANGELOG note |
| 16 | Fix PARE's test double immediately so plan 2 starts green | A test edit lands early |
| 17 | **Override "no second fix wave"** — we had introduced a shutdown deadlock worse than the bug it replaced | One extra review cycle |

**Ruling 8 was wrong.** It parked a leak on the reasoning that `close_all` reaps
orphans at shutdown. Both halves were false: after a disconnect timeout the owner is
no longer in `_owners`, and the reap was unbounded regardless. It resurfaced as part
of Critical 1 in the whole-branch review and was fixed in `dee8eb6`/`b62ee1a`.

## 6. Corrections made to the spec during the build

The spec is the binding authority, so factual errors in it were corrected rather than
worked around:

- **`start_serving=False` socket behaviour** (`bf21926`). §6.7 claimed a client
  connecting during startup would queue in the listen backlog. False for AF_UNIX —
  the socket has not been `listen()`ed, so the connect is refused. The decision stands
  on the stale-inode argument; only the rationale was wrong. The refusal is now the
  discriminator in `tests/test_daemon_startup.py`.
- **mitm's tool count and risk profile** (`d7a7f75`, `054d970`). `workers.yaml` and the
  README described mitm as "four read-only tools, all wire tier low". It advertises
  eleven, six of which mutate state, with `inject_request` at `critical` — and no
  operator pin covered any of them. Pins added.

## 6b. Verified by use, not only by test

The suite exercises the lifecycle against a toy stub. Before starting phase 2 the
whole thing was also run against the real `pare-static-mcp` binary via
[`scripts/live_worker_lifecycle.py`](../../scripts/live_worker_lifecycle.py) —
no inference server required, since the lifecycle never touches the model.

All ten sections passed: the constructor's duck-type guard rejects a bare
`MCPClientPool`; a real load registers 10 prefixed tools and spawns a live pid; wire
tiers resolve through the gate; `status()` matches reality; unload removes the tools
and reaps the process; the spec is gone so a dispatch cannot resurrect it; reload
yields a genuinely fresh pid twice over; a missing binary fails with
`spawn_failed`, surfaces in `last_error`, and leaves no residue; `close_all` reaps
everything; and the audit log carries `worker_loaded`/`worker_unloaded` rows with
`action`, `resolved_command`, `command_mtime` and `command_size` — so a reload is
distinguishable from an unload plus a load, and an artifact swap is visible.

Two failures surfaced during the run, both in the harness rather than the code: it
assumed a `hardware` worker that plan 2 has not declared yet, and it asserted on
`mtime`/`size` where the shipped fields are `command_mtime`/`command_size`. Worth
recording, because it is what an unrun assumption looks like when it finally runs.


## Phase 2 — final review outcome and the limits of what was verified

The whole-branch review found **five Important defects and no path where a tool
dispatches below its risk floor**. That negative result is the most useful line in this
document: the reviewer went looking specifically for a tier downgrade — registry-gated
loading, the floor and wire-tier ratchets surviving unload, pins snapshotted at
`setup()` and never re-read, both executor-bypass sites guarded — and did not find one.

The five, and what they have in common:

| Defect | Note |
|---|---|
| A failed `/worker reload` destroyed live state but rendered via the *load* renderer, so the operator read "failed" as "unchanged" | reload has unload's semantics; the branch got load and unload right and missed the third path |
| `worker_of` prefix-matched the **declarative** tool `static_analyze` to the `static` worker, producing a false handback about a tool that had just worked | spec §8.2 called this namespace overlap "latent"; a later task made it active in a way §8.2 did not anticipate |
| `requires = ("worker_manager",)` was vacuous — a class-level default made `hasattr` always true — and `/worker` was the one consumer without a None-guard | a guarantee specified, built, then neutralised in the same file |
| `docs/mitm-quickstart.md` still called mitm "four read-only tools" | the exact falsehood §8.8 was written to remove, in the doc the corrected README links to |
| `CancelledError` passed through `except Exception`, so a client disconnect mid-dispatch left unsettled tool-call ids | pre-existing; this branch is what states the invariant |

Two of the five are about **telling the operator something false**, which in a tool that
hooks processes and writes memory is its own kind of unsafe.

Fixes went in two waves rather than one of fifteen — the phase-1 lesson about fix-round
size applied deliberately. Both waves were re-reviewed; the wave-1 re-review validated a
deviation by proving executor membership and `_loaded` membership are mutated with no
`await` between them on every path, so they cannot disagree.

### What is verified, and how

| Property | How |
|---|---|
| Risk gating holds across load/unload/reload | Whole-branch review, adversarial, plus `agent_core`'s own suite |
| Worker lifecycle against a real binary | `scripts/live_worker_lifecycle.py` — real pids, real MCP handshake, real audit rows |
| Operator surface against a real daemon | `scripts/smoke_worker_commands.py` — real socket, real protocol, no inference server |
| Captures survive an unload | Unit test plus the executor-provenance argument |
| **The model handing back instead of grinding** | **NOT live-verified — see below** |

### The one thing that stayed unverified, and why

The unloaded-worker handback trigger was driven against a real inference server twice
and **could not be provoked**:

- Probe 1 (unload `static`, ask for a static grep): the model made zero tool calls. It
  noticed it had no static tool and asked how to proceed.
- Probe 2 (seed the channel with a real `static_grep_smali` call *first*, then unload,
  then ask for the same tool): it still did not reach for the removed tool.

Both failed for the same reason: **the primary mechanism works.** Unloading removes the
tools from `schemas()`, and the model does not call what it cannot see. The handback is
defence-in-depth for stale-history reaching; it is unit-tested — including the
`POLL_TOOLS` case that would otherwise burn the whole round budget — and remains
verified-by-test only. Forcing the condition synthetically would only re-test what the
unit tests cover.

Two observations from those transcripts, both model behaviour rather than branch
defects, both worth knowing for an RE agent:

- Probe 1's model offered to "switch to dynamic enumeration" via frida — the
  route-around-the-gap tendency `system.md`'s new paragraph exists to suppress. It
  asked rather than acting, so the prompt is half-working.
- Probe 2's model called `search_capture` and reported the *prior* result as though the
  tool had just run again ("failed again with the same error"). It had not run.

### An incidental validation

The first probe crashed with `ConnectionRefusedError` against a socket whose *file*
existed — exactly the `start_serving=False` behaviour the spec correction documents,
observed from the client side rather than reasoned about. Retrying as a real client
must, it connected after three refused attempts: `astartup` finished loading all three
workers before the daemon accepted anything.

## 7. Status of related documents

| Document | State |
|---|---|
| `specs/2026-09-04-dynamic-worker-loading-design.md` | v2; §6/§7 implemented in `agent_core`, §8.1/§8.4/§9 now implemented in PARE (phase 2, below) |
| `plans/2026-09-05-dynamic-workers-agent-core.md` | **Executed.** Deviations recorded in §5 above |
| `plans/2026-09-05-dynamic-workers-pare-wiring.md` | **Executed.** Deviations and defects recorded in the phase-2 sections below |
| `agent_core/CHANGELOG.md` | 1.8.0 entry written and verified against shipped code; 1.7.0–1.7.3 backfilled |
| `agent_core/README.md` | Updated for the new surface |
| PARE `README.md` | Corrected in phase 2's Task 9 — "Adding a worker" no longer says "restart the daemon"; `/worker` is documented |

---

# Phase 2: PARE wiring (`feat/dynamic-worker-loading`)

**Date:** 2026-09-05
**Plan:** [`plans/2026-09-05-dynamic-workers-pare-wiring.md`](plans/2026-09-05-dynamic-workers-pare-wiring.md)
**Ledger:** `.superpowers/sdd/2026-09-05-dynamic-workers-pare-wiring/progress.md` (scratch, gitignored — this section is the durable record of it)
**Outcome:** PARE, branch `feat/dynamic-worker-loading` — 11 commits over 9 tasks,
165 → 202 passed / 3 skipped (agent_core unaffected, holds at 852 passed / 2
skipped throughout). Two new execution scripts (`scripts/live_worker_lifecycle.py`,
`scripts/smoke_worker_commands.py`) exercise the real `WorkerManager` and the real
operator surface with no inference server.

This picks up where phase 1 left off: `agent_core` v1.8.0 shipped the library
mechanism (§6/§7 of the spec); this phase wires it into PARE — the `/worker`
command, the unloaded-worker handback, and the docs that describe both (§8.1,
§8.4, §9 of the spec).

## 8. How this phase was structured

Same discipline as phase 1, scaled down: a pre-flight conflict scan across all
nine tasks before any code was written (cross-task dependency pairs, per-task
internal agreement, two rulings up front), then nine tasks each with a fresh
implementer, a task-scoped review, and a fix loop where needed. No whole-branch
review was run separately — the tasks are more sequential and lower-fan-out than
phase 1's library change, so each task's review carried the cross-task check
(e.g. Task 4's reviewer traced the implementer's `_render_unload` deviation
against `manager.py`'s real teardown order rather than trusting the diff).

## 9. Commit trail

### PARE, branch `feat/dynamic-worker-loading`, `e589c3d` → `a7f6352`

| # | Task | Commit | What |
|---|---|---|---|
| 1 | 1 | `e589c3d` | require `agent_core` v1.8.0, pin `mcp<2` |
| 2 | 2 | `859f52f` | declare `hardware` as a catalog entry, add `autoload` keys |
| 3 | 3 | `4fc24d6` | move worker discovery from `register_tools` to `astartup` |
| 4 | 4 | `c00e998` | add `/worker` for runtime worker lifecycle |
| 5 | 4 (fix) | `d012933` | surface the full `last_error`, not just `render_table`'s clip |
| 6 | 5 | `eb9a860` | short-circuit fast-path commands' calls to an unloaded worker |
| 7 | 6 | `3764273` | hand back to the operator on an unloaded-worker call |
| 8 | 7 | `c7eada2` | tell the model and the operator about worker state (prompt, `/health`) |
| 9 | 8 | `186b47c` | pin that unloading a worker keeps its captured findings |
| 10 | 8 | `4a63a94` | add a non-interactive smoke script for `/worker` and `/health` |
| 11 | 8 (fix) | `a7f6352` | tighten `/devices` discrimination, footer assertion, run-scoped audit dir |
| — | 9 | (this change) | correct README, spec status/example, add this section |

## 10. Defects found in phase 2, and what caught each

Same point as phase 1's table: different stages catch structurally different
defect classes. Phase 2's tasks are smaller and more sequential than phase 1's
library rewrite, so most defects here were caught one task earlier in the
pipeline — by the implementer noticing the brief's own sketch was wrong, or by
task review executing the code rather than reading it.

| Defect | Caught by |
|---|---|
| The brief's `_render_unload` sketch said "client disconnected" unconditionally, contradicting the `disconnect_timeout` WARNING printed right after it — wrong on the one path where the worker's process may still be running | Implementer (Task 4), reasoning from `WorkerManager.unload()`'s own contract, not the brief's sketch |
| `/worker list`'s `last_error` column clipped a real `FileNotFoundError` to ~22 chars inside `render_table`'s 100-char budget across 8 columns — invisible on exactly the error §8.4 exists to surface | Task-scoped review (Task 4), by rendering it with the real configured path |
| The controller's own premise — that distinct `frida_*` tool names mean `RepeatGuard` never fires, burning all 50 rounds — was wrong for the implementer's chosen scenario (the repeated tail still trips the guard, just with a generic message); right in general only for non-repeating calls, and the `POLL_TOOLS` case is the one that genuinely burns rounds | Implementer disclosure (Task 6), adjudicated by task review hand-tracing `RepeatGuard` against both scripts |
| `/devices`-after-unload regression test asserted "non-empty and no literal 'error'" — `unavailable_reason`'s own message ("worker 'frida' is not loaded — …") satisfies both, so the test could not fail on the regression it was written to catch | Task-scoped review (Task 8), by unloading `frida` and calling `/devices` against the real assertion |
| `scripts/live_worker_lifecycle.py` used a date-keyed audit scratch path, so same-day reruns accumulate rows and a count-based check can fail spuriously | Self-caught (controller's own script), fixed alongside Task 8's fix round rather than deferred |
| Spec §9.1's `/worker unload` example showed only the success path, so as written it would print the self-contradicting "client disconnected" on a `disconnect_timeout` | Task 4's implementer, carried forward explicitly in the ledger to Task 9 (fixed here — see §12) |

**The lesson repeats from phase 1:** three of these six were defects in the
*brief's own sketch code* (the `_render_unload` text, the RepeatGuard reasoning,
and the spec example it was copied from), not implementer error — confirming the
phase-1 working rule that plan-supplied code carries the planner's misconceptions.

## 11. Rulings

| # | Ruling | Cost if wrong |
|---|---|---|
| 1 | Carry Task 6's brief-supplied stub test (`test_every_tool_call_id_is_settled`, a known-incomplete sketch flagged in the plan's own self-review) into the dispatch as a named defect the implementer must fix, not a spec to transcribe | An implementer transcribes a vacuous test, which task review should then catch |
| 2 | Split Task 8's manual smoke test: the non-interactive half (daemon starts, workers autoload, `/worker` round-trips, a missing binary surfaces as `spawn_failed`) is scriptable and belongs to the implementer (`scripts/smoke_worker_commands.py`); the genuinely interactive half (asking the model something that needs an unloaded worker and watching it hand back) needs an inference server and a human reading the transcript, done separately after the branch was otherwise complete | The model-facing handback stays verified-by-unit-test only, not exercised end to end, until that interactive pass runs |
| 3 | Run `scripts/live_worker_lifecycle.py` (phase 1's harness) as part of Task 8's verification, not pytest alone | A lifecycle regression that a toy-stub test suite cannot see (it never touched a real worker binary in phase 1 either) ships unnoticed |

## 12. Corrections made to the spec during the build

- **§9.1's `/worker unload` example** (fixed in this doc pass, Task 9). The
  example showed only the success-path text ("client disconnected")
  unconditionally. `WorkerManager.unload()` removes the worker's tools
  *before* attempting the timeout-bounded disconnect, so on a
  `disconnect_timeout` the disconnect did not complete and the process may
  still be running — the implemented `_render_unload` (`pare/commands/worker.py`,
  Task 4) branches on `res.ok` and only claims "client disconnected" when
  true. Found by Task 4's implementer while building against the brief's
  copy of the example; carried in the ledger to Task 9 rather than
  silently fixed mid-task, and corrected here with an inline note, matching
  the style of the existing `start_serving=False` correction in §6.7.
- **Status header** (this doc pass, Task 9). §8.1/§8.4/§9 moved from
  "pending in the PARE wiring plan" to implemented, dated to this branch.
