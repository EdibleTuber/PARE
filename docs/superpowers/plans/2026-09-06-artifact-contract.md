# Artifact Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Establish the cross-repo wire contract that lets a worker return a *reference to a file it wrote* instead of the file's contents, with the security controls that make acting on that reference safe.

**Architecture:** A tool declares `produces="artifact"` in its contract; that declaration travels as an MCP `_meta` key exactly as the risk tier already does. The daemon caches it monotonically (a tool seen producing artifacts can never later be treated as producing results), validates the returned descriptor's shape, and refuses the dispatch entirely if the operator has not declared an `artifact_root` for that worker. Containment against symlink escape is enforced **on the worker**, because the daemon has no view of the worker's filesystem.

**Tech Stack:** Python 3.12 (kit also supports 3.10), pydantic v2, `mcp` 1.29.x, pytest.

**Spec:** `docs/superpowers/specs/2026-09-06-arcticbase-artifacts-design.md`

## Global Constraints

- **`pare-worker-kit` depends on `mcp` alone.** Adding any dependency re-creates the problem the package exists to solve (agent_core costs a worker +46 packages / +355 MB). Do not import `pydantic`, `agent_core`, or anything else in the kit.
- **Neither package imports the other.** Both state the constant; a bidirectional guard test in each suite asserts the other's literal matches, skipping when the other is absent. This is a **wire** constant: the daemon and the Pi worker are separately installed on different machines and never share a Python environment.
- **`produces` default is `"result"`.** Absent or unrecognised means `result` at the daemon; an unrecognised value is a *conformance* failure at build time.
- **`produces` is monotonic.** Once a `(worker, tool)` has been observed as `artifact`, it may never be treated as `result` — otherwise a reload silently disables descriptor validation. Mirrors `_tier_highwater`, which `_bump()` deliberately never clears.
- **A missing `artifact_root` fails CLOSED.** A `produces="artifact"` dispatch against a worker with no resolved root is refused, not accepted unvalidated.
- **Slug alphabet is ArcticBase's, verbatim:** `^[a-z0-9][a-z0-9_-]{0,63}$`, matched with `re.fullmatch`. It is the strictest consumer, and a leading alphanumeric is what keeps a slug out of argument-injection range in the `scp`/`tar` commands an operator later runs.
- **The kit supports Python 3.10.** Do not use 3.11+ syntax there. `agent_core` is 3.12+.
- Run `pytest -q` in the repo you changed before every commit.

---

## File Structure

| File | Responsibility |
|---|---|
| `pare-worker-kit/src/pare_worker_kit/artifacts.py` (new) | Worker side: the `produces` constant, valid values, and the containment-checked open |
| `pare-worker-kit/tests/test_artifacts.py` (new) | Its tests |
| `agent_core/agent_core/workers/artifacts.py` (new) | Daemon side: mirror constant, descriptor validation, slug validation |
| `agent_core/tests/workers/test_artifacts.py` (new) | Its tests |
| `agent_core/agent_core/workers/types.py` | `WorkerSpec` gains `artifact_root`, `artifact_drive_id`, `extra="forbid"` |
| `agent_core/agent_core/workers/risk_pool.py` | Capture `produces` in `list_tools` with the monotonic ratchet |
| `agent_core/agent_core/workers/conformance.py` | `_assert_valid_produces_meta`, called from both live suites |

---

### Task 1: The `produces` constant in `pare-worker-kit`

**Files:**
- Create: `pare-worker-kit/src/pare_worker_kit/artifacts.py`
- Modify: `pare-worker-kit/src/pare_worker_kit/__init__.py`
- Test: `pare-worker-kit/tests/test_artifacts.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `PRODUCES_META_KEY: str`, `VALID_PRODUCES: tuple[str, ...]`, `PRODUCES_RESULT: str`, `PRODUCES_ARTIFACT: str`.

- [ ] **Step 1: Write the failing test**

```python
# pare-worker-kit/tests/test_artifacts.py
from pare_worker_kit import (PRODUCES_ARTIFACT, PRODUCES_META_KEY,
                             PRODUCES_RESULT, VALID_PRODUCES)


def test_the_meta_key_is_stable_protocol():
    """agent_core states this same literal independently; a guard test in each
    suite keeps them equal. Changing it is a wire-breaking change."""
    assert PRODUCES_META_KEY == "agent_core/produces"


def test_result_is_the_default_value():
    """A tool that says nothing produces a result. Only an explicit
    declaration opts into the artifact path."""
    assert PRODUCES_RESULT == "result"
    assert PRODUCES_ARTIFACT == "artifact"
    assert VALID_PRODUCES == ("result", "artifact")
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd /mnt/secondary/projects/pare-worker-kit && python -m pytest tests/test_artifacts.py -q`
Expected: FAIL — `ImportError: cannot import name 'PRODUCES_ARTIFACT'`

- [ ] **Step 3: Write the implementation**

```python
# pare-worker-kit/src/pare_worker_kit/artifacts.py
"""What a tool produces, and where a worker is allowed to write it.

The daemon routes on this declaration rather than on the model's choice of
tool: a tool marked `artifact` returns a DESCRIPTOR of a file it wrote, never
the file's contents. Without it, a two-gigabyte firmware dump would cross the
network as one tool result.
"""

