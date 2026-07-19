# Operator-Handback Checkpoints Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make PARE hand control back to the operator when the tool loop misbehaves — on a confirmed spin, and at commit-time to one of ≥2 near-duplicate name-matched classes — instead of spinning to the cap or silently committing to the wrong class.

**Architecture:** Handback = early turn-termination (`conv.add_assistant(q); yield ResponseMessage(q); return`) — no agent_core change, no mid-turn pause. Pure helpers live in a new `pare/handback.py`; `handle_chat` stays thin and wires two triggers plus per-channel resolution state. `RepeatGuard` gains a `tripped()` method.

**Tech Stack:** Python 3.12, pytest, `.venv/bin/python`. Base branch `feat/operator-handback-checkpoints` (off merged `main`; spec committed `68e3456`).

## Global Constraints

- Source of truth: `docs/superpowers/specs/2026-07-18-operator-handback-checkpoints-design.md` (post-panel version). Implement it; don't re-decide.
- **No agent_core change** (pinned @v1.7.3); **no `pare-static-mcp` change**. Classes come from the tool result / `capture_store`.
- Tool names are **worker-prefixed**: `static_grep_smali`, `static_list_methods`, `static_decompile_method`, `frida_java_hook`, `frida_read_hook_events`. Centralize in constants.
- The commit-tool arg is **`cls`** (confirmed in `pare-static-mcp` `contract.py`) — do not use `class`.
- Candidate class names come from **`L…;` type tokens scanned across the whole row**, filtered to those whose *simple name contains the search pattern* — NOT the row's `class` column (which is the enclosing/dispatcher class).
- Real capture ground truth (use as the canonical fixture): a `grep OMTG_DATAST_001_SQLite` row looks like
  `{"class":"Lsg/vp/owasp_mobile/OMTG_Android/MyActivity;","method":"OMTG_DATAST_001_SQLite_Encrypted","insn":"const-class v0, Lsg/vp/owasp_mobile/OMTG_Android/OMTG_DATAST_001_SQLite_Encrypted;","match":"OMTG_DATAST"}`
  and the correct candidate set for that grep is exactly `{sg.vp.owasp_mobile.OMTG_Android.OMTG_DATAST_001_SQLite_Encrypted, sg.vp.owasp_mobile.OMTG_Android.OMTG_DATAST_001_SQLite_Not_Encrypted}`.
- Run tests with `.venv/bin/python -m pytest`.

---

### Task 1: `RepeatGuard.tripped()`

**Files:**
- Modify: `pare/repeat_guard.py`
- Test: `tests/test_repeat_guard.py`

**Interfaces:**
- Produces: `RepeatGuard.tripped(name: str, arguments: object) -> bool` — `True` exactly once per signature per guard instance, the first time that signature is hard-blocked (`should_run` would return `False`). Subsequent calls for the same signature return `False`.

- [ ] **Step 1: Write the failing test** (append to `tests/test_repeat_guard.py`):

```python
def test_tripped_fires_once_per_signature():
    g = RepeatGuard(soft_after=1, hard_after=3, call_ceiling=5)
    for _ in range(3):
        g.record("static_grep_smali", {"pattern": "X"}, "0 matches")
    # now hard-blocked
    assert g.should_run("static_grep_smali", {"pattern": "X"}) is False
    assert g.tripped("static_grep_smali", {"pattern": "X"}) is True   # first time
    assert g.tripped("static_grep_smali", {"pattern": "X"}) is False  # only once
    # a different, non-blocked signature never trips
    assert g.tripped("static_grep_smali", {"pattern": "Y"}) is False
```

- [ ] **Step 2: Run it, expect fail**

Run: `.venv/bin/python -m pytest tests/test_repeat_guard.py::test_tripped_fires_once_per_signature -v`
Expected: FAIL (`AttributeError: 'RepeatGuard' object has no attribute 'tripped'`).

- [ ] **Step 3: Implement** in `pare/repeat_guard.py` (add a `_handed_back: set[str]` initialized in `__init__`, and the method):

```python
    def tripped(self, name: str, arguments: object) -> bool:
        """True the first time this signature is hard-blocked and has not yet
        handed back this turn. Used to escalate a confirmed spin to the operator
        exactly once (not on every subsequent blocked call)."""
        if self.should_run(name, arguments):
            return False
        sig = _signature(name, arguments)
        if sig in self._handed_back:
            return False
        self._handed_back.add(sig)
        return True
```
(add `self._handed_back: set[str] = set()` in `__init__`.)

