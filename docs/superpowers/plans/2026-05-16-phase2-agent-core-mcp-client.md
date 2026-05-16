# PARE Phase 2: agent_core MCP Execution Layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the live MCP execution layer to `agent_core` on top of the Phase 0 data layer — a Streamable HTTP MCP client, a worker discovery driver, and dynamic `Tool` registration so any consumer (PARE, future agents) can declare workers in `workers.yaml` and have their tools auto-registered at startup. Ships as `agent_core@v1.3.0`. The worker contract version stays at `1` (purely additive release).

**Architecture:** Wrap the official `mcp` Python SDK 1.27.x (Streamable HTTP transport) in a thin async client that exposes `initialize`, `list_tools`, `tools/call`, progress notifications, and cancellation. A discovery driver iterates `WorkerRegistry.all()`, connects to each worker, exchanges `worker_contract_version`, lists its tools, and produces synthetic `agent_core.tools.base.Tool` subclasses whose `run(args, ctx)` calls back into the MCP client. A helper hook lets an `Agent` subclass implement `register_tools()` by returning the result of `discover_and_register(self.workers_registry, self.mcp_client_pool)`. The conformance pytest suite gets a real Streamable-HTTP FastMCP fixture so workers can verify against a live transport, not just the in-process `MockWorkerContract`.

**Tech Stack:** Python 3.12, `agent_core` repo, `mcp>=1.27.0` (new dep, official Anthropic SDK), `httpx` (transitive via mcp), Pydantic v2 (already declared in v1.2.0), `fastmcp>=0.2.0` as a dev dep for the conformance fixture, pytest + pytest-asyncio.

**Working directory:** `~/Projects/agent_core/` throughout. PAL pin update is a no-op verification step at the end.

---

## File Structure

**New in `~/Projects/agent_core/`:**

- `agent_core/workers/client.py` — `MCPClient` class: thin async wrapper over `mcp.client.session.ClientSession` + `mcp.client.streamable_http.streamablehttp_client`. Methods: `connect()`, `initialize()`, `list_tools()`, `call_tool()`, `close()`. Surfaces MCP error objects unchanged.
- `agent_core/workers/discovery.py` — `discover_and_register(registry, *, agent_meta)`: iterates `WorkerRegistry.all()`, connects each, exchanges contract version, builds `Tool` subclasses, returns the list ready for `register_tools()`.
- `agent_core/workers/tool_factory.py` — `make_tool_class(worker_spec, tool_def, client_pool)`: builds an `agent_core.tools.base.Tool` subclass for one MCP tool. Name is prefixed `{worker}_{tool}`; `parameters` is the MCP-supplied JSON Schema; `run(args, ctx)` calls back through the client pool.
- `agent_core/workers/client_pool.py` — `MCPClientPool`: holds one `MCPClient` per worker name, lazy-connects on first use, exposes `call(worker, tool, args)` and `close_all()`. Reused across all dynamic Tool calls so we don't reconnect per tool call.
- `tests/workers/fixtures/streamable_http_stub.py` — a tiny FastMCP server exposing two known tools (`noop_low`, `risky_high`) over Streamable HTTP. Started in a pytest fixture so live transport tests have a real server.

**Modified in `~/Projects/agent_core/`:**

- `agent_core/workers/__init__.py` — re-export the new public symbols (`MCPClient`, `MCPClientPool`, `discover_and_register`).
- `agent_core/workers/conformance.py` — add a new `assert_streamable_http_conformance(base_url)` helper that uses the live transport (not just the in-process duck type).
- `pyproject.toml` — version `1.2.0` → `1.3.0`; add `mcp>=1.27.0` to dependencies; add `fastmcp>=0.2.0` to `[project.optional-dependencies] dev`.
- `CHANGELOG.md` — entry for v1.3.0.

**New tests in `~/Projects/agent_core/tests/`:**

- `tests/workers/test_client.py` — connect, initialize, list_tools, call_tool, close. Uses the live FastMCP fixture from `tests/workers/fixtures/`.
- `tests/workers/test_client_pool.py` — lazy connect, reused connection, close_all releases everything.
- `tests/workers/test_tool_factory.py` — `make_tool_class` produces a valid Tool subclass with the right name/parameters/run shape.
- `tests/workers/test_discovery.py` — `discover_and_register` against a registry pointing at the FastMCP fixture; returns Tool subclasses with prefixed names.
- `tests/workers/test_conformance_streamable_http.py` — `assert_streamable_http_conformance` passes against the fixture.

**PAL repo updates (no-op verification, separate PR):**

- `~/Projects/PAL/pyproject.toml` — `agent_core@v1.2.1` (or whatever PAL is currently pinned to) → `@v1.3.0`. PAL tests still pass.

---

## Setup

### Task 0: Verify prerequisites + create branch

**Files:** none (env setup only)

- [ ] **Step 1: Confirm agent_core working tree is clean and on main**

```bash
cd /home/edible/Projects/agent_core
git status
git log --oneline -3
```

Expected: working tree clean; `main` HEAD is the Phase 0 release (commit message mentions v1.2.0 or shell_helpers landings). If there are uncommitted changes from elsewhere, STOP and report.

- [ ] **Step 2: Pull latest in case the user has pushed something since**

```bash
git pull --ff-only origin main
```