PRODUCES_META_KEY = "agent_core/produces"
"""The _meta key a tool uses to declare what it returns.

Stated here AND in agent_core, with a guard test on each side, because the
daemon and the worker are separately installed packages that never share a
Python environment -- so a shared import could not guarantee agreement across
the wire any better than two literals can.
"""

PRODUCES_RESULT = "result"
PRODUCES_ARTIFACT = "artifact"

VALID_PRODUCES = (PRODUCES_RESULT, PRODUCES_ARTIFACT)
"""Absent means `result`. An unrecognised value is a conformance failure at
build time, not a silent default -- the same choice the risk tier makes, and
copying the mechanism without copying that choice would lose the property."""
```

Then in `__init__.py`, add to the imports and `__all__`:

```python
from pare_worker_kit.artifacts import (PRODUCES_ARTIFACT, PRODUCES_META_KEY,
                                       PRODUCES_RESULT, VALID_PRODUCES)
```

and extend `__all__` with `"PRODUCES_META_KEY", "PRODUCES_RESULT", "PRODUCES_ARTIFACT", "VALID_PRODUCES"`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /mnt/secondary/projects/pare-worker-kit && python -m pytest -q`
Expected: PASS, all tests (41 existing + 2 new)

- [ ] **Step 5: Commit**

```bash
cd /mnt/secondary/projects/pare-worker-kit
git add src/pare_worker_kit/artifacts.py src/pare_worker_kit/__init__.py tests/test_artifacts.py
git commit -m "feat: declare what a tool produces, worker side"
```

---

### Task 2: The mirror constant in `agent_core`, with guards both ways

**Files:**
- Create: `agent_core/agent_core/workers/artifacts.py`
- Create: `agent_core/tests/workers/test_artifacts.py`
- Modify: `pare-worker-kit/tests/test_artifacts.py` (add the reverse guard)

**Interfaces:**
- Consumes: `pare_worker_kit.PRODUCES_META_KEY` (in tests only, via `importorskip`).
- Produces: `agent_core.workers.artifacts.PRODUCES_META_KEY`, `VALID_PRODUCES`, `PRODUCES_RESULT`, `PRODUCES_ARTIFACT`.

**Ordering note:** the slug half of each guard test asserts `SLUG_RE`, which
Task 4 (agent_core) and Task 8 (kit) create. Write those two guard tests here
but expect them to fail until both land — or add them at the end of Task 8.
Either is fine; do not silently drop them.

- [ ] **Step 1: Write the failing tests**

```python
# agent_core/tests/workers/test_artifacts.py
import pytest

from agent_core.workers.artifacts import (PRODUCES_ARTIFACT, PRODUCES_META_KEY,
                                          PRODUCES_RESULT, VALID_PRODUCES)


def test_the_daemon_states_the_wire_constant_itself():
    assert PRODUCES_META_KEY == "agent_core/produces"
    assert VALID_PRODUCES == ("result", "artifact")
    assert (PRODUCES_RESULT, PRODUCES_ARTIFACT) == ("result", "artifact")


def test_it_agrees_with_the_worker_kit():
    """The two packages are installed separately, usually on different
    machines. This test is what keeps the two statements the same, in
    whichever environment happens to have both."""
    kit = pytest.importorskip(
        "pare_worker_kit.artifacts",
        reason="pare-worker-kit is not installed here; the worker-side half "
               "of this check runs in that package's own suite")
    assert kit.PRODUCES_META_KEY == PRODUCES_META_KEY
    assert kit.VALID_PRODUCES == VALID_PRODUCES


def test_the_slug_rule_agrees_with_the_worker_kit():
    """Also duplicated across the wire. A slug the daemon accepts and the
    worker refuses means hardware tools stop working with no useful error."""
    kit = pytest.importorskip("pare_worker_kit.artifacts")
    from agent_core.workers.artifacts import SLUG_RE
    assert kit.SLUG_RE.pattern == SLUG_RE.pattern
```

And the reverse guard, appended to `pare-worker-kit/tests/test_artifacts.py`:

```python
def test_it_agrees_with_agent_cores():
    import pytest
    ac = pytest.importorskip(
        "agent_core.workers.artifacts",
        reason="agent_core is not installed here; the daemon-side half of "
               "this check runs in agent_core's own suite")
    assert ac.PRODUCES_META_KEY == PRODUCES_META_KEY
    assert ac.VALID_PRODUCES == VALID_PRODUCES


def test_the_slug_rule_agrees_with_agent_cores():
    import pytest
    from pare_worker_kit.artifacts import SLUG_RE
    ac = pytest.importorskip("agent_core.workers.artifacts")
    assert ac.SLUG_RE.pattern == SLUG_RE.pattern
```

- [ ] **Step 2: Run to verify failure**

Run: `cd /mnt/secondary/projects/agent_core && python -m pytest tests/workers/test_artifacts.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'agent_core.workers.artifacts'`

- [ ] **Step 3: Write the implementation**

```python
# agent_core/agent_core/workers/artifacts.py
"""The daemon's half of the artifact contract.

A tool that declares `produces: artifact` returns a DESCRIPTOR of a file it
wrote on its own machine, not the file's contents. This module states the wire
constant and validates what comes back.
"""
from __future__ import annotations

PRODUCES_META_KEY = "agent_core/produces"
"""Stated here and in pare-worker-kit, with a guard test on each side. See
RISK_TIER_META_KEY for the same arrangement and the same reasoning: the two
packages are installed separately on machines that never share a Python
environment."""

PRODUCES_RESULT = "result"
PRODUCES_ARTIFACT = "artifact"

VALID_PRODUCES = (PRODUCES_RESULT, PRODUCES_ARTIFACT)
```

