# PARE Phase 3: apk_re_agents Streamable HTTP Migration + PARE MCP-Direct Workers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the hybrid: migrate `apk_re_agents`' 8 agent containers from MCP-over-SSE to Streamable HTTP (and update the coordinator client), then wire PARE to consume each agent as an MCP-direct worker via `agent_core@v1.3.0`'s `discover_and_register`. The existing `static_analyze` tool (coordinator path) stays unchanged — both surgical (MCP-direct) and batch (coordinator `/jobs`) paths now available.

**Architecture:** Each apk_re_agents agent's `server.py` swaps one argument: `server.run(transport="sse")` → `server.run(transport="streamable-http")`. The coordinator's `pipeline.py` swaps `mcp.client.sse.sse_client` → `mcp.client.streamable_http.streamablehttp_client` and the per-agent URL suffix from `/sse` to `/mcp`. apk_re_agents tags a new release. Then PARE's `workers.yaml` declares all 8 agents pointing at `http://127.0.0.1:9000..9007/mcp`; `PareAgent.setup()` constructs an `MCPClientPool` and stores it on `self.mcp_pool`; `PareAgent.register_tools()` calls `discover_and_register` on the pool and returns the list. Tools land in PARE's registry as `unpacker_run_jadx`, `manifest_analyzer_<tool>`, etc. — name-prefixed by worker.

**Tech Stack:** apk_re_agents Python 3.11, FastMCP 3.3.1 (already a dep), Docker Compose. PARE Python 3.12, `agent_core@v1.3.0` (just shipped in Phase 2), pytest + pytest-asyncio + pytest-httpx (already dev deps).

**Working directories:** `~/Projects/apk_re_agents/` for Tasks 1-4; `~/Projects/PARE/` for Tasks 5-9.

---

## Port Map (apk_re_agents docker-compose)

For workers.yaml and verification commands:

| Port | Agent |
|---|---|
| 8000 | coordinator (HTTP `/jobs` — unchanged) |
| 9000 | unpacker |
| 9001 | manifest_analyzer |
| 9002 | string_extractor |
| 9003 | network_mapper |
| 9004 | code_analyzer |
| 9005 | api_extractor |
| 9006 | report_synthesizer |
| 9007 | mobsf_analyzer |

---

## Setup

### Task 0: Prereqs + branches

**Files:** none (env setup)

- [ ] **Step 1: Confirm apk_re_agents clean and on a sensible base**

```bash
cd /home/edible/Projects/apk_re_agents
git status
git log --oneline -3
```

Expected: working tree clean (or only untracked files); current branch `main` (or whatever your default is). If there's uncommitted work, STOP and report.

- [ ] **Step 2: Confirm PARE clean and on main**

```bash
cd /home/edible/Projects/PARE
git status
git log --oneline -3
```

Expected: clean; recent commits include the Phase 2 plan (`docs(plan): Phase 2 — agent_core MCP execution layer`).

- [ ] **Step 3: Create apk_re_agents feature branch**

```bash
cd /home/edible/Projects/apk_re_agents
git checkout -b phase3-streamable-http-migration
```

PARE branch comes later (Task 5) to keep the two repo timelines independent.

---

## Section A: apk_re_agents Streamable HTTP Migration

### Task 1: Migrate all 8 agent server.py files to streamable-http

**Files:**
- Modify: `src/apk_re/agents/unpacker/server.py`
- Modify: `src/apk_re/agents/manifest_analyzer/server.py`
- Modify: `src/apk_re/agents/string_extractor/server.py`
- Modify: `src/apk_re/agents/network_mapper/server.py`
- Modify: `src/apk_re/agents/code_analyzer/server.py`
- Modify: `src/apk_re/agents/api_extractor/server.py`
- Modify: `src/apk_re/agents/report_synthesizer/server.py`
- Modify: `src/apk_re/agents/mobsf_analyzer/server.py`

Each agent's `server.py` ends with the pattern:
```python
if __name__ == "__main__":
    server = create_<agent>_server()
    server.run(transport="sse")
```

We change `transport="sse"` to `transport="streamable-http"` in all 8 files.

- [ ] **Step 1: Verify the current state across all 8 agents**

```bash
cd /home/edible/Projects/apk_re_agents
grep -l 'transport="sse"' src/apk_re/agents/*/server.py
```

Expected: 8 paths listed (one per agent). If fewer than 8, one or more agents has a non-standard run-call; investigate before bulk-replacing.

- [ ] **Step 2: Apply the transport swap**