Expected: `Already up to date.` or a clean fast-forward. If a merge is required, STOP and report (likely the user's parallel work needs to be on a separate branch).

- [ ] **Step 3: Create feature branch**

```bash
git checkout -b phase2-mcp-execution-layer
```

- [ ] **Step 4: Reinstall the dev venv (in case main has moved since Phase 0)**

```bash
.venv/bin/pip install -e ".[dev]" 2>&1 | tail -5
```

Expected: install completes without errors. If `.venv` is missing, recreate: `python -m venv .venv && .venv/bin/pip install -e ".[dev]"`.

- [ ] **Step 5: Baseline test run**

```bash
.venv/bin/pytest -x -q 2>&1 | tail -5
```

Expected: all existing tests pass. Note the count — Phase 0 left it around 605 + 2 skipped. Should be similar (or higher if main has new work).

---

## Section A: MCP Client Primitive

### Task 1: Add `mcp` dependency + client.py skeleton

**Files:**
- Modify: `pyproject.toml`
- Create: `agent_core/workers/client.py`

- [ ] **Step 1: Add `mcp` to dependencies**

Edit `pyproject.toml`. In the `[project] dependencies` array, append:
```toml
    "mcp>=1.27.0",
```

The full block should look something like:
```toml
dependencies = [
    "httpx>=0.27.0",
    "pyyaml>=6.0",
    "prompt-toolkit>=3.0.0",
    "rich>=13.0.0",
    "trafilatura>=1.12.0",
    "markitdown[pdf,docx,pptx,xlsx]>=0.1.0",
    "pydantic>=2.0",
    "mcp>=1.27.0",
]
```

- [ ] **Step 2: Install the new dep**

```bash
.venv/bin/pip install -e ".[dev]" 2>&1 | tail -5
```

Expected: `Successfully installed mcp-1.27.x` and transitive deps. If install fails (e.g., network), STOP and report.

- [ ] **Step 3: Verify import + locate the correct Streamable HTTP transport path**

```bash
.venv/bin/python -c "
import mcp
import mcp.client.session
print('ClientSession:', mcp.client.session.ClientSession)
# Find the Streamable HTTP transport — the path may be one of:
try:
    from mcp.client.streamable_http import streamablehttp_client
    print('OK: mcp.client.streamable_http.streamablehttp_client')
except ImportError:
    pass
try:
    from mcp.client.streamable import streamable_client
    print('OK: mcp.client.streamable.streamable_client')
except ImportError:
    pass
"
```

Note the working import path. If neither works, run `python -c "import mcp.client; help(mcp.client)"` to inspect what's available. The plan below assumes `from mcp.client.streamable_http import streamablehttp_client` — adapt to whichever import actually works and report what you used.

- [ ] **Step 4: Create the client skeleton**

Create `agent_core/workers/client.py`:
```python
"""MCPClient: thin async wrapper over the official mcp SDK's
Streamable HTTP transport.

Lifecycle:
    client = MCPClient(endpoint="http://host:port/mcp")
    await client.connect()
    await client.initialize()          # MCP handshake
    tools = await client.list_tools()  # tools/list
    result = await client.call_tool(name, arguments)
    await client.close()

The wrapper exposes the methods Phase 2's discovery driver needs
(initialize, list_tools, call_tool) plus close(). MCP error objects
are returned unchanged — translation into agent_core error semantics
happens at the call site (tool_factory.py).
"""
from __future__ import annotations

import logging
from typing import Any

# IMPORTANT: adapt this import to whatever the Step 3 check confirmed.
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client

logger = logging.getLogger(__name__)


class MCPClient:
    """One MCP client connection to one worker endpoint."""

    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint
        self._session: ClientSession | None = None
        self._transport_ctx: Any = None  # held to keep the streams open

    async def connect(self) -> None:
        """Open the Streamable HTTP transport and wrap it in a ClientSession.

        The transport context manager is held on the instance; close() releases it."""
        self._transport_ctx = streamablehttp_client(self.endpoint)
        read_stream, write_stream, *_ = await self._transport_ctx.__aenter__()
        self._session = ClientSession(read_stream, write_stream)
        await self._session.__aenter__()

    async def close(self) -> None:
        if self._session is not None:
            await self._session.__aexit__(None, None, None)
            self._session = None
        if self._transport_ctx is not None:
            await self._transport_ctx.__aexit__(None, None, None)
            self._transport_ctx = None
```

Don't add `initialize`, `list_tools`, or `call_tool` yet — those come in Tasks 2-4 (one method per task, TDD-style).

- [ ] **Step 5: Sanity check the module imports**

```bash
.venv/bin/python -c "from agent_core.workers.client import MCPClient; print('import ok')"
```

Expected: `import ok`. If it fails, the mcp import paths in Step 4 are wrong — fix.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml agent_core/workers/client.py
git commit -m "feat(workers): scaffold MCPClient with Streamable HTTP transport

Wraps the official mcp SDK 1.27.x ClientSession + streamablehttp_client.
connect() / close() only in this commit; initialize, list_tools,
call_tool land in following commits (TDD per method)."
```

### Task 2: Streamable HTTP fixture for live-transport tests

**Files:**
- Create: `tests/workers/fixtures/__init__.py`
- Create: `tests/workers/fixtures/streamable_http_stub.py`
- Modify: `pyproject.toml` (add fastmcp to dev deps)

We need a real Streamable HTTP server before Tasks 3-5 can test live behavior. FastMCP is the simplest way to stand one up.

- [ ] **Step 1: Add fastmcp to dev deps**

Edit `pyproject.toml`. In `[project.optional-dependencies] dev`, append:
```toml
    "fastmcp>=0.2.0",
```

Install:
```bash
.venv/bin/pip install -e ".[dev]" 2>&1 | tail -3
```

- [ ] **Step 2: Create the fixture stub**

Create `tests/workers/fixtures/__init__.py` (empty file).

Create `tests/workers/fixtures/streamable_http_stub.py`:
```python
"""Minimal FastMCP server used as a live Streamable HTTP fixture for
agent_core's MCP client + discovery tests.

Exposes two toy tools:
    noop_low   — risk_tier=low, returns "ok"
    risky_high — risk_tier=high, returns "did the thing"

Both echo their input args back in the result for assertion convenience.
"""
from __future__ import annotations

from fastmcp import FastMCP


def build_stub() -> FastMCP:
    """Construct a fresh FastMCP instance with two toy tools registered."""
    mcp = FastMCP("agent-core-stub-worker")

    @mcp.tool()
    def noop_low(message: str = "hi") -> dict:
        """A risk_tier=low tool that returns ok with the input echoed."""
        return {"status": "ok", "echo": message}

    @mcp.tool()
    def risky_high(target: str) -> dict:
        """A risk_tier=high tool that pretends to do something risky."""
        return {"status": "did the thing", "target": target}

    return mcp
```

NOTE: FastMCP's risk-tier metadata convention isn't part of the MCP base spec — it's part of agent_core's worker contract. For Phase 2 we test that `list_tools` returns the tools and that `call_tool` round-trips correctly; risk tiers come from the contract metadata pass which is the consumer's responsibility (Phase 3+ apk_re_agents migration will add the right metadata). For Phase 2's tests, the *presence* of tools is what matters.

- [ ] **Step 3: Create a pytest fixture that starts the server**

Append to `tests/workers/fixtures/__init__.py`:
```python
"""Pytest fixtures for live Streamable HTTP testing."""
import asyncio
import contextlib
import socket
import threading

import pytest
import uvicorn

from tests.workers.fixtures.streamable_http_stub import build_stub


def _free_port() -> int:
    with contextlib.closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def streamable_http_fixture():
    """Start the FastMCP stub on a free port and yield its base URL.

    Teardown stops the uvicorn server cleanly."""
    port = _free_port()
    stub = build_stub()
    app = stub.http_app()  # FastMCP exposes an ASGI app for Streamable HTTP
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)

    server_task = asyncio.create_task(server.serve())
    # Give uvicorn a moment to bind.
    for _ in range(50):
        if server.started:
            break
        await asyncio.sleep(0.05)
    else:
        raise RuntimeError("uvicorn did not start within 2.5s")

    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        await server_task