- [ ] **Step 4: Run tests, expect pass**

Run: `.venv/bin/python -m pytest tests/test_repeat_guard.py -q`
Expected: PASS (all, including existing).

- [ ] **Step 5: Commit**

```bash
git add pare/repeat_guard.py tests/test_repeat_guard.py
git commit -m "feat(guard): add RepeatGuard.tripped() for one-shot spin escalation

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_016FtMrYfHohBYZ551UQUJNe"
```

---

### Task 2: Candidate extraction + normalization (`pare/handback.py`)

**Files:**
- Create: `pare/handback.py`
- Test: `tests/test_handback.py`

**Interfaces:**
- Produces:
  - `normalize_class(name: str) -> str` — smali `Lsg/vp/Foo$Bar;` → dotted `sg.vp.Foo$Bar`; dotted passes through; strips a trailing `;` and leading `L`.
  - `candidate_classes(result: str, pattern: str, *, capture_store=None) -> set[str]` — parse a `static_grep_smali` result into distinct dotted class names drawn from `L…;` tokens anywhere in each row, keeping only those whose *simple name* contains `pattern`. Handles both a **raw JSON** result (has `rows`) and a **capture stub** (has `ref` → `capture_store.get(ref)["body"]`). Returns `set()` on unparseable input.

- [ ] **Step 1: Write failing tests** (`tests/test_handback.py`), using the real fixture:

```python
import json
from pare.handback import normalize_class, candidate_classes

PKG = "sg.vp.owasp_mobile.OMTG_Android"
LPKG = "Lsg/vp/owasp_mobile/OMTG_Android"

# Real capture rows: class column is the MyActivity dispatcher; the variant is the
# const-class type token in `insn`.
_ROWS = [
    {"class": f"{LPKG}/MyActivity;", "method": "OMTG_DATAST_001_SQLite_Encrypted",
     "insn": f"const-class v0, {LPKG}/OMTG_DATAST_001_SQLite_Encrypted;", "match": "OMTG_DATAST"},
    {"class": f"{LPKG}/MyActivity;", "method": "OMTG_DATAST_001_SQLite_Not_Encrypted",
     "insn": f"const-class v0, {LPKG}/OMTG_DATAST_001_SQLite_Not_Encrypted;", "match": "OMTG_DATAST"},
]
_GREP_RESULT = json.dumps({"summary": "grep_smali: 2 row(s)", "package": PKG.lower(), "rows": _ROWS})


def test_normalize_class_smali_to_dotted():
    assert normalize_class(f"{LPKG}/OMTG_DATAST_001_SQLite_Encrypted;") == f"{PKG}.OMTG_DATAST_001_SQLite_Encrypted"
    assert normalize_class(f"{PKG}.Foo") == f"{PKG}.Foo"  # dotted passes through


def test_candidate_classes_from_referenced_type_not_class_column():
    got = candidate_classes(_GREP_RESULT, "OMTG_DATAST_001_SQLite")
    assert got == {
        f"{PKG}.OMTG_DATAST_001_SQLite_Encrypted",
        f"{PKG}.OMTG_DATAST_001_SQLite_Not_Encrypted",
    }
    # MyActivity (the class column / dispatcher) must NOT be a candidate
    assert not any("MyActivity" in c for c in got)


def test_candidate_classes_broad_instruction_grep_yields_no_named_variants():
    # a grep whose matches don't reference classes named like the pattern
    rows = [{"class": f"{LPKG}/Foo;", "method": "m",
             "insn": "invoke-virtual v0, Landroid/database/sqlite/SQLiteDatabase;->rawQuery", "match": "SQLiteDatabase"}]
    res = json.dumps({"rows": rows})
    assert candidate_classes(res, "SQLiteDatabase") == set()  # no class *named* SQLiteDatabase


def test_candidate_classes_reads_capture_stub_when_ref_present():
    class _Store:
        def get(self, ref): return {"body": _GREP_RESULT}
    stub = json.dumps({"summary": "grep_smali: 2 row(s)", "captured": {"ref": "abc"}, "hint": "read_capture"})
    got = candidate_classes(stub, "OMTG_DATAST_001_SQLite", capture_store=_Store())
    assert f"{PKG}.OMTG_DATAST_001_SQLite_Encrypted" in got
```

- [ ] **Step 2: Run, expect fail** (`ModuleNotFoundError: pare.handback`).

Run: `.venv/bin/python -m pytest tests/test_handback.py -q`

