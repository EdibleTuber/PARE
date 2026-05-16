# PARE Phase 1: Scaffold + apk-re-agents Wrapper Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up the PARE daemon as a working `agent_core@v1.2.0` consumer with one production tool (`static_analyze`) that wraps the existing `apk_re_agents` `/jobs` HTTP API, ready to serve a conversational session and run end-to-end against a fixture APK.

**Architecture:** Scaffold the PARE Python package from `~/Projects/agent_template/` using its `init-agent.sh` script (which renames `pare` → `pare`, derives `PareAgent` and `PARE_` env prefix). Bump the agent_core pin to `v1.2.0`. Wire the framework's `RetrievalClient` to read PAL's vault (`~/pal-vault-prod`). Add a single new tool — `static_analyze` — as an async HTTP wrapper around `apk_re_agents` with submit-then-poll semantics. Carry the agent_template's existing systemd unit forward (already renamed by init); add a `/health` socket command.

**Tech Stack:** Python 3.12, `agent_core@v1.2.0`, `httpx` (already a transitive dep), Pydantic v2 (now an explicit agent_core dep), pytest + pytest-asyncio. Bash for the init scaffold step.

**Working directory:** `~/Projects/PARE/` throughout.

---

## File Structure (post-Phase-1)

```
~/Projects/PARE/
  pare/
    __init__.py            # placeholder, from template
    __main__.py            # daemon entry point (run_daemon); from template, edited to wire RetrievalClient
    agent.py               # PareAgent class; from template, edited to add the static_analyze tool
    config.py              # PAREConfig subclass of BaseConfig with the apk_re_agents URL + vault path
    prompts/
      system.md            # PARE system prompt (RE operator framing)
    commands/
      __init__.py
      hello.py             # from template, kept as a smoke command
    tools/
      __init__.py          # exports StaticAnalyze
      static_analyze.py    # the apk_re_agents wrapper Tool
      _http.py             # internal HTTP client for /jobs (kept private; one place to mock)
  systemd/
    pare-daemon.service    # renamed by init from pare-daemon.service
  scripts/                 # init-agent.sh removed by init; folder may still exist or get cleaned
  tests/
    test_smoke.py          # from template — import + instantiation + commands
    test_static_analyze.py # new — HTTP client + Tool behaviour against a mocked httpx server
  pyproject.toml           # template with agent_core pin updated to v1.2.0 + httpx dep added
  README.md                # template, "Before Init" block stripped
  docs/                    # existing — spec, plans (don't touch in this phase)
  .env.example             # config knobs documented
```

---

## Setup

### Task 0: Verify prerequisites

**Files:** none (read-only)

- [ ] **Step 1: Confirm clean PARE working tree**

Run:
```bash
cd /home/edible/Projects/PARE
git status
```
Expected: `working tree clean`, on branch `main`. If there are uncommitted changes from elsewhere, STOP and report.

- [ ] **Step 2: Confirm agent_template is local and untouched**

Run:
```bash
ls /home/edible/Projects/agent_template/pare/agent.py
ls /home/edible/Projects/agent_template/scripts/init-agent.sh
grep -F 'pare' /home/edible/Projects/agent_template/pyproject.toml >/dev/null && echo "template placeholders intact" || echo "template already initialised"
```
Expected: agent.py + init-agent.sh exist; placeholders intact. If "template already initialised", STOP and report — we need the un-init'd template.

- [ ] **Step 3: Confirm agent_core@v1.2.0 reachable on origin**

Run:
```bash
git ls-remote --tags https://github.com/EdibleTuber/agent_core.git v1.2.0
```
Expected: one line with the tag SHA. If empty, STOP — Phase 0 wasn't fully published.

- [ ] **Step 4: Confirm apk_re_agents repo is local**

Run:
```bash
ls /home/edible/Projects/apk_re_agents/docker-compose.yml >/dev/null && echo "ok"
```
Expected: `ok`. If missing, STOP — Phase 1 can't run the end-of-phase smoke test.

- [ ] **Step 5: Create the feature branch**

Run:
```bash
cd /home/edible/Projects/PARE
git checkout -b phase1-scaffold-and-static
```
Expected: switched to new branch.

---

## Section A: Scaffold from agent_template

### Task 1: Copy template files into PARE

**Files:** many — populated from `~/Projects/agent_template/`

- [ ] **Step 1: Copy template content (excluding `.git`) into PARE**