```

NOTE: FastMCP's exact ASGI-app accessor may differ (`http_app()`, `as_asgi()`, `.app`, etc.). If `stub.http_app()` doesn't exist, inspect with `dir(stub)` to find the right method. Common candidates: `http_app`, `streamable_http_app`, `asgi_app`. Adapt and report what you used.

- [ ] **Step 4: Smoke-test the fixture (no agent_core client yet, just confirm the server starts)**

Create a minimal sanity test in `tests/workers/test_fixture_smoke.py`:
```python
"""Sanity check that the streamable_http_fixture starts and serves."""
import pytest
import httpx


@pytest.mark.asyncio
async def test_fixture_serves_http(streamable_http_fixture):
    """The fixture URL responds to a basic HTTP request (any status; just verifies
    the server is alive and bound)."""
    async with httpx.AsyncClient(timeout=2.0) as client:
        # MCP Streamable HTTP responds to POST/GET on the /mcp path; any
        # response (even a 405 or 400) means the server is alive.
        resp = await client.get(streamable_http_fixture)
        assert resp.status_code < 500
```

Run:
```bash
.venv/bin/pytest tests/workers/test_fixture_smoke.py -v 2>&1 | tail -8
```
Expected: 1 PASS. If the fixture errors at startup, fix that before moving on.

- [ ] **Step 5: Commit**

```bash
git add tests/workers/fixtures pyproject.toml tests/workers/test_fixture_smoke.py
git commit -m "test(workers): add Streamable HTTP fixture for live-transport tests

Tiny FastMCP server with two toy tools, started on a free port via a
pytest fixture. The MCP client + discovery + conformance tests in
later commits hit this fixture rather than mocking the transport."
```

### Task 3: MCPClient.initialize

**Files:**
- Modify: `agent_core/workers/client.py`
- Create: `tests/workers/test_client.py`

- [ ] **Step 1: Write the failing test**

Create `tests/workers/test_client.py`:
```python
"""Tests for agent_core.workers.client.MCPClient against a live FastMCP fixture."""
import pytest

from agent_core.workers.client import MCPClient


@pytest.mark.asyncio
async def test_initialize_completes_against_live_fixture(streamable_http_fixture):
    """initialize() returns the server's InitializeResult without raising."""
    client = MCPClient(streamable_http_fixture)
    try:
        await client.connect()
        result = await client.initialize()
        # MCP InitializeResult exposes serverInfo.name; the stub names itself.
        assert result is not None
        assert getattr(result, "server_info", None) is not None or \
               getattr(result, "serverInfo", None) is not None
    finally:
        await client.close()
```

- [ ] **Step 2: Verify the test fails**

```bash
.venv/bin/pytest tests/workers/test_client.py -v 2>&1 | tail -8
```
Expected: FAIL with `AttributeError: 'MCPClient' object has no attribute 'initialize'`.

- [ ] **Step 3: Add the initialize method**

Edit `agent_core/workers/client.py`. Inside `class MCPClient`, after `close`, add:
```python
    async def initialize(self):
        """Send the MCP initialize request. Returns the server's InitializeResult."""
        assert self._session is not None, "call connect() before initialize()"
        return await self._session.initialize()
```

- [ ] **Step 4: Verify the test passes**

```bash
.venv/bin/pytest tests/workers/test_client.py -v 2>&1 | tail -8
```
Expected: 1 PASS. If the MCP SDK's `InitializeResult` shape uses different attribute names than `server_info` / `serverInfo`, adjust the assertion to whatever's actually present.

- [ ] **Step 5: Commit**

```bash
git add agent_core/workers/client.py tests/workers/test_client.py
git commit -m "feat(workers): MCPClient.initialize() — MCP handshake

Round-trips against the FastMCP fixture; returns the server's
InitializeResult object."
```

### Task 4: MCPClient.list_tools

**Files:**
- Modify: `agent_core/workers/client.py`
- Modify: `tests/workers/test_client.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/workers/test_client.py`:
```python
@pytest.mark.asyncio
async def test_list_tools_returns_stub_tools(streamable_http_fixture):
    """list_tools() returns the two tools the FastMCP stub registered."""
    client = MCPClient(streamable_http_fixture)
    try:
        await client.connect()
        await client.initialize()
        tools = await client.list_tools()
        names = {t.name for t in tools.tools}  # mcp.types.ListToolsResult.tools
        assert "noop_low" in names
        assert "risky_high" in names
    finally:
        await client.close()
```

- [ ] **Step 2: Verify the test fails**

```bash
.venv/bin/pytest tests/workers/test_client.py::test_list_tools_returns_stub_tools -v 2>&1 | tail -5
```
Expected: FAIL with `AttributeError: 'MCPClient' object has no attribute 'list_tools'`.

- [ ] **Step 3: Add the list_tools method**

In `agent_core/workers/client.py`, add:
```python
    async def list_tools(self):
        """Send the MCP tools/list request. Returns ListToolsResult."""
        assert self._session is not None, "call connect() before list_tools()"
        return await self._session.list_tools()