- [ ] **Step 3: Implement `pare/handback.py`:**

```python
"""Pure helpers for operator-handback checkpoints (see the 2026-07-18 spec).

No agent_core / worker changes: candidate classes are parsed from a grep result
(or its capture body), scanning L...; type tokens across the whole row so a
variant is found whether it is the enclosing class or a referenced type."""
from __future__ import annotations

import json
import re

_LTOKEN = re.compile(r"L[\w/$]+;")

# Worker-prefixed tool names (agent_core prefixes by worker). Any new class-scoped
# dig-in / instrumentation tool that could commit to the wrong class goes here.
COMMIT_TOOLS = frozenset({"static_list_methods", "static_decompile_method", "frida_java_hook"})
NAME_SEARCH_TOOLS = frozenset({"static_grep_smali"})
POLL_TOOLS = frozenset({"frida_read_hook_events", "list_sessions"})


def normalize_class(name: str) -> str:
    """smali `Lsg/vp/Foo$Bar;` -> dotted `sg.vp.Foo$Bar`; dotted passes through."""
    if not name:
        return name
    n = name.strip()
    if n.startswith("L") and n.endswith(";") and "/" in n:
        n = n[1:-1].replace("/", ".")
    return n


def _simple_name(dotted: str) -> str:
    return dotted.rsplit(".", 1)[-1]


def _rows_from(result: str, capture_store) -> list:
    try:
        d = json.loads(result)
    except (TypeError, ValueError):
        return []
    if isinstance(d, dict) and isinstance(d.get("rows"), list):
        return d["rows"]
    ref = None
    if isinstance(d, dict):
        ref = (d.get("captured") or {}).get("ref") or d.get("ref")
    if ref and capture_store is not None:
        rec = capture_store.get(ref)
        if rec and rec.get("body"):
            try:
                inner = json.loads(rec["body"])
                if isinstance(inner, dict) and isinstance(inner.get("rows"), list):
                    return inner["rows"]
            except (TypeError, ValueError):
                return []
    return []


def candidate_classes(result: str, pattern: str, *, capture_store=None) -> set[str]:
    """Distinct dotted class names referenced in a grep result whose simple name
    contains `pattern`. Scans L...; tokens across each row (class/insn/match)."""
    out: set[str] = set()
    for row in _rows_from(result, capture_store):
        blob = json.dumps(row) if not isinstance(row, str) else row
        for tok in _LTOKEN.findall(blob):
            dotted = normalize_class(tok)
            if pattern in _simple_name(dotted):
                out.add(dotted)
    return out
```

- [ ] **Step 4: Run, expect pass.**

Run: `.venv/bin/python -m pytest tests/test_handback.py -q`

- [ ] **Step 5: Commit.**

```bash
git add pare/handback.py tests/test_handback.py
git commit -m "feat(handback): candidate-class extraction from grep results

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_016FtMrYfHohBYZ551UQUJNe"
```

---

### Task 3: Near-duplicate gate + question builders

**Files:**
- Modify: `pare/handback.py`
- Test: `tests/test_handback.py`

**Interfaces:**
- Produces:
  - `near_duplicate(candidates: set[str], pattern: str) -> bool` — `True` iff ≥2 candidates AND they are variants of each other: every candidate's simple name shares a common stem that (a) contains `pattern` and (b) is a large fraction (≥ 0.6) of each simple name. `grep OMTG_DATAST_001_SQLite`→2 variants arms; `grep User`→{UserManager,UserActivity,UserRepository} does not.
  - `disambig_question(cls: str, candidates: set[str]) -> str`
  - `spin_question(name: str, arguments: dict, repeats: int, last_result: str, candidates: set[str]) -> str`

- [ ] **Step 1: Failing tests** (append to `tests/test_handback.py`):

```python
from pare.handback import near_duplicate, disambig_question, spin_question

def test_near_duplicate_arms_on_omtg_variants():
    cands = {f"{PKG}.OMTG_DATAST_001_SQLite_Encrypted", f"{PKG}.OMTG_DATAST_001_SQLite_Not_Encrypted"}
    assert near_duplicate(cands, "OMTG_DATAST_001_SQLite") is True

def test_near_duplicate_does_not_arm_on_unrelated_shared_token():
    cands = {f"{PKG}.UserManager", f"{PKG}.UserActivity", f"{PKG}.UserRepository"}
    assert near_duplicate(cands, "User") is False

def test_near_duplicate_needs_at_least_two():
    assert near_duplicate({f"{PKG}.OMTG_DATAST_001_SQLite_Encrypted"}, "OMTG_DATAST_001_SQLite") is False

def test_questions_list_the_candidates():
    cands = {f"{PKG}.OMTG_DATAST_001_SQLite_Encrypted", f"{PKG}.OMTG_DATAST_001_SQLite_Not_Encrypted"}
    q = disambig_question(f"{PKG}.OMTG_DATAST_001_SQLite_Encrypted", cands)
    assert "OMTG_DATAST_001_SQLite_Not_Encrypted" in q and "?" in q
    s = spin_question("static_grep_smali", {"pattern": "X"}, 6, "0 matches", cands)
    assert "6" in s and "static_grep_smali" in s
```

