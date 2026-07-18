# RE-Methodology System-Prompt Reorg Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reorganize `pare/prompts/system.md` around a tool-agnostic RE methodology (Orient → Enumerate → Hypothesize → Verify → Re-orient) instead of by tool surface, so the model orients to the right target and recovers from dead-ends.

**Architecture:** One prose file (`system.md`) is rewritten per the approved spec. A structure-level test (`test_system_prompt.py`) pins the five beats and the load-bearing content that must survive (data-flow lesson, empty≠contradiction, vault/approval discipline) without asserting exact wording. A subagent review panel then adversarially checks the draft before it's final. No Python behavior changes — `system_prompt()` still reads the same file.

**Tech Stack:** Python 3.12, pytest, `.venv` interpreter at `.venv/bin/python`.

## Global Constraints

- Prompt content source of truth: `docs/superpowers/specs/2026-07-17-pare-re-methodology-prompt-design.md` (sections A–D). Implement its design; do not re-decide it.
- Spine is exactly **five beats**, in order: Orient → Enumerate → Hypothesize → Verify → Re-orient.
- Beat headings stay **tag-free** — no "(static serves this)" / "(dynamic serves this)".
- Tests assert **structure, not wording** (presence of beats + key content substrings), so prose can be tuned without breaking tests.
- Do **not** anchor Orient on a harness/menu label (anti-overfitting).
- Run tests with `.venv/bin/python -m pytest`.
- Target inference model is `gemma-4-26b`; prose favors concrete, imperative cues.
- Branch: `feat/re-methodology-prompt` (already created off `main`; spec already committed as `ee440f3`).

---

### Task 1: Pin the new prompt structure in tests (TDD red)

Rewrite `test_system_prompt.py` so it asserts the five-beat structure and the content the reorg must preserve, and drops the two brittle assertions the reorg intentionally removes (`"static forms the hypothesis"` heading, `"loop runs both ways"` verbatim). These tests fail against the *current* prompt — that's the red state that Task 2 turns green.

**Files:**
- Modify: `tests/test_system_prompt.py`

**Interfaces:**
- Consumes: `PareAgent().system_prompt(ctx)` → `str` (unchanged signature). The `prompt_builder` is a `MagicMock` whose `render_*` methods return `""` (see existing helper pattern).
- Produces: nothing consumed by later tasks (test-only).

- [ ] **Step 1: Add a shared helper and the new/updated tests.**

Replace the body of `tests/test_system_prompt.py` with the following (keeps the passing vault/session/dynamic-flow tests, replaces `test_system_prompt_includes_re_workflow` with beat-structure + new-content tests):