```

- [ ] **Step 4: Verify the test passes**

```bash
.venv/bin/pytest tests/workers/test_client.py -v 2>&1 | tail -8
```
Expected: 2 PASS. If `tools.tools` isn't the right field (e.g., it's `tools.result` or just iterable), adapt the test to match the SDK's actual return shape.

- [ ] **Step 5: Commit**

```bash
git add agent_core/workers/client.py tests/workers/test_client.py
git commit -m "feat(workers): MCPClient.list_tools()

Returns the server's ListToolsResult. Test asserts both stub tools
appear by name."
```

### Task 5: MCPClient.call_tool

**Files:**
- Modify: `agent_core/workers/client.py`
- Modify: `tests/workers/test_client.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/workers/test_client.py`:
```python
@pytest.mark.asyncio
async def test_call_tool_round_trips_arguments(streamable_http_fixture):
    """call_tool sends arguments and receives the stub's echo."""
    client = MCPClient(streamable_http_fixture)
    try:
        await client.connect()
        await client.initialize()
        result = await client.call_tool("noop_low", {"message": "ping"})
        # mcp.types.CallToolResult.content is a list of content blocks;
        # the stub's dict return is serialized to a text content block by FastMCP.
        text_blocks = [b for b in result.content if getattr(b, "type", None) == "text"]
        assert text_blocks, f"no text content in result: {result!r}"
        body = text_blocks[0].text
        assert "ping" in body  # the echo
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_call_tool_unknown_name_raises(streamable_http_fixture):
    """call_tool with a nonexistent tool name raises (MCP error)."""
    import mcp
    client = MCPClient(streamable_http_fixture)
    try:
        await client.connect()
        await client.initialize()
        with pytest.raises(Exception):  # mcp.McpError or similar — adapt
            await client.call_tool("nonexistent_tool", {})
    finally:
        await client.close()
```

- [ ] **Step 2: Verify the tests fail**

```bash
.venv/bin/pytest tests/workers/test_client.py -v 2>&1 | tail -8
```
Expected: 2 new tests FAIL with `AttributeError: 'MCPClient' object has no attribute 'call_tool'`.

- [ ] **Step 3: Add the call_tool method**

In `agent_core/workers/client.py`, add:
```python
    async def call_tool(self, name: str, arguments: dict | None = None):
        """Send the MCP tools/call request. Returns CallToolResult.

        Raises mcp.McpError (or the SDK's equivalent) on protocol errors;
        the caller decides how to map those to agent_core error semantics.
        """
        assert self._session is not None, "call connect() before call_tool()"
        return await self._session.call_tool(name, arguments or {})
```

- [ ] **Step 4: Verify the tests pass**

```bash
.venv/bin/pytest tests/workers/test_client.py -v 2>&1 | tail -10
```
Expected: 4 PASS total in `test_client.py`. If the result-content unpacking doesn't match the SDK's `CallToolResult` shape, adapt the test — the goal is "round-trip works."

- [ ] **Step 5: Full suite regression check**

```bash
.venv/bin/pytest -q 2>&1 | tail -5
```
Expected: baseline + 5 new (4 client + 1 fixture smoke) tests passing.

- [ ] **Step 6: Commit**

```bash
git add agent_core/workers/client.py tests/workers/test_client.py
git commit -m "feat(workers): MCPClient.call_tool() — tools/call round-trip

Round-trips arguments through the FastMCP stub. Unknown tool names
raise an MCP error (mapped to agent_core semantics later in the
tool_factory layer)."
```

---

## Section B: Client Pool + Discovery + Dynamic Tool Factory

### Task 6: MCPClientPool — one connection per worker

**Files:**
- Create: `agent_core/workers/client_pool.py`
- Create: `tests/workers/test_client_pool.py`

`MCPClient` is per-connection; production needs one client per worker, reused across many tool calls. The pool lazily connects on first use and tears them all down on `close_all()`.

- [ ] **Step 1: Write the failing tests**

Create `tests/workers/test_client_pool.py`:
```python
"""Tests for MCPClientPool — lazy connect, reuse, close_all."""
import pytest

from agent_core.workers.client_pool import MCPClientPool
from agent_core.workers.types import WorkerSpec


@pytest.mark.asyncio
async def test_pool_lazy_connects_and_reuses(streamable_http_fixture):
    """The pool doesn't connect until first call; second call reuses the same client."""
    spec = WorkerSpec(
        name="stub",
        endpoint=streamable_http_fixture,
        transport="streamable_http",
        risk_default="low",
    )
    pool = MCPClientPool([spec])
    try:
        # First call triggers connect + initialize.
        tools = await pool.list_tools("stub")
        assert len(tools.tools) >= 2
        # Second call reuses the same connection — no error, returns same shape.
        tools2 = await pool.list_tools("stub")
        assert {t.name for t in tools.tools} == {t.name for t in tools2.tools}
    finally:
        await pool.close_all()


@pytest.mark.asyncio
async def test_pool_call_tool_via_pool(streamable_http_fixture):
    spec = WorkerSpec(
        name="stub",
        endpoint=streamable_http_fixture,
        transport="streamable_http",
        risk_default="low",
    )
    pool = MCPClientPool([spec])
    try:
        result = await pool.call_tool("stub", "noop_low", {"message": "via-pool"})
        text_blocks = [b for b in result.content if getattr(b, "type", None) == "text"]
        assert any("via-pool" in b.text for b in text_blocks)
    finally:
        await pool.close_all()


@pytest.mark.asyncio
async def test_pool_unknown_worker_raises(streamable_http_fixture):
    spec = WorkerSpec(
        name="stub",
        endpoint=streamable_http_fixture,
        transport="streamable_http",
        risk_default="low",
    )
    pool = MCPClientPool([spec])
    try:
        with pytest.raises(KeyError):
            await pool.list_tools("nonexistent")
    finally:
        await pool.close_all()