- [ ] **Step 4: Run tests both sides**

Run: `cd /mnt/secondary/projects/agent_core && python -m pytest -q`
Expected: PASS

Run: `cd /mnt/secondary/projects/pare-worker-kit && python -m pytest -q`
Expected: PASS

- [ ] **Step 5: Commit both repos**

```bash
cd /mnt/secondary/projects/agent_core
git add agent_core/workers/artifacts.py tests/workers/test_artifacts.py
git commit -m "feat(workers): state the produces wire constant, guarded against the kit"
cd /mnt/secondary/projects/pare-worker-kit
git add tests/test_artifacts.py
git commit -m "test: guard the produces constant against agent_core"
```

---

### Task 3: Descriptor validation

**Files:**
- Modify: `agent_core/agent_core/workers/artifacts.py`
- Test: `agent_core/tests/workers/test_artifacts.py`

**Interfaces:**
- Consumes: `PRODUCES_ARTIFACT`.
- Produces: `DescriptorError(ValueError)`, and
  `validate_descriptor(payload: dict, *, worker: str, tool: str) -> dict`
  returning the normalised descriptor or raising `DescriptorError`.

- [ ] **Step 1: Write the failing tests**

```python
# append to agent_core/tests/workers/test_artifacts.py
import pytest

from agent_core.workers.artifacts import DescriptorError, validate_descriptor

_GOOD = {
    "host": "pare-bench",
    "path": "/mnt/bench-store/router-b/fw-0001.bin",
    "size": 2147483648,
    "sha256": "a" * 64,
    "hashed_at": 1757160000.0,
    "media_type": "application/octet-stream",
}


def test_a_well_formed_descriptor_passes_through():
    out = validate_descriptor(dict(_GOOD), worker="hardware", tool="dump_firmware")
    assert out["path"] == _GOOD["path"]
    assert out["size"] == _GOOD["size"]


@pytest.mark.parametrize("missing", ["host", "path", "size", "sha256"])
def test_a_missing_required_field_is_rejected(missing):
    """A tool that declares `artifact` and returns something else is a contract
    violation, recorded as an error rather than silently treated as a result."""
    payload = dict(_GOOD)
    del payload[missing]
    with pytest.raises(DescriptorError, match=missing):
        validate_descriptor(payload, worker="hardware", tool="dump_firmware")


def test_a_non_dict_payload_is_rejected():
    with pytest.raises(DescriptorError, match="not a JSON object"):
        validate_descriptor(["nope"], worker="hardware", tool="dump_firmware")


@pytest.mark.parametrize("bad", ["", "xyz", "a" * 63, "A" * 64, "g" * 64])
def test_a_malformed_sha256_is_rejected(bad):
    """Content-addressing on retrieval is the only control that survives an
    untrusted producer, so the hash has to be a hash."""
    payload = dict(_GOOD, sha256=bad)
    with pytest.raises(DescriptorError, match="sha256"):
        validate_descriptor(payload, worker="hardware", tool="dump_firmware")


@pytest.mark.parametrize("bad", [-1, "big", 1.5, None])
def test_a_non_integer_size_is_rejected(bad):
    payload = dict(_GOOD, size=bad)
    with pytest.raises(DescriptorError, match="size"):
        validate_descriptor(payload, worker="hardware", tool="dump_firmware")


def test_a_relative_path_is_rejected():
    """Containment is checked against an absolute root; a relative path cannot
    be compared against one."""
    payload = dict(_GOOD, path="fw-0001.bin")
    with pytest.raises(DescriptorError, match="absolute"):
        validate_descriptor(payload, worker="hardware", tool="dump_firmware")


def test_the_error_names_the_worker_and_tool():
    """An operator reading an audit row needs to know which tool violated the
    contract, not merely that one did."""
    with pytest.raises(DescriptorError) as e:
        validate_descriptor({}, worker="hardware", tool="dump_firmware")
    assert "hardware" in str(e.value) and "dump_firmware" in str(e.value)
```

- [ ] **Step 2: Run to verify failure**

Run: `cd /mnt/secondary/projects/agent_core && python -m pytest tests/workers/test_artifacts.py -q`
Expected: FAIL — `ImportError: cannot import name 'DescriptorError'`

- [ ] **Step 3: Write the implementation**

Append to `agent_core/agent_core/workers/artifacts.py`:

```python
import re

_SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")

_REQUIRED = ("host", "path", "size", "sha256")


class DescriptorError(ValueError):
    """A tool declared `produces: artifact` and returned something else."""


def validate_descriptor(payload, *, worker: str, tool: str) -> dict:
    """Check the SHAPE of an artifact descriptor. Not its truthfulness.

    Everything here is self-reported by the worker. This rejects a malformed
    descriptor, a confused one, and a buggy one. It does not make a hostile
    worker honest -- that is what containment (worker side) and
    content-addressing on retrieval are for.
    """
    where = f"{worker}.{tool}"
    if not isinstance(payload, dict):
        raise DescriptorError(
            f"{where} declared produces=artifact but returned "
            f"{type(payload).__name__}, not a JSON object")
    for field in _REQUIRED:
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

    return dict(payload)
```