Run:
```bash
cd /home/edible/Projects/PARE
# Copy everything except .git and any cached files.
rsync -av --exclude='.git' --exclude='__pycache__' --exclude='*.egg-info' --exclude='.venv' \
  /home/edible/Projects/agent_template/ ./
```
Expected: files appear under `pare/`, `scripts/`, `systemd/`, `tests/`, plus `pyproject.toml` and `README.md`. The existing `docs/` directory is preserved (rsync overlays, doesn't delete).

- [ ] **Step 2: Confirm placeholders are intact in the copied files**

Run:
```bash
grep -lF 'pare' .
```
Expected: at least `pyproject.toml`, `systemd/pare-daemon.service`, `README.md`. If none, the rsync didn't pull placeholders — investigate.

- [ ] **Step 3: Stage and commit the raw template**

```bash
cd /home/edible/Projects/PARE
git add pare scripts systemd tests pyproject.toml README.md
# .env.example may exist in template; add if so:
[ -f .env.example ] && git add .env.example
git commit -m "chore: import agent_template scaffold (pre-init)

Raw template copied into PARE; placeholders still intact. Next commit
runs init-agent.sh to substitute pare, pare,
PareAgent, PARE, and Personal Agentic Reverse Engineer — conversational mobile RE operator.."
```

### Task 2: Run init-agent.sh

**Files:** rename `pare/` → `pare/`; rename systemd unit file; substitute placeholders.

- [ ] **Step 1: Run the init script with name "pare"**

The script will prompt for an agent description. Use exactly: `Personal Agentic Reverse Engineer — conversational mobile RE operator.`

Run:
```bash
cd /home/edible/Projects/PARE
echo "Personal Agentic Reverse Engineer — conversational mobile RE operator." | ./scripts/init-agent.sh pare --no-git
```

`--no-git` is important — we don't want the script to commit on its own; we'll commit ourselves in Step 3.

Expected stdout (substring): `agent_pkg=pare`, `AGENT_CLASS=PareAgent`, `AGENT_PREFIX=PARE`. Script ends by removing itself.

- [ ] **Step 2: Confirm the rename and substitution worked**

Run:
```bash
ls pare/agent.py
ls systemd/pare-daemon.service
grep -F 'pare' pyproject.toml && echo "PLACEHOLDERS REMAIN — investigate" || echo "all placeholders substituted"
grep "^name = " pyproject.toml
grep "class PareAgent" pare/agent.py
```
Expected: `pare/agent.py` exists; `systemd/pare-daemon.service` exists; "all placeholders substituted"; `name = "pare"`; `class PareAgent(Agent):`.

- [ ] **Step 3: Stage and commit the post-init state**

```bash
cd /home/edible/Projects/PARE
git add -u  # rename of pare/ → pare/ and the deleted scripts/init-agent.sh
git add pare systemd pyproject.toml README.md
git status  # confirm only the rename + substitutions; no untracked stray files (check .env.example etc.)
git commit -m "chore: run agent_template init for 'pare'

Substituted pare → pare, pare → pare,
PareAgent → PareAgent, PARE → PARE_,
Personal Agentic Reverse Engineer — conversational mobile RE operator. → Personal Agentic Reverse Engineer …
Renamed systemd/pare-daemon.service → systemd/pare-daemon.service.
Removed scripts/init-agent.sh (one-shot)."
```

### Task 3: Bump agent_core pin, install deps, run smoke tests

**Files:**
- Modify: `pyproject.toml` (bump `agent_core` pin; add `httpx`)

- [ ] **Step 1: Bump agent_core pin to v1.2.0**

Edit `pyproject.toml`. Find the dependency line:
```toml
"agent_core @ git+https://github.com/EdibleTuber/agent_core.git@v0.7.0",
```
Change `v0.7.0` to `v1.2.0`.

Also add `httpx>=0.27.0` to the dependencies array (it's used by Task 7 and not declared by the template) and add `pyyaml>=6.0` (used by agent_core's worker registry but PARE may also load workers.yaml directly in later phases — declaring it now is cheap):

```toml
dependencies = [
    "agent_core @ git+https://github.com/EdibleTuber/agent_core.git@v1.2.0",
    "httpx>=0.27.0",
    "pyyaml>=6.0",
]
```

- [ ] **Step 2: Create venv and install dev deps**

Run:
```bash
cd /home/edible/Projects/PARE
python -m venv .venv
.venv/bin/pip install -e ".[dev]" 2>&1 | tail -5
```
Expected: `Successfully installed agent_core-1.2.0 pare-0.1.0` (and transitive deps). No errors.

- [ ] **Step 3: Run the template's smoke tests**

```bash
.venv/bin/pytest -v 2>&1 | tail -15
```
Expected: 3 tests pass (`test_import`, `test_instantiation`, `test_commands_registered` or similar). The template ships these as a "did init work" check. If any fail, the init missed a substitution — investigate before committing.

- [ ] **Step 4: Commit the pin bump and venv-verified state**

```bash
git add pyproject.toml
git commit -m "chore: pin agent_core@v1.2.0 + declare httpx, pyyaml

Bumps from the template's default v0.7.0 to the current published
release. Adds httpx (used by the static_analyze HTTP client in Task 7)
and pyyaml (used by agent_core's worker registry; declared now since
PARE will load workers.yaml in later phases)."
```

---

## Section B: Vault Retrieval Wiring

### Task 4: Add PAREConfig with vault + apk_re_agents URLs

**Files:**
- Create: `pare/config.py`
- Test: `tests/test_config.py`

PARE's config extends `agent_core.config.BaseConfig` and adds the apk_re_agents endpoint, vault path, and inference/retrieval URLs (mirroring the env-var pattern PAL uses).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_config.py`:
```python
"""Tests for PAREConfig — env-driven configuration."""
import os
from pathlib import Path

import pytest

from pare.config import PAREConfig


def test_config_defaults(monkeypatch, tmp_path):
    # Clear any PARE_ env so defaults take effect.
    for key in list(os.environ):
        if key.startswith("PARE_"):
            monkeypatch.delenv(key, raising=False)

    cfg = PAREConfig.from_env()
    # Defaults exist for every required field.
    assert cfg.inference_url.startswith("http")
    assert cfg.apk_re_agents_url.startswith("http")
    assert cfg.vault_path.endswith("pal-vault-prod") or "vault" in cfg.vault_path.lower()


def test_config_reads_env(monkeypatch):
    monkeypatch.setenv("PARE_INFERENCE_URL", "http://example.invalid:11434")
    monkeypatch.setenv("PARE_APK_RE_AGENTS_URL", "http://example.invalid:8000")
    monkeypatch.setenv("PARE_VAULT_PATH", "/tmp/example-vault")
    monkeypatch.setenv("PARE_MODEL", "test-model")
    monkeypatch.setenv("PARE_COLLECTION_ID", "test-collection")

    cfg = PAREConfig.from_env()
    assert cfg.inference_url == "http://example.invalid:11434"
    assert cfg.apk_re_agents_url == "http://example.invalid:8000"
    assert cfg.vault_path == "/tmp/example-vault"
    assert cfg.model == "test-model"
    assert cfg.collection_id == "test-collection"
```

- [ ] **Step 2: Verify the test fails**

Run: `.venv/bin/pytest tests/test_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'pare.config'`.

- [ ] **Step 3: Implement PAREConfig**

Create `pare/config.py`:
```python
"""PARE configuration. Subclasses agent_core's BaseConfig and adds the
PARE-specific endpoints (apk_re_agents URL, vault path).

All settings use env vars with the `PARE_` prefix. Defaults assume a
local lab setup: the inference manager on the GPU server, apk_re_agents
on localhost (per its v1 hardening), and PAL's vault as the read source.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from agent_core.config import BaseConfig


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default)


@dataclass
class PAREConfig(BaseConfig):
    """Per-deployment configuration for PARE.

    Fields are populated from PARE_* environment variables in `from_env`.
    """
    inference_url: str = "http://192.168.1.14:11434"
    model: str = "gemma-4-26b-a4b-it-q4_k_m"
    vault_path: str = "/home/edible/pal-vault-prod"
    socket_path: str = ""  # XDG_RUNTIME_DIR/pare.sock; set by from_env
    collection_id: str = "vault"
    apk_re_agents_url: str = "http://127.0.0.1:8000"

    @classmethod
    def from_env(cls) -> "PAREConfig":
        xdg = os.environ.get("XDG_RUNTIME_DIR", "/tmp")
        return cls(
            inference_url=_env("PARE_INFERENCE_URL", "http://192.168.1.14:11434"),
            model=_env("PARE_MODEL", "gemma-4-26b-a4b-it-q4_k_m"),
            vault_path=_env("PARE_VAULT_PATH", "/home/edible/pal-vault-prod"),
            socket_path=_env("PARE_SOCKET_PATH", f"{xdg}/pare.sock"),
            collection_id=_env("PARE_COLLECTION_ID", "vault"),
            apk_re_agents_url=_env("PARE_APK_RE_AGENTS_URL", "http://127.0.0.1:8000"),
        )
```

NOTE: If `agent_core.config.BaseConfig` already declares any of these fields (e.g., `inference_url`, `model`), inheriting will conflict. Read `~/Projects/agent_core/agent_core/config.py` first; if so, drop the duplicated fields from PAREConfig and only declare PARE-specific ones (`vault_path`, `apk_re_agents_url`). Adjust `from_env` accordingly. Report what you found.

- [ ] **Step 4: Verify tests pass**

Run: `.venv/bin/pytest tests/test_config.py -v`
Expected: both PASS.

- [ ] **Step 5: Run full suite**

Run: `.venv/bin/pytest -q 2>&1 | tail -3`
Expected: 5 passed (3 smoke + 2 config) or similar.

- [ ] **Step 6: Commit**

```bash
git add pare/config.py tests/test_config.py
git commit -m "feat(config): add PAREConfig with env-driven defaults

Subclass of agent_core.config.BaseConfig adding apk_re_agents_url and
vault_path. All settings read from PARE_* env vars in from_env; defaults
target the local lab (inference manager LAN IP, 127.0.0.1 for the
static-analysis backend, ~/pal-vault-prod for the vault)."
```

### Task 5: Wire __main__.py to use PAREConfig and PareAgent

**Files:**
- Modify: `pare/__main__.py`

The agent_template's `__main__.py` calls `run_daemon(agent, config)`. We need it to instantiate `PAREConfig.from_env()` and pass it through. The exact shape depends on what the template generated.

- [ ] **Step 1: Read the current __main__.py**

```bash
cat /home/edible/Projects/PARE/pare/__main__.py
```

Look for where the agent and config are constructed. Common pattern:
```python
from agent_core.runtime import run_daemon
from pare.agent import PareAgent

def main():
    agent = PareAgent()
    run_daemon(agent)
```

- [ ] **Step 2: Update __main__.py to pass PAREConfig**

If `run_daemon` takes a `config` argument (check `agent_core/runtime.py` `run_daemon` signature), pass `PAREConfig.from_env()`. If `run_daemon` reads config off the agent itself (e.g., `agent.config`), set `agent.config = PAREConfig.from_env()` before passing the agent.

Target shape:
```python
"""PARE daemon entry point."""
from __future__ import annotations

import asyncio

from agent_core.runtime import run_daemon

from pare.agent import PareAgent
from pare.config import PAREConfig


def main() -> None:
    config = PAREConfig.from_env()
    agent = PareAgent(config=config)
    asyncio.run(run_daemon(agent))


if __name__ == "__main__":
    main()
```

Read `agent_core/runtime.py` to confirm the actual `run_daemon` signature — adapt the call accordingly. If `PareAgent` doesn't accept `config=` in its constructor (the template's might not), either add it to PareAgent's `__init__` or set it as an attribute after construction.