```

- [ ] **Step 2: Verify the tests fail**

```bash
.venv/bin/pytest tests/workers/test_client_pool.py -v 2>&1 | tail -5
```
Expected: FAIL with `ModuleNotFoundError: agent_core.workers.client_pool`.

- [ ] **Step 3: Implement the pool**

Create `agent_core/workers/client_pool.py`:
```python
"""MCPClientPool — one MCPClient per worker, lazy connect, reused across calls.

A long-lived pool held on the Agent instance (constructed in setup()).
Tools created by the discovery driver call back into this pool to run
their actual MCP exchanges.
"""
from __future__ import annotations

import asyncio
from typing import Any

from agent_core.workers.client import MCPClient
from agent_core.workers.types import WorkerSpec


class MCPClientPool:
    """Holds one MCPClient per worker name, lazy-connecting on first use."""

    def __init__(self, specs: list[WorkerSpec]) -> None:
        self._specs: dict[str, WorkerSpec] = {s.name: s for s in specs}
        self._clients: dict[str, MCPClient] = {}
        self._connect_lock = asyncio.Lock()

    async def _ensure_connected(self, worker: str) -> MCPClient:
        if worker not in self._specs:
            raise KeyError(f"no worker named {worker!r} in this pool")
        async with self._connect_lock:
            if worker not in self._clients:
                spec = self._specs[worker]
                client = MCPClient(spec.endpoint)
                await client.connect()
                await client.initialize()
                self._clients[worker] = client
        return self._clients[worker]

    async def list_tools(self, worker: str):
        client = await self._ensure_connected(worker)
        return await client.list_tools()

    async def call_tool(self, worker: str, tool: str, arguments: dict[str, Any]):
        client = await self._ensure_connected(worker)
        return await client.call_tool(tool, arguments)

    async def close_all(self) -> None:
        for client in self._clients.values():
            await client.close()
        self._clients.clear()
```

- [ ] **Step 4: Verify the tests pass**

```bash
.venv/bin/pytest tests/workers/test_client_pool.py -v 2>&1 | tail -8
```
Expected: 3 PASS.

- [ ] **Step 5: Commit**

```bash
git add agent_core/workers/client_pool.py tests/workers/test_client_pool.py
git commit -m "feat(workers): add MCPClientPool — per-worker connection cache

One MCPClient per worker_name, lazy-connected on first use, shared
lock to prevent racing connects. close_all() releases everything;
intended to be called from the agent's teardown."
```

### Task 7: Dynamic Tool factory

**Files:**
- Create: `agent_core/workers/tool_factory.py`
- Create: `tests/workers/test_tool_factory.py`

`make_tool_class(worker_spec, tool_def, pool)` produces an `agent_core.tools.base.Tool` subclass for one MCP tool. The synthesized class:
- Has `name = f"{worker}_{tool}"` (prefix avoids cross-worker collisions).
- Has `description` and `parameters` copied from the MCP tool definition.
- Has `run(args, ctx)` that calls `pool.call_tool(worker, tool_name, args)` and converts the result to a string for the LLM.

- [ ] **Step 1: Write the failing tests**

Create `tests/workers/test_tool_factory.py`:
```python
"""Tests for the dynamic Tool factory."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_core.workers.tool_factory import make_tool_class
from agent_core.workers.types import WorkerSpec


def _spec(name: str = "stub") -> WorkerSpec:
    return WorkerSpec(
        name=name,
        endpoint="http://x.invalid/mcp",
        transport="streamable_http",
        risk_default="low",
    )


def _tool_def(name: str = "noop_low") -> dict:
    """Minimal MCP tool definition (matches mcp.types.Tool shape)."""
    return {
        "name": name,
        "description": "A toy tool.",
        "inputSchema": {
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": [],
        },
    }


def test_factory_produces_tool_subclass():
    """The factory returns a class that is-a Tool with the right metadata."""
    from agent_core.tools.base import Tool

    pool = MagicMock()
    cls = make_tool_class(_spec(), _tool_def(), pool)
    assert issubclass(cls, Tool)
    assert cls.name == "stub_noop_low"  # worker prefix
    assert cls.description == "A toy tool."
    assert cls.parameters["type"] == "object"


@pytest.mark.asyncio
async def test_factory_run_calls_pool_call_tool():
    """The synthesized run() forwards to pool.call_tool."""
    pool = MagicMock()
    # The CallToolResult body the pool returns: one text block.
    fake_result = MagicMock()
    text_block = MagicMock()
    text_block.type = "text"
    text_block.text = '{"status": "ok"}'
    fake_result.content = [text_block]
    pool.call_tool = AsyncMock(return_value=fake_result)

    cls = make_tool_class(_spec(), _tool_def(), pool)
    tool = cls()
    ctx = MagicMock()
    out = await tool.run({"message": "hello"}, ctx)

    pool.call_tool.assert_awaited_once_with("stub", "noop_low", {"message": "hello"})
    assert "ok" in out  # text block content surfaces in the return string


@pytest.mark.asyncio
async def test_factory_run_handles_error_result():
    """When the MCP result has isError=True, the tool returns an error string
    rather than the content as a normal value."""
    pool = MagicMock()
    fake_result = MagicMock()
    fake_result.isError = True
    text_block = MagicMock()
    text_block.type = "text"
    text_block.text = "boom"
    fake_result.content = [text_block]
    pool.call_tool = AsyncMock(return_value=fake_result)

    cls = make_tool_class(_spec(), _tool_def(), pool)
    tool = cls()
    out = await tool.run({}, MagicMock())
    assert "error" in out.lower()
    assert "boom" in out
```

- [ ] **Step 2: Verify the tests fail**

```bash
.venv/bin/pytest tests/workers/test_tool_factory.py -v 2>&1 | tail -5
```
Expected: FAIL with `ModuleNotFoundError: agent_core.workers.tool_factory`.

- [ ] **Step 3: Implement the factory**

Create `agent_core/workers/tool_factory.py`:
```python
"""Dynamic Tool subclass factory.