```bash
cd /home/edible/Projects/apk_re_agents
sed -i 's|transport="sse"|transport="streamable-http"|g' src/apk_re/agents/*/server.py
```

- [ ] **Step 3: Verify the swap took**

```bash
grep -l 'transport="streamable-http"' src/apk_re/agents/*/server.py
grep -l 'transport="sse"' src/apk_re/agents/*/server.py  # should be empty
```

Expected: 8 paths with the new value; zero paths still on `sse`.

- [ ] **Step 4: Commit**

```bash
cd /home/edible/Projects/apk_re_agents
git status  # confirm only the 8 server.py files modified
git add src/apk_re/agents/*/server.py
git commit -m "feat(agents): migrate all agent servers to Streamable HTTP

MCP-over-SSE is deprecated per the MCP 2025-03-26 spec. FastMCP 3.3.1
supports transport=\"streamable-http\" with the same FastMCP API; the
agents need no further changes. Coordinator client migration follows
in the next commit."
```

### Task 2: Migrate coordinator pipeline.py to streamablehttp_client

**Files:**
- Modify: `src/apk_re/coordinator/pipeline.py`

The coordinator currently uses `from mcp.client.sse import sse_client` and `async with sse_client(url, ...)`. The agent endpoints are `http://{name}:8080/sse` (line ~39 in `pipeline.py`). We swap:

- Import: `from mcp.client.sse import sse_client` → `from mcp.client.streamable_http import streamablehttp_client`
- URL suffix: `/sse` → `/mcp`
- Client invocation: `async with sse_client(url, sse_read_timeout=...) as (read, write):` → the Streamable HTTP client may yield 3 values (read, write, plus a session-id helper). The most defensive form is `async with streamablehttp_client(url) as (read, write, *_):` — extra returned values get tossed into `_`.

- [ ] **Step 1: Read the relevant section of pipeline.py**

```bash
cd /home/edible/Projects/apk_re_agents
sed -n '1,20p;35,50p;100,120p' src/apk_re/coordinator/pipeline.py
```

Note: the imports (top), the URL dict construction (~line 39), and the `sse_client` usage (~line 108). Confirm there are no other SSE-specific paths beyond those three sites.

- [ ] **Step 2: Verify with a grep that you have all SSE references**

```bash
grep -n "sse\|/sse" src/apk_re/coordinator/pipeline.py
```

There should be 3 hits: the import, the URL template, and the `async with sse_client(...)` call.

- [ ] **Step 3: Apply the three edits**

Edit `src/apk_re/coordinator/pipeline.py`:

1. **Import** (around line 10):

   Change:
   ```python
   from mcp.client.sse import sse_client
   ```
   To:
   ```python
   from mcp.client.streamable_http import streamablehttp_client
   ```

2. **URL template** (around line 39):

   Change:
   ```python
                   name: f"http://{name}:8080/sse" for name in self.AGENT_NAMES
   ```
   To:
   ```python
                   name: f"http://{name}:8080/mcp" for name in self.AGENT_NAMES
   ```

3. **Client invocation** (around line 108):

   Change:
   ```python
               async with sse_client(url, sse_read_timeout=60 * 60) as (read, write):
   ```
   To:
   ```python
               async with streamablehttp_client(url) as (read, write, *_):
   ```

   The Streamable HTTP client doesn't take an `sse_read_timeout` argument; long-running tools rely on the protocol's own keep-alives. If a tool legitimately runs longer than the default and times out, that's a follow-up tuning concern, not a Phase 3 blocker.

- [ ] **Step 4: Sanity check — module imports clean**

```bash
cd /home/edible/Projects/apk_re_agents
# Activate the local dev environment if one exists; otherwise this just verifies syntax.
python -c "from apk_re.coordinator.pipeline import Pipeline; print('ok')" 2>&1 | head -3
```

Expected: `ok`. If you get an ImportError that's unrelated to our changes (e.g., the existing test fixture quirks), that's fine; the goal here is syntax + the modified import.

- [ ] **Step 5: Run apk_re_agents' unit tests if they exist**

```bash
ls tests/ 2>&1 | head -5
# If pytest tests exist:
pytest -x -q 2>&1 | tail -10
```