```python
"""system_prompt embeds the base RE-methodology prompt (Orient -> Enumerate ->
Hypothesize -> Verify -> Re-orient), plus vault + live-session mechanics."""
from unittest.mock import MagicMock

from pare.agent import PareAgent


def _prompt() -> str:
    agent = PareAgent()
    pb = MagicMock()
    pb.render_profile.return_value = ""
    pb.render_wisdom.return_value = ""
    pb.render_scratchpad.return_value = ""
    pb.render_commands_catalog.return_value = ""
    agent.prompt_builder = pb
    ctx = MagicMock(); ctx.channel_id = "c"
    return agent.system_prompt(ctx)


def test_prompt_has_all_five_methodology_beats():
    p = _prompt()
    for beat in ("Orient", "Enumerate", "Hypothesize", "Verify", "Re-orient"):
        assert beat in p, f"missing beat: {beat}"


def test_enumerate_builds_candidate_set_before_committing():
    """The core fix: enumerate the candidate family before committing to one."""
    p = _prompt().lower()
    assert "candidate" in p
    assert "family" in p


def test_operator_description_is_a_lead_not_ground_truth():
    """Anti-overfitting: don't anchor on the operator/harness label as the target."""
    p = _prompt().lower()
    assert "lead" in p
    assert "corroborate" in p


def test_empty_is_not_a_contradiction():
    """Empty capture => action not triggered yet; do NOT change targets."""
    p = _prompt().lower()
    assert "triggered" in p
    assert "contradict" in p  # matches "contradict"/"contradicts"/"contradiction"


def test_hypothesis_before_action_is_explicit():
    p = _prompt().lower()
    assert "before you" in p  # "...before you act / attach / hook"


def test_preserves_dataflow_exit_point_lesson():
    """Trace data to where it appears, not the named method's argument."""
    p = _prompt()
    assert "doFinal" in p
    assert "not the named" in p.lower()


def test_reorient_keeps_bidirectional_forward_lead():
    """A runtime-only class / native call is a forward lead back to static,
    not a dead-end."""
    p = _prompt().lower()
    assert "native" in p


def test_no_repeat_discipline_has_requery_carveout():
    """The general no-repeat rule must not suppress the mandatory liveness check."""
    p = _prompt()
    assert "list_sessions" in p
    assert "cannot have changed" in p.lower()


def test_preserves_vault_discipline():
    p = _prompt()
    assert "search_vault" in p
    assert "read_vault_doc" in p


def test_preserves_dynamic_flow_steering():
    p = _prompt()
    assert "enumerate_processes" in p
    assert "read_hook_events" in p
    assert "instrument from there" in p


def test_preserves_approval_gate_line():
    p = _prompt().lower()
    assert "least-invasive" in p
```

- [ ] **Step 2: Run the new tests to verify they fail against the current prompt.**