- [ ] **Step 2: Run, expect fail.**

- [ ] **Step 3: Implement** (append to `pare/handback.py`):

```python
import os


def _common_prefix(names: list[str]) -> str:
    return os.path.commonprefix(names)


def near_duplicate(candidates: set[str], pattern: str) -> bool:
    """≥2 candidates that are variants of EACH OTHER: a shared stem that contains
    the pattern and is most of every simple name (guards against unrelated classes
    that merely share a short token, e.g. User*)."""
    simples = [_simple_name(c) for c in candidates]
    if len(set(simples)) < 2:
        return False
    stem = _common_prefix(simples)
    if pattern not in stem:
        return False
    return all(len(stem) >= 0.6 * len(s) for s in simples)


def disambig_question(cls: str, candidates: set[str]) -> str:
    listed = ", ".join(f"`{_simple_name(c)}`" for c in sorted(candidates))
    return (f"I'm about to dig into `{_simple_name(cls)}`, but the search referenced "
            f"{len(candidates)} near-identical classes: {listed}. Which is the target?")


def spin_question(name: str, arguments: dict, repeats: int, last_result: str,
                  candidates: set[str]) -> str:
    base = (f"I've re-run `{name}({_fmt_args(arguments)})` {repeats}× with the same "
            f"result (`{last_result[:80]}`) and I'm stuck.")
    if candidates:
        listed = ", ".join(f"`{_simple_name(c)}`" for c in sorted(candidates))
        base += f" That search referenced: {listed}."
    return base + " Which should I dig into, or how would you like me to proceed?"


def _fmt_args(arguments: dict) -> str:
    return ", ".join(f'{k}="{v}"' for k, v in (arguments or {}).items())
```

- [ ] **Step 4: Run, expect pass.** `.venv/bin/python -m pytest tests/test_handback.py -q`

- [ ] **Step 5: Commit.**

```bash
git add pare/handback.py tests/test_handback.py
git commit -m "feat(handback): near-duplicate gate + operator question builders

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_016FtMrYfHohBYZ551UQUJNe"
```

---

### Task 4: Wire both triggers into `handle_chat`

**Files:**
- Modify: `pare/agent.py`
- Test: `tests/test_handle_chat.py`

**Interfaces:**
- Consumes: `RepeatGuard.tripped` (Task 1); `pare/handback.py` (Tasks 2–3).
- Adds: `self._disambig_resolved: dict[str, set[frozenset[str]]]` (per-channel, like `self.last_usage`), created lazily.

- [ ] **Step 1: Failing integration tests** (append to `tests/test_handle_chat.py`). These assert: spin ends the turn well-formed; ambiguous commit is not dispatched; unambiguous commit runs; poll spin does not hand back.

```python
from pare.handback import PKG  # noqa  -- if you export it; else inline the strings
```
(Actually inline the class strings — do not add exports. Use the four scenarios below.)