- [ ] **Step 3: Run smoke tests to confirm the daemon still instantiates**

Run: `.venv/bin/pytest -q 2>&1 | tail -3`
Expected: same test count as before passes. The template's smoke test instantiates the agent; if our config wiring broke that, it'll fail here.

- [ ] **Step 4: Commit**

```bash
git add pare/__main__.py pare/agent.py
git commit -m "feat(daemon): wire PAREConfig.from_env into the daemon entry

__main__.py now constructs PAREConfig.from_env() and threads it into
the agent + run_daemon. Smoke tests still pass against the wired
configuration."
```

---

## Section C: static_analyze Tool

### Task 6: HTTP client for apk_re_agents

**Files:**
- Create: `pare/tools/_http.py`
- Test: `tests/test_apk_re_agents_client.py`

A thin async HTTP wrapper around `apk_re_agents`'s `/jobs` API. Lives in `pare/tools/_http.py` (underscore prefix = internal). One place to mock for testing.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_apk_re_agents_client.py`:
```python
"""Tests for the apk_re_agents HTTP client."""
import json

import httpx
import pytest

from pare.tools._http import ApkReAgentsClient, JobResult


@pytest.mark.asyncio
async def test_submit_returns_job_id(httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url="http://test.invalid/jobs",
        status_code=202,
        json={"job_id": "abc-123", "state": "pending"},
    )
    client = ApkReAgentsClient("http://test.invalid")
    job_id = await client.submit_job(apk_path="/work/input/sample.apk")
    assert job_id == "abc-123"
    await client.close()


@pytest.mark.asyncio
async def test_get_status_returns_state(httpx_mock):
    httpx_mock.add_response(
        method="GET",
        url="http://test.invalid/jobs/abc-123",
        json={"job_id": "abc-123", "state": "running", "current_stage": "parallel_analysis"},
    )
    client = ApkReAgentsClient("http://test.invalid")
    status = await client.get_status("abc-123")
    assert status["state"] == "running"
    assert status["current_stage"] == "parallel_analysis"
    await client.close()


@pytest.mark.asyncio
async def test_wait_for_completion_polls_until_done(httpx_mock):
    """submit + poll until state=completed."""
    httpx_mock.add_response(
        method="POST",
        url="http://test.invalid/jobs",
        status_code=202,
        json={"job_id": "j1", "state": "pending"},
    )
    # Two "running" then one "completed".
    for state in ("running", "running", "completed"):
        response = {"job_id": "j1", "state": state}
        if state == "completed":
            response["results"] = {"manifest_analyzer": "/work/findings/j1/manifest_analyzer.json"}
        httpx_mock.add_response(
            method="GET",
            url="http://test.invalid/jobs/j1",
            json=response,
        )

    client = ApkReAgentsClient("http://test.invalid")
    result = await client.run_to_completion(
        apk_path="/work/sample.apk", poll_interval_s=0.01, timeout_s=5.0
    )
    assert isinstance(result, JobResult)
    assert result.job_id == "j1"
    assert result.state == "completed"
    assert "manifest_analyzer" in result.results
    await client.close()


@pytest.mark.asyncio
async def test_wait_for_completion_raises_on_failed(httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url="http://test.invalid/jobs",
        status_code=202,
        json={"job_id": "j2", "state": "pending"},
    )
    httpx_mock.add_response(
        method="GET",
        url="http://test.invalid/jobs/j2",
        json={"job_id": "j2", "state": "failed"},
    )
    client = ApkReAgentsClient("http://test.invalid")
    with pytest.raises(RuntimeError, match="failed"):
        await client.run_to_completion(
            apk_path="/work/sample.apk", poll_interval_s=0.01, timeout_s=5.0
        )
    await client.close()
```