- [ ] **Step 4: Run to verify pass**

Run: `cd /mnt/secondary/projects/agent_core && python -m pytest -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /mnt/secondary/projects/agent_core
git add agent_core/workers/artifacts.py tests/workers/test_artifacts.py
git commit -m "feat(workers): validate the shape of an artifact descriptor"
```

---

### Task 4: Slug validation

**Files:**
- Modify: `agent_core/agent_core/workers/artifacts.py`
- Test: `agent_core/tests/workers/test_artifacts.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `SLUG_RE: re.Pattern`, `validate_slug(slug: str) -> str` raising `ValueError`.

- [ ] **Step 1: Write the failing tests**

```python
# append to agent_core/tests/workers/test_artifacts.py
from agent_core.workers.artifacts import validate_slug


@pytest.mark.parametrize("ok", ["router-b", "a", "proj_2", "a" * 64,
                                "0target", "fw-dump_2026"])
def test_a_legal_slug_passes(ok):
    assert validate_slug(ok) == ok


@pytest.mark.parametrize("bad", [
    "proj/../../etc",     # traversal — the reason fullmatch is used
    "../etc",
    "a/b",
    "-rf",                # a leading dash is argument injection into scp/tar
    "--checkpoint-action=exec=sh",
    "_leading",           # ArcticBase requires a leading alphanumeric
    "UPPER",
    "has space",
    "a" * 65,             # ArcticBase caps at 64
    "",
    "Ünïcode",
])
def test_an_illegal_slug_is_rejected(bad):
    with pytest.raises(ValueError, match="slug"):
        validate_slug(bad)


def test_the_rule_matches_arcticbase_exactly():
    """The slug names a workbench, a capture store and a directory on the bench
    drive. ArcticBase is the strictest consumer, so its rule is the shared one:
    a project accepted here but rejected there would silently have no
    workbench."""
    from agent_core.workers.artifacts import SLUG_RE
    assert SLUG_RE.pattern == r"[a-z0-9][a-z0-9_-]{0,63}"


def test_a_non_string_is_rejected():
    with pytest.raises(ValueError, match="slug"):
        validate_slug(None)
```

- [ ] **Step 2: Run to verify failure**

Run: `cd /mnt/secondary/projects/agent_core && python -m pytest tests/workers/test_artifacts.py -q`
Expected: FAIL — `ImportError: cannot import name 'validate_slug'`

- [ ] **Step 3: Write the implementation**

Append to `agent_core/agent_core/workers/artifacts.py`:

```python
SLUG_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
"""ArcticBase's own rule, verbatim (its storage layer enforces the anchored
form). Adopted rather than invented because the slug names three things -- a
workbench, a capture store and a directory on the bench drive -- and the
strictest consumer has to win. A project accepted here but rejected there
would silently have no approval or report surface at all.

Note `fullmatch` below, not `match`: an unanchored check accepts
`proj/../../etc`, which is the single most common way this is written wrong.
The leading-alphanumeric requirement is what keeps a slug out of
argument-injection range in the scp/tar commands an operator later runs by
hand -- `-rf` and `--checkpoint-action=exec=sh` are legal directory names.
"""


def validate_slug(slug) -> str:
    """The project slug supplied to a worker. Raises ValueError if unusable."""
    if not isinstance(slug, str) or not SLUG_RE.fullmatch(slug):
        raise ValueError(
            f"invalid project slug {slug!r}: must match {SLUG_RE.pattern!r} "
            f"(lowercase, starts alphanumeric, max 64 chars)")
    return slug
```

- [ ] **Step 4: Run to verify pass**

Run: `cd /mnt/secondary/projects/agent_core && python -m pytest -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /mnt/secondary/projects/agent_core
git add agent_core/workers/artifacts.py tests/workers/test_artifacts.py
git commit -m "feat(workers): validate the project slug against ArcticBase's rule"
```

---

### Task 5: `WorkerSpec` gains the artifact root, and stops ignoring typos

**Files:**
- Modify: `agent_core/agent_core/workers/types.py:59` (the `WorkerSpec` class)
- Test: `agent_core/tests/workers/test_types.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `WorkerSpec.artifact_root: str | None`, `WorkerSpec.artifact_drive_id: str | None`, and `model_config = ConfigDict(extra="forbid")` on `WorkerSpec`.

**Note for the implementer:** `extra="forbid"` is a behaviour change for every consumer. `WorkerSpec`'s own docstring currently warns that pydantic's default `extra="ignore"` silently drops unknown keys. That was tolerable for `autoload`; it is **not** tolerable for `artifact_root`, which is a security control — a dropped root would mean an unvalidated descriptor. Verify PARE's real `workers.yaml` still loads (Step 4).

- [ ] **Step 1: Write the failing tests**