```python
@pytest.mark.asyncio
async def test_spin_hands_back_wellformed(monkeypatch):
    agent = _make_agent(mode="on")
    call = ToolCall(id="t1", name="static_grep_smali", arguments={"pattern": "X"})
    agent.inference.complete = AsyncMock(return_value=CompletionResult(
        type="tool_calls", tool_calls=[call], usage=None))
    agent.tool_executor.run = AsyncMock(return_value='{"rows": []}')
    ctx = _ctx()
    out = [m async for m in agent.handle_chat(_msg("hi"), ctx)]
    assert isinstance(out[-1], ResponseMessage) and "stuck" in out[-1].text.lower()
    # message list well-formed: every assistant tool_calls id has a following tool result
    msgs = ctx.conversation.get_messages_for_api(system_prompt="S")
    _assert_toolcalls_paired(msgs)

@pytest.mark.asyncio
async def test_ambiguous_commit_not_dispatched():
    agent = _make_agent(mode="on")
    # a prior grep result is already in play via a stubbed capture; model commits to a variant
    grep = ToolCall(id="g", name="static_grep_smali", arguments={"pattern": "OMTG_DATAST_001_SQLite"})
    commit = ToolCall(id="c", name="static_decompile_method",
                      arguments={"cls": "sg.vp.owasp_mobile.OMTG_Android.OMTG_DATAST_001_SQLite_Encrypted"})
    # first inference: grep; second: commit
    agent.inference.complete = AsyncMock(side_effect=[
        CompletionResult(type="tool_calls", tool_calls=[commit], usage=None),
    ])
    agent.inference.stream = MagicMock(return_value=_Stream([[grep]]))
    agent.tool_executor.run = AsyncMock(return_value=_GREP_RESULT_2VARIANTS)  # module-level fixture
    ctx = _ctx()
    out = [m async for m in agent.handle_chat(_msg("hi"), ctx)]
    # the commit (decompile) tool must NOT have been dispatched
    dispatched = [c.args[0] for c in agent.tool_executor.run.await_args_list]
    assert "static_decompile_method" not in dispatched
    assert isinstance(out[-1], ResponseMessage) and "Not_Encrypted" in out[-1].text
```
(Provide `_msg`, `_assert_toolcalls_paired`, and `_GREP_RESULT_2VARIANTS` as small test helpers; `_assert_toolcalls_paired` walks messages and asserts each `assistant` with `tool_calls` is followed by a `tool` message per id.)

- [ ] **Step 2: Run, expect fail.**

- [ ] **Step 3: Implement in `pare/agent.py`.** Add the import and, in `handle_chat`, replace the per-`tc` body with the spec's control flow. Key points: build `name_searches` (dict pattern→candidate set) per turn; check Trigger 2 **before** `ToolProgressMessage`; settle all batch ids on handback; use `self._disambig_resolved`.

```python
from pare.handback import (
    COMMIT_TOOLS, NAME_SEARCH_TOOLS, POLL_TOOLS,
    candidate_classes, near_duplicate, normalize_class,
    disambig_question, spin_question,
)
```

Inside `handle_chat`, before the round loop:
```python
guard = RepeatGuard()
name_searches: dict[str, set[str]] = {}   # pattern -> candidate set (this turn)
resolved = self._disambig_resolved.setdefault(ctx.channel_id, set())

def _settle_and_handback(question, done_ids):
    for tc in tool_calls:
        if tc.id not in done_ids:
            conv.add_tool_result(tc.id, "[handed back to operator — call not executed]")
    conv.add_assistant(question)
    return ResponseMessage(text=question)
```
(Define `self._disambig_resolved = {}` in `setup()`.)

Per-round loop body:
```python
done_ids = set()
for tc in tool_calls:
    if tc.name in COMMIT_TOOLS:
        cls = normalize_class(str((tc.arguments or {}).get("cls", "")))
        for pat, cands in name_searches.items():
            if cls in cands and near_duplicate(cands, pat) and frozenset(cands) not in resolved:
                q = disambig_question(cls, cands)
                resolved.add(frozenset(cands))
                yield _settle_and_handback(q, done_ids); return
    yield ToolProgressMessage(tool=tc.name, arguments=tc.arguments)
    if guard.should_run(tc.name, tc.arguments):
        result = await self.tool_executor.run(tc.name, tc.arguments, ctx)
        result = guard.record(tc.name, tc.arguments, result)
        if tc.name in NAME_SEARCH_TOOLS:
            pat = str((tc.arguments or {}).get("pattern", ""))
            if pat:
                name_searches[pat] = candidate_classes(result, pat, capture_store=self.capture_store)
    else:
        if tc.name not in POLL_TOOLS and guard.tripped(tc.name, tc.arguments):
            pat = str((tc.arguments or {}).get("pattern", ""))
            q = spin_question(tc.name, tc.arguments,
                              _repeat_count(guard, tc), _last_result(guard, tc),
                              name_searches.get(pat, set()))
            yield _settle_and_handback(q, done_ids); return
        result = guard.blocked(tc.name, tc.arguments)
    conv.add_tool_result(tc.id, result); done_ids.add(tc.id)
```
For `_repeat_count`/`_last_result`: expose the guard's entry (`RepeatGuard.entry(name, args) -> _Entry | None`) or pass `guard`'s `total`/`result`; simplest is a tiny accessor added to the guard returning `(total, result)`.