NOTE: `pytest-httpx` provides the `httpx_mock` fixture. If it's not in the dev deps, install it now and add to pyproject:
```bash
.venv/bin/pip install pytest-httpx
# Then edit pyproject.toml [project.optional-dependencies] dev to add "pytest-httpx>=0.30".
```

- [ ] **Step 2: Verify tests fail**

Run: `.venv/bin/pytest tests/test_apk_re_agents_client.py -v 2>&1 | tail -10`
Expected: 4 tests FAIL with `ModuleNotFoundError: pare.tools._http` (or `pytest_httpx` not available, in which case fix the dev dep).

- [ ] **Step 3: Implement the HTTP client**

Create `pare/tools/_http.py`:
```python
"""HTTP client for apk_re_agents — internal to the static_analyze tool.

Wraps the legacy /jobs HTTP API with async submit + poll semantics:
    submit_job(apk_path)              → job_id
    get_status(job_id)                → status dict
    run_to_completion(apk_path, …)    → JobResult (submit + poll until done)

The wrapper does no schema enrichment beyond the API's own shape; the
tool layer (static_analyze.py) is responsible for translating into the
agent_core worker contract.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class JobResult:
    """Result of a completed apk_re_agents job."""
    job_id: str
    state: str
    results: dict[str, str]  # agent_name -> findings file path


class ApkReAgentsClient:
    """Thin async client for apk_re_agents /jobs HTTP API."""

    def __init__(self, base_url: str, *, timeout_s: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=timeout_s)

    async def close(self) -> None:
        await self._client.aclose()

    async def submit_job(self, *, apk_path: str) -> str:
        """POST /jobs. Returns the job_id."""
        resp = await self._client.post(
            f"{self.base_url}/jobs",
            json={"apk_path": apk_path},
        )
        resp.raise_for_status()
        return resp.json()["job_id"]

    async def get_status(self, job_id: str) -> dict[str, Any]:
        """GET /jobs/{job_id}. Returns the raw status dict."""
        resp = await self._client.get(f"{self.base_url}/jobs/{job_id}")
        resp.raise_for_status()
        return resp.json()

    async def run_to_completion(
        self,
        *,
        apk_path: str,
        poll_interval_s: float = 5.0,
        timeout_s: float = 1800.0,
    ) -> JobResult:
        """Submit a job then poll until state ∈ {completed, failed} or timeout.

        Raises:
            RuntimeError: if the job reports state="failed" or polling times out.
        """
        job_id = await self.submit_job(apk_path=apk_path)
        deadline = asyncio.get_event_loop().time() + timeout_s
        while True:
            status = await self.get_status(job_id)
            state = status.get("state", "")
            if state == "completed":
                return JobResult(
                    job_id=job_id,
                    state=state,
                    results=status.get("results") or {},
                )
            if state == "failed":
                raise RuntimeError(
                    f"apk_re_agents job {job_id} failed: {status}"
                )
            if asyncio.get_event_loop().time() > deadline:
                raise RuntimeError(
                    f"apk_re_agents job {job_id} timed out after {timeout_s}s "
                    f"in state {state!r}"
                )
            await asyncio.sleep(poll_interval_s)
```