```python
# append to agent_core/tests/workers/test_types.py
import pytest
from pydantic import ValidationError

from agent_core.workers.types import WorkerSpec


def _spec(**kw):
    base = dict(name="hardware", transport="stdio", command="/bin/true",
                risk_default="high")
    base.update(kw)
    return WorkerSpec(**base)


def test_artifact_root_defaults_to_none():
    assert _spec().artifact_root is None
    assert _spec().artifact_drive_id is None


def test_artifact_root_must_be_absolute():
    """Containment is checked against this root; a relative path cannot be
    compared against one."""
    with pytest.raises(ValidationError, match="absolute"):
        _spec(artifact_root="bench-store")


def test_a_typo_in_workers_yaml_is_an_error_not_a_silent_drop():
    """pydantic's default extra='ignore' meant an older agent_core silently
    dropped a key it did not know. For autoload that was a documented
    annoyance; for artifact_root it would mean a security control absent with
    no error anywhere."""
    with pytest.raises(ValidationError):
        _spec(artifact_roots="/mnt/bench-store")


def test_the_real_workers_yaml_still_loads():
    """extra='forbid' is a behaviour change for every consumer. This is the
    canary: PARE's live catalog must still parse."""
    from pathlib import Path

    from agent_core.workers.registry import WorkerRegistry

    live = Path("/mnt/secondary/projects/PARE/workers.yaml")
    if not live.is_file():
        pytest.skip("PARE checkout not present next to agent_core")
    reg = WorkerRegistry.load(live)
    assert reg.all(), "the live catalog parsed to nothing"
```

- [ ] **Step 2: Run to verify failure**

Run: `cd /mnt/secondary/projects/agent_core && python -m pytest tests/workers/test_types.py -q`
Expected: FAIL — `AttributeError: 'WorkerSpec' object has no attribute 'artifact_root'`

- [ ] **Step 3: Write the implementation**

In `agent_core/agent_core/workers/types.py`, add `ConfigDict` to the pydantic import, then inside `class WorkerSpec` add the config line immediately after the docstring and the two fields after `read_timeout`:

```python
class WorkerSpec(BaseModel):
    """A single worker entry from workers.yaml."""

    model_config = ConfigDict(extra="forbid")
    """An unknown key is an ERROR, not a silent drop.

    pydantic's default extra="ignore" meant an older agent_core reading a
    newer workers.yaml quietly discarded keys it did not understand. That was
    a documented annoyance for `autoload`. It is not acceptable for
    `artifact_root`, which is a security control: a dropped root would leave
    descriptor containment silently unenforced with no error anywhere.
    """

    # ... existing fields ...

    artifact_root: str | None = None
    """Absolute directory on the WORKER's machine under which that worker may
    write artifacts. Operator-declared here, in workers.yaml, because the trust
    anchor has to be the file the worker cannot touch -- the same reasoning as
    the risk pins.

    None means the worker may not produce artifacts at all: a
    produces="artifact" dispatch against a worker with no root is REFUSED
    rather than accepted unvalidated.
    """

    artifact_drive_id: str | None = None
    """Expected contents of `{artifact_root}/.bench-store-id`.

    os.path.ismount() cannot tell one project's removable drive from another's,
    so writing a dump to the wrong stick would otherwise be silent.
    """

    @field_validator("artifact_root")
    @classmethod
    def artifact_root_is_absolute(cls, v: str | None) -> str | None:
        if v is not None and not v.startswith("/"):
            raise ValueError(
                f"artifact_root must be an absolute path, got {v!r}")
        return v
```

- [ ] **Step 4: Run tests, and confirm the live catalog still parses**

Run: `cd /mnt/secondary/projects/agent_core && python -m pytest -q`
Expected: PASS

Run: `python -m pytest -q`
Expected: PASS — this is the real check that `extra="forbid"` broke no consumer.

- [ ] **Step 5: Commit**

```bash
cd /mnt/secondary/projects/agent_core
git add agent_core/workers/types.py tests/workers/test_types.py
git commit -m "feat(workers): artifact_root on WorkerSpec, and forbid unknown keys"
```

---

### Task 6: Capture `produces` on discovery, monotonically

**Files:**
- Modify: `agent_core/agent_core/workers/risk_pool.py:338-341` (inside `list_tools`), plus the `__init__` state and `_bump`
- Test: `agent_core/tests/workers/test_produces_ratchet.py` (new)

**Interfaces:**
- Consumes: `agent_core.workers.artifacts.PRODUCES_META_KEY`, `PRODUCES_ARTIFACT`.
- Produces: `RiskAwareToolPool.produces(worker: str, tool: str) -> str` returning `"result"` or `"artifact"`.

- [ ] **Step 1: Write the failing tests**