For each MCP tool definition discovered from a worker, produce an
agent_core.tools.base.Tool subclass whose run(args, ctx) calls back
into the pool. The class is name-prefixed by worker (`{worker}_{tool}`)
to avoid cross-worker collisions.
"""
from __future__ import annotations

from typing import Any

from agent_core.tools.base import Tool
from agent_core.workers.client_pool import MCPClientPool
from agent_core.workers.types import WorkerSpec


def _stringify_result(result: Any) -> str:
    """Flatten a CallToolResult's content blocks into a single string for the LLM."""
    parts: list[str] = []
    for block in getattr(result, "content", []) or []:
        if getattr(block, "type", None) == "text":
            parts.append(getattr(block, "text", ""))
        else:
            parts.append(repr(block))
    return "\n".join(parts)


def make_tool_class(
    worker: WorkerSpec,
    tool_def: dict,
    pool: MCPClientPool,
) -> type[Tool]:
    """Produce a Tool subclass that calls the given worker's tool via the pool.

    Args:
        worker: WorkerSpec for the worker (provides name + endpoint).
        tool_def: One MCP tool definition (dict with name, description, inputSchema).
        pool: The shared MCPClientPool the synthesized Tool calls into.

    Returns:
        A new Tool subclass. Caller registers it via agent.register_tools().
    """
    tool_name = tool_def["name"]
    prefixed = f"{worker.name}_{tool_name}"
    description = tool_def.get("description", "")
    parameters = tool_def.get("inputSchema") or {"type": "object", "properties": {}}

    class _DynamicTool(Tool):
        name = prefixed
        description = description  # noqa: A003
        parameters = parameters

        async def run(self, args: dict[str, Any], ctx: Any) -> str:
            try:
                result = await pool.call_tool(worker.name, tool_name, args)
            except Exception as exc:
                return f"{prefixed} call failed: {exc}"
            if getattr(result, "isError", False):
                return f"{prefixed} returned an error: {_stringify_result(result)}"
            return _stringify_result(result) or f"{prefixed} returned no content"

    _DynamicTool.__name__ = f"DynamicTool_{prefixed}"
    return _DynamicTool
```

- [ ] **Step 4: Verify the tests pass**

```bash
.venv/bin/pytest tests/workers/test_tool_factory.py -v 2>&1 | tail -8
```
Expected: 3 PASS.

- [ ] **Step 5: Commit**

```bash
git add agent_core/workers/tool_factory.py tests/workers/test_tool_factory.py
git commit -m "feat(workers): dynamic Tool subclass factory

make_tool_class(worker_spec, tool_def, pool) produces an agent_core
Tool subclass whose run() forwards to the pool. Name-prefixed by
worker (e.g. stub_noop_low). Surfaces MCP errors as descriptive
strings rather than raising — matches agent_core's tool convention."
```

### Task 8: discover_and_register driver

**Files:**
- Create: `agent_core/workers/discovery.py`
- Create: `tests/workers/test_discovery.py`

The top-level entrypoint an agent's `register_tools()` will call.

- [ ] **Step 1: Write the failing tests**

Create `tests/workers/test_discovery.py`:
```python
"""Tests for discover_and_register — top-level worker discovery driver."""
import pytest

from agent_core.workers.discovery import discover_and_register
from agent_core.workers.client_pool import MCPClientPool
from agent_core.workers.types import WorkerSpec


@pytest.mark.asyncio
async def test_discover_returns_tool_classes_from_live_fixture(streamable_http_fixture):
    """discover_and_register against the FastMCP fixture returns Tool subclasses
    named after the fixture's two stub tools."""
    spec = WorkerSpec(
        name="stub",
        endpoint=streamable_http_fixture,
        transport="streamable_http",
        risk_default="low",
    )
    pool = MCPClientPool([spec])
    try:
        tool_classes = await discover_and_register([spec], pool)
        names = {cls.name for cls in tool_classes}
        assert "stub_noop_low" in names
        assert "stub_risky_high" in names
    finally:
        await pool.close_all()


@pytest.mark.asyncio
async def test_discover_skips_unreachable_workers(caplog):
    """A worker whose endpoint refuses connection is logged and skipped, not raised."""
    bad_spec = WorkerSpec(
        name="bogus",
        endpoint="http://127.0.0.1:1/mcp",  # nothing listens here
        transport="streamable_http",
        risk_default="low",
    )
    pool = MCPClientPool([bad_spec])
    try:
        tool_classes = await discover_and_register([bad_spec], pool)
        assert tool_classes == []  # nothing registered
        assert any("bogus" in rec.message for rec in caplog.records)
    finally:
        await pool.close_all()
```

- [ ] **Step 2: Verify the tests fail**

```bash
.venv/bin/pytest tests/workers/test_discovery.py -v 2>&1 | tail -5
```
Expected: FAIL with `ModuleNotFoundError: agent_core.workers.discovery`.

- [ ] **Step 3: Implement the driver**

Create `agent_core/workers/discovery.py`:
```python
"""discover_and_register — top-level worker discovery driver.

Iterates a list of WorkerSpec entries, connects each via the pool,
fetches list_tools, and produces Tool subclasses ready for an agent's
register_tools() to return.

A worker that fails to connect or list tools is logged loudly and
skipped — the agent still starts with whichever workers DID respond.
This is the framework-side enforcement of the spec's "connection
failures non-fatal, surfaced in /health" guarantee.
"""
from __future__ import annotations

import logging

from agent_core.tools.base import Tool
from agent_core.workers.client_pool import MCPClientPool
from agent_core.workers.tool_factory import make_tool_class
from agent_core.workers.types import WorkerSpec

logger = logging.getLogger(__name__)


async def discover_and_register(
    specs: list[WorkerSpec],
    pool: MCPClientPool,
) -> list[type[Tool]]:
    """Discover tools across all workers; return ready-to-register Tool classes.

    Args:
        specs: WorkerSpec entries from the agent's WorkerRegistry.
        pool: The MCPClientPool that will back the synthesized Tools at call time.

    Returns:
        List of Tool subclasses; empty if no workers responded. Caller passes
        this list to its agent.register_tools() return value (or extends it
        alongside any declarative tools).
    """
    tool_classes: list[type[Tool]] = []
    for spec in specs:
        try:
            list_result = await pool.list_tools(spec.name)
        except Exception as exc:
            logger.warning(
                "worker %s discovery failed (%s); skipping registration",
                spec.name,
                exc,
            )
            continue

        for tool in getattr(list_result, "tools", []):
            tool_def = {
                "name": tool.name,
                "description": getattr(tool, "description", "") or "",
                "inputSchema": getattr(tool, "inputSchema", None)
                or {"type": "object", "properties": {}},
            }
            cls = make_tool_class(spec, tool_def, pool)
            tool_classes.append(cls)
            logger.info("registered tool %s from worker %s", cls.name, spec.name)
    return tool_classes