- [ ] **Step 4: Verify tests pass**

Run: `.venv/bin/pytest tests/test_apk_re_agents_client.py -v 2>&1 | tail -8`
Expected: 4 PASS.

- [ ] **Step 5: Full suite**

Run: `.venv/bin/pytest -q 2>&1 | tail -3`
Expected: 9 passed (3 smoke + 2 config + 4 http).

- [ ] **Step 6: Commit**

```bash
git add pare/tools/_http.py tests/test_apk_re_agents_client.py pyproject.toml
git commit -m "feat(tools): add apk_re_agents HTTP client

Internal async wrapper over the existing /jobs REST API:
submit_job, get_status, run_to_completion. The wrapper does no schema
enrichment beyond the API's own shape; the static_analyze tool layer
translates into the agent_core worker contract."
```

### Task 7: `static_analyze` Tool

**Files:**
- Create: `pare/tools/static_analyze.py`
- Create: `pare/tools/__init__.py` (or modify if it exists post-init)
- Test: `tests/test_static_analyze.py`

The `static_analyze` Tool wraps `ApkReAgentsClient.run_to_completion` and exposes it via the agent_core Tool framework. Risk tier: `low` (matches §5.4 of the design).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_static_analyze.py`:
```python
"""Tests for the static_analyze Tool."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from pare.tools.static_analyze import StaticAnalyze
from pare.tools._http import JobResult


@pytest.mark.asyncio
async def test_static_analyze_returns_findings_summary():
    """Happy path: the tool returns a summary string with the job id and a
    list of agent_name → findings file path entries."""
    tool = StaticAnalyze()

    # Mock the agent context: the tool reads ctx.agent.apk_re_agents_client
    # (a long-lived client constructed in PareAgent.setup()).
    fake_client = MagicMock()
    fake_client.run_to_completion = AsyncMock(return_value=JobResult(
        job_id="abc-123",
        state="completed",
        results={
            "manifest_analyzer": "/work/findings/abc-123/manifest_analyzer.json",
            "string_extractor": "/work/findings/abc-123/string_extractor.json",
        },
    ))

    ctx = MagicMock()
    ctx.agent.apk_re_agents_client = fake_client

    result = await tool.run({"apk_path": "/work/input/sample.apk"}, ctx)

    assert "abc-123" in result
    assert "manifest_analyzer" in result
    assert "string_extractor" in result
    fake_client.run_to_completion.assert_awaited_once_with(
        apk_path="/work/input/sample.apk"
    )


@pytest.mark.asyncio
async def test_static_analyze_surfaces_failure_as_string():
    """When the job fails, the tool returns a descriptive error string
    (not raises) — Tool runs are expected to surface errors to the LLM
    as text per agent_core convention."""
    tool = StaticAnalyze()
    fake_client = MagicMock()
    fake_client.run_to_completion = AsyncMock(side_effect=RuntimeError(
        "apk_re_agents job j1 failed: {'state': 'failed'}"
    ))
    ctx = MagicMock()
    ctx.agent.apk_re_agents_client = fake_client

    result = await tool.run({"apk_path": "/work/input/bad.apk"}, ctx)
    assert "failed" in result.lower()


def test_static_analyze_tool_metadata():
    """Tool exposes name, description, parameters."""
    assert StaticAnalyze.name == "static_analyze"
    assert "apk" in StaticAnalyze.description.lower()
    schema = StaticAnalyze.parameters
    assert schema["type"] == "object"
    assert "apk_path" in schema["properties"]
    assert "apk_path" in schema.get("required", [])
```

- [ ] **Step 2: Verify tests fail**

Run: `.venv/bin/pytest tests/test_static_analyze.py -v 2>&1 | tail -8`
Expected: 3 tests FAIL with `ModuleNotFoundError: pare.tools.static_analyze`.

- [ ] **Step 3: Implement the Tool**

Create `pare/tools/static_analyze.py`:
```python
"""static_analyze: agent_core Tool wrapping apk_re_agents /jobs.

