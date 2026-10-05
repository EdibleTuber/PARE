# Artifact wiring — P1 (wire vocabulary) — implementation plan

**Date:** 2026-10-02
**Spec:** PARE `docs/superpowers/specs/2026-09-12-artifact-wiring-design.md` — §7a (P1), §3 (A3, A4, A5), §4, §6 step 5, §7
**Parent spec (context only):** PARE `docs/superpowers/specs/2026-09-06-arcticbase-artifacts-design.md` — D6/D7, §5.1
**Status:** planned, **not started**. Runs in a FRESH session via `superpowers:executing-plans`; the
ledger lives at `PARE/.superpowers/sdd/2026-10-02-artifact-wiring-p1/progress.md`.
**Code is in this plan on purpose.** §7a classifies P1 as *"mechanical, fully specified,
single-file edits: include code in the plan."* §5's no-implementation-code rule binds P2
(`open_artifact`), not this plan.

Nothing in this plan touches `pare-hardware-mcp`, PARE's code, or the bench drive. The hardware
console-capture plan stays halted at its Task 1 gate until P1–P4 have landed.

## What P1 is, and is not

P1 (this plan), in the spec's own order (§7a, §7):

1. The descriptor **field set** (7 fields, wire order) stated in both packages — pure
   declaration, both repos (§4, §7 step 1).
2. The two **reserved-argument names** stated in both packages — pure declaration, both repos
   (A4, §6 step 3: *"wire vocabulary and get the same treatment as every other shared constant"*).
3. The **bidirectional cross-package guard** (§7 step 2), named so the existing CI filters
   collect it.
4. agent_core's **`validate_descriptor`**: new keyword signature (`spec`, `tool`, `slug`), the
   7-field requirement, the three new field grammars, containment against
   `{artifact_root}/{slug}`, and the drive-id comparison (§6 step 5, §4, A3, A5).

Not P1: `open_artifact` (P2), the `call_tool` dispatch wiring (P3), tags/pins/releases (P4), the
`WorkerSpec.artifact_host` field and host reconciliation (A2/P3), the probe rig (§8).

## Repos, environments, commands

| Repo | Path | Venv (verified 2026-10-02) | Test command |
|---|---|---|---|
| pare-worker-kit | `/mnt/secondary/projects/pare-worker-kit` | **none** — created in Task 1 with `python3.12` | `.venv/bin/python -m pytest` |
| agent_core | `/mnt/secondary/projects/agent_core` | `.venv` exists, Python 3.12; `pare_worker_kit` NOT installed | `.venv/bin/python -m pytest` |

- **Never `uv run`.**
- The kit venv is 3.12, not 3.10, because the cross-package guard imports agent_core, which
  declares `requires-python >=3.12`. The 3.10 leg is covered by the kit's CI matrix, not locally
  (ruling R7).
- **A skip is not a pass.** The guards use `pytest.importorskip`; both repos' CI already has a
  "guards must run, not skip" step — kit: 3.12-only `cross-package` job,
  `python -m pytest -k "agrees_with_agent_core"`; agent_core:
  `python -m pytest -k "agrees_with_the_worker_kit"` — and both CI workflows install the sibling
  from git **main**, not from a release. Locally the sibling is installed from the local checkout
  (Task 3), and every local verification of the guards must show **PASSED, never SKIPPED**.

## Global constraints

1. **No version bumps, no tags, no pin changes anywhere in P1.** The kit stays `0.2.0`
   (pyproject *and* `__init__.py:18`, which `stamp_version` feeds into `serverInfo`) and
   agent_core stays `1.11.1`. Releases are P4.
2. **The kit keeps exactly one dependency (`mcp`).** agent_core is a test fixture in the kit's
   venv, never a pyproject dependency.
3. **Merge order is load-bearing** (CI installs the sibling from main): both declaration commits
   (Tasks 1–2) must be on `origin/main` before the guard commits (Task 3) merge. Task 4 needs only
   the declarations on main.
4. **TDD:** every task shows the red before the green; the red output goes in the ledger, and the
   red must be the failure the test exists to discriminate — not a collection error that could
   pass for the wrong reason (spec §8; the one deliberate exception is Task 1/2's import red,
   which is itself the discrimination: the name does not exist yet).
5. **House style:** long explanatory docstrings on wire constants; wire literals pinned as
   literals in a LOCAL test in each suite (the importorskip guard skips on every routine run);
   relationship tests where the spec is a relationship.
6. **Provenance:** every file:line fact in this plan was read from landed source on 2026-10-02;
   the preflight re-verifies them, and the executor re-reads at use time. Nothing is typed from
   memory — in particular the bench drive's sentinel UUID, which P1 never touches (test fixtures
   use a made-up canonical UUID).

## Rulings (plan-level decisions; recorded here, not made in secret)