```python
# agent_core/tests/workers/test_produces_ratchet.py
"""`produces` must ratchet the way the risk tier does.

_tier_highwater is deliberately never cleared by _bump(), because a reload
would otherwise be a downgrade channel. The same argument applies here: if a
reload could turn `artifact` back into `result`, a worker could disable
descriptor validation by reconnecting.
"""
import pytest

from agent_core.workers.artifacts import PRODUCES_META_KEY
from agent_core.workers.audit import AuditLog
from agent_core.workers.client_pool import MCPClientPool
from agent_core.workers.risk import RiskGate
from agent_core.workers.risk_pool import RiskAwareToolPool
from agent_core.workers.tool_approval import ToolApprovalRegistry
from agent_core.workers.types import WorkerSpec


class _Tool:
    def __init__(self, name, produces=None):
        self.name = name
        self.meta = {} if produces is None else {PRODUCES_META_KEY: produces}


class _Listing:
    def __init__(self, tools):
        self.tools = tools


class _Inner(MCPClientPool):
    def __init__(self, specs, listing):
        super().__init__(list(specs))
        self._listing = listing

    async def list_tools(self, worker):
        return self._listing


def _pool(tmp_path, listing):
    spec = WorkerSpec(name="hardware", transport="stdio", command="/bin/true",
                      risk_default="high", artifact_root="/mnt/bench-store")
    return RiskAwareToolPool(
        inner=_Inner([spec], listing), specs={"hardware": spec},
        risk_gate=RiskGate(overrides=[]),
        approval_registry=ToolApprovalRegistry(), audit_log=AuditLog(tmp_path))


async def test_an_undeclared_tool_produces_a_result(tmp_path):
    pool = _pool(tmp_path, _Listing([_Tool("read_uart")]))
    await pool.list_tools("hardware")
    assert pool.produces("hardware", "read_uart") == "result"


async def test_a_declared_tool_produces_an_artifact(tmp_path):
    pool = _pool(tmp_path, _Listing([_Tool("dump_firmware", "artifact")]))
    await pool.list_tools("hardware")
    assert pool.produces("hardware", "dump_firmware") == "artifact"


async def test_artifact_never_downgrades_to_result(tmp_path):
    """The ratchet. A worker that reconnects advertising `result` for a tool
    previously seen as `artifact` must not thereby switch off descriptor
    validation."""
    pool = _pool(tmp_path, _Listing([_Tool("dump_firmware", "artifact")]))
    await pool.list_tools("hardware")
    assert pool.produces("hardware", "dump_firmware") == "artifact"

    pool._inner._listing = _Listing([_Tool("dump_firmware", "result")])
    pool._bump("hardware")
    await pool.list_tools("hardware")
    assert pool.produces("hardware", "dump_firmware") == "artifact", (
        "a reload downgraded produces, which would disable descriptor "
        "validation for that tool")


async def test_an_unrecognised_value_is_treated_as_result_at_dispatch(tmp_path):
    """Build-time conformance rejects it (Task 7). At dispatch the safe
    reading is `result`: an unrecognised value must not be taken as a licence
    to skip validation."""
    pool = _pool(tmp_path, _Listing([_Tool("weird", "ARTIFACT")]))
    await pool.list_tools("hardware")
    assert pool.produces("hardware", "weird") == "result"


async def test_an_unknown_tool_produces_a_result(tmp_path):
    pool = _pool(tmp_path, _Listing([]))
    assert pool.produces("hardware", "never-seen") == "result"
```

- [ ] **Step 2: Run to verify failure**

Run: `cd /mnt/secondary/projects/agent_core && python -m pytest tests/workers/test_produces_ratchet.py -q`
Expected: FAIL — `AttributeError: 'RiskAwareToolPool' object has no attribute 'produces'`

- [ ] **Step 3: Write the implementation**

In `risk_pool.py`, import the constants at the top:

```python
from agent_core.workers.artifacts import (PRODUCES_ARTIFACT, PRODUCES_META_KEY,
                                          PRODUCES_RESULT)
```

In `__init__`, beside `self._tier_highwater`:

```python
        # Never evicted, for the same reason _tier_highwater is not: a reload
        # must not become a channel for turning descriptor validation off.
        self._produces_highwater: dict[tuple[str, str], str] = {}
```

In `list_tools`, immediately after the existing `self._tier_highwater[...] = hw` block and inside the same `try`:

```python
                produces = (meta.get(PRODUCES_META_KEY)
                            if isinstance(meta, dict) else None)
                if produces == PRODUCES_ARTIFACT:
                    self._produces_highwater[(worker, name)] = PRODUCES_ARTIFACT
```

Add the accessor next to `generation()`:

```python
    def produces(self, worker: str, tool: str) -> str:
        """What this tool returns: "result" or "artifact".

        Ratcheted and never evicted. An unrecognised or absent declaration
        reads as "result" at dispatch -- an unrecognised value must not be a
        licence to skip descriptor validation. Build-time conformance is what
        rejects it outright.
        """
        return self._produces_highwater.get((worker, tool), PRODUCES_RESULT)
```

**Do not** clear `_produces_highwater` in `_bump()`.

- [ ] **Step 4: Run to verify pass**

Run: `cd /mnt/secondary/projects/agent_core && python -m pytest -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /mnt/secondary/projects/agent_core
git add agent_core/workers/risk_pool.py tests/workers/test_produces_ratchet.py
git commit -m "feat(workers): ratchet produces on discovery, never downgrade"
```

---

### Task 7: Conformance rejects an unrecognised `produces`

**Files:**
- Modify: `agent_core/agent_core/workers/conformance.py` (beside `_assert_valid_risk_tier_meta` at `:46`, and its two call sites)
- Test: `agent_core/tests/workers/test_conformance_produces.py` (new)

**Interfaces:**
- Consumes: `agent_core.workers.artifacts.PRODUCES_META_KEY`, `VALID_PRODUCES`.
- Produces: `_assert_valid_produces_meta(tool: Any) -> None`.

- [ ] **Step 1: Write the failing tests**