Risk tier: low. The wrapper submits an APK to the apk_re_agents
coordinator, polls until completion, and returns a summary of the
findings paths. Findings remain on the apk_re_agents shared volume;
the LLM gets a reference, not the raw content. Reading the actual
findings is a separate operation (Phase 1+ may add `read_findings`
shortcuts; for v1 the operator inspects findings out-of-band).
"""
from __future__ import annotations

from typing import Any, ClassVar

from agent_core.tools.base import Tool


class StaticAnalyze(Tool):
    """Submit an APK to apk_re_agents and return findings refs on completion."""

    name: ClassVar[str] = "static_analyze"
    description: ClassVar[str] = (
        "Submit an APK file to the apk_re_agents static-analysis pipeline "
        "and wait for the job to complete. Returns a summary listing the "
        "findings file paths per analyser (manifest_analyzer, "
        "string_extractor, network_mapper, code_analyzer, api_extractor, "
        "report_synthesizer). Use this as the first step on a new APK "
        "before dynamic analysis. Path must be reachable by the "
        "apk_re_agents coordinator's shared volume (typically "
        "/work/input/<apk>)."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "apk_path": {
                "type": "string",
                "description": "Absolute path to the APK on the apk_re_agents shared volume.",
            },
        },
        "required": ["apk_path"],
    }
    requires: ClassVar[tuple[str, ...]] = ("apk_re_agents_client",)

    async def run(self, args: dict[str, Any], ctx: Any) -> str:
        apk_path = args["apk_path"]
        client = ctx.agent.apk_re_agents_client
        try:
            result = await client.run_to_completion(apk_path=apk_path)
        except RuntimeError as exc:
            return f"static_analyze failed: {exc}"
        if not result.results:
            return (
                f"static_analyze completed (job {result.job_id}) but reported "
                "no findings. Check apk_re_agents logs."
            )
        lines = [f"static_analyze completed (job {result.job_id}). Findings:"]
        for analyser, path in sorted(result.results.items()):
            lines.append(f"  {analyser}: {path}")
        return "\n".join(lines)
```

- [ ] **Step 4: Ensure `pare/tools/__init__.py` exports the Tool**

Check the current state:
```bash
cat /home/edible/Projects/PARE/pare/tools/__init__.py
```
The template may have shipped an empty `__init__.py`. Update it (or create) to:
```python
"""PARE tool exports."""
from pare.tools.static_analyze import StaticAnalyze

__all__ = ["StaticAnalyze"]
```

- [ ] **Step 5: Verify tests pass**

Run: `.venv/bin/pytest tests/test_static_analyze.py -v 2>&1 | tail -8`
Expected: 3 PASS.

- [ ] **Step 6: Full suite**

Run: `.venv/bin/pytest -q 2>&1 | tail -3`
Expected: 12 passed (3 smoke + 2 config + 4 http + 3 tool).

- [ ] **Step 7: Commit**

```bash
git add pare/tools/static_analyze.py pare/tools/__init__.py tests/test_static_analyze.py
git commit -m "feat(tools): add static_analyze Tool wrapping apk_re_agents

Submits an APK to the existing /jobs pipeline, polls until completion,
and returns a summary of findings paths. Risk tier: low. The client
is read from ctx.agent.apk_re_agents_client (constructed in
PareAgent.setup, next task)."
```

### Task 8: Register static_analyze on PareAgent + construct the client in setup()

**Files:**
- Modify: `pare/agent.py`

- [ ] **Step 1: Read the current pare/agent.py**

```bash
cat /home/edible/Projects/PARE/pare/agent.py
```

The template generated:
- `class PareAgent(Agent)` with `tools = []`, `commands = [Hello]`.
- A `setup()` placeholder.

We need to:
1. Import `StaticAnalyze` from `pare.tools`.
2. Add `StaticAnalyze` to the `tools` ClassVar.
3. In `setup()`, construct `ApkReAgentsClient(self.config.apk_re_agents_url)` and assign to `self.apk_re_agents_client`.

- [ ] **Step 2: Update pare/agent.py**

Add the imports at the top (after the existing `from agent_core.agent import Agent, HandlerContext`):
```python
from pare.tools import StaticAnalyze
from pare.tools._http import ApkReAgentsClient
```

Change the `tools = []` line to:
```python
    tools = [StaticAnalyze]
```

Replace the `setup()` placeholder with:
```python
    def setup(self) -> None:
        """Construct the apk_re_agents client (long-lived; reused per call).

        Framework managers (profile, wisdom, channels, inference, retrieval,
        websearch, allowlist, approval_registry, learning, fetcher, config)
        are already populated on self at this point.
        """
        self.apk_re_agents_client = ApkReAgentsClient(self.config.apk_re_agents_url)
```

- [ ] **Step 3: Add an `apk_re_agents_client` attribute test**

Append to `tests/test_smoke.py` (the template's existing smoke test file):
```python
def test_static_analyze_registered():
    from pare.agent import PareAgent
    from pare.tools import StaticAnalyze
    assert StaticAnalyze in PareAgent.tools
```

- [ ] **Step 4: Run smoke tests**

Run: `.venv/bin/pytest tests/test_smoke.py -v 2>&1 | tail -8`
Expected: the existing template tests + the new `test_static_analyze_registered` all PASS. If the template's instantiation test fails because `setup()` now does work that requires `self.config`, that means PareAgent isn't being constructed with config in the template's smoke fixture — adapt the smoke test to pass a config (e.g., `PareAgent(config=PAREConfig.from_env())`) or stub `apk_re_agents_url`.

- [ ] **Step 5: Full suite**

Run: `.venv/bin/pytest -q 2>&1 | tail -3`
Expected: 13 passed (12 + 1 new smoke).

- [ ] **Step 6: Commit**

```bash
git add pare/agent.py tests/test_smoke.py
git commit -m "feat(agent): register static_analyze + construct apk_re_agents client

PareAgent.tools now includes StaticAnalyze. setup() constructs a
long-lived ApkReAgentsClient bound to the configured URL; tools read
it via ctx.agent.apk_re_agents_client at call time. Smoke test verifies
the registration."
```

---

## Section D: Health endpoint + systemd

### Task 9: Add `/health` style status command

**Files:**
- Modify: `pare/agent.py` (or a new `pare/commands/status.py` — pick the cleaner spot)

The spec says `/health` lives on the daemon socket. The agent_core daemon already has a protocol layer (`agent_core.protocol`). We expose health as a slash-command `/health` that returns daemon info — agent name, version, configured endpoints, inference reachability.

- [ ] **Step 1: Write a failing test**

Create `tests/test_health.py`:
```python
"""Tests for the /health slash command."""
import pytest

from pare.commands.health import Health


@pytest.mark.asyncio
async def test_health_returns_status_lines(monkeypatch):
    """The /health command returns a short multi-line status string."""
    # Mock context with a minimal agent shape.
    class FakeConfig:
        inference_url = "http://example.invalid:11434"
        model = "gemma-test"
        vault_path = "/tmp/nowhere"
        apk_re_agents_url = "http://127.0.0.1:8000"

    class FakeAgent:
        config = FakeConfig()
        name = "pare"

    class FakeCtx:
        agent = FakeAgent()

    cmd = Health()
    out = await cmd.run({}, FakeCtx())
    assert "pare" in out
    assert "inference" in out.lower()
    assert "apk_re_agents" in out.lower() or "static" in out.lower()
    assert FakeConfig.model in out
```

- [ ] **Step 2: Verify the test fails**

Run: `.venv/bin/pytest tests/test_health.py -v`
Expected: FAIL with `ModuleNotFoundError: pare.commands.health`.

- [ ] **Step 3: Implement the command**

Create `pare/commands/health.py`:
```python
"""/health — daemon status command."""
from __future__ import annotations

from typing import Any, ClassVar

from agent_core.commands.base import Command


class Health(Command):
    """Report PARE daemon status: agent name, model, configured endpoints."""

    name: ClassVar[str] = "health"
    description: ClassVar[str] = "Show PARE daemon status and configured endpoints."

    async def run(self, args: dict[str, Any], ctx: Any) -> str:
        cfg = ctx.agent.config
        lines = [
            f"agent: {ctx.agent.name}",
            f"model: {cfg.model}",
            f"inference: {cfg.inference_url}",
            f"vault: {cfg.vault_path}",
            f"apk_re_agents: {cfg.apk_re_agents_url}",
        ]
        return "\n".join(lines)
```

NOTE: If `agent_core.commands.base.Command` has a different base signature, adapt. The template's `Hello` command at `pare/commands/hello.py` is the canonical example — read it first to mirror the shape exactly.

- [ ] **Step 4: Register the command on PareAgent**

Edit `pare/agent.py`. Add import:
```python
from pare.commands.health import Health
```
Change `commands = [Hello]` to `commands = [Hello, Health]`.

- [ ] **Step 5: Run tests**

Run: `.venv/bin/pytest -q 2>&1 | tail -3`
Expected: 14 passed.

- [ ] **Step 6: Commit**

```bash
git add pare/commands/health.py pare/agent.py tests/test_health.py
git commit -m "feat(commands): add /health daemon status command

Returns agent name, model, configured inference / vault /
apk_re_agents endpoints. Operator and watchdog tools can issue
\"/health\" over the unix socket to verify the daemon is up and
configured as expected."
```

### Task 10: Systemd unit final pass + .env.example

**Files:**
- Modify: `systemd/pare-daemon.service`
- Modify: `.env.example` (if it exists; create if not)

The init script already renamed and substituted placeholders. This task does a final sanity pass on the unit and writes a `.env.example` documenting all PARE_* env vars.

- [ ] **Step 1: Read the unit file**

```bash
cat /home/edible/Projects/PARE/systemd/pare-daemon.service
```

Verify:
- `WorkingDirectory` is a sensible placeholder (operator will edit).
- `ExecStart` references `pare-daemon` or `python -m pare`.
- `EnvironmentFile` points to a path (typical: `/etc/pare/pare.env` or operator-local).

- [ ] **Step 2: Add a brief comment noting operator-edit points**

If the unit doesn't already have a header explaining what to edit per-host, add one. Example top-of-file:
```
# pare-daemon.service — PARE daemon systemd unit (user service).
#
# Operator edits before `systemctl --user enable --now pare-daemon`:
#   - WorkingDirectory: path to the PARE checkout
#   - EnvironmentFile: path to the PARE env file
```

- [ ] **Step 3: Write/update .env.example**

Create or update `.env.example` with all configurable knobs:
```bash
# PARE configuration. Copy to .env and edit for your host.
#
# Inference manager (the llama-manager / OpenAI-compatible endpoint).
PARE_INFERENCE_URL=http://192.168.1.14:11434
PARE_MODEL=gemma-4-26b-a4b-it-q4_k_m

# PAL vault (read-only).
PARE_VAULT_PATH=/home/edible/pal-vault-prod
PARE_COLLECTION_ID=vault

# apk_re_agents static-analysis backend.
PARE_APK_RE_AGENTS_URL=http://127.0.0.1:8000

# Daemon socket (defaults to $XDG_RUNTIME_DIR/pare.sock).
# PARE_SOCKET_PATH=
```

- [ ] **Step 4: Commit**

```bash
git add systemd/pare-daemon.service .env.example
git commit -m "chore(systemd): document operator edits + add .env.example

Header comment in pare-daemon.service points operators at the per-host
fields they must edit. .env.example enumerates every PARE_* env var
the daemon reads, with default values matching PAREConfig.from_env."
```

---

## Section E: Phase Exit Verification

### Task 11: End-to-end smoke against apk_re_agents

**Files:**
- Create: `tests/test_phase1_smoke.py` (env-gated integration test, like the agent_core reasoning smoke)

This is the phase-exit check: with apk_re_agents running and PARE installed, can the daemon's `static_analyze` tool actually drive the pipeline against a fixture APK?

- [ ] **Step 1: Identify a fixture APK**

apk_re_agents was tested against an Ergatta rower APK. Check what's available:
```bash
ls /home/edible/Projects/apk_re_agents/shared_volume/input/ 2>/dev/null || ls /home/edible/Projects/apk_re_agents/ 2>/dev/null
```
If a fixture APK is checked in to apk_re_agents, note its path. If not, the operator will provide one at runtime via `PARE_PHASE1_SMOKE_APK_PATH`.

- [ ] **Step 2: Write the env-gated smoke test**

Create `tests/test_phase1_smoke.py`:
```python
"""Phase 1 end-to-end smoke: PARE → apk_re_agents → findings ref.