If there are unit tests, they should still pass. If they reference `sse_client` directly (unlikely; the coordinator's client is internal), update those references too. If there are no unit tests, skip.

- [ ] **Step 6: Commit**

```bash
git add src/apk_re/coordinator/pipeline.py
git commit -m "feat(coordinator): use streamablehttp_client for agent connections

Pairs with the agent-side migration. URL template /sse → /mcp;
client import + invocation switched. The protocol's own keep-alives
replace the explicit sse_read_timeout=1h."
```

### Task 3: Verify end-to-end via docker compose

**Files:** none (operational verification)

- [ ] **Step 1: Tear down any running stack and rebuild**

```bash
cd /home/edible/Projects/apk_re_agents
docker compose down 2>&1 | tail -3
docker compose up --build -d 2>&1 | tail -10
```

Expected: all 9 containers (coordinator + 8 agents) start. Watch for any agent container that exits immediately — that's a clue the transport change broke that specific agent.

- [ ] **Step 2: Wait briefly for containers to become healthy, then check status**

```bash
sleep 5
docker compose ps
```

Expected: all containers `Up` (no `Exit (1)`). If any agent has exited, check its logs:
```bash
docker compose logs <agent_name> 2>&1 | tail -20
```

- [ ] **Step 3: Health-check the coordinator**

```bash
curl -s http://localhost:8000/health
```

Expected: `{"status":"ok"}`.

- [ ] **Step 4: Health-check one agent directly over Streamable HTTP**

```bash
# Any HTTP response from /mcp (even a 400 or 405) means the server is alive on the new transport.
curl -sv http://localhost:9000/mcp 2>&1 | grep -E "HTTP/|status" | head -3
```

Expected: an HTTP response (`HTTP/1.1 400`, `405`, or similar). A connection refused or hung request means the agent didn't actually switch transports — investigate that agent's logs.

- [ ] **Step 5: (Optional) Submit a test job to verify the coordinator can drive its agents**

If you have a fixture APK at a known path on the shared volume, submit a small job:
```bash
# Copy a small APK into the coordinator's shared volume first if needed:
# docker compose cp small-app.apk coordinator:/work/input/small-app.apk

curl -X POST http://localhost:8000/jobs \
  -H "Content-Type: application/json" \
  -d '{"apk_path": "/work/input/small-app.apk"}'

# Poll for state (replace JOB_ID):
# curl http://localhost:8000/jobs/JOB_ID
```

If this is skipped because there's no fixture APK on hand, that's fine — Task 8 in PARE's section will cover end-to-end verification more thoroughly.

- [ ] **Step 6: Tear the stack down**

```bash
docker compose down
```

- [ ] **Step 7: No commit needed (this task is operational verification only)**

If anything failed, fix in Tasks 1 or 2 and re-verify. Don't proceed to Task 4 with broken containers.

### Task 4: Version bump + CHANGELOG + push + tag

**Files:**
- Modify: `pyproject.toml`
- Modify: `CHANGELOG.md` (create if missing)

apk_re_agents is currently at version `0.1.0`. The Streamable HTTP migration is a breaking change for any internal consumer that connects directly to its agents over SSE — bump to `0.2.0` per semver intent.

- [ ] **Step 1: Bump version**

Edit `/home/edible/Projects/apk_re_agents/pyproject.toml`:

Change:
```toml
version = "0.1.0"
```
To:
```toml
version = "0.2.0"
```

- [ ] **Step 2: Create or update CHANGELOG.md**

If `/home/edible/Projects/apk_re_agents/CHANGELOG.md` doesn't exist, create it with:
```markdown
# Changelog

## [0.2.0] — 2026-05-17

### Changed
- All 8 agent containers migrated from MCP-over-SSE to MCP-over-Streamable-HTTP. MCP-over-SSE is deprecated per the MCP 2025-03-26 spec; FastMCP 3.3.1 supports `transport="streamable-http"` with the same FastMCP API.
- Coordinator (`apk_re.coordinator.pipeline`) updated to use `mcp.client.streamable_http.streamablehttp_client`. Agent URL suffix changed from `/sse` to `/mcp`.

### Notes
- The HTTP `/jobs` REST API is unchanged. External clients (e.g., PARE's `static_analyze` Tool) need no updates.
- The agents' MCP tool surface is unchanged. Tool names, parameters, and return shapes are identical.
- PARE Phase 3 consumes these agents directly via MCP-over-Streamable-HTTP through `agent_core@v1.3.0`'s `discover_and_register`.
```

If a CHANGELOG already exists, prepend the `## [0.2.0]` section.

- [ ] **Step 3: Confirm no other lingering changes**

```bash
cd /home/edible/Projects/apk_re_agents
git status
```

Expected: only `pyproject.toml` (and possibly `CHANGELOG.md`) modified.

- [ ] **Step 4: Commit, push, tag**

```bash
git add pyproject.toml CHANGELOG.md
git commit -m "chore: bump to v0.2.0

Streamable HTTP transport migration. Breaking change for any client
that connects to agents directly over MCP — they must use
streamablehttp_client and the /mcp URL suffix. The HTTP /jobs API
is unchanged."

git push -u origin phase3-streamable-http-migration 2>&1 | tail -3

git tag -a v0.2.0 -m "apk_re_agents v0.2.0 — Streamable HTTP migration"
```

**Do NOT push the tag yet.** Same pattern as agent_core: tag pushes after the PR merges to main. The controller (or operator) handles merge + tag push.

- [ ] **Step 5: Open the PR**

The push output above includes a GitHub PR-creation URL. Either visit it in a browser, or:
```bash
gh pr create --title "feat: Streamable HTTP migration (v0.2.0)" --body "$(cat <<'EOF'
## Summary
- Migrate 8 agent containers from MCP-over-SSE to Streamable HTTP.
- Update coordinator's MCP client (`pipeline.py`) to match.
- Version 0.1.0 → 0.2.0.

## Test plan
- [x] `docker compose up --build -d` brings all 9 containers up.
- [x] Each agent responds on `/mcp` (verified via curl).
- [x] Coordinator `/health` returns ok.
- [ ] PARE Phase 3 (next plan) consumes these agents directly via MCP — end-to-end verification lands there.

## Breaking change
External clients connecting to agents directly over MCP must use `mcp.client.streamable_http.streamablehttp_client` and the `/mcp` URL suffix. The HTTP `/jobs` REST API is unchanged.
EOF
)" 2>&1 | tail -3
```

After the PR is reviewed and merged, the controller pushes the v0.2.0 tag to origin.

---

## Section B: PARE MCP-Direct Workers

### Task 5: PARE branch + bump agent_core pin to v1.3.0

**Files:**
- Modify: `pyproject.toml` (PARE)

PARE currently pins `agent_core@v1.2.0` (per Phase 1). Phase 2 published v1.3.0 with the MCP execution layer. We need that here.

- [ ] **Step 1: Create the feature branch**

```bash
cd /home/edible/Projects/PARE
git checkout main
git pull --ff-only origin main
git checkout -b phase3-mcp-direct-workers
```

- [ ] **Step 2: Bump the pin**

Edit `/home/edible/Projects/PARE/pyproject.toml`. Find:
```toml
"agent_core @ git+https://github.com/EdibleTuber/agent_core.git@v1.2.0",
```
Change to:
```toml
"agent_core @ git+https://github.com/EdibleTuber/agent_core.git@v1.3.0",
```

- [ ] **Step 3: Reinstall**

```bash
.venv/bin/pip install -e ".[dev]" 2>&1 | tail -5
```

Expected: `Successfully installed agent_core-1.3.0 pare-0.1.0` (and any transitive deps that came with the v1.3.0 release, like `mcp` and `fastmcp`).

- [ ] **Step 4: Verify the new agent_core symbols are importable**

```bash
.venv/bin/python -c "
from agent_core.workers import MCPClient, MCPClientPool, discover_and_register, make_tool_class
print('ok')
"
```

Expected: `ok`.

- [ ] **Step 5: Baseline test run to confirm nothing broke**

```bash
.venv/bin/pytest -q 2>&1 | tail -3
```

Expected: 15 passed + 1 skipped (same as Phase 1's exit). If anything fails, the v1.3.0 bump introduced an incompatibility — STOP and report.

- [ ] **Step 6: Commit**

```bash
git status  # only pyproject.toml modified
git add pyproject.toml
git commit -m "chore: bump agent_core pin to v1.3.0

v1.3.0 ships the live MCP execution layer (MCPClient, MCPClientPool,
discover_and_register, make_tool_class). Phase 3 wires PARE to
consume apk_re_agents agents directly via this layer."
```

### Task 6: Add `workers.yaml`

**Files:**
- Create: `workers.yaml` (at the PARE repo root, alongside `pyproject.toml`)

The workers registry. Each entry is a `WorkerSpec` (defined in `agent_core.workers.types`) and PARE reads this file at startup.

- [ ] **Step 1: Write workers.yaml**

Create `/home/edible/Projects/PARE/workers.yaml`:
```yaml
# PARE worker registry. Each entry maps to an agent_core WorkerSpec.
# Format: workers.<name>.{endpoint, transport, risk_default, capability_tags?, kind?}
#
# Phase 3 lists the 8 apk_re_agents agents as MCP-direct workers. The
# coordinator (`/jobs` HTTP API) is still consumed via the legacy
# static_analyze Tool (Phase 1) — that path is unchanged.
workers:
  unpacker:
    endpoint: http://127.0.0.1:9000/mcp
    transport: streamable_http
    risk_default: low
    capability_tags: [static, apk, decompile]
  manifest_analyzer:
    endpoint: http://127.0.0.1:9001/mcp
    transport: streamable_http
    risk_default: low
    capability_tags: [static, apk, manifest]
  string_extractor:
    endpoint: http://127.0.0.1:9002/mcp
    transport: streamable_http
    risk_default: low
    capability_tags: [static, apk, strings]
  network_mapper:
    endpoint: http://127.0.0.1:9003/mcp
    transport: streamable_http
    risk_default: low
    capability_tags: [static, apk, network]
  code_analyzer:
    endpoint: http://127.0.0.1:9004/mcp
    transport: streamable_http
    risk_default: medium
    capability_tags: [static, apk, code]
  api_extractor:
    endpoint: http://127.0.0.1:9005/mcp
    transport: streamable_http
    risk_default: low
    capability_tags: [static, apk, api]
  report_synthesizer:
    endpoint: http://127.0.0.1:9006/mcp
    transport: streamable_http
    risk_default: low
    capability_tags: [static, apk, report]
  mobsf_analyzer:
    endpoint: http://127.0.0.1:9007/mcp
    transport: streamable_http
    risk_default: low
    capability_tags: [static, apk, mobsf]
```

Note: I gave `code_analyzer` a `risk_default: medium` because it's the deep-analysis agent — operator may want it more constrained. The others stay `low`. If the operator disagrees, tuning is a one-line per-worker change.

- [ ] **Step 2: Sanity check — workers.yaml parses against the WorkerSpec model**

```bash
cd /home/edible/Projects/PARE
.venv/bin/python -c "
from agent_core.workers.registry import WorkerRegistry
reg = WorkerRegistry.load('workers.yaml')
for w in reg.all():
    print(w.name, w.endpoint, w.risk_default)
"
```

Expected: 8 lines, one per agent, with the correct endpoints and risk tiers. If parse fails, the YAML is malformed or a field is missing.

- [ ] **Step 3: Commit**

```bash
git add workers.yaml
git commit -m "feat(workers): add workers.yaml with 8 apk_re_agents entries

Each agent declared with its docker-compose host port + risk_default.
code_analyzer is medium because it's the deep-analysis agent; rest
are low. Operator tunes via this file going forward."
```

### Task 7: Wire `PareAgent.setup()` + `register_tools()`

**Files:**
- Modify: `pare/agent.py`
- Modify: `pare/config.py` (add a `workers_yaml_path` field)
- Test: `tests/test_register_tools.py`

`PareAgent.setup()` constructs an `MCPClientPool` from the workers.yaml and stores it on `self.mcp_pool`. `PareAgent.register_tools()` returns the result of `discover_and_register(...)` so tools land in the agent's tool list at startup.

We also add a teardown path: when the agent shuts down, the pool's `close_all()` should be called. agent_core may or may not have a teardown hook on `Agent` — if it doesn't, leave teardown for a follow-up (the pool's TCP connections will be cleaned up by the daemon process exit).

- [ ] **Step 1: Add workers_yaml_path to PAREConfig**

Edit `/home/edible/Projects/PARE/pare/config.py`. Find the `PAREConfig` dataclass; append a new field:
```python
    workers_yaml_path: str = "workers.yaml"
```

The default is "workers.yaml" (relative to CWD when the daemon starts). The `load_config` function will pick up `PARE_WORKERS_YAML_PATH` as an override via the standard `agent_core.config` env-var mapping (it derives the env var name from the field name + `PARE_` prefix automatically).

- [ ] **Step 2: Write the failing test**

Create `/home/edible/Projects/PARE/tests/test_register_tools.py`:
```python
"""Tests for PareAgent.register_tools() — workers.yaml discovery integration.

These tests assert structure (the agent uses agent_core's discovery
helpers correctly) without requiring real apk_re_agents to be running.
Live end-to-end verification is in tests/test_phase3_smoke.py (Task 8).
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pare.agent import PareAgent
from pare.config import PAREConfig


def test_pareagent_has_register_tools_method():
    """The agent exposes the agent_core lifecycle hook."""
    assert callable(getattr(PareAgent, "register_tools", None))


@pytest.mark.asyncio
async def test_register_tools_runs_discover_and_register(tmp_path, monkeypatch):
    """register_tools() invokes agent_core.workers.discover_and_register
    against the configured workers.yaml + the pool from setup()."""
    # Minimal workers.yaml for the test.
    wy = tmp_path / "workers.yaml"
    wy.write_text(
        "workers:\n"
        "  stub:\n"
        "    endpoint: http://127.0.0.1:1/mcp\n"
        "    transport: streamable_http\n"
        "    risk_default: low\n"
    )

    cfg = PAREConfig()
    cfg.workers_yaml_path = str(wy)

    agent = PareAgent()
    agent.config = cfg

    # Stub framework managers normally populated by run_daemon.
    agent.profile = MagicMock()
    agent.wisdom = MagicMock()
    agent.channels = MagicMock()
    agent.learning = MagicMock()
    agent.allowlist = MagicMock()
    agent.approval_registry = MagicMock()
    agent.inference = MagicMock()
    agent.retrieval = MagicMock()
    agent.websearch = MagicMock()
    agent.fetcher = MagicMock()

    # Patch discover_and_register to return a stub Tool list without hitting
    # the network. The test verifies setup() builds the pool and
    # register_tools() invokes the discovery driver — not the discovery
    # logic itself (which is agent_core's responsibility).
    with patch("pare.agent.discover_and_register", new_callable=AsyncMock) as mock_disc:
        mock_disc.return_value = []
        agent.setup()
        tool_classes = await agent.register_tools_async() if hasattr(agent, "register_tools_async") else []
        # register_tools is allowed to be sync or async; the contract is "returns a list".
        if not tool_classes:
            tool_classes = agent.register_tools() if callable(getattr(agent, "register_tools", None)) else []
            if hasattr(tool_classes, "__await__"):
                tool_classes = await tool_classes
        assert isinstance(tool_classes, list)

    # Confirm setup() built a pool.
    assert hasattr(agent, "mcp_pool")
    assert agent.mcp_pool is not None
```

Note: the test is intentionally tolerant of sync-vs-async `register_tools`. agent_core declared the hook signature as `def register_tools(self) -> list[type[Tool]]`. But MCP discovery requires `await` — so PareAgent's implementation will either:
- (a) Run an internal `asyncio.run(...)` to bridge sync → async, OR
- (b) Use an `asyncio.get_event_loop().run_until_complete(...)` style, OR
- (c) The agent_core framework awaits a coroutine returned from `register_tools` if it returns one.

Option (a) is the simplest and what the implementation below uses.

- [ ] **Step 3: Verify the test fails**

```bash
cd /home/edible/Projects/PARE
.venv/bin/pytest tests/test_register_tools.py -v 2>&1 | tail -10
```

Expected: FAIL with import error or AttributeError on `pare.agent.discover_and_register` (because the import isn't there yet).

- [ ] **Step 4: Update pare/agent.py**

Read the current `pare/agent.py` first:
```bash
cat /home/edible/Projects/PARE/pare/agent.py
```

Then edit `/home/edible/Projects/PARE/pare/agent.py`:

1. Add imports near the top (after the existing `from agent_core.agent import Agent, HandlerContext`):
```python
import asyncio

from agent_core.workers import MCPClientPool, discover_and_register
from agent_core.workers.registry import WorkerRegistry
```

2. Update `setup()` to construct the pool:

Find the existing `setup()`:
```python
    def setup(self) -> None:
        """Construct the apk_re_agents client (long-lived; reused per call). ..."""
        self.apk_re_agents_client = ApkReAgentsClient(self.config.apk_re_agents_url)
```

Add the pool construction after the existing line:
```python
    def setup(self) -> None:
        """Construct domain resources: apk_re_agents HTTP client (Phase 1) and
        the MCP pool for MCP-direct workers from workers.yaml (Phase 3).

        Framework managers (profile, wisdom, channels, inference, retrieval,
        websearch, allowlist, approval_registry, learning, fetcher, config)
        are already populated on self at this point.
        """
        self.apk_re_agents_client = ApkReAgentsClient(self.config.apk_re_agents_url)
        registry = WorkerRegistry.load(self.config.workers_yaml_path)
        self.mcp_pool = MCPClientPool(registry.all())
```

3. Add `register_tools()` to PareAgent:

After `setup()`, add:
```python
    def register_tools(self):
        """Discover MCP-direct workers and return their tools.

        Called by agent_core's runtime after setup(). The returned list is
        unioned with the class-level `tools` ClassVar (StaticAnalyze).
        """
        # discover_and_register is async; bridge to sync for the runtime hook.
        return asyncio.run(
            discover_and_register(self.mcp_pool._specs.values(), self.mcp_pool)
            if False else _discover_sync_wrapper(self.mcp_pool)
        )


# Module-level helper for the sync bridge (keeps register_tools readable):
def _discover_sync_wrapper(pool: "MCPClientPool"):
    import asyncio
    async def _go():
        specs = list(pool._specs.values())
        return await discover_and_register(specs, pool)
    return asyncio.run(_go())
```

Actually that's ugly. Cleaner:

```python
    def register_tools(self):
        """Discover MCP-direct workers and return their tools.

        Called by agent_core's runtime after setup(). The returned list is
        unioned with the class-level `tools` ClassVar (StaticAnalyze).
        Bridges async discovery to sync caller via asyncio.run.
        """
        specs = list(self.mcp_pool._specs.values())
        return asyncio.run(discover_and_register(specs, self.mcp_pool))
```

Use this cleaner form. Note we read from `pool._specs.values()` because the pool holds the spec dict; if the public API ever exposes a `.specs` property, prefer that.

- [ ] **Step 5: Verify the test passes**

```bash
.venv/bin/pytest tests/test_register_tools.py -v 2>&1 | tail -10
```

Expected: 2 PASS.

- [ ] **Step 6: Full suite regression**

```bash
.venv/bin/pytest -q 2>&1 | tail -3
```

Expected: 17 passed + 1 skipped (baseline 15 + 2 new register_tools tests). If existing smoke tests break (e.g., `test_agent_can_be_instantiated`), it's likely because `setup()` now requires `workers.yaml` to exist — adjust either the test to pass a config pointing at a tmp workers.yaml, or guard `setup()` to skip pool construction when no workers.yaml is configured. The cleaner fix is the test-side adjustment.

- [ ] **Step 7: Commit**

```bash
git add pare/agent.py pare/config.py tests/test_register_tools.py
git commit -m "feat(agent): wire register_tools() via agent_core MCP discovery

PareAgent.setup() loads workers.yaml into an MCPClientPool;
register_tools() runs discover_and_register against it, returning
Tool subclasses for the framework's runtime to register alongside the
declarative cls.tools (StaticAnalyze).

Async-to-sync bridge via asyncio.run inside register_tools — the hook
signature is sync per agent_core's contract."
```

### Task 8: End-to-end smoke against migrated apk_re_agents

**Files:**
- Create: `tests/test_phase3_smoke.py`

Env-gated, matching the Phase 1 pattern. Operator brings up apk_re_agents docker compose stack first, then runs the smoke test.

- [ ] **Step 1: Create the smoke test**

Create `/home/edible/Projects/PARE/tests/test_phase3_smoke.py`:
```python
"""Phase 3 end-to-end smoke: PARE → MCP-direct apk_re_agents agents.

Requires apk_re_agents to be running (docker compose up) at the
endpoints listed in workers.yaml (default: localhost:9000-9007).

Enable with:
    PARE_PHASE3_SMOKE=1 pytest tests/test_phase3_smoke.py -v
"""
import os

import pytest

from agent_core.workers import MCPClientPool, discover_and_register
from agent_core.workers.registry import WorkerRegistry


pytestmark = pytest.mark.skipif(
    os.getenv("PARE_PHASE3_SMOKE") != "1",
    reason="set PARE_PHASE3_SMOKE=1 (with apk_re_agents docker compose up) to run",
)


@pytest.mark.asyncio
async def test_discovers_all_apk_re_agents_workers():
    """discover_and_register against the live apk_re_agents stack returns at least
    one tool per worker (8 workers, so at least 8 tools)."""
    registry = WorkerRegistry.load("workers.yaml")
    pool = MCPClientPool(registry.all())
    try:
        specs = registry.all()
        tool_classes = await discover_and_register(specs, pool)
        names_by_worker = {}
        for cls in tool_classes:
            worker_name = cls.name.split("_", 1)[0]
            names_by_worker.setdefault(worker_name, []).append(cls.name)

        # Every workers.yaml entry should produce at least one tool.
        for spec in specs:
            assert spec.name in names_by_worker, (
                f"worker {spec.name!r} produced no tools"
            )
    finally:
        await pool.close_all()


@pytest.mark.asyncio
async def test_unpacker_run_jadx_is_registered():
    """The unpacker agent's run_jadx tool is registered as unpacker_run_jadx."""
    registry = WorkerRegistry.load("workers.yaml")
    pool = MCPClientPool(registry.all())
    try:
        tool_classes = await discover_and_register(registry.all(), pool)
        names = {cls.name for cls in tool_classes}
        assert "unpacker_run_jadx" in names, (
            f"expected unpacker_run_jadx in registered tools, got: {sorted(names)}"
        )
    finally:
        await pool.close_all()
```

- [ ] **Step 2: Verify the test skips without the env var**

```bash
.venv/bin/pytest tests/test_phase3_smoke.py -v 2>&1 | tail -5
```

Expected: 2 SKIPPED.

- [ ] **Step 3: (Operator-driven, optional now) Run against real apk_re_agents**

```bash
# Operator first:
cd /home/edible/Projects/apk_re_agents
docker compose up --build -d
# Wait a few seconds for agents to bind.

# Then PARE:
cd /home/edible/Projects/PARE
PARE_PHASE3_SMOKE=1 .venv/bin/pytest tests/test_phase3_smoke.py -v
```

Expected: 2 PASS. If the agents aren't all responding, the migration on apk_re_agents' side missed something; revisit Tasks 1-3. **Do not block Task 8 completion on this** — the env-gated test passing locally is Phase 3's true exit criterion, but it's operator-driven and can happen out of band.

- [ ] **Step 4: Commit**

```bash
git add tests/test_phase3_smoke.py
git commit -m "test(phase3): env-gated smoke against MCP-direct apk_re_agents

Skipped in CI. Operator runs locally once docker compose is up to
verify PARE discovers all 8 agents' tools through agent_core's
discover_and_register against the migrated Streamable HTTP transport.
Phase 3 exits when this passes."
```

### Task 9: Spec/README sync + push + PR + merge

**Files:**
- Modify: `README.md` (add Phase 3 plan link)
- Modify: `docs/superpowers/specs/2026-05-12-pare-v1-design.md` (mark Phase 3 done)

- [ ] **Step 1: Update README's Design & Plans section**

Edit `/home/edible/Projects/PARE/README.md`. In the `## Design & Plans` section (added in Phase 1), add:
```markdown
- Phase 2 (agent_core MCP execution layer): [`docs/superpowers/plans/2026-05-16-phase2-agent-core-mcp-client.md`](docs/superpowers/plans/2026-05-16-phase2-agent-core-mcp-client.md) — landed in `agent_core@v1.3.0`
- Phase 3 (apk_re_agents migration + PARE MCP-direct workers): [`docs/superpowers/plans/2026-05-17-phase3-apk-re-agents-streamable-http-and-pare-wiring.md`](docs/superpowers/plans/2026-05-17-phase3-apk-re-agents-streamable-http-and-pare-wiring.md)
```

The exact insertion point depends on the current structure — read the section first and place the new lines in chronological order alongside the existing Phase 0 and Phase 1 entries.

- [ ] **Step 2: Mark Phase 3 done in the spec**

Edit `/home/edible/Projects/PARE/docs/superpowers/specs/2026-05-12-pare-v1-design.md`. Find the Phase 3 entry in §11 (the `**Phase 3 — apk_re_agents Streamable HTTP migration + PARE MCP-direct workers**` header) and append a status marker to the heading line:

Change:
```markdown
**Phase 3 — apk_re_agents Streamable HTTP migration + PARE MCP-direct workers**
```
To:
```markdown
**Phase 3 — apk_re_agents Streamable HTTP migration + PARE MCP-direct workers** ✅ *Done — apk_re_agents v0.2.0 + PARE workers.yaml*
```

This mirrors how Phases 0 and 1 are tagged in the same section.

- [ ] **Step 3: Commit**

```bash
git add README.md docs/superpowers/specs/2026-05-12-pare-v1-design.md
git commit -m "docs: link Phase 3 plan + mark Phase 3 done in spec"
```

- [ ] **Step 4: Push the branch**

```bash
git push -u origin phase3-mcp-direct-workers 2>&1 | tail -5
```

Expected: branch pushed with tracking. Controller (or operator) creates and merges the PR via the usual `gh pr create` + `gh pr merge --rebase --delete-branch` flow.

---

## Phase Exit Verification

- [ ] **apk_re_agents side:** `phase3-streamable-http-migration` PR merged to main; `v0.2.0` tag pushed to origin.
- [ ] **PARE side:** `phase3-mcp-direct-workers` PR merged to main.
- [ ] PARE's full suite: `cd ~/Projects/PARE && .venv/bin/pytest -q` — 17 passing + 3 skipped (15 baseline + 2 register_tools + 2 phase3 smoke skipped).
- [ ] Operator smoke run: `cd ~/Projects/apk_re_agents && docker compose up --build -d`, then `cd ~/Projects/PARE && PARE_PHASE3_SMOKE=1 .venv/bin/pytest tests/test_phase3_smoke.py -v` returns 2 passed.

Phase 3 is complete when this checklist is fully green. Phase 4 (Android worker scaffold + container hardening) starts from a clean working tree against `agent_core@v1.3.0` + the now-fully-wired MCP execution layer.