```

- [ ] **Step 4: Verify the tests pass**

```bash
.venv/bin/pytest tests/workers/test_discovery.py -v 2>&1 | tail -8
```
Expected: 2 PASS.

- [ ] **Step 5: Commit**

```bash
git add agent_core/workers/discovery.py tests/workers/test_discovery.py
git commit -m "feat(workers): discover_and_register driver

Top-level entry that an agent's register_tools() calls. Iterates the
worker registry, fetches list_tools per worker via the pool, produces
Tool subclasses via the factory. Unreachable workers are logged and
skipped — not fatal."
```

### Task 9: Public re-exports

**Files:**
- Modify: `agent_core/workers/__init__.py`

- [ ] **Step 1: Add the public exports**

Read current content:
```bash
cat agent_core/workers/__init__.py
```

Append (preserve the existing docstring; just add imports and `__all__`):
```python
from agent_core.workers.client import MCPClient
from agent_core.workers.client_pool import MCPClientPool
from agent_core.workers.discovery import discover_and_register
from agent_core.workers.tool_factory import make_tool_class

__all__ = [
    "MCPClient",
    "MCPClientPool",
    "discover_and_register",
    "make_tool_class",
]
```

- [ ] **Step 2: Verify imports resolve from the package root**

```bash
.venv/bin/python -c "from agent_core.workers import MCPClient, MCPClientPool, discover_and_register, make_tool_class; print('ok')"
```
Expected: `ok`.

- [ ] **Step 3: Full suite regression**

```bash
.venv/bin/pytest -q 2>&1 | tail -3
```
Expected: all tests pass (baseline + new Phase 2 tests).

- [ ] **Step 4: Commit**

```bash
git add agent_core/workers/__init__.py
git commit -m "feat(workers): re-export Phase 2 public API

MCPClient, MCPClientPool, discover_and_register, make_tool_class are
the public surface a consumer agent uses in its register_tools()."
```

---

## Section C: Streamable HTTP Conformance Extension

### Task 10: assert_streamable_http_conformance

**Files:**
- Modify: `agent_core/workers/conformance.py`
- Create: `tests/workers/test_conformance_streamable_http.py`

The existing `assert_conformance` is in-process (operates on any object satisfying the `WorkerContract` Protocol). For real workers (apk_re_agents, future Android), we want a live-transport check that connects, initializes, lists tools, and verifies each tool's schema is well-formed.

- [ ] **Step 1: Write the failing test**

Create `tests/workers/test_conformance_streamable_http.py`:
```python
"""Streamable HTTP conformance suite — runs against a live MCP worker."""
import pytest

from agent_core.workers.conformance import assert_streamable_http_conformance


@pytest.mark.asyncio
async def test_streamable_http_fixture_passes_conformance(streamable_http_fixture):
    """The FastMCP fixture satisfies the live-transport conformance checks."""
    await assert_streamable_http_conformance(streamable_http_fixture)


@pytest.mark.asyncio
async def test_unreachable_endpoint_fails_conformance():
    """A nonexistent endpoint fails the conformance checks (raises)."""
    with pytest.raises(AssertionError):
        await assert_streamable_http_conformance("http://127.0.0.1:1/mcp")
```

- [ ] **Step 2: Verify the test fails**

```bash
.venv/bin/pytest tests/workers/test_conformance_streamable_http.py -v 2>&1 | tail -5
```
Expected: FAIL with `ImportError: cannot import name 'assert_streamable_http_conformance'`.

- [ ] **Step 3: Add the new conformance helper**

Append to `agent_core/workers/conformance.py`:
```python
async def assert_streamable_http_conformance(endpoint: str) -> None:
    """Verify a live Streamable HTTP MCP worker meets contract expectations.

    Connects, initializes, lists tools, and checks each tool's metadata
    is well-formed. Raises AssertionError with a clear message on first
    failure.

    Workers' own test suites import this and run it against their
    real running server.
    """
    from agent_core.workers.client import MCPClient

    client = MCPClient(endpoint)
    try:
        try:
            await client.connect()
        except Exception as exc:
            raise AssertionError(
                f"streamable_http_conformance: connect failed for {endpoint!r}: {exc}"
            ) from exc

        try:
            await client.initialize()
        except Exception as exc:
            raise AssertionError(
                f"streamable_http_conformance: initialize failed: {exc}"
            ) from exc

        list_result = await client.list_tools()
        tools = getattr(list_result, "tools", None)
        assert tools is not None, "list_tools returned no .tools attribute"
        assert isinstance(tools, list), f"tools is not a list: {type(tools).__name__}"

        for tool in tools:
            assert tool.name, f"tool has empty name: {tool!r}"
            schema = getattr(tool, "inputSchema", None)
            assert schema is not None, (
                f"tool {tool.name!r} has no inputSchema"
            )
            assert isinstance(schema, dict), (
                f"tool {tool.name!r} inputSchema is not a dict"
            )
            assert "type" in schema, (
                f"tool {tool.name!r} inputSchema missing top-level 'type'"
            )
    finally:
        await client.close()
```

- [ ] **Step 4: Verify the tests pass**

```bash
.venv/bin/pytest tests/workers/test_conformance_streamable_http.py -v 2>&1 | tail -8
```
Expected: 2 PASS.

- [ ] **Step 5: Full suite regression**

```bash
.venv/bin/pytest -q 2>&1 | tail -3
```
Expected: all tests pass (Phase 2's count should now be: baseline + 5 client + 3 pool + 3 factory + 2 discovery + 1 fixture smoke + 2 conformance = baseline + 16 new).

- [ ] **Step 6: Commit**

```bash
git add agent_core/workers/conformance.py tests/workers/test_conformance_streamable_http.py
git commit -m "feat(workers): add assert_streamable_http_conformance