- **R1 — `validate_descriptor` gains a third keyword, `slug`.** A3 mandates `spec` replacing
  `worker`, and §6 step 5 mandates containment against `{spec.artifact_root}/{slug}` inside this
  function — but §6's call expression (`validate_descriptor(payload, spec=spec, tool=tool)`)
  carries no slug, and the slug is not in the payload (the worker's 7 fields do not include it).
  The daemon validated the slug, injected it as the reserved slug argument, and holds it at
  dispatch, so it is a required keyword:
  `validate_descriptor(payload, *, spec: WorkerSpec, tool: str, slug: str)`. P3's dispatch passes
  it.
- **R2 — the agent_core declaration adds `ARTIFACT_DESCRIPTOR_FIELDS` ALONGSIDE the existing
  `_REQUIRED`; Task 4 switches the validator to the new constant and deletes `_REQUIRED`.**
  A declaration PR that rewrote `_REQUIRED` in place would break agent_core's own suite (and CI)
  until the validator changed; §7's ordering exists so that every step is CI-green. Two constants
  coexist for the span of one task.
- **R3 — the reserved-argument wire names.** The spec fixes the slug argument as
  `project_slug` (its only stated instance, §6 step 3's example); the drive argument's value is
  chosen here, mirroring the descriptor field `drive_id` and open_artifact's `expect_drive_id`
  (P2, §5): **`expected_drive_id`**. Constant names on both sides: `RESERVED_SLUG_ARG`,
  `RESERVED_DRIVE_ID_ARG`.
- **R4 — grammars for the three new fields.** §4: *"required without a grammar is not a
  contract."*
  - `hashed_at`: strict RFC 3339 UTC, `Z` form, optional fractional seconds —
    `\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z\Z`. FORMAT, not calendar: a non-UTC offset is
    refused (the field IS UTC), the lowercase `z` is refused (the producer, open_artifact, emits
    uppercase `Z`; a second accepted spelling is comparison surface the operator's eye cannot
    audit), and whether the timestamp is true is a custody record the daemon cannot verify.
  - `media_type`: IANA type/subtype only — no parameters, no wildcards —
    `\A[A-Za-z0-9!#$&^_.+-]{1,126}/[A-Za-z0-9!#$&^_.+-]{1,126}\Z` (the RFC 2045 token set minus
    the characters no registered type uses; the 126 cap is the RFC token limit; case-insensitive
    by RFC 2046, so `TEXT/PLAIN` passes).
  - `drive_id`: canonical lowercase UUID
    `\A[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z` — the grammar of the
    sentinel file it is read from — **plus** the explicit `_CONTROL_RE` check §4 names, because
    the value is printed into the mismatch message an operator reads.
- **R5 — the validator fails closed on a missing spec field.** `spec.artifact_root is None` →
  refuse; `spec.artifact_drive_id is None` → refuse. The dispatch-time refusals are P3 (§6 step
  2); the validator-level refusals make the function safe standalone and keep "no root, no
  artifact" true at every layer.
- **R6 — the guard's local RED uses a stale snapshot.** §7 already established the failure mode:
  a guard written like its neighbours accesses the attribute directly, so a sibling that lacks
  the constant fails with `AttributeError` — a real failure, and the guard's teeth. Task 3
  installs the last TAGGED snapshot of the sibling (`git archive` of the tag) to produce the red,
  then the local checkout for the green. P1 does not bump versions, so the reinstall needs
  `pip uninstall` first (pip would otherwise report "already satisfied" for 0.2.0 → 0.2.0 /
  1.11.1 → 1.11.1).
- **R7 — the kit's local venv is Python 3.12.** See the repos table.

## Preflight (run in the execution session, before Task 1)

Record each result in the ledger. **STOP and re-read the spec if any check fails** — a failed
check means the world moved while this plan was written.

1. `git fetch origin` in PARE, the kit, and agent_core. All three on `main`, no unpushed commits
   (`git log --oneline origin/main..HEAD` empty), working trees clean — record anything dirty and
   leave it alone.
2. Newest tags: kit `v0.2.0` (= `973d7de`), agent_core `v1.11.1` (= `a5a6129`) —
   `git -C <repo> tag --sort=-v:refname | head -1` in each. A newer tag means a release landed
   while this plan was written: stop.
3. No non-test consumers of the function being changed:
   `grep -rn "validate_descriptor\|_REQUIRED" /mnt/secondary/projects/agent_core --include="*.py" | grep -v ".venv" | grep -v "/tests/"`
   Expected: the `types.py` docstring mention plus `artifacts.py` only (lines 39/46/144 at plan
   writing). §1 of the spec established this window; it closes when Task 4 lands.
4. No consumers in the other repos:
   `grep -rn "validate_descriptor" /mnt/secondary/projects/PARE /mnt/secondary/projects/pare-hardware-mcp --include="*.py" | grep -v .venv`
   Expected: nothing.
5. CI filters unchanged: `grep -c "agrees_with_agent_core" /mnt/secondary/projects/pare-worker-kit/.github/workflows/test.yml`
   and `grep -c "agrees_with_the_worker_kit" /mnt/secondary/projects/agent_core/.github/workflows/test.yml`
   — both ≥ 1.
6. WorkerSpec still carries both fields:
   `grep -n "artifact_root: \|artifact_drive_id: " /mnt/secondary/projects/agent_core/agent_core/workers/types.py`
   — two hits (112 and 135 at plan writing).
7. `python3.12 --version` works (kit venv, Task 1).
8. Record the version field of each pyproject (kit `0.2.0`, agent_core `1.11.1`) and the kit's
   `__init__.py` `__version__`; acceptance asserts all three are unchanged.
9. The agent_core venv lacks the kit
   (`.venv/bin/python -c "import pare_worker_kit"` → `ModuleNotFoundError`) and the kit has no
   venv (`ls /mnt/secondary/projects/pare-worker-kit/.venv` → not found).

## File map

| File | Task | Change |
|---|---|---|
| kit `src/pare_worker_kit/artifacts.py` | 1 | +3 constants (field set, 2 reserved args) |
| kit `src/pare_worker_kit/__init__.py` | 1 | export the 3 constants |
| kit `tests/test_artifacts.py` | 1, 3 | local pins (T1); cross-package guard (T3) |
| agent_core `agent_core/workers/artifacts.py` | 2, 4 | +3 constants (T2); validator rewrite (T4) |
| agent_core `agent_core/workers/types.py` | 4 | docstring surgery on the two artifact fields (no behaviour) |
| agent_core `tests/workers/test_artifacts.py` | 2, 3, 4 | local pins (T2); guard (T3); descriptor-section rewrite (T4) |

No other file in either repo changes. PARE gets exactly one commit in the whole plan: this plan
file (docs only).

---

## Task 1 — kit: the wire vocabulary as pure declarations

**Files:** kit `src/pare_worker_kit/artifacts.py`, `src/pare_worker_kit/__init__.py`,
`tests/test_artifacts.py`.
**Spec:** §4 (the seven fields, all required), A4, §7 step 1, §7a.

**Step 1 — the venv (first run only).**

```
cd /mnt/secondary/projects/pare-worker-kit
python3.12 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest -q
```

Baseline must be green before the change (the produces/slug guards SKIP — expected: agent_core is
not in this venv; they run in the CI cross-package job and locally in Task 3). If `.venv` already
exists and is 3.12.x, skip creation and record it.

**Step 2 — RED.** In `tests/test_artifacts.py`:

Replace the top import (lines 3–4):

```python
from pare_worker_kit import (ARTIFACT_DESCRIPTOR_FIELDS, PRODUCES_ARTIFACT,
                             PRODUCES_META_KEY, PRODUCES_RESULT,
                             RESERVED_DRIVE_ID_ARG, RESERVED_SLUG_ARG,
                             VALID_PRODUCES)
```

Immediately after `test_result_is_the_default_value` (line 18), before
`test_it_agrees_with_agent_cores`:

```python
def test_the_descriptor_field_set_is_the_wire_contract():
    """A LOCAL pin, for the reason the slug pin below gives: the importorskip
    guard that follows SKIPS on every routine run of this suite and on every
    job of this package's own CI, so a change to the field set could ship
    silently. This is the wire contract crossing a machine boundary, stated
    independently in each package; a change to it here must fail a test in
    THIS suite.

    Pinned as a literal tuple: the order is the wire order. The
    no-duplicates check is a relationship because seven distinct names is
    the property, not the number itself.
    """
    assert ARTIFACT_DESCRIPTOR_FIELDS == ("host", "path", "size", "sha256",
                                          "hashed_at", "media_type",
                                          "drive_id")
    assert len(set(ARTIFACT_DESCRIPTOR_FIELDS)) == len(
        ARTIFACT_DESCRIPTOR_FIELDS)


def test_the_reserved_argument_names_are_wire_literals_and_identifiers():
    """The daemon injects these as tool arguments and a worker handler names
    a parameter after each value, so each must be wire vocabulary AND a legal
    Python identifier. Pinned as literals: changing one is a wire-breaking
    change. The distinctness is pinned because a collision would mean the
    daemon overwrites the wrong input silently."""
    assert RESERVED_SLUG_ARG == "project_slug"
    assert RESERVED_DRIVE_ID_ARG == "expected_drive_id"
    assert RESERVED_SLUG_ARG.isidentifier()
    assert RESERVED_DRIVE_ID_ARG.isidentifier()
    assert RESERVED_SLUG_ARG != RESERVED_DRIVE_ID_ARG
```

Run:

```
.venv/bin/python -m pytest tests/test_artifacts.py -q
```

Expected RED: a collection error — `ImportError: cannot import name
'ARTIFACT_DESCRIPTOR_FIELDS' from 'pare_worker_kit'` — because the names do not exist yet. That
is the discrimination (constraint 4's stated exception). Record the output in the ledger.

**Step 3 — GREEN.** In `src/pare_worker_kit/artifacts.py`, after the `VALID_PRODUCES` docstring
(ends line 26) and before `_SLUG_MAX` (line 29), insert:

```python
ARTIFACT_DESCRIPTOR_FIELDS = ("host", "path", "size", "sha256", "hashed_at",
                              "media_type", "drive_id")
"""The seven fields a `produces: artifact` tool returns, in wire order.

Stated here AND in agent_core, with a guard test on each side, for the same
reason as PRODUCES_META_KEY above: the two packages are separately installed
and never share a Python environment, so a shared import could not guarantee
agreement across the wire any better than two statements can. The worker
builds exactly these fields (open_artifact, which lands after this) and the
daemon's validate_descriptor requires exactly these; a field present on one
side and not the other is a descriptor that validates on one machine and is
refused on the other, with no useful error anywhere. All seven are required;
none is optional. The eighth field of the object that gets published,
produced_by, is added by the daemon AFTER validation and never travels.
"""

RESERVED_SLUG_ARG = "project_slug"
"""The tool-argument name the daemon injects with the project's ArcticBase
slug. RESERVED_DRIVE_ID_ARG is the same arrangement for the drive id the
artifact must land on; see it below.

Reserved means the daemon supplies the value: injected at the dispatch
chokepoint, overwriting whatever the model supplied, so the model never sees
it as an input it may choose. A worker names its tool-handler parameter after
this value so the injection lands where the handler expects it -- which is
why the value must be a legal Python identifier as well as wire vocabulary.
Stated here AND in agent_core, with a guard test on each side. Changing
either value is a wire-breaking change.
"""

RESERVED_DRIVE_ID_ARG = "expected_drive_id"
"""The same arrangement as RESERVED_SLUG_ARG, for the drive id. The worker
passes the injected value through to open_artifact as expect_drive_id; a
descriptor whose drive_id differs is refused.
"""
```

In `__init__.py`, replace the artifacts import (lines 6–8) and `__all__` (lines 13–17):

```python
from pare_worker_kit.artifacts import (ARTIFACT_DESCRIPTOR_FIELDS,
                                        PRODUCES_ARTIFACT, PRODUCES_META_KEY,
                                        PRODUCES_RESULT, RESERVED_DRIVE_ID_ARG,
                                        RESERVED_SLUG_ARG, VALID_PRODUCES,
                                        ArtifactPathError, artifact_path)
```

```python
__all__ = ["ARTIFACT_DESCRIPTOR_FIELDS", "PRODUCES_ARTIFACT",
           "PRODUCES_META_KEY", "PRODUCES_RESULT", "RESERVED_DRIVE_ID_ARG",
           "RESERVED_SLUG_ARG", "VALID_PRODUCES", "ArtifactPathError",
           "artifact_path", "RISK_TIER_META_KEY", "VALID_RISK_TIERS",
           "run_worker", "resolve_bind_address", "stamp_version",
           "WorkerServeError"]
```

Run the full suite:

```
.venv/bin/python -m pytest -q
```

Expected: all green (the two new tests pass; the importorskip guards still skip — agent_core is
still not in the venv). Record.

**Step 4 — commit and land.**

`feat(artifacts): state the descriptor field set and reserved argument names`

Kit flow: feature branch + PR (the repo's recent flow), merged to `origin/main`. Before Task 3
starts, verify the commit is on `origin/main` (`git fetch && git log origin/main -1 --oneline`).

---

## Task 2 — agent_core: the same vocabulary as pure declarations

**Files:** agent_core `agent_core/workers/artifacts.py`, `tests/workers/test_artifacts.py`.
**Spec:** same as Task 1. **Order:** after Task 1's commit is on `origin/main` (§7 step 1; either
declaration may go first, but the guard task needs both).

**Step 1 — RED.** In `tests/workers/test_artifacts.py`, replace the top import (lines 5–6):

```python
from agent_core.workers.artifacts import (ARTIFACT_DESCRIPTOR_FIELDS,
                                          PRODUCES_ARTIFACT,
                                          PRODUCES_META_KEY,
                                          PRODUCES_RESULT,
                                          RESERVED_DRIVE_ID_ARG,
                                          RESERVED_SLUG_ARG,
                                          VALID_PRODUCES)
```

Immediately after `test_the_daemon_states_the_wire_constant_itself` (line 12), before
`test_it_agrees_with_the_worker_kit`:

```python
def test_the_daemon_states_the_descriptor_field_set_itself():
    """A LOCAL pin, the daemon-side half of the arrangement the meta-key test
    above states: the field set crosses a wire and is stated independently in
    each package, so a change to it here must fail a test in THIS suite even
    when the importorskip guard below skips."""
    assert ARTIFACT_DESCRIPTOR_FIELDS == ("host", "path", "size", "sha256",
                                          "hashed_at", "media_type",
                                          "drive_id")
    assert len(set(ARTIFACT_DESCRIPTOR_FIELDS)) == len(
        ARTIFACT_DESCRIPTOR_FIELDS)


def test_the_reserved_argument_names_are_stable_wire_literals():
    """Injected by the daemon at dispatch, named after by worker handlers:
    wire vocabulary AND Python identifiers. Pinned as literals; changing one
    is a wire-breaking change."""
    assert RESERVED_SLUG_ARG == "project_slug"
    assert RESERVED_DRIVE_ID_ARG == "expected_drive_id"
    assert RESERVED_SLUG_ARG.isidentifier()
    assert RESERVED_DRIVE_ID_ARG.isidentifier()
    assert RESERVED_SLUG_ARG != RESERVED_DRIVE_ID_ARG
```

Run:

```
cd /mnt/secondary/projects/agent_core
.venv/bin/python -m pytest tests/workers/test_artifacts.py -q
```

Expected RED: collection error — `ImportError: cannot import name
'ARTIFACT_DESCRIPTOR_FIELDS' from 'agent_core.workers.artifacts'`. Record.

**Step 2 — GREEN.** In `agent_core/workers/artifacts.py`, after `VALID_PRODUCES` (line 20) and
before `_SHA256_RE` (line 22), insert (daemon-perspective docstrings, otherwise the same
constants — the guard in Task 3 is what keeps the two statements byte-identical in value):

```python
ARTIFACT_DESCRIPTOR_FIELDS = ("host", "path", "size", "sha256", "hashed_at",
                              "media_type", "drive_id")
"""The seven fields a `produces: artifact` tool must return, in wire order.

Stated here AND in pare-worker-kit, with a guard test on each side, for the
same reason as PRODUCES_META_KEY above: the two packages are separately
installed and never share a Python environment. validate_descriptor requires
exactly these fields and pare_worker_kit's open_artifact builds exactly
these; a field present on one side and not the other is a descriptor that
validates on one machine and is refused on the other, with no useful error
anywhere. All seven are required; none is optional. The eighth field of the
object that gets published, produced_by, is added by the daemon AFTER
validation and never travels.
"""

RESERVED_SLUG_ARG = "project_slug"
"""The tool-argument name dispatch injects with the project's ArcticBase
slug. RESERVED_DRIVE_ID_ARG is the same arrangement for the drive id the
artifact must land on; see it below.

Reserved means the daemon supplies the value: injected at the dispatch
chokepoint, overwriting whatever the model supplied, so the model never sees
it as an input it may choose. A worker names its tool-handler parameter after
this value so the injection lands where the handler expects it -- which is
why the value must be a legal Python identifier as well as wire vocabulary.
Stated here AND in pare-worker-kit, with a guard test on each side. Changing
either value is a wire-breaking change.
"""

RESERVED_DRIVE_ID_ARG = "expected_drive_id"
"""The same arrangement as RESERVED_SLUG_ARG, for the drive id. A descriptor
whose drive_id differs from the injected value is refused.
"""
```

`_REQUIRED` (line 39) stays for now — ruling R2. No `__init__` change: agent_core's modules are
imported by path, the way the existing guards do it.

Run the full suite:

```
.venv/bin/python -m pytest -q
```

Expected: all green. Record.

**Step 3 — commit and land.**

`feat(artifacts): state the descriptor field set and reserved argument names`

agent_core ships to main: commit on `main`, push, verify `git log origin/main -1 --oneline`.
**Both declaration commits must be on `origin/main` before Task 3** (global constraint 3).

---

## Task 3 — the bidirectional guard

**Files:** kit `tests/test_artifacts.py`; agent_core `tests/workers/test_artifacts.py`.
**Spec:** §7 step 2 — *"Now the attribute exists on both sides, the guard compares two real
values, and it neither skips nor raises."*

The guard's teeth are demonstrated locally with a STALE snapshot of the sibling (ruling R6): the
last tagged release, which lacks the constants, must make the guard FAIL with
`AttributeError` — the exact failure mode §7 reasons about. Then the local checkout (with the
Task 1/2 commits) makes it PASS.

### Task 3K — kit side

**Step 1 — add the test.** In `tests/test_artifacts.py`, after
`test_the_slug_rule_agrees_with_agent_cores` (line 46), before the
`# Task 8: worker-side containment` comment (line 49):

```python
def test_the_descriptor_contract_agrees_with_agent_core():
    """The field set and the two reserved argument names are stated twice --
    once per package -- and this is what keeps the two statements the same,
    for the reason every guard in this file gives.

    Named so the CI filter (`-k agrees_with_agent_core`) collects it: the
    cross-package job fails if a guard matching that filter is skipped or if
    none is collected.

    The field set is compared as a TUPLE, not a set: the order is the wire
    order, and a reordering on one side is drift this guard exists to catch.
    """
    ac = pytest.importorskip(
        "agent_core.workers.artifacts",
        reason="agent_core is not installed here; the daemon-side half of "
               "this check runs in agent_core's own suite")
    assert ac.ARTIFACT_DESCRIPTOR_FIELDS == ARTIFACT_DESCRIPTOR_FIELDS
    assert ac.RESERVED_SLUG_ARG == RESERVED_SLUG_ARG
    assert ac.RESERVED_DRIVE_ID_ARG == RESERVED_DRIVE_ID_ARG
```

**Step 2 — baseline.** Full suite: the new guard SKIPS (agent_core not in the venv). Record —
the skip is the baseline, not a pass.

**Step 3 — RED against the stale sibling.**

```
rm -rf /tmp/opencode/ac-stale && mkdir -p /tmp/opencode/ac-stale
git -C /mnt/secondary/projects/agent_core archive v1.11.1 | tar -x -C /tmp/opencode/ac-stale
/mnt/secondary/projects/pare-worker-kit/.venv/bin/pip install /tmp/opencode/ac-stale
```

(First run pulls agent_core's dependency tree — trafilatura, markitdown[...]; allow a few
minutes.) Then:

```
cd /mnt/secondary/projects/pare-worker-kit
.venv/bin/python -m pytest tests/test_artifacts.py -v
```

Expected RED: `test_the_descriptor_contract_agrees_with_agent_core` FAILS with
`AttributeError: module 'agent_core.workers.artifacts' has no attribute
'ARTIFACT_DESCRIPTOR_FIELDS'`; the two older guards (produces, slug) PASS against the stale
sibling — which is what proves the failure discriminates THIS constant set, not the install.
Record.

**Step 4 — GREEN against the new sibling.**

```
/mnt/secondary/projects/pare-worker-kit/.venv/bin/pip uninstall -y agent_core
/mnt/secondary/projects/pare-worker-kit/.venv/bin/pip install /mnt/secondary/projects/agent_core
/mnt/secondary/projects/pare-worker-kit/.venv/bin/python -m pytest -q
/mnt/secondary/projects/pare-worker-kit/.venv/bin/python -m pytest -k agrees_with_agent_core -v
```

Expected: full suite green; `-k` run shows **3 passed, 0 skipped** (produces, slug, descriptor).
Record both outputs. (The uninstall is required: both sides are 0.2.0/1.11.1 and pip would
otherwise report "already satisfied" — ruling R6.)

**Step 5 — commit and land.**

`test(artifacts): guard the descriptor contract against agent_core`

Kit PR, merged to `origin/main`; verify.

### Task 3A — agent_core side (mirror)

**Step 1 — add the test.** In `tests/workers/test_artifacts.py`, after
`test_the_slug_rule_agrees_with_the_worker_kits` (line 187), before the
`# Fix round 2:` comment (line 190):

```python
def test_the_descriptor_contract_agrees_with_the_worker_kit():
    """The field set and the two reserved argument names are stated on both
    sides of the wire, and this is what keeps the two statements the same,
    for the reason the slug-rule guard above gives.

    Named so the CI filter (`-k agrees_with_the_worker_kit`) collects it: the
    cross-package step fails if a guard matching that filter is skipped or if
    none is collected.

    Compared as a tuple, not a set: the order is the wire order.
    """
    kit = pytest.importorskip(
        "pare_worker_kit.artifacts",
        reason="pare-worker-kit is not installed here; the worker-side half "
               "of this check runs in that package's own suite")
    assert kit.ARTIFACT_DESCRIPTOR_FIELDS == ARTIFACT_DESCRIPTOR_FIELDS
    assert kit.RESERVED_SLUG_ARG == RESERVED_SLUG_ARG
    assert kit.RESERVED_DRIVE_ID_ARG == RESERVED_DRIVE_ID_ARG
```

**Step 2 — baseline.** Full suite: the new guard SKIPS (preflight 9 verified the kit is not in
this venv). Record.

**Step 3 — RED against the stale sibling.**

```
rm -rf /tmp/opencode/kit-stale && mkdir -p /tmp/opencode/kit-stale
git -C /mnt/secondary/projects/pare-worker-kit archive v0.2.0 | tar -x -C /tmp/opencode/kit-stale
/mnt/secondary/projects/agent_core/.venv/bin/pip install /tmp/opencode/kit-stale
cd /mnt/secondary/projects/agent_core
.venv/bin/python -m pytest tests/workers/test_artifacts.py -v
```

(Kit is fast to install: its only dependency, `mcp`, is already present.) Expected RED:
`test_the_descriptor_contract_agrees_with_the_worker_kit` FAILS with `AttributeError: module
'pare_worker_kit.artifacts' has no attribute 'ARTIFACT_DESCRIPTOR_FIELDS'`; the two older guards
(produces, slug) PASS. Record.

**Step 4 — GREEN.**

```
/mnt/secondary/projects/agent_core/.venv/bin/pip uninstall -y pare-worker-kit
/mnt/secondary/projects/agent_core/.venv/bin/pip install /mnt/secondary/projects/pare-worker-kit
/mnt/secondary/projects/agent_core/.venv/bin/python -m pytest -q
/mnt/secondary/projects/agent_core/.venv/bin/python -m pytest -k agrees_with_the_worker_kit -v
```

Expected: full suite green; `-k` run shows **3 passed, 0 skipped**. Record.

**Step 5 — commit and land.**

`test(artifacts): guard the descriptor contract against pare-worker-kit`

agent_core main; push; verify `origin/main`.

**Ledger note after Task 3:** both venvs now contain a NON-EDITABLE local-path install of the
sibling (agent_core 1.11.1 in the kit venv, pare-worker-kit 0.2.0 in the agent_core venv —
versions unchanged by design; the content is the local tree). Record `pip list` excerpts from
both.

---

## Task 4 — agent_core: `validate_descriptor` takes the spec

**Files:** agent_core `agent_core/workers/artifacts.py`, `agent_core/workers/types.py`,
`tests/workers/test_artifacts.py`.
**Spec:** A3 (`spec` replaces `worker: str`; `spec.name` carries the name into the errors), A5
(drive id required whenever the root is), §4 (the three grammars), §6 step 5 (containment
against `{artifact_root}/{slug}`, drive-id comparison, host stays well-formedness-only — the
endpoint field `artifact_host` is P3/A2), D6/D7 (the operator-declared root; the daemon's check
is lexical, the worker enforces the real one), §10 Risk 1 (containment against the root alone
binds the injected slug to nothing).

### Task 4 RED — the test rewrite first

In `tests/workers/test_artifacts.py`:

**4.1** Extend the mid-file import block (line 28) with the spec type:

```python
# Task 3: Descriptor validation tests
from agent_core.workers.artifacts import DescriptorError, validate_descriptor
from agent_core.workers.types import WorkerSpec
```

**4.2** Replace the `_GOOD` block (lines 30–37) with:

```python
_DRIVE = "12345678-90ab-4cd0-8e12-34567890abcd"
"""A made-up sentinel UUID for tests. Never the bench drive's id: that value
is read from the drive, not typed from memory."""

_GOOD = {
    "host": "pare-bench",
    "path": "/mnt/bench-store/router-b/fw-0001.bin",
    "size": 2147483648,
    "sha256": "a" * 64,
    "hashed_at": "2026-09-06T12:34:56Z",
    "media_type": "application/octet-stream",
    "drive_id": _DRIVE,
}


def _spec(root="/mnt/bench-store", drive_id=_DRIVE):
    return WorkerSpec(name="hardware", transport="stdio", command="/bin/true",
                      risk_default="high", artifact_root=root,
                      artifact_drive_id=drive_id)


def _v(payload, *, root="/mnt/bench-store", drive_id=_DRIVE, slug="router-b"):
    """The one call shape for the rest of this file. The default spec makes
    the _GOOD path contained (root /mnt/bench-store, slug router-b), so the
    shape tests keep testing shape, and the containment tests opt out by
    changing root, drive_id or slug."""
    return validate_descriptor(payload, spec=_spec(root, drive_id),
                               tool="dump_firmware", slug=slug)
```

**4.3** Mechanical call-site changes: every
`validate_descriptor(X, worker="hardware", tool="dump_firmware")` becomes `_v(X)`. The affected
tests and their new forms (bodies only; docstrings unchanged unless stated):

```python
def test_a_well_formed_descriptor_passes_through():
    out = _v(dict(_GOOD))
    assert out == dict(_GOOD)


@pytest.mark.parametrize("missing", ARTIFACT_DESCRIPTOR_FIELDS)
def test_a_missing_required_field_is_rejected(missing):
    """A tool that declares `artifact` and returns something else is a contract
    violation, recorded as an error rather than silently treated as a result.
    Parametrised over the constant, not a re-typed list: the field set IS the
    constant, and a field landing in it must light this test up."""
    payload = dict(_GOOD)
    del payload[missing]
    with pytest.raises(DescriptorError, match=missing):
        _v(payload)


def test_a_non_dict_payload_is_rejected():
    with pytest.raises(DescriptorError, match="not a JSON object"):
        _v(["nope"])
```

`test_a_malformed_sha256_is_rejected`, `test_a_non_integer_size_is_rejected`,
`test_a_relative_path_is_rejected`: parametrize lists and docstrings unchanged; the call line
becomes `_v(payload)`. `test_the_error_names_the_worker_and_tool`:

```python
    with pytest.raises(DescriptorError) as e:
        _v({})
    assert "hardware" in str(e.value) and "dump_firmware" in str(e.value)
```

(same docstring; `hardware` now comes from `spec.name` — A3).
`test_a_host_that_is_not_a_hostname_is_rejected` and `test_a_real_host_passes`: call lines become
`_v(payload)` / `out = _v(dict(_GOOD, host=ok))` (the default path is contained, so the host
check is still what fires). `test_a_path_that_would_misbehave_in_the_retrieval_command_is_rejected`:
parametrize list and docstring unchanged; every listed path is refused by a pre-existing check
BEFORE containment, so the default spec is fine; call line becomes `_v(payload)`.

**4.4** Re-root the positive path tests under a project directory (containment now binds them to
`{root}/{slug}`):

```python
@pytest.mark.parametrize("path,root,slug", [
    ("/mnt/bench-store/router-b/fw-0001.bin", "/mnt/bench-store", "router-b"),
    ("/mnt/s/proj/fw..bin", "/mnt/s", "proj"),           # two dots in a NAME
    ("/mnt/s/proj/..hidden", "/mnt/s", "proj"),          # daemon looser than the kit
    ("/mnt/s/proj/v1.2.3/fw.bin", "/mnt/s", "proj"),
    ("/mnt/s/proj/dump-2026-09-06.bin", "/mnt/s", "proj"),
    ("/mnt/s/proj/a b.bin", "/mnt/s", "proj"),           # a space is ordinary
])
def test_a_legitimate_path_still_passes(path, root, slug):
    """The traversal check is COMPONENT-WISE, never a substring search.
    Refusing every path containing the two characters `..` would refuse
    ordinary filenames and buy nothing: `fw..bin` escapes nothing.

    `..hidden` is the case where the two packages DELIBERATELY differ, and
    neither said so until then. pare-worker-kit's `_NAME_RE` requires a
    leading alphanumeric, so no kit-BUILT path can ever have that basename.
    This function is looser on purpose: it validates descriptors from ANY
    worker, including ones that never used the kit to construct the path, and
    a leading dot is an ordinary filename on the filesystem the worker owns.
    Tightening here to match the kit would reject a conformant non-kit worker
    for a rule the wire contract never stated -- and would buy nothing, since
    a leading dot escapes nothing and is not argument-injection range (that
    is the leading DASH, refused above).

    The paths now carry a project directory because containment is checked
    against {root}/{slug} (Task 4): the slug the daemon injected is what the
    path must sit under.
    """
    out = _v(dict(_GOOD, path=path), root=root, slug=slug)
    assert out["path"] == path


@pytest.mark.parametrize("accepted", [
    "/mnt/s/proj/f;rm -rf /", "/mnt/s/proj/$(id)", "/mnt/s/proj/`id`",
    "/mnt/s/proj/a|b", "/mnt/s/proj/a&b", "/mnt/s/proj/*.bin",
])
def test_shell_metacharacters_are_deliberately_accepted(accepted):
    """(Docstring unchanged -- transfer mode, not containment, is what this
    decision is about.)"""
    out = _v(dict(_GOOD, path=accepted), root="/mnt/s", slug="proj")
    assert out["path"] == accepted
```

(Keep the shell-metachar test's FULL existing docstring verbatim; only the parametrize list and
the call change.)

**4.5** DELETE `test_containment_against_the_artifact_root_is_not_checked_here` (lines 280–303) —
its own docstring says the test that pins the gap must move when the gap closes, and it now
asserts the inverse of the new behaviour.

**4.6** ADD, after the shell-metachar test:

```python
def test_the_signature_holds_the_spec_and_the_slug():
    """The inverse of the gap pin deleted in this round. The old test pinned
    the ABSENCE of a spec from this signature; its docstring said the day a
    spec is threaded in, the test and the docstring paragraph move together.
    This asserts the new half structurally -- not by searching __doc__, which
    is None under `python -OO` -- so the signature cannot be stripped back to
    a worker name while the docstring still claims containment."""
    params = set(inspect.signature(validate_descriptor).parameters)
    assert {"spec", "slug"} <= params
    assert "worker" not in params


def test_a_path_outside_the_root_is_refused():
    """The case the deleted gap test pinned as ACCEPTED: `/etc/shadow` is
    well-formed by every shape rule and is now refused, because containment
    is checked and the path is not under the project directory."""
    with pytest.raises(DescriptorError, match="not under"):
        _v(dict(_GOOD, path="/etc/shadow"))


def test_a_path_under_the_root_but_another_project_is_refused():
    """The reason containment is against {root}/{slug} and not the root alone
    (spec §10, Risk 1): a descriptor that lies about its project is under
    the root and would bind the injected slug to nothing if the check were
    against the root."""
    with pytest.raises(DescriptorError, match="not under"):
        _v(dict(_GOOD, path="/mnt/bench-store/other-proj/fw.bin"))


def test_a_path_directly_under_the_root_is_refused():
    """The worker builds {root}/{slug}/{name}; a file with no project
    directory component is not a descriptor this contract recognises."""
    with pytest.raises(DescriptorError, match="not under"):
        _v(dict(_GOOD, path="/mnt/bench-store/fw.bin"))


def test_a_prefix_spoof_of_the_project_directory_is_refused():
    """`/mnt/bench-store/router-b-evil/...` has the project directory as a
    STRING prefix. Containment is decided by commonpath on components, not
    by startswith -- the same reason the traversal check is component-wise."""
    with pytest.raises(DescriptorError, match="not under"):
        _v(dict(_GOOD, path="/mnt/bench-store/router-b-evil/fw.bin"))


def test_a_trailing_slash_root_is_contained_normally():
    """An operator's trailing slash in workers.yaml is not a mistake worth
    refusing: the root is normalised before the comparison, the way the
    kit's artifact_path normalises it."""
    out = _v(dict(_GOOD), root="/mnt/bench-store/")
    assert out["path"] == _GOOD["path"]


def test_a_worker_that_declares_no_root_refuses_the_descriptor():
    """Fail closed at the validator level (ruling R5). Dispatch (P3) refuses
    earlier, with the operator-facing message; this keeps the function safe
    to call standalone."""
    with pytest.raises(DescriptorError, match="artifact_root"):
        _v(dict(_GOOD), root=None)


def test_a_drive_id_mismatch_is_refused_and_names_both():
    other = "ffffeeee-dddd-4ccc-8bbb-aaaaaaaaaaaa"
    with pytest.raises(DescriptorError) as e:
        _v(dict(_GOOD, drive_id=other))
    msg = str(e.value)
    assert other in msg and _DRIVE in msg


def test_a_worker_without_a_declared_drive_id_refuses_the_descriptor():
    """A5: the drive id is required whenever the root is; a descriptor cannot
    be checked against a drive the worker never declared."""
    with pytest.raises(DescriptorError, match="artifact_drive_id"):
        _v(dict(_GOOD), drive_id=None)


@pytest.mark.parametrize("bad", [
    "12345678-90AB-4CD0-8E12-34567890ABCD",   # uppercase: a second spelling
    "1234567890ab4cd08e1234567890abcd",       # no dashes
    "12345678-90ab-4cd0-8e12-34567890abc",    # final group 11, not 12
    "12345678-90ab-4cd0-8e12-34567890abcd1",  # final group 13
    "12345678-90ab-4cd0-8e12-34567890abc\x1b",  # the value is printed into
                                                # the error an operator reads
    None, 123,
])
def test_a_drive_id_that_is_not_the_sentinel_grammar_is_refused(bad):
    with pytest.raises(DescriptorError, match="drive_id"):
        _v(dict(_GOOD, drive_id=bad))


@pytest.mark.parametrize("bad", [
    1757160000.0,                  # the old fixture's epoch float
    "2026-09-06 12:34:56Z",        # space, not T
    "2026-09-06T12:34:56+02:00",   # a non-UTC offset
    "2026-09-06T12:34:56",         # no zone
    "2026-09-06T12:34:56z",        # lowercase z: one spelling, pinned
    None,
])
def test_a_hashed_at_that_is_not_rfc3339_utc_is_refused(bad):
    with pytest.raises(DescriptorError, match="hashed_at"):
        _v(dict(_GOOD, hashed_at=bad))


@pytest.mark.parametrize("ok", ["2026-09-06T12:34:56Z",
                                "2026-09-06T12:34:56.789Z"])
def test_an_rfc3339_utc_hashed_at_passes(ok):
    assert _v(dict(_GOOD, hashed_at=ok))["hashed_at"] == ok


@pytest.mark.parametrize("bad", [
    "application",                         # no subtype
    "/octet-stream",
    "application/",
    "application/octet-stream; q=0.5",     # parameters are not part of
                                            # type/subtype
    "*/*",                                 # wildcards: a descriptor names
                                            # what the file IS
    "application/octet stream",
    "", None, 123,
])
def test_a_media_type_that_is_not_an_iana_type_subtype_is_refused(bad):
    with pytest.raises(DescriptorError, match="media_type"):
        _v(dict(_GOOD, media_type=bad))


@pytest.mark.parametrize("ok", ["application/octet-stream", "text/plain",
                                "multipart/related", "TEXT/PLAIN"])
def test_an_iana_type_subtype_passes(ok):
    assert _v(dict(_GOOD, media_type=ok))["media_type"] == ok
```

**Step — run the red:**

```
cd /mnt/secondary/projects/agent_core
.venv/bin/python -m pytest tests/workers/test_artifacts.py -v
```

Expected RED: every descriptor test fails with
`TypeError: validate_descriptor() got an unexpected keyword argument 'spec'`; the constant pins,
guards and slug tests still pass. Record a representative sample.

### Task 4 GREEN — the validator

In `agent_core/workers/artifacts.py`:

**G.1** Imports (line 9): `import re` becomes

```python
import os
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_core.workers.types import WorkerSpec
```

(`from __future__ import annotations` is already at line 7, so the `spec: WorkerSpec` annotation
is never evaluated at runtime; `types.py` does not import `artifacts.py`, so the cycle guard is
belt-and-braces only.)

**G.2** After the `_CONTROL_RE` docstring (line 37), add the three grammars (ruling R4):

```python
_HASHED_AT_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z\Z")
"""RFC 3339 UTC in the strict `Z` form, optional fractional seconds.

"Required" without a format is not a contract, so the wire states one. The
check is FORMAT, not calendar: whether the timestamp is true is a custody
record the daemon cannot verify (the worker's clock is what it is), and this
function checks shape, not truthfulness, like every other field. A non-UTC
offset is refused because the field IS UTC, not "a time with a zone"; the
uppercase `Z` is pinned because the producer (open_artifact) emits that
spelling and a second accepted spelling is comparison surface the operator's
eye cannot audit.
"""

_MEDIA_TYPE_RE = re.compile(
    r"\A[A-Za-z0-9!#$&^_.+-]{1,126}/[A-Za-z0-9!#$&^_.+-]{1,126}\Z")
"""An IANA type/subtype, and nothing else.

No parameters (`;q=`, `;charset=`): the field is the type/subtype, not a full
media-type production. No wildcards (`*`): a descriptor names what the file
IS, not a range of what it might be. The character class is the RFC 2045
token set minus the characters no registered type uses, and the 126 cap is
the RFC token length limit. Case-insensitive by RFC 2046, so
`application/octet-stream` and `APPLICATION/OCTET-STREAM` both pass.
"""

_DRIVE_ID_RE = re.compile(
    r"\A[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
"""Canonical lowercase UUID -- the grammar of the sentinel file
(`{artifact_root}/.bench-store-id`) the value is read from.

Lowercase and dashed, and only that: the sentinel is written that way, and a
second accepted spelling is a second comparison the operator's eye cannot
audit. drive_id also gets the explicit _CONTROL_RE check: the value is
printed into the mismatch message an operator reads, and an escape sequence
in it would rewrite what that message says.
"""
```

**G.3** Delete `_REQUIRED = ("host", "path", "size", "sha256")` (line 39) — ruling R2's second
half; `ARTIFACT_DESCRIPTOR_FIELDS` takes over.

**G.4** Replace `validate_descriptor` (lines 46–187) with:

```python
def validate_descriptor(payload, *, spec: WorkerSpec, tool: str, slug: str) -> dict:
    """Check the SHAPE of an artifact descriptor. Not its truthfulness.

    [Paragraphs from "Everything here is self-reported" through "So this
    function refuses what NO caller can fix, and leaves what the right
    transfer mode fixes completely." are KEPT VERBATIM -- the sha256, path,
    shell-metacharacter and call-site paragraphs are unchanged.]

    CONTAINMENT IS CHECKED HERE, AGAINST THE PROJECT DIRECTORY, NOT THE ROOT.
    The slug arrives as a keyword because it is not in the payload: the
    daemon validated it, injected it into the tool call as the reserved slug
    argument, and hands it back here. The rule is `path` under
    `{artifact_root}/{slug}`, decided by `commonpath` on normalised paths --
    a LEXICAL check, D7's division of labour: the worker enforces the real
    containment (it is the only side that can see symlinks and mount points),
    and this is the daemon's shadow of the same rule, so a descriptor that
    lies about its project is refused at the chokepoint rather than at
    retrieval time. Against the root alone the check would bind the injected
    slug to nothing: `/mnt/bench-store/other-proj/fw.bin` is well contained
    by the root and is another project's dump.

    A worker whose `artifact_root` is None refuses every descriptor, as does
    one whose `artifact_drive_id` is None: the dispatch path (which lands
    after this) refuses earlier, with the operator-facing message, and these
    refusals keep the function safe to call standalone.

    THE DRIVE ID IS COMPARED, NOT JUST FORM-CHECKED. `drive_id` must match
    the sentinel's UUID grammar AND equal `spec.artifact_drive_id`; a
    mismatch means the bytes went to a different drive than workers.yaml
    names, and the error names both values so the operator can see which is
    which.

    THE REMAINING GAP: `host` is checked for SHAPE and never against the
    worker it came from. `_HOST_RE` accepts any well-formed hostname, so a
    compromised worker A can return `host: "bench-b"` and aim the operator's
    retrieval at a machine of its choosing -- which is what makes the
    remote-shell caveat above reachable at all. This function now RECEIVES
    the WorkerSpec but still does not reconcile the host: the endpoint field
    (`artifact_host`) is not on the spec yet, and lands with the dispatch
    wiring. Until it does, host stays shape-only on purpose, and the
    dispatch path must reconcile a descriptor's `host` with the spec it
    dispatched to.
    """
    where = f"{spec.name}.{tool}"
    if not isinstance(payload, dict):
        raise DescriptorError(
            f"{where} declared produces=artifact but returned "
            f"{type(payload).__name__}, not a JSON object")
    for field in ARTIFACT_DESCRIPTOR_FIELDS:
        if field not in payload:
            raise DescriptorError(f"{where}: descriptor is missing {field!r}")

    size = payload["size"]
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise DescriptorError(
            f"{where}: descriptor size must be a non-negative int, got {size!r}")

    sha = payload["sha256"]
    if not isinstance(sha, str) or not _SHA256_RE.match(sha):
        raise DescriptorError(
            f"{where}: descriptor sha256 must be 64 lowercase hex chars, "
            f"got {sha!r}")

    path = payload["path"]
    if not isinstance(path, str) or not path.startswith("/"):
        raise DescriptorError(
            f"{where}: descriptor path must be absolute, got {path!r}")
    if _CONTROL_RE.search(path):
        raise DescriptorError(
            f"{where}: descriptor path contains a control character, "
            f"got {path!r}")
    parts = path.split("/")
    if ".." in parts:
        raise DescriptorError(
            f"{where}: descriptor path contains a '..' component, "
            f"got {path!r}")
    # The last NON-EMPTY component: `/mnt/s/-rf/` names `-rf` just as
    # `/mnt/s/-rf` does, and os.path.basename would return "" for the first.
    name = next((c for c in reversed(parts) if c), "")
    if name.startswith("-"):
        raise DescriptorError(
            f"{where}: descriptor path names {name!r}, which begins with '-' "
            f"and is read as an option rather than a filename by the commands "
            f"an operator runs on it; got {path!r}")

    if spec.artifact_root is None:
        raise DescriptorError(
            f"{where}: worker {spec.name!r} declares no artifact_root, so no "
            f"artifact it returns can be contained; refused, not validated")
    project = os.path.normpath(os.path.join(spec.artifact_root, slug))
    if os.path.commonpath([os.path.normpath(path), project]) != project:
        raise DescriptorError(
            f"{where}: descriptor path {path!r} is not under {project!r}: "
            f"containment is against the project directory "
            f"({spec.artifact_root!r}/{slug!r}), not the root, so the "
            f"injected slug binds the path to the project it names")

    hashed_at = payload["hashed_at"]
    if not isinstance(hashed_at, str) or not _HASHED_AT_RE.match(hashed_at):
        raise DescriptorError(
            f"{where}: descriptor hashed_at must be RFC 3339 UTC "
            f"(e.g. 2026-09-06T12:34:56Z), got {hashed_at!r}")

    media_type = payload["media_type"]
    if not isinstance(media_type, str) or not _MEDIA_TYPE_RE.match(media_type):
        raise DescriptorError(
            f"{where}: descriptor media_type must be an IANA type/subtype, "
            f"got {media_type!r}")

    drive_id = payload["drive_id"]
    if (not isinstance(drive_id, str) or _CONTROL_RE.search(drive_id)
            or not _DRIVE_ID_RE.match(drive_id)):
        raise DescriptorError(
            f"{where}: descriptor drive_id must be the canonical lowercase "
            f"UUID read from the sentinel file, got {drive_id!r}")
    if spec.artifact_drive_id is None:
        raise DescriptorError(
            f"{where}: worker {spec.name!r} declares artifact_root without "
            f"artifact_drive_id; a descriptor it returns cannot be checked "
            f"against the drive it names, so it is refused")
    if drive_id != spec.artifact_drive_id:
        raise DescriptorError(
            f"{where}: descriptor drive_id {drive_id!r} is not the declared "
            f"{spec.artifact_drive_id!r}: the artifact was written to a "
            f"different drive than workers.yaml names")

    host = payload["host"]
    if not isinstance(host, str) or not _HOST_RE.match(host):
        raise DescriptorError(
            f"{where}: descriptor host must be a hostname or address, "
            f"got {host!r}")

    return dict(payload)
```

Check order is deliberate: the pre-existing path checks keep their order and their messages
word-for-word (the old tests pin them by substring), containment extends the path block, the
three new grammars follow, and `host` stays last exactly as before. `SLUG_RE`/`validate_slug`
at the bottom of the file are untouched.

**G.5** `types.py` docstring surgery — the two "DECLARED, NOT YET ENFORCED" paragraphs are now
false and must move with the behaviour (no code change in types.py):

`artifact_root` (lines 120–133): replace the paragraph beginning
`DECLARED, NOT YET ENFORCED -- read this as an intention...` with:

```
    DECLARED, ENFORCED AT TWO OF THREE LAYERS -- read this as an intention,
    not as current behaviour. The absolute-path validator below runs at
    config load, and agent_core.workers.artifacts.validate_descriptor now
    takes this object (as `spec`) and refuses a descriptor whose path is not
    under `{artifact_root}/{slug}` -- lexically, D7's daemon-side shadow of
    the worker's real check. What is still missing is the dispatch path that
    routes on the produces declaration and calls the validator with this
    spec: until it lands, a produces="artifact" dispatch against a worker
    whose root is None is refused nowhere, and the validator's own no-root
    refusal is defence in depth rather than the operator's experience.
```

`artifact_drive_id` (lines 141–146): replace the paragraph beginning
`DECLARED, NOT YET ENFORCED, like artifact_root above...` with:

```
    HALF ENFORCED, like artifact_root above:
    agent_core.workers.artifacts.validate_descriptor now COMPARES a
    descriptor's drive_id against this value and refuses a mismatch -- and
    refuses outright when this field is None. What is still missing is the
    dispatch path that carries this value to the validator. Note where the
    OTHER half has to live when it is built -- the daemon cannot see the
    worker's filesystem, so whatever reads `.bench-store-id` runs on the
    WORKER, next to artifact_path in pare-worker-kit.
```

**Step — run the green:**

```
.venv/bin/python -m pytest -q
```

Expected: full agent_core suite green (the guard suite included, against the locally installed
kit). Record. Also run the guard filter explicitly and record:

```
.venv/bin/python -m pytest -k agrees_with_the_worker_kit -v
```

**Step — commit and land.**

`feat(artifacts): validate_descriptor takes the spec, with project-directory containment and drive-id comparison`

agent_core main; push; verify `origin/main`. This closes the §1 window: `validate_descriptor`
now has a new signature and any consumer that appears must use it.

---

## Acceptance — P1 is done when

1. **Both suites green locally, guards RUN (not skipped):**
   - kit: `.venv/bin/python -m pytest -q` all green;
     `-k agrees_with_agent_core -v` → **3 passed, 0 skipped**.
   - agent_core: `.venv/bin/python -m pytest -q` all green;
     `-k agrees_with_the_worker_kit -v` → **3 passed, 0 skipped**.
   Both outputs recorded in the ledger.
2. **CI green on both repos** after each push, including the cross-package jobs (kit's
   `cross-package` job and agent_core's "Cross-package guards must run, not skip" step). Verify
   with `gh run list`/`gh run watch` if `gh` is authenticated, otherwise the operator confirms.
3. **Nothing version-shaped moved:** the versions recorded at preflight step 8 (kit pyproject
   `0.2.0`, kit `__init__.py` `__version__ "0.2.0"`, agent_core pyproject `1.11.1`) are
   unchanged; `git tag` lists are unchanged in both repos; PARE's `pyproject.toml` pin is
   untouched (that is P4).
4. **Exactly five commits landed** (kit declaration; agent_core declaration; two guards; agent
   core behaviour), one per task half, on `origin/main` (kit via merged PR), plus this plan file
   committed to PARE's docs.
5. **The ledger** carries: preflight results, every red (Task 1/2 import reds, Task 3's two
   AttributeError reds, Task 4's TypeError red), every green, the `pip list` sibling state, and
   the CI run IDs.

## Handoff

- **P2 — `open_artifact`** (kit). §5's heaviest review: interfaces and invariants in the plan,
  **no code**, loopback rig required, attack-sequence brief. Builds exactly
  `ARTIFACT_DESCRIPTOR_FIELDS`; takes the reserved values as its parameters.
- **P3 — dispatch wiring** (agent_core `call_tool`): route on `pool.produces()`, reserved-argument
  injection before `snapshot`, tier floor to `high`, `validate_slug` before injection, the
  step-2 refusals + audit rows, extraction (exactly-one text block), the call
  `validate_descriptor(payload, spec=spec, tool=tool, slug=slug)` per ruling R1, `artifact_host`
  on WorkerSpec (A2), host reconciliation, `produced_by` added after validation.
- **P4 — release, consumers, acceptance:** tag kit `v0.3.0` and agent_core `v1.12.0` (the actual
  next versions as of 2026-10-02; §7's literals `v0.2.0`/`v1.11.0` predate both repos' current
  releases), four pin bumps (PARE's agent_core pin; the three worker repos' kit pins), consuming
  suites, the §8 probe levels.
- **Then** the hardware console-capture plan resumes at **Task 1 step 1**: its gate (read from
  that plan, not this one) requires kit v0.3.0 with `open_artifact` + the reserved-argument
  constants, an agent_core release with the new `validate_descriptor` signature and dispatch
  wiring, and PARE's pin bump — i.e. P1–P4 all landed. The gate's placeholders
  (`⟨SLUG_ARG_CONST⟩`, `⟨slug_param⟩`, ...) resolve from the landed source: here they will be
  `RESERVED_SLUG_ARG`/`RESERVED_DRIVE_ID_ARG` and `"project_slug"`/`"expected_drive_id"` — but the
  resume step re-reads the source, per its own placeholder rule.