Requires apk_re_agents to be running (docker compose up) and a fixture
APK reachable by the coordinator's shared volume.

Enable with:
    PARE_PHASE1_SMOKE_APK_PATH=/work/input/sample.apk \\
    PARE_PHASE1_SMOKE_AGENTS_URL=http://127.0.0.1:8000 \\
    pytest tests/test_phase1_smoke.py -v
"""
import os

import pytest

from pare.tools._http import ApkReAgentsClient


APK_PATH = os.getenv("PARE_PHASE1_SMOKE_APK_PATH")
AGENTS_URL = os.getenv("PARE_PHASE1_SMOKE_AGENTS_URL")


pytestmark = pytest.mark.skipif(
    not (APK_PATH and AGENTS_URL),
    reason="set PARE_PHASE1_SMOKE_APK_PATH and PARE_PHASE1_SMOKE_AGENTS_URL to run",
)


@pytest.mark.asyncio
async def test_static_analyze_against_real_apk_re_agents():
    """Submit a real APK; wait up to 30 min for completion; verify findings
    paths come back."""
    client = ApkReAgentsClient(AGENTS_URL)
    try:
        result = await client.run_to_completion(
            apk_path=APK_PATH,
            poll_interval_s=5.0,
            timeout_s=1800.0,
        )
        assert result.state == "completed"
        assert result.results, "expected at least one analyser to produce findings"
        # Spot-check that manifest analyser ran (it's deterministic, no LLM
        # variance in whether it reports findings).
        assert "manifest_analyzer" in result.results
    finally:
        await client.close()
```

- [ ] **Step 3: Verify it skips without env vars**

Run: `.venv/bin/pytest tests/test_phase1_smoke.py -v`
Expected: 1 SKIPPED.

- [ ] **Step 4 (optional, operator-driven): run against a live apk_re_agents**

Operator brings up apk_re_agents:
```bash
cd /home/edible/Projects/apk_re_agents
docker compose up -d
# Copy a fixture APK into the coordinator's shared volume:
docker compose cp some-app.apk coordinator:/work/input/some-app.apk
```

Then from PARE:
```bash
cd /home/edible/Projects/PARE
PARE_PHASE1_SMOKE_APK_PATH=/work/input/some-app.apk \
PARE_PHASE1_SMOKE_AGENTS_URL=http://127.0.0.1:8000 \
.venv/bin/pytest tests/test_phase1_smoke.py -v
```

This is operator-driven; don't block Task 11 completion on it. The skipped test passing at least once before merging Phase 1 is the phase-exit criterion.

- [ ] **Step 5: Commit**

```bash
git add tests/test_phase1_smoke.py
git commit -m "test(phase1): env-gated end-to-end smoke against apk_re_agents

Skipped in CI. Operator runs locally once apk_re_agents is up to
verify PARE's static_analyze can actually drive the pipeline against
a fixture APK and receive findings refs. Phase 1 exits when this
passes against the operator's chosen APK."
```

### Task 12: README polish + final push

**Files:**
- Modify: `README.md`
- Push: `phase1-scaffold-and-static` branch

The template's README had a "Before Init" block that the init script stripped, and placeholder fields the script substituted. Verify it now describes PARE correctly.

- [ ] **Step 1: Skim README.md**

```bash
head -50 /home/edible/Projects/PARE/README.md
```
Confirm:
- Title is "PARE" (not "PareAgent" or "Agent Template").
- Install instructions reference `pare-daemon`.
- No stray placeholders.

- [ ] **Step 2: Add a short header section pointing to the spec + Phase 0 / Phase 1 plans**

Insert near the top (after the title and one-line description):
```markdown
## Design & Plans

- Design spec: [`docs/superpowers/specs/2026-05-12-pare-v1-design.md`](docs/superpowers/specs/2026-05-12-pare-v1-design.md)
- Phase 0 (agent_core extraction): [`docs/superpowers/plans/2026-05-13-phase0-agent-core-extraction.md`](docs/superpowers/plans/2026-05-13-phase0-agent-core-extraction.md) — landed in `agent_core@v1.2.0`
- Phase 1 (this scaffold + apk_re_agents wrapper): [`docs/superpowers/plans/2026-05-14-phase1-pare-scaffold-and-static-wrapper.md`](docs/superpowers/plans/2026-05-14-phase1-pare-scaffold-and-static-wrapper.md)
```

- [ ] **Step 3: Commit the README polish**

```bash
git add README.md
git commit -m "docs(readme): point at the spec + Phase 0 / Phase 1 plans"
```

- [ ] **Step 4: Push the feature branch**

```bash
git push -u origin phase1-scaffold-and-static
```
Expected: branch pushed with tracking. The branch is ready for review / merge to main when the operator decides.

---

## Phase Exit Verification

- [ ] All PARE tests pass: `cd /home/edible/Projects/PARE && .venv/bin/pytest -q` — expect ~14 passed + 1 skipped (the phase1 smoke).
- [ ] The daemon imports and starts (it doesn't need to be running long; just verify no import errors):
  ```bash
  cd /home/edible/Projects/PARE
  .venv/bin/python -c "from pare.__main__ import main; print('ok')"
  ```
- [ ] The operator runs the env-gated phase1 smoke test against a live apk_re_agents at least once and it passes (see Task 11 Step 4).
- [ ] `phase1-scaffold-and-static` is pushed to origin and ready for review.

Phase 1 is complete when this checklist is green. Phase 2 (Android worker scaffold + container hardening) is the next deliverable.