Live-transport conformance check. Workers run this against their own
running MCP server to verify they expose well-formed tools/list and
correct initialize handshake. Complements the in-process
assert_conformance for non-MCP workers and stubs."
```

---

## Section D: Version Bump + CHANGELOG + Push

### Task 11: Bump to v1.3.0 + CHANGELOG entry

**Files:**
- Modify: `pyproject.toml`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Bump the version**

Edit `pyproject.toml`. Find the current version line (should be `1.2.1` as of 2026-05-16; if main has moved further, use whatever's there as the source and bump to `1.3.0`):
```toml
version = "1.2.1"
```
Change to:
```toml
version = "1.3.0"
```

- [ ] **Step 2: Add CHANGELOG entry**

Edit `CHANGELOG.md`. Above the most recent existing section (probably `## [1.2.0]`), insert:
```markdown
## [1.3.0] — 2026-05-16

### Added
- `agent_core.workers.MCPClient` — thin async wrapper over the official `mcp` Python SDK (v1.27.x) Streamable HTTP transport. Methods: `connect`, `initialize`, `list_tools`, `call_tool`, `close`.
- `agent_core.workers.MCPClientPool` — per-worker client cache, lazy-connect, reused across all dynamic Tool calls.
- `agent_core.workers.discover_and_register(specs, pool)` — discovers tools across all workers in a registry and produces ready-to-register `Tool` subclasses (name-prefixed `{worker}_{tool}`). The natural return shape for an agent's `register_tools()`.
- `agent_core.workers.make_tool_class(worker_spec, tool_def, pool)` — dynamic Tool subclass factory.
- `agent_core.workers.conformance.assert_streamable_http_conformance(endpoint)` — live-transport conformance check. Workers import this into their own test suites.

### Dependencies
- Added `mcp>=1.27.0` (official Anthropic MCP Python SDK) to dependencies.
- Added `fastmcp>=0.2.0` to dev dependencies (for the Streamable HTTP test fixture).

### Notes
- `worker_contract_version` stays at `1`. v1.3.0 is purely additive on top of the v1.2.0 data layer; no fields removed, no schemas changed, no behaviour broken.
- PAL consumers can bump the pin transparently; PAL doesn't use MCP workers in v1.
```

If the existing CHANGELOG uses different bullet conventions, adapt to match.

- [ ] **Step 3: Full suite final check**

```bash
.venv/bin/pytest -q 2>&1 | tail -3
```
Expected: all tests pass.

- [ ] **Step 4: Commit, push, tag locally**

```bash
git add pyproject.toml CHANGELOG.md
git commit -m "chore: bump to v1.3.0

Adds the live MCP execution layer on top of v1.2.0's data layer:
MCPClient (Streamable HTTP), MCPClientPool, discover_and_register,
make_tool_class, assert_streamable_http_conformance. Purely additive;
worker_contract_version stays at 1."
git push -u origin phase2-mcp-execution-layer 2>&1 | tail -5
git tag -a v1.3.0 -m "agent_core v1.3.0 — MCP execution layer"
```

Do NOT push the tag yet — same pattern as Phase 0. Tag pushes after PR merges to main.

---

## Section E: PAL Pin Verification (separate, small)

### Task 12: Verify PAL still works against agent_core@v1.3.0

**Working directory:** `~/Projects/PAL/`

**Files:**
- Modify: `pyproject.toml`

This is the "PAL pin update is a no-op" verification step. Do this AFTER the agent_core PR is merged and the v1.3.0 tag is pushed to origin (i.e., the operator merges first, then completes this task).

- [ ] **Step 1: Branch and bump the pin**

```bash
cd /home/edible/Projects/PAL
git checkout main
git pull --ff-only origin main
git checkout -b agent-core-v1.3.0-pin-bump
```

Edit `pyproject.toml`. Find the current `agent_core` pin (likely `v1.2.1` as of 2026-05-16):
```toml
"agent_core @ git+https://github.com/EdibleTuber/agent_core.git@v1.2.1",
```
Change the version suffix to `v1.3.0`.

- [ ] **Step 2: Reinstall PAL with the new pin**

```bash
.venv/bin/pip install -e ".[dev]" 2>&1 | tail -5
```
Expected: `Successfully installed agent_core-1.3.0 ...`.

- [ ] **Step 3: Run the PAL test suite**

```bash
.venv/bin/pytest -q --ignore=tests/test_chat_research_integration.py --ignore=tests/test_client.py --ignore=tests/test_daemon.py --ignore=tests/test_integration.py --ignore=tests/test_prompt_injection.py 2>&1 | tail -5
```
(Same ignore list as Phase 0 — these have pre-existing collection errors unrelated to this work.)

Expected: 643 passed (or whatever the current PAL baseline is).

- [ ] **Step 4: Commit and push**

```bash
git add pyproject.toml
git commit -m "chore: bump agent_core pin to v1.3.0

No code changes required — agent_core v1.3.0 ships purely additive
changes (live MCP execution layer). PAL doesn't use MCP workers in v1,
so the bump is a no-op functionally."
git push -u origin agent-core-v1.3.0-pin-bump
```

Open and merge the PR via your normal flow (or ask the controller to dispatch `gh pr create` + `gh pr merge`).

---

## Phase Exit Verification

- [ ] All `agent_core` tests pass: `cd ~/Projects/agent_core && .venv/bin/pytest -q` — baseline + 16 new Phase 2 tests.
- [ ] Streamable HTTP conformance check passes against the FastMCP fixture: included above.
- [ ] agent_core `phase2-mcp-execution-layer` branch is pushed; PR opened and reviewed.
- [ ] After merge: `cd ~/Projects/agent_core && git push origin v1.3.0` to publish the tag.
- [ ] PAL pin bumped to v1.3.0 and tests still pass.

Phase 2 is complete when this checklist is fully green. Phase 3 (apk_re_agents Streamable HTTP migration + PARE MCP-direct workers) starts from a clean working tree against `agent_core@v1.3.0`.