```python
# agent_core/tests/workers/test_conformance_produces.py
"""Build-time enforcement, matching how the risk tier is handled.

_assert_valid_risk_tier_meta is described in conformance.py as "the
compensating control that lets dispatch fall back to the risk_default floor
without a runtime fail-safe". `produces` has the same shape: dispatch falls
back to "result", so something has to reject a typo before it ships.
"""
import pytest

from agent_core.workers.artifacts import PRODUCES_META_KEY
from agent_core.workers.conformance import _assert_valid_produces_meta


class _Tool:
    def __init__(self, name, meta):
        self.name = name
        self.meta = meta


def test_absent_is_valid_because_result_is_the_default():
    _assert_valid_produces_meta(_Tool("read_uart", {}))
    _assert_valid_produces_meta(_Tool("read_uart", None))


@pytest.mark.parametrize("value", ["result", "artifact"])
def test_the_two_declared_values_are_valid(value):
    _assert_valid_produces_meta(_Tool("t", {PRODUCES_META_KEY: value}))


@pytest.mark.parametrize("bad", ["ARTIFACT", "Artifact", "artifacts", "file",
                                 "", 1, True, ["artifact"]])
def test_anything_else_fails_the_build(bad):
    with pytest.raises(AssertionError, match="produces"):
        _assert_valid_produces_meta(_Tool("dump_firmware", {PRODUCES_META_KEY: bad}))


def test_the_message_names_the_tool_and_the_bad_value():
    with pytest.raises(AssertionError) as e:
        _assert_valid_produces_meta(_Tool("dump_firmware",
                                          {PRODUCES_META_KEY: "ARTIFACT"}))
    assert "dump_firmware" in str(e.value) and "ARTIFACT" in str(e.value)
```

- [ ] **Step 2: Run to verify failure**

Run: `cd /mnt/secondary/projects/agent_core && python -m pytest tests/workers/test_conformance_produces.py -q`
Expected: FAIL — `ImportError: cannot import name '_assert_valid_produces_meta'`

- [ ] **Step 3: Write the implementation**

In `conformance.py`, add the import:

```python
from agent_core.workers.artifacts import PRODUCES_META_KEY, VALID_PRODUCES
```

and the function directly below `_assert_valid_risk_tier_meta`:

```python
def _assert_valid_produces_meta(tool: Any) -> None:
    """Assert a live tool's `produces` declaration is one this daemon knows.

    Absent is valid and means "result". An unrecognised value is rejected at
    build/test time for the same reason the risk tier is: dispatch falls back
    to the safe reading, so nothing at runtime would ever surface the typo --
    a tool meaning to declare `artifact` and writing `ARTIFACT` would silently
    stream its file contents as a tool result.
    """
    meta = getattr(tool, "meta", None) or {}
    if not isinstance(meta, dict) or PRODUCES_META_KEY not in meta:
        return
    produces = meta[PRODUCES_META_KEY]
    assert produces in VALID_PRODUCES, (
        f"tool {getattr(tool, 'name', tool)!r} advertises "
        f"{PRODUCES_META_KEY!r}={produces!r} in _meta, which is not one of "
        f"{VALID_PRODUCES}"
    )
```

Then call it from both live suites, immediately after each existing
`_assert_valid_risk_tier_meta(tool)` call:

```python
            _assert_valid_produces_meta(tool)
```

- [ ] **Step 4: Run to verify pass**

Run: `cd /mnt/secondary/projects/agent_core && python -m pytest -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /mnt/secondary/projects/agent_core
git add agent_core/workers/conformance.py tests/workers/test_conformance_produces.py
git commit -m "feat(conformance): reject an unrecognised produces at build time"
```

---

### Task 8: Worker-side containment

**Files:**
- Modify: `pare-worker-kit/src/pare_worker_kit/artifacts.py`
- Test: `pare-worker-kit/tests/test_artifacts.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `ArtifactPathError(ValueError)`, and
  `artifact_path(root: str, slug: str, name: str) -> str` returning the
  contained absolute path, raising `ArtifactPathError` on escape.

**Note for the implementer:** this is the control that the daemon *cannot*
perform. The daemon has no view of the worker's filesystem, so
`Path.resolve()` daemon-side would resolve against the wrong namespace —
worse than not checking. Symlink resolution has to happen here, on the machine
that owns the files.

- [ ] **Step 1: Write the failing tests**

```python
# append to pare-worker-kit/tests/test_artifacts.py
import os
import pytest

from pare_worker_kit import ArtifactPathError, artifact_path


def test_a_normal_name_lands_under_root_and_slug(tmp_path):
    root = str(tmp_path)
    out = artifact_path(root, "router-b", "fw-0001.bin")
    assert out == os.path.join(root, "router-b", "fw-0001.bin")


@pytest.mark.parametrize("bad", ["../escape", "a/../../escape", "/etc/shadow",
                                 "sub/dir/file", "..", "."])
def test_a_name_that_escapes_is_refused(bad):
    """The worker constructs the path; the daemon supplies only a slug. A name
    containing a separator or a traversal is a bug or an attack, never a
    legitimate artifact name."""
    with pytest.raises(ArtifactPathError):
        artifact_path("/mnt/bench-store", "router-b", bad)


@pytest.mark.parametrize("bad", ["../other", "a/b", "/abs", ""])
def test_a_slug_that_escapes_is_refused(bad):
    with pytest.raises(ArtifactPathError):
        artifact_path("/mnt/bench-store", bad, "fw.bin")


def test_a_symlinked_project_directory_is_refused(tmp_path):
    """The case the daemon cannot check. A slug directory that is a symlink
    pointing outside the root would put every artifact for that project
    somewhere the operator did not authorise -- and `scp` follows symlinks."""
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "router-b").symlink_to(outside)
    with pytest.raises(ArtifactPathError, match="symlink"):
        artifact_path(str(root), "router-b", "fw.bin")