- [ ] **Step 4: Run tests, expect pass.**

Run: `.venv/bin/python -m pytest tests/test_handle_chat.py -q`

- [ ] **Step 5: Full suite.**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS (no regressions).

- [ ] **Step 6: Commit.**

```bash
git add pare/agent.py pare/repeat_guard.py tests/test_handle_chat.py
git commit -m "feat: operator-handback checkpoints in handle_chat (spin + disambiguation)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_016FtMrYfHohBYZ551UQUJNe"
```

---

### Task 5: Cross-turn resolution + poll-exemption + schema-drift guard tests

**Files:**
- Test: `tests/test_handle_chat.py`, `tests/test_handback_schema.py`

**Interfaces:** none new — hardening tests for the must-fixes.

- [ ] **Step 1: Answer-turn resume test** — after a disambiguation handback, a follow-up turn that re-greps then re-commits to a class in the same set runs the commit (no second handback). Reuse the same agent/ctx across two `handle_chat` calls; assert `static_decompile_method` IS dispatched on the second turn.

```python
@pytest.mark.asyncio
async def test_answer_turn_does_not_reblock():
    agent = _make_agent(mode="on")
    # turn 1: grep -> commit -> handback (marks the set resolved on self._disambig_resolved)
    # turn 2: grep -> commit -> should DISPATCH the commit now
    ... (drive two turns; assert commit dispatched on turn 2)
```

- [ ] **Step 2: Poll-exemption test** — `frida_read_hook_events` returning identical empty results hits the result-aware block but must get only the nudge, never a handback (turn does NOT end early with "stuck").

- [ ] **Step 3: Schema-drift guard** (`tests/test_handback_schema.py`) — build the agent's real `tool_executor.schemas()` (or the static/frida contracts) and assert every name in `COMMIT_TOOLS` exists and declares a `cls` parameter; assert `NAME_SEARCH_TOOLS`/`POLL_TOOLS` names exist. Fails loudly if a rename/prefix drift silently disarms a trigger.

- [ ] **Step 4: Run, expect fail, implement any accessor gaps, expect pass.**

Run: `.venv/bin/python -m pytest tests/test_handle_chat.py tests/test_handback_schema.py -q`

- [ ] **Step 5: Full suite + commit.**

```bash
.venv/bin/python -m pytest -q
git add tests/test_handle_chat.py tests/test_handback_schema.py
git commit -m "test: cross-turn resume, poll exemption, schema-drift guard for handback

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_016FtMrYfHohBYZ551UQUJNe"
```

- [ ] **Step 6: Behavioral check (manual, for operator).** Re-run the OMTG-SQLite prompt: PARE should hand back with the `_Encrypted` / `_Not_Encrypted` choice instead of spinning or silently picking `_Encrypted`; after "the non-encrypted one," it proceeds without re-prompting. Note results in the PR.

---

## Self-Review

**Spec coverage:**
- §0 settle-pending → Task 4 `_settle_and_handback` + `test_spin_hands_back_wellformed` (`_assert_toolcalls_paired`).
- §1 spin trigger + poll exemption → Task 1 (`tripped`), Task 4 wiring, Task 5 poll-exemption test.
- §2 commit-time disambiguation, referenced-type extraction, raw-result-not-ref → Task 2 (`candidate_classes`), Task 4 Trigger-2 block.
- Near-duplicate gate → Task 3 (`near_duplicate`) + arms/does-not-arm tests.
- Cross-turn resolution (no ping-pong) → Task 4 `self._disambig_resolved`, Task 5 answer-turn test.
- Prefixed tool names / `cls` arg / schema-drift → constants in Task 2, Task 5 schema test.
- `frida_java_hook` in commit set → `COMMIT_TOOLS` (Task 2).

**Placeholder scan:** Task 4/5 test bodies note "drive two turns"/"..." for the multi-turn scenarios — these are described precisely (reuse agent+ctx across two `handle_chat` calls; assert dispatch on turn 2) but not fully spelled as copy-paste code because they depend on the exact helper shape chosen in Task 4 Step 3. Acceptable: the assertions and setup are unambiguous. All pure-helper tests are complete code.

**Type consistency:** `candidate_classes`/`near_duplicate`/`normalize_class` signatures match between Tasks 2–3 and their use in Task 4. `COMMIT_TOOLS`/`NAME_SEARCH_TOOLS`/`POLL_TOOLS` defined once in `handback.py`, imported in `agent.py`.