Run: `.venv/bin/python -m pytest tests/test_system_prompt.py -v`
Expected: FAIL. At minimum `test_prompt_has_all_five_methodology_beats` (no "Enumerate"/"Re-orient" in current prompt), `test_enumerate_builds_candidate_set_before_committing`, `test_operator_description_is_a_lead_not_ground_truth`, `test_empty_is_not_a_contradiction`, and `test_no_repeat_discipline_has_requery_carveout` fail. (Some `test_preserves_*` may already pass — that's fine; they guard against regression in Task 2.)

- [ ] **Step 3: Commit the red tests.**

```bash
git add tests/test_system_prompt.py
git commit -m "test: pin 5-beat RE-methodology prompt structure (red)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_016FtMrYfHohBYZ551UQUJNe"
```

---

### Task 2: Rewrite `system.md` to the five-beat methodology (TDD green)

Rewrite the prompt per spec sections A–D. Compose the prose in one coherent voice; the tests from Task 1 pin the required anchors, and the spec gives the per-beat content. Preserve every substring the `test_preserves_*` / `test_*_carveout` tests assert.

**Files:**
- Modify (full rewrite): `pare/prompts/system.md`

**Interfaces:**
- Consumes: nothing (prose file). `system_prompt()` reads it unchanged.
- Produces: a prompt string containing, at minimum, these anchor substrings (required by Task 1 tests): `Orient`, `Enumerate`, `Hypothesize`, `Verify`, `Re-orient`, `candidate`, `family`, `lead`, `corroborate`, `triggered`, `contradict`, `before you`, `doFinal`, `not the named` (case-insensitive), `native`, `list_sessions`, `cannot have changed` (case-insensitive), `search_vault`, `read_vault_doc`, `enumerate_processes`, `read_hook_events`, `instrument from there`, `least-invasive`.

- [ ] **Step 1: Write the new `pare/prompts/system.md`.**

Structure (implement spec A–D — this is the required shape and content; compose the actual sentences):

1. **Identity** — "You are PARE… analyze binaries, apps, and protocols." Keep the approval-gate line verbatim in spirit: *high/critical actions pause for operator approval; prefer the **least-invasive** tool that answers the question.*
2. **## How to work: the RE loop** — one intro line ("Reverse engineering here is a loop; static and dynamic analysis are tools that serve its beats, not the structure itself."), then the five beats as a numbered/bulleted list, headings **tag-free**:
   - **Orient** — start from the *runtime behavior the operator exercised*; the operator's description is a **lead to corroborate** against evidence, **not ground truth**; if evidence contradicts the framing, distrust the framing. Do not anchor on a harness/menu label. Lead with a quick **triage** (language/runtime, packing, obfuscation, anti-debug/anti-Frida); if static looks obfuscated/empty/encrypted, treat it as unreliable and pivot to dynamic-first.
   - **Enumerate** — **build the candidate set** before committing: all sites that could produce the symptom. If it maps to a known API **family**, enumerate the whole family, not the first idiom that matches (e.g. Android DB creation = `openOrCreateDatabase` / `SQLiteOpenHelper.getWritable|ReadableDatabase` / `SQLiteDatabase.openDatabase` / Room). Unchosen **candidate**s become Re-orient's fallback list.
   - **Hypothesize** — pick one candidate and pin **what you expect to observe at runtime**; state the hypothesis **before you** act (attach/hook/compute). Choose the source from the target (static-first when names are meaningful; dynamic-first for protocols/obfuscated/native/reflective). Data-flow lesson at full force, symbol-free, then the labeled example: *the value is usually not the named method's argument (often an alias, key id, or handle) — trace it to where it appears: the buffer handed to a `write` / **`doFinal`** / `getBytes`, or the bytes assembled before a send. E.g. (Android) `encryptString`'s arg is the alias `"Dummy"`; hook `CipherOutputStream.write`, **not the named** method.* (Mark the Android example a liftable card-candidate in a comment or aside.)
   - **Verify** — confirm, don't re-discover. **Cross-check** the captured value against the hypothesis; a value that **contradict**s it means the target is wrong — go back. Two confidence levels: observed-consistent (weaker) vs `transform(candidate) == target` byte-for-byte (proof). **Empty ≠ contradiction:** an empty capture usually means the action has not been **triggered** — ask the operator to trigger it and read again; do not change targets on empty.
   - **Re-orient** — (a) dead-end/contradiction → advance to the next unexplored **candidate**; re-reading the operator hint is a last resort; do not re-run a probe whose answer **cannot have changed** (a `[repeat-guard]` note means you are spinning). (b) unexpected runtime lead (a runtime-only class, a call into **native** code) → forward progress; return to static to explain it. Default to advancing; step back only for a specific contradiction.
3. **## Discipline** — the no-repeat rule stated generally, first-class across surfaces, with the **re-query carve-out**: re-querying genuinely mutable state (`list_sessions` liveness, on-device state) is not a repeat and is required; the rule forbids re-running a probe whose answer **cannot have changed**.
4. **## Tool mechanics (card candidates)** — live-session mechanics: once `attach` returns a `session_id` you are attached — **instrument from there**; do not `enumerate_processes`-loop or re-`attach`; flow is attach → (`enumerate_methods` if needed) → `java_hook` → operator triggers → `read_hook_events`; empty `read_hook_events` = not triggered yet. Vault: use **`search_vault`** first, **`read_vault_doc`** to read a hit, cite what you found; if nothing relevant, say so and proceed.

- [ ] **Step 2: Run the prompt tests to verify they pass.**

Run: `.venv/bin/python -m pytest tests/test_system_prompt.py -v`
Expected: PASS (all tests green).

- [ ] **Step 3: Run the full suite to confirm no regression.**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS (previously 118 passed / 3 skipped on this base; the prompt-test count is higher now — no failures).

- [ ] **Step 4: Commit.**

```bash
git add pare/prompts/system.md
git commit -m "feat(prompt): reorganize system prompt around RE methodology

Orient -> Enumerate -> Hypothesize -> Verify -> Re-orient. Static/dynamic
demoted from spine to tools serving beats; no-repeat rule generalized with
a re-query carve-out; operator description treated as a lead to corroborate,
not the target. Implements the approved spec.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_016FtMrYfHohBYZ551UQUJNe"
```

---

### Task 3: Adversarial review panel on the drafted prompt

Mirror the spec-review panel, now on the drafted prose. Independent lenses check that the draft actually implements all five beats, regresses no current strength, and dodges the overfitting trap; then apply the surviving fixes.

**Files:**
- Modify (if the panel surfaces must-fixes): `pare/prompts/system.md`, possibly `tests/test_system_prompt.py`

**Interfaces:**
- Consumes: the committed `pare/prompts/system.md` from Task 2 and the spec.
- Produces: a revised prompt (or a confirmation that none is needed).

- [ ] **Step 1: Run the review panel.**

Use the Workflow tool: 4 lenses (methodology-fidelity, model-behavior/gemma-actionability, overfitting/transfer, regression-vs-current-prompt) each read `pare/prompts/system.md` + the spec and return `{verdict, must_fix[], should_fix[]}`; one synthesis agent consolidates. (Reuse the structure of `pare-prompt-design-panel`.)

- [ ] **Step 2: Apply surviving must-fixes.**

For each must-fix that holds up, edit `system.md` (and add a structure assertion to `test_system_prompt.py` if the fix is about presence of content). Re-run `.venv/bin/python -m pytest tests/test_system_prompt.py -q` — expect PASS.

- [ ] **Step 3: Commit any revisions.**

```bash
git add pare/prompts/system.md tests/test_system_prompt.py
git commit -m "fix(prompt): apply panel review to RE-methodology prompt

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_016FtMrYfHohBYZ551UQUJNe"
```

- [ ] **Step 4: Behavioral transfer check (manual, non-blocking for merge).**

Note in the PR description that the spec calls for two live checks that are *not* unit tests: (1) re-run the OMTG-SQLite prompt and confirm the model *enumerates* the DB-creation family and reaches `OMTG_DATAST_001_SQLite`; (2) one symbol-poor target (stripped binary / protocol capture) to confirm Orient degrades gracefully. These require a running PARE + inference and are for you to run/observe; the plan does not automate them.

---

## Self-Review

**Spec coverage:**
- Spine 5 beats → Task 1 `test_prompt_has_all_five_methodology_beats`, Task 2 Step 1.2.
- Enumerate candidate set → Task 1 `test_enumerate_builds_candidate_set_before_committing`, Task 2 Enumerate beat.
- Drop menu-label anchor / lead-not-ground-truth → Task 1 `test_operator_description_is_a_lead_not_ground_truth`, Task 2 Orient beat.
- Empty ≠ contradiction → Task 1 `test_empty_is_not_a_contradiction`, Task 2 Verify beat.
- Hypothesis-before-action, source-not-hardcoded → Task 1 `test_hypothesis_before_action_is_explicit`, Task 2 Hypothesize beat.
- Data-flow lesson full force → Task 1 `test_preserves_dataflow_exit_point_lesson`, Task 2 Hypothesize beat.
- Bidirectional Re-orient → Task 1 `test_reorient_keeps_bidirectional_forward_lead`, Task 2 Re-orient beat.
- No-repeat + re-query carve-out → Task 1 `test_no_repeat_discipline_has_requery_carveout`, Task 2 Discipline section.
- Vault + approval-gate retained → Task 1 `test_preserves_vault_discipline` / `test_preserves_approval_gate_line`, Task 2 Identity + mechanics.
- Tag-free headings → Global Constraints + Task 2 Step 1.2 (not directly unit-tested; enforced by review in Task 3).
- Transfer/behavioral validation → Task 3 Step 4 (documented as manual).
- Compute-verify fold-in → present on `main`; Task 2 Verify beat references it.

**Placeholder scan:** none — test code is complete; prompt content is specified by structure + required anchor substrings + spec reference (verbatim final prose is the implementation deliverable, deliberately composed at execution, with anchors pinned by tests).

**Type consistency:** helper `_prompt()` used by all tests; anchor substrings in Task 2 "Produces" match the asserts in Task 1 exactly.