def test_an_existing_symlinked_artifact_is_refused(tmp_path):
    """A descriptor naming /etc/shadow via a symlink inside the root would turn
    the worker into a file-exfiltration primitive against its own host, because
    the operator will later act on that path."""
    root = tmp_path / "root"
    (root / "router-b").mkdir(parents=True)
    (root / "router-b" / "fw.bin").symlink_to("/etc/hostname")
    with pytest.raises(ArtifactPathError, match="symlink"):
        artifact_path(str(root), "router-b", "fw.bin")


def test_a_relative_root_is_refused():
    with pytest.raises(ArtifactPathError, match="absolute"):
        artifact_path("bench-store", "router-b", "fw.bin")
```

- [ ] **Step 2: Run to verify failure**

Run: `cd /mnt/secondary/projects/pare-worker-kit && python -m pytest tests/test_artifacts.py -q`
Expected: FAIL — `ImportError: cannot import name 'ArtifactPathError'`

- [ ] **Step 3: Write the implementation**

Append to `pare-worker-kit/src/pare_worker_kit/artifacts.py`:

```python
import os
import re

SLUG_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
"""The same rule agent_core states, for the same reason the produces key is
stated twice: the two packages never share a Python environment. A guard test
on each side keeps them equal. If they drift, a slug the daemon accepts is one
the worker refuses, and hardware tools stop working with no useful error."""

_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


class ArtifactPathError(ValueError):
    """A path that would have escaped the operator-declared artifact root."""


def artifact_path(root: str, slug: str, name: str) -> str:
    """Build a contained path for an artifact, refusing anything that escapes.

    THIS CHECK CANNOT BE DONE BY THE DAEMON. The daemon runs on another
    machine and has no view of this filesystem, so resolving symlinks there
    would resolve against the wrong namespace -- worse than not checking at
    all. It has to happen here, on the machine that owns the files.

    Be honest about what it buys: this defends against a buggy worker, a
    confused path and a hostile *client*. It does not make a hostile worker
    honest -- nothing running on the worker can. The control that survives an
    untrusted producer is content-addressing: the operator verifies sha256
    after transfer and refuses on mismatch.
    """
    if not root.startswith("/"):
        raise ArtifactPathError(f"artifact root must be absolute, got {root!r}")
    if not SLUG_RE.fullmatch(slug or ""):
        raise ArtifactPathError(f"invalid project slug {slug!r}")
    if not _NAME_RE.fullmatch(name or ""):
        raise ArtifactPathError(
            f"invalid artifact name {name!r}: no separators or traversal")

    project = os.path.join(root, slug)
    # lstat, not stat: stat follows the link and would report the TARGET's
    # type, so a symlinked project directory pointing at / would look like a
    # perfectly ordinary directory.
    if os.path.lexists(project) and os.path.islink(project):
        raise ArtifactPathError(
            f"project directory {project!r} is a symlink; refusing to write "
            f"artifacts through it")

    path = os.path.join(project, name)
    if os.path.lexists(path) and os.path.islink(path):
        raise ArtifactPathError(
            f"artifact path {path!r} is a symlink; refusing to write through "
            f"it, because the operator will later act on this path")

    # Belt and braces: normalise and confirm containment, so a future edit to
    # the regexes above cannot quietly reopen traversal.
    if os.path.commonpath([os.path.normpath(path), os.path.normpath(root)]) \
            != os.path.normpath(root):
        raise ArtifactPathError(f"{path!r} escapes artifact root {root!r}")
    return path
```

Export `ArtifactPathError` and `artifact_path` from `__init__.py` and add both to `__all__`.

- [ ] **Step 4: Run to verify pass, on both Pythons**

Run: `cd /mnt/secondary/projects/pare-worker-kit && python -m pytest -q`
Expected: PASS

Run the 3.10 floor too, because the kit ships to whatever Python a worker machine has:
`cd /mnt/secondary/projects/pare-worker-kit && ~/.local/share/uv/python/cpython-3.10-linux-x86_64-gnu/bin/python3.10 -m pytest -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /mnt/secondary/projects/pare-worker-kit
git add src/pare_worker_kit/artifacts.py src/pare_worker_kit/__init__.py tests/test_artifacts.py
git commit -m "feat: contained artifact paths, enforced where the files actually are"
```

---

## Done when

- `pytest -q` green in `agent_core`, `pare-worker-kit` and `PARE`.
- `pare-worker-kit` still declares `mcp` as its only dependency.
- A clean-venv install of the kit still works (`pip install -e .` in a fresh venv, then import with no source tree on the path) — the check that caught the uninstallable-worker bug previously.

## Not in this plan

Deliberately, because they belong to later steps and would make this one untestable on its own:

- Wiring `produces` into dispatch so a descriptor is actually validated and a missing `artifact_root` refuses the call. That needs the PARE-side publisher and belongs with it.
- Adding `produces=` to the three existing workers' `ToolSpec` dataclasses and their `server.py` meta dicts (step 2 of the spec) — including `pare-mitm-mcp/tests/test_server.py`, which asserts `tool.meta == {RISK_TIER_META_KEY: spec.risk_tier}` and must become a subset assertion.
- Everything ArcticBase.
