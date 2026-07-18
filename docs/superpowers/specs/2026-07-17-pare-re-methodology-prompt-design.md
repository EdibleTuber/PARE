# PARE system prompt: RE methodology reorg

**Date:** 2026-07-17
**Status:** approved (design)
**Related:** `pare/repeat_guard.py` (the mechanical counterpart, same incident)

## Motivation

A live OMTG-DATAST-001-SQLite run stalled: the model burned its whole tool-call
budget (49 calls, 41% verbatim repeats) orbiting the wrong class
(`OMTG_CODING_003_SQL_Injection_Content_Provider`) and never touched the real
target (`OMTG_DATAST_001_SQLite`, which exists in the APK). Root cause was two
**methodology** failures, not tool failures:

1. **No orientation.** The model idiom-matched (`SQLiteOpenHelper` /
   `getWritableDatabase`) across the whole binary. That idiom exists only in the
   content provider; the real target creates its DB with `openOrCreateDatabase`,
   so it never surfaced. The model anchored on the one class its guess matched.
2. **No re-orientation on dead-ends.** Once anchored, it re-issued identical
   probes returning identical (often empty) results, with nothing prompting it to
   step back.

The current `system.md` organizes "How to work" around **tool surfaces** (static
tools → hypothesis, live sessions → verify), so the one anti-loop rule that
exists lives *only* in the dynamic section — added where it was first needed, not
as a principle. "Orient first" is absent entirely.

## Thesis

RE **methodology** belongs in PARE, stated once and tool-agnostic. Tool
**mechanics** (how to attach, how `read_capture` works, smali specifics) are
secondary and are candidates for extraction into per-worker capability cards
(see the context-scaling / capability-cards direction). Reframing the prompt
around methodology is both the correct fix and the shape the cards architecture
already wants.

Prose sets the *default approach*; the mechanical repeat-guard enforces the
*floor*. This split matters: the anti-loop guidance already existed in prose on
the dynamic side and the model ignored it — which is why the guard exists. We do
not expect prose alone to stop the pathology.

## Panel review

A 5-lens judge panel (RE-methodology, model-behavior, overfitting, cards-
architecture, regression-risk) reviewed this spec: unanimous **sound-with-fixes /
proceed-with-fixes**. The direction was endorsed with no redesign, but the panel
found the reorg *as first drafted* would **relabel, not prevent** the incident —
and caught two overfitting holes. All 4 must-fixes and the highlighted should-
fixes are folded into the design below. Key catches:

- The loop needed a beat that builds a **candidate set** before committing —
  without it, "reconsider the target" has no fallback and the model re-lands on
  the first idiom (the 49-call orbit). → new **Enumerate** beat.
- Orienting from "the menu message" lets the prompt **pass the OMTG test by
  reading the answer off the harness label** — designing-for-the-test. → operator
  description is a *lead to corroborate*, not ground truth; add a transfer check
  on a symbol-poor target.
- Collapsing "empty result" and "contradiction" into one dead-end branch would
  make the model abandon a *correct* hook when the operator simply hasn't
  triggered the action yet. → keep the distinction.

## Design: reorganized `system.md`

Separate methodology (core) from tool mechanics (card candidates), with a clean
seam.

### A. Identity *(keep — do not trim load-bearing lines)*
"You are PARE… analyze binaries, apps, protocols." Explicitly **retain** the
approval-gate line ("high/critical actions pause for operator approval; prefer
the least-invasive tool that answers the question"). Only prose is reorganized,
not removed.

### B. RE methodology — the loop *(new spine)*
Tool-agnostic, **five beats**. Static/dynamic stop being the organizing principle
and become *tools serving beats*. Beat headings stay **pure** — no
"(static serves this)" / "(dynamic serves this)" tags, which would re-smuggle the
spine we are demoting (static also serves Orient/Enumerate/Re-orient).

- **Orient** *(new)* — start from the *runtime behavior the operator exercised*
  and locate the region of code it enters. The operator's description is a **lead
  to corroborate against the evidence, not ground truth**: if the code/runtime
  contradicts the framing, distrust the framing, not just the current probe. Do
  **not** anchor on a harness/menu label as the target. Begin with a quick
  **triage** — language/runtime (Java/Kotlin/native/Flutter), packing, string/
  name obfuscation, anti-debug/anti-Frida — because it decides whether static
  output is even trustworthy; if static looks obfuscated/empty/encrypted, treat
  it as unreliable and pivot to dynamic-first tracing.
- **Enumerate** *(new — the core fix)* — before committing to one target,
  **build the candidate set**: all the sites that could produce the symptom. If
  the symptom maps to a known API family, enumerate the *whole family*, not the
  first idiom that matches (e.g. Android DB creation = `openOrCreateDatabase` /
  `SQLiteOpenHelper.getWritable|ReadableDatabase` / `SQLiteDatabase.openDatabase`
  / Room). Disambiguate by which candidate the triggered behavior actually
  reaches. The unchosen candidates become Re-orient's fallback list.
- **Hypothesize** — pick one candidate and pin **what you expect to observe at
  runtime.** Keep the explicit imperative **"form the hypothesis and expected
  observation before you act (attach/hook/compute)"** — do not let loop-order
  imply it; gemma needs the hard brake. Choose the hypothesis *source* from the
  target: static-first when names/structure are meaningful; **dynamic-first** for
  protocols (observe the wire, then explain) and for obfuscated/packed/native/
  reflective targets where no nameable static method exists. Preserve the
  data-flow lesson at **full force**, stated symbol-free: *the value you want is
  usually not the named entry point's argument (often just an alias, key id, or
  handle) — it materializes downstream; trace it to where it appears: the buffer
  handed to a `write`/`doFinal`/`getBytes`, or the bytes assembled just before a
  send.* Then the labeled concrete example, marked a liftable card-candidate:
  *e.g. (Android) `encryptString`'s arg is the key alias `"Dummy"`; hook
  `CipherOutputStream.write`, not `encryptString`.*
- **Verify** — confirm, don't re-discover. Cross-check the captured value against
  the hypothesis; **a value that positively contradicts it means the target is
  wrong — go back, don't declare success.** Distinguish two confidence levels:
  "observed value consistent with hypothesis" (weaker) vs compute-and-verify
  `transform(candidate) == target` byte-for-byte (proof). **Empty ≠
  contradiction:** an empty capture usually means the action has not been
  triggered — ask the operator to trigger it and read again; do **not** treat
  empty as a reason to change targets.
- **Re-orient** *(new / consolidated)* — two directions, both explicit:
  (a) **dead-end/contradiction →** advance to the **next unexplored candidate**
  from Enumerate's set (default); re-reading the operator hint is a *last resort*,
  not the first move, and **do not re-run a probe whose answer cannot have
  changed** — a `[repeat-guard]` note means you are spinning, change approach.
  (b) **unexpected runtime lead →** if Verify surfaces something unpredicted (a
  runtime-only class, a call into native code), that is *forward progress*, not a
  dead-end: return to static to explain it. Keep the forward-progress brake:
  default to advancing; step back only to resolve a *specific* contradiction, not
  to re-explore covered ground.

### C. Discipline / guardrails *(consolidated)*
The no-repeat principle stated once and generally (currently buried as "don't
loop on `enumerate_processes`"), first-class across every surface — **with the
re-query carve-out**: re-querying genuinely mutable state (session liveness via
`list_sessions`, on-device state) is *not* a repeat and is required; the rule
forbids re-running a probe whose answer cannot have changed. Retain a concrete
situated instance next to the live-session mechanics in D ("once `attach` returns
a `session_id` you are attached; do not re-attach or re-enumerate to re-find the
app") — a rule already ignored as prose gets weaker, not stronger, when made
purely abstract.

### D. Tool mechanics *(reorganized; explicitly marked card candidates)*
Static mechanics; live-session mechanics (attach → hook → trigger → read;
liveness is mutable, re-query don't trust memory); PAL vault. Preserve the vault
discipline at **full strength**: *search the vault first, then cite what you
found; if nothing relevant, say so and proceed* (first-resort ordering + citation
habit + say-if-empty — the curbs on answering from training data). Kept in
`system.md` for now under a clearly-labeled "mechanics" heading so the cards
project can lift each cleanly later. No card system is built here.

## Key moves (summary)

- Anti-loop rule: leaves the dynamic section → general discipline (C) + re-query
  carve-out + a retained situated instance in D.
- Orient + **Enumerate**: added as the front of the loop; Enumerate (candidate set
  before commit) is the fix that turns "orient" from a slogan into prevention.
- Operator description reframed as a corroborated lead, not the target; harness/
  menu label dropped as an anchor (anti-overfitting).
- Static/dynamic: demoted from spine to tools-serving-beats; entry mode chosen
  from the target, not hardcoded static-first.
- Data-flow lesson kept at full force (symbol-free principle + labeled Android
  example); beat headings kept tag-free.
- Empty ≠ contradiction preserved; bidirectional Re-orient (dead-end *and*
  forward-lead) preserved with the forward-progress brake.

## Non-goals

- Not building capability cards (separate project); only making the seam clean
  and marking the Android example a liftable card-candidate.
- Not relying on prose to enforce the anti-loop floor — that is the guard's job.
- No behavior change to tools, workers, or the handle_chat loop.

## Testing / validation

- `tests/test_system_prompt.py` covers prompt assembly; extend to assert the
  **five** methodology beats (incl. Enumerate) and the discipline rule are present
  (structure, not wording).
- **Transfer check, not just the observed input.** Re-running only the OMTG-SQLite
  prompt proves little (it may prove menu-label matching — the overfitting hole
  the panel flagged). Validate on: (1) OMTG-SQLite — confirm the model *enumerates*
  the DB-creation family and reaches `OMTG_DATAST_001_SQLite` rather than idiom-
  matching the content provider; and (2) at least one **symbol-poor target**
  (stripped binary or a protocol capture) — confirm Orient degrades gracefully
  and does not depend on a named class in menu text. Behavioral; not unit tests.
