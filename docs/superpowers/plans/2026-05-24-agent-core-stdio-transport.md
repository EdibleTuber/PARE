# agent_core stdio Transport Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add stdio transport support to `agent_core.workers.MCPClient` so PARE (and future agent_core consumers) can talk to the community ecosystem of stdio-based MCP servers — Frida MCP, Ghidra MCP, mitmdump wrappers, etc. — alongside the existing Streamable HTTP transport.

**Architecture:** Extend `MCPClient` to support both transports through one class. The constructor accepts either `endpoint` (Streamable HTTP, existing) or `command` + `args` + `env` (stdio, new); `connect()` branches on which set is configured. `WorkerSpec` gains optional `command`/`args`/`env`/`cwd` fields with a model validator that enforces "endpoint OR command depending on transport." `MCPClientPool` switches to `MCPClient.from_spec(spec)` so the spec is the single source of transport choice. `discover_and_register` is unchanged — it only deals with the pool. A new `assert_stdio_conformance(spec)` companion to the existing `assert_streamable_http_conformance(endpoint)` lets workers verify themselves over their actual transport. Ships as `agent_core@v1.4.0`.

**Tech Stack:** Python 3.12, `mcp>=1.27.0` (already pinned), `fastmcp>=0.2.0` (dev dep, already there). `mcp.client.stdio` exposes `stdio_client` (async context manager yielding read/write streams) + `StdioServerParameters` (Pydantic model with `command`, `args`, `env`, `cwd`, `encoding`, `encoding_error_handler` fields). Same context-manager protocol as `streamablehttp_client`, slots in cleanly.

**Working directory:** `~/Projects/agent_core/` throughout. PAL pin update is a no-op verification at the end.

---

## File Structure

**Modified in `~/Projects/agent_core/`:**

- `agent_core/workers/types.py` — `WorkerSpec` gains `command`, `args`, `env`, `cwd` optional fields + a `model_validator` enforcing the transport↔fields invariant.
- `agent_core/workers/client.py` — `MCPClient.__init__` takes keyword-only stdio params alongside the existing positional `endpoint`. `connect()` branches on which transport is configured. Adds `MCPClient.from_spec(spec)` classmethod.
- `agent_core/workers/client_pool.py` — replaces `MCPClient(spec.endpoint)` with `MCPClient.from_spec(spec)`.
- `agent_core/workers/conformance.py` — adds `assert_stdio_conformance(spec)` companion to `assert_streamable_http_conformance(endpoint)`. The existing HTTP-only helper stays unchanged for backwards compatibility.
- `pyproject.toml` — version `1.3.1` → `1.4.0`.
- `CHANGELOG.md` — entry for v1.4.0.

**New tests in `~/Projects/agent_core/tests/`:**

- `tests/workers/fixtures/stdio_stub.py` — a tiny FastMCP server that runs over stdio. Same two toy tools as the Streamable HTTP fixture (`noop_low`, `risky_high`).
- `tests/workers/conftest.py` — extended with a new `stdio_fixture_spec` fixture that returns a `WorkerSpec` pointing at the stdio stub (`command="python"`, `args=["-m", "tests.workers.fixtures.stdio_stub"]`).
- `tests/workers/test_types.py` — tests for new fields + validator (transport↔fields invariant).
- `tests/workers/test_client.py` — extended with stdio-path tests (initialize/list_tools/call_tool against the stdio fixture).
- `tests/workers/test_client_pool.py` — extended with a stdio pool test (mixed pool of HTTP + stdio).
- `tests/workers/test_discovery.py` — extended with stdio discovery test.
- `tests/workers/test_conformance_stdio.py` — new file for `assert_stdio_conformance`.

---

## Setup

### Task 0: Prereqs + branch

**Files:** none (env only).

- [ ] **Step 1: Confirm agent_core is clean and on main**

```bash
cd /home/edible/Projects/agent_core
git status
git log --oneline -3
```

Expected: working tree clean; HEAD includes the v1.3.1 release (`fix(reasoning):` or similar most-recent commit). If uncommitted changes are present, STOP and report.

- [ ] **Step 2: Pull in case main has moved**

```bash
git pull --ff-only origin main
```

Expected: `Already up to date.` or clean fast-forward.

- [ ] **Step 3: Create the feature branch**

```bash
git checkout -b stdio-transport
```

- [ ] **Step 4: Refresh the dev venv + baseline test**

```bash
.venv/bin/pip install -e ".[dev]" 2>&1 | tail -3
.venv/bin/pytest -x -q 2>&1 | tail -3
```

Expected: install completes; baseline test count noted (Phase 2 left it around 623 + 2 skipped; v1.3.1 may have shifted slightly — use whatever current pass count is as the baseline for later regression checks).

---

## Section A: WorkerSpec stdio fields

### Task 1: Add `command`, `args`, `env`, `cwd` to WorkerSpec + validator

**Files:**
- Modify: `agent_core/workers/types.py`
- Modify: `tests/workers/test_types.py`

- [ ] **Step 1: Write failing tests**

Append to `/home/edible/Projects/agent_core/tests/workers/test_types.py`:
```python
# Tests for stdio-transport WorkerSpec fields.

def test_worker_spec_stdio_minimal_valid():
    spec = WorkerSpec(
        name="frida",
        transport="stdio",
        risk_default="medium",
        command="frida-mcp",
    )
    assert spec.command == "frida-mcp"
    assert spec.args == []
    assert spec.env == {}
    assert spec.cwd is None
    # endpoint not required for stdio
    assert spec.endpoint is None


def test_worker_spec_stdio_with_args_env():
    spec = WorkerSpec(
        name="frida",
        transport="stdio",
        risk_default="medium",
        command="python",
        args=["-m", "frida_mcp"],
        env={"FRIDA_DEBUG": "1"},
    )
    assert spec.args == ["-m", "frida_mcp"]
    assert spec.env == {"FRIDA_DEBUG": "1"}


def test_worker_spec_stdio_requires_command():
    """transport=stdio without command must fail."""
    with pytest.raises(ValidationError, match="command"):
        WorkerSpec(
            name="bad",
            transport="stdio",
            risk_default="low",
        )


def test_worker_spec_http_requires_endpoint():
    """transport=streamable_http without endpoint must fail."""
    with pytest.raises(ValidationError, match="endpoint"):
        WorkerSpec(
            name="bad",
            transport="streamable_http",
            risk_default="low",
        )


def test_worker_spec_endpoint_field_optional_at_field_level():
    """endpoint becomes optional at the field level (None allowed) so stdio
    specs can omit it. The transport↔fields invariant is in the validator."""
    # Just verifies the field is `str | None`, not required.
    fields = WorkerSpec.model_fields
    assert fields["endpoint"].is_required() is False
```

- [ ] **Step 2: Verify the new tests fail**

```bash
cd /home/edible/Projects/agent_core
.venv/bin/pytest tests/workers/test_types.py -v 2>&1 | tail -15
```

Expected: 4 new tests FAIL (validator errors for stdio-related fields not yet declared; some may error at model construction because `endpoint` is still required).

- [ ] **Step 3: Update WorkerSpec**

Edit `/home/edible/Projects/agent_core/agent_core/workers/types.py`. The current `WorkerSpec` requires `endpoint`. Update it to make `endpoint` optional and add stdio fields with a model validator. The relevant class becomes:

```python
from pydantic import BaseModel, Field, model_validator


class WorkerSpec(BaseModel):
    name: str
    transport: Transport
    risk_default: RiskTier

    # streamable_http / http_job_api transports use endpoint.
    endpoint: str | None = None

    # stdio transport uses command + args + env + cwd.
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None

    container: str | None = None
    capability_tags: list[str] = Field(default_factory=list)
    kind: Literal["internal", "external_mcp"] = "internal"

    @model_validator(mode="after")
    def validate_transport_fields(self) -> "WorkerSpec":
        if self.transport in ("streamable_http", "http_job_api"):
            if not self.endpoint:
                raise ValueError(
                    f"worker {self.name!r}: transport {self.transport!r} requires endpoint"
                )
        elif self.transport == "stdio":
            if not self.command:
                raise ValueError(
                    f"worker {self.name!r}: transport 'stdio' requires command"
                )
        return self
```

Preserve any existing fields (`container`, `capability_tags`, `kind`) and any existing field validators. The change is: (a) `endpoint` becomes `str | None = None`, (b) new `command`/`args`/`env`/`cwd` fields, (c) new `model_validator`. The existing field validator for `name` should stay.

- [ ] **Step 4: Verify the new tests pass + existing tests still pass**

```bash
.venv/bin/pytest tests/workers/test_types.py -v 2>&1 | tail -15
```

Expected: all WorkerSpec tests PASS (new 4 + existing).

- [ ] **Step 5: Full suite regression**

```bash
.venv/bin/pytest -q 2>&1 | tail -3
```

Expected: baseline + 4 new tests. No regressions in worker_registry / client_pool / etc.

- [ ] **Step 6: Commit**

```bash
git add agent_core/workers/types.py tests/workers/test_types.py
git commit -m "feat(workers): add stdio fields to WorkerSpec

endpoint becomes optional at the field level; new optional
command/args/env/cwd fields support stdio transport. A model
validator enforces the transport↔fields invariant: streamable_http
and http_job_api need endpoint; stdio needs command."
```

---

## Section B: stdio Fixture

### Task 2: stdio FastMCP fixture

**Files:**
- Create: `tests/workers/fixtures/stdio_stub.py`
- Modify: `tests/workers/conftest.py`

We need a stdio-transport FastMCP server for downstream client tests. The mcp SDK's `stdio_client` spawns the server as a subprocess based on the `StdioServerParameters` (command + args), so the fixture only needs to provide a Python file that runs FastMCP with `transport="stdio"`.

- [ ] **Step 1: Create the stub server**

Create `/home/edible/Projects/agent_core/tests/workers/fixtures/stdio_stub.py`:
```python
"""Minimal FastMCP server over stdio for agent_core's stdio MCP client tests.

Exposes the same two toy tools as the Streamable HTTP fixture for
parity:
    noop_low   — returns ok with the input echoed.
    risky_high — returns "did the thing" with the target echoed.

Launched by stdio_client as a subprocess; communicates over its
stdin/stdout per the MCP stdio transport spec.
"""
from __future__ import annotations

from fastmcp import FastMCP


def build_stub() -> FastMCP:
    mcp = FastMCP("agent-core-stdio-stub-worker")

    @mcp.tool()
    def noop_low(message: str = "hi") -> dict:
        """A toy tool that returns ok with the input echoed."""
        return {"status": "ok", "echo": message}

    @mcp.tool()
    def risky_high(target: str) -> dict:
        """A toy tool that pretends to do something risky."""
        return {"status": "did the thing", "target": target}

    return mcp


if __name__ == "__main__":
    build_stub().run(transport="stdio")
```

- [ ] **Step 2: Add the WorkerSpec fixture to conftest.py**

Read the existing conftest.py:
```bash
cat /home/edible/Projects/agent_core/tests/workers/conftest.py
```

Append a new fixture that returns a `WorkerSpec` for the stdio stub:
```python
import sys

from agent_core.workers.types import WorkerSpec


@pytest.fixture
def stdio_fixture_spec() -> WorkerSpec:
    """A WorkerSpec pointing at the stdio FastMCP stub.

    Uses `sys.executable` to ensure the subprocess runs in the same Python
    environment (with fastmcp installed). The stub is launched as a module
    so its sys.path is consistent regardless of CWD.
    """
    return WorkerSpec(
        name="stdio_stub",
        transport="stdio",
        risk_default="low",
        command=sys.executable,
        args=["-m", "tests.workers.fixtures.stdio_stub"],
    )
```

Confirm `pytest` is already imported at the top of conftest.py (it should be — the existing `streamable_http_fixture` uses it). If not, add `import pytest`.

- [ ] **Step 3: Verify the fixture imports cleanly + a smoke test**

Create `/home/edible/Projects/agent_core/tests/workers/test_stdio_fixture_smoke.py`:
```python
"""Sanity check that the stdio fixture spec validates as a WorkerSpec
and the subprocess command resolves on this machine."""
import shutil
from pathlib import Path

from agent_core.workers.types import WorkerSpec


def test_stdio_fixture_spec_is_valid_workerspec(stdio_fixture_spec):
    """The fixture returns a valid WorkerSpec for stdio transport."""
    assert isinstance(stdio_fixture_spec, WorkerSpec)
    assert stdio_fixture_spec.transport == "stdio"
    assert stdio_fixture_spec.command  # command path resolved
    assert "tests.workers.fixtures.stdio_stub" in stdio_fixture_spec.args


def test_stdio_stub_module_exists():
    """The stub file exists at the expected path."""
    path = Path(__file__).parent / "fixtures" / "stdio_stub.py"
    assert path.exists(), f"missing stdio stub at {path}"
```

Run:
```bash
.venv/bin/pytest tests/workers/test_stdio_fixture_smoke.py -v 2>&1 | tail -8
```

Expected: 2 PASS.

- [ ] **Step 4: Full suite regression**

```bash
.venv/bin/pytest -q 2>&1 | tail -3
```

Expected: baseline + 2 new fixture-smoke tests.

- [ ] **Step 5: Commit**

```bash
git add tests/workers/fixtures/stdio_stub.py tests/workers/conftest.py tests/workers/test_stdio_fixture_smoke.py
git commit -m "test(workers): add stdio FastMCP fixture for live-transport tests

Tiny FastMCP server with the same two toy tools as the Streamable
HTTP fixture, launched as a subprocess by stdio_client. The
stdio_fixture_spec pytest fixture returns a WorkerSpec the downstream
client + discovery + conformance tests use to drive the new
stdio path."
```

---

## Section C: MCPClient stdio Path

### Task 3: Extend MCPClient with stdio support

**Files:**
- Modify: `agent_core/workers/client.py`
- Modify: `tests/workers/test_client.py`

- [ ] **Step 1: Write the failing test**

Append to `/home/edible/Projects/agent_core/tests/workers/test_client.py`:
```python
@pytest.mark.asyncio
async def test_stdio_initialize_list_tools_call_tool(stdio_fixture_spec):
    """Full MCPClient surface (initialize / list_tools / call_tool) works
    against the stdio fixture."""
    client = MCPClient(
        command=stdio_fixture_spec.command,
        args=stdio_fixture_spec.args,
        env=stdio_fixture_spec.env or None,
    )
    try:
        await client.connect()
        await client.initialize()

        tools = await client.list_tools()
        names = {t.name for t in tools.tools}
        assert "noop_low" in names
        assert "risky_high" in names

        result = await client.call_tool("noop_low", {"message": "ping-stdio"})
        text_blocks = [b for b in result.content if getattr(b, "type", None) == "text"]
        assert any("ping-stdio" in b.text for b in text_blocks)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_client_requires_endpoint_or_command():
    """MCPClient with neither endpoint nor command raises."""
    with pytest.raises(ValueError, match="endpoint.*command|command.*endpoint"):
        MCPClient()
```

- [ ] **Step 2: Verify the new tests fail**

```bash
.venv/bin/pytest tests/workers/test_client.py::test_stdio_initialize_list_tools_call_tool -v 2>&1 | tail -5
.venv/bin/pytest tests/workers/test_client.py::test_client_requires_endpoint_or_command -v 2>&1 | tail -5
```

Expected: both FAIL. The first because MCPClient doesn't accept `command=`/`args=`; the second because MCPClient currently requires `endpoint` positionally and doesn't validate this.

- [ ] **Step 3: Extend MCPClient**

Edit `/home/edible/Projects/agent_core/agent_core/workers/client.py`. Update the imports + class to support stdio:

Replace the existing imports near the top with:
```python
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamablehttp_client
```

Replace the existing `MCPClient.__init__` with:
```python
    def __init__(
        self,
        endpoint: str | None = None,
        *,
        command: str | None = None,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> None:
        if not endpoint and not command:
            raise ValueError(
                "MCPClient requires either endpoint (streamable_http) or "
                "command (stdio)"
            )
        if endpoint and command:
            raise ValueError(
                "MCPClient cannot accept both endpoint and command — choose one transport"
            )
        self.endpoint = endpoint
        self.command = command
        self.args = args or []
        self.env = env or {}
        self.cwd = cwd
        self._transport: str = "stdio" if command else "streamable_http"
        self._session: ClientSession | None = None
        self._transport_ctx: object | None = None
```

Replace the existing `connect()` with the branching version:
```python
    async def connect(self) -> None:
        """Open the configured transport and wrap it in a ClientSession.

        The transport context manager is held on the instance; close()
        releases it."""
        if self._transport == "stdio":
            params = StdioServerParameters(
                command=self.command,
                args=self.args,
                env=self.env or None,
                cwd=self.cwd,
            )
            self._transport_ctx = stdio_client(params)
        else:
            self._transport_ctx = streamablehttp_client(self.endpoint)

        read_stream, write_stream, *_ = await self._transport_ctx.__aenter__()
        self._session = ClientSession(read_stream, write_stream)
        await self._session.__aenter__()
```

`close()`, `initialize()`, `list_tools()`, `call_tool()` stay unchanged — they operate on `self._session` which is set the same way regardless of transport.

- [ ] **Step 4: Verify the new tests pass**

```bash
.venv/bin/pytest tests/workers/test_client.py -v 2>&1 | tail -15
```

Expected: existing 4 client tests still PASS (streamable HTTP path unchanged) + 2 new tests PASS (stdio path + validation).

- [ ] **Step 5: Full suite regression**

```bash
.venv/bin/pytest -q 2>&1 | tail -3
```

Expected: previous count + 2.

- [ ] **Step 6: Commit**

```bash
git add agent_core/workers/client.py tests/workers/test_client.py
git commit -m "feat(workers): MCPClient supports stdio transport

Constructor accepts either endpoint (Streamable HTTP, existing) or
command/args/env/cwd (stdio, new). connect() branches on which is
configured. initialize/list_tools/call_tool are transport-agnostic.

stdio path uses mcp.client.stdio.{stdio_client, StdioServerParameters};
yields the same read/write streams ClientSession expects."
```

---

### Task 4: `MCPClient.from_spec` factory + Pool integration

**Files:**
- Modify: `agent_core/workers/client.py`
- Modify: `agent_core/workers/client_pool.py`
- Modify: `tests/workers/test_client_pool.py`

- [ ] **Step 1: Write the failing tests**

Append to `/home/edible/Projects/agent_core/tests/workers/test_client_pool.py`:
```python
@pytest.mark.asyncio
async def test_pool_works_with_stdio_spec(stdio_fixture_spec):
    """MCPClientPool drives a stdio worker the same way as Streamable HTTP."""
    pool = MCPClientPool([stdio_fixture_spec])
    try:
        tools = await pool.list_tools("stdio_stub")
        names = {t.name for t in tools.tools}
        assert "noop_low" in names

        result = await pool.call_tool("stdio_stub", "risky_high", {"target": "via-stdio"})
        text_blocks = [b for b in result.content if getattr(b, "type", None) == "text"]
        assert any("via-stdio" in b.text for b in text_blocks)
    finally:
        await pool.close_all()


@pytest.mark.asyncio
async def test_pool_mixed_transports(streamable_http_fixture, stdio_fixture_spec):
    """A pool with both an HTTP worker and a stdio worker handles each correctly."""
    http_spec = WorkerSpec(
        name="http_stub",
        endpoint=streamable_http_fixture,
        transport="streamable_http",
        risk_default="low",
    )
    pool = MCPClientPool([http_spec, stdio_fixture_spec])
    try:
        http_tools = await pool.list_tools("http_stub")
        stdio_tools = await pool.list_tools("stdio_stub")
        # Both workers expose the same toy surface in their fixtures.
        http_names = {t.name for t in http_tools.tools}
        stdio_names = {t.name for t in stdio_tools.tools}
        assert "noop_low" in http_names
        assert "noop_low" in stdio_names
    finally:
        await pool.close_all()
```

- [ ] **Step 2: Verify the new tests fail**

```bash
.venv/bin/pytest tests/workers/test_client_pool.py -v 2>&1 | tail -10
```

Expected: 2 new tests FAIL. The first fails because `MCPClientPool._ensure_connected` calls `MCPClient(spec.endpoint)` which now needs to handle a `None` endpoint + use the stdio path.

- [ ] **Step 3: Add `MCPClient.from_spec` classmethod**

Append to `MCPClient` in `agent_core/workers/client.py`:
```python
    @classmethod
    def from_spec(cls, spec: "WorkerSpec") -> "MCPClient":
        """Construct an MCPClient from a WorkerSpec, dispatching transport.

        Imported lazily to avoid a circular import — WorkerSpec lives in
        workers.types which doesn't import client.
        """
        if spec.transport == "stdio":
            return cls(
                command=spec.command,
                args=list(spec.args),
                env=dict(spec.env) if spec.env else None,
                cwd=spec.cwd,
            )
        return cls(endpoint=spec.endpoint)
```

Add the forward-reference import at the top of `client.py` (in `TYPE_CHECKING`):
```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_core.workers.types import WorkerSpec
```

(`from_spec` uses the string forward ref so no runtime import needed.)

- [ ] **Step 4: Update MCPClientPool to use from_spec**

Edit `/home/edible/Projects/agent_core/agent_core/workers/client_pool.py`. Find the line:
```python
                client = MCPClient(spec.endpoint)
```

Replace with:
```python
                client = MCPClient.from_spec(spec)
```

No other changes needed; the rest of the pool is transport-agnostic.

- [ ] **Step 5: Verify the new tests pass**

```bash
.venv/bin/pytest tests/workers/test_client_pool.py -v 2>&1 | tail -10
```

Expected: existing 3 pool tests PASS + 2 new tests PASS (5 total).

- [ ] **Step 6: Full suite regression**

```bash
.venv/bin/pytest -q 2>&1 | tail -3
```

Expected: previous count + 2.

- [ ] **Step 7: Commit**

```bash
git add agent_core/workers/client.py agent_core/workers/client_pool.py tests/workers/test_client_pool.py
git commit -m "feat(workers): MCPClient.from_spec + pool uses it

Factory routes a WorkerSpec to the right MCPClient constructor based
on its transport field. Pool's _ensure_connected switches from
MCPClient(spec.endpoint) to MCPClient.from_spec(spec), letting mixed
HTTP + stdio pools work transparently."
```

---

### Task 5: Discovery against stdio

**Files:**
- Modify: `tests/workers/test_discovery.py`

`discover_and_register` is already transport-agnostic (it goes through the pool). This task adds a regression test confirming that.

- [ ] **Step 1: Write the failing test**

Append to `/home/edible/Projects/agent_core/tests/workers/test_discovery.py`:
```python
@pytest.mark.asyncio
async def test_discover_against_stdio_fixture(stdio_fixture_spec):
    """discover_and_register returns prefixed Tool subclasses for a stdio
    worker, same as for an HTTP worker."""
    pool = MCPClientPool([stdio_fixture_spec])
    try:
        tool_classes = await discover_and_register([stdio_fixture_spec], pool)
        names = {cls.name for cls in tool_classes}
        assert "stdio_stub_noop_low" in names
        assert "stdio_stub_risky_high" in names
    finally:
        await pool.close_all()
```

- [ ] **Step 2: Verify the test passes (already)**

```bash
.venv/bin/pytest tests/workers/test_discovery.py::test_discover_against_stdio_fixture -v 2>&1 | tail -8
```

Expected: PASS. `discover_and_register` was already transport-agnostic; this test confirms it. If it FAILS, something in the pool integration broke — investigate.

- [ ] **Step 3: Full suite regression**

```bash
.venv/bin/pytest -q 2>&1 | tail -3
```

Expected: previous count + 1.

- [ ] **Step 4: Commit**

```bash
git add tests/workers/test_discovery.py
git commit -m "test(workers): regression test for discovery against stdio

discover_and_register was already transport-agnostic via the pool;
this test pins that property so future refactors don't break it."
```

---

### Task 6: `assert_stdio_conformance` companion

**Files:**
- Modify: `agent_core/workers/conformance.py`
- Create: `tests/workers/test_conformance_stdio.py`

The existing `assert_streamable_http_conformance(endpoint: str)` takes an HTTP URL. For stdio workers (community Frida MCP, etc.), workers need an equivalent check they can run in their own test suites.

- [ ] **Step 1: Write the failing tests**

Create `/home/edible/Projects/agent_core/tests/workers/test_conformance_stdio.py`:
```python
"""Stdio conformance suite — runs against a live MCP worker over stdio."""
import sys

import pytest

from agent_core.workers.conformance import assert_stdio_conformance
from agent_core.workers.types import WorkerSpec


@pytest.mark.asyncio
async def test_stdio_fixture_passes_conformance(stdio_fixture_spec):
    """The stdio fixture spec satisfies the live-transport conformance checks."""
    await assert_stdio_conformance(stdio_fixture_spec)


@pytest.mark.asyncio
async def test_nonexistent_command_fails_conformance():
    """A bogus command (file doesn't exist) fails the conformance check (raises)."""
    bad_spec = WorkerSpec(
        name="bogus",
        transport="stdio",
        risk_default="low",
        command="/nonexistent/binary",
        args=[],
    )
    with pytest.raises(AssertionError):
        await assert_stdio_conformance(bad_spec)
```

- [ ] **Step 2: Verify the test fails**

```bash
cd /home/edible/Projects/agent_core
.venv/bin/pytest tests/workers/test_conformance_stdio.py -v 2>&1 | tail -5
```

Expected: FAIL with `ImportError: cannot import name 'assert_stdio_conformance'`.

- [ ] **Step 3: Add the new conformance helper**

Append to `/home/edible/Projects/agent_core/agent_core/workers/conformance.py`:
```python
async def assert_stdio_conformance(spec: "WorkerSpec") -> None:
    """Verify a live stdio MCP worker meets contract expectations.

    Spawns the worker subprocess, runs the MCP handshake, lists tools,
    and checks each tool's metadata is well-formed. Raises
    AssertionError with a clear message on first failure.

    Workers' own test suites import this and pass their WorkerSpec in.
    """
    from agent_core.workers.client import MCPClient

    if spec.transport != "stdio":
        raise AssertionError(
            f"stdio_conformance: spec {spec.name!r} has transport "
            f"{spec.transport!r}, not 'stdio'"
        )

    client = MCPClient.from_spec(spec)
    try:
        try:
            await client.connect()
        except Exception as exc:
            raise AssertionError(
                f"stdio_conformance: connect failed for {spec.name!r} "
                f"(command={spec.command!r}): {exc}"
            ) from exc

        try:
            await client.initialize()
        except Exception as exc:
            raise AssertionError(
                f"stdio_conformance: initialize failed: {exc}"
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

If `conformance.py` doesn't already have a `TYPE_CHECKING` block, add the forward reference at the top:
```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_core.workers.types import WorkerSpec
```

- [ ] **Step 4: Verify the tests pass**

```bash
.venv/bin/pytest tests/workers/test_conformance_stdio.py -v 2>&1 | tail -8
```

Expected: 2 PASS. The nonexistent-command test should fail-fast (connect raises within the subprocess-spawn path; conformance helper translates to AssertionError).

- [ ] **Step 5: Full suite regression**

```bash
.venv/bin/pytest -q 2>&1 | tail -3
```

Expected: previous count + 2.

- [ ] **Step 6: Commit**

```bash
git add agent_core/workers/conformance.py tests/workers/test_conformance_stdio.py
git commit -m "feat(workers): add assert_stdio_conformance

Companion to assert_streamable_http_conformance for stdio workers.
Takes a WorkerSpec, spawns the subprocess, runs the MCP handshake,
verifies tool metadata. Workers import this into their own test suites
and run it against their actual configured WorkerSpec."
```

---

## Section D: Version Bump + Push

### Task 7: Bump to v1.4.0 + CHANGELOG entry

**Files:**
- Modify: `pyproject.toml`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Bump the version**

Edit `/home/edible/Projects/agent_core/pyproject.toml`. Find:
```toml
version = "1.3.1"
```
Change to:
```toml
version = "1.4.0"
```

If pyproject shows a different version (main moved further), use whatever's there as the source and bump to `1.4.0`. STOP and report if current version is already ≥ `1.4.0`.

- [ ] **Step 2: Add CHANGELOG entry**

Edit `/home/edible/Projects/agent_core/CHANGELOG.md`. Above the most recent existing version section (probably `## [1.3.1]`), insert:

```markdown
## [1.4.0] — 2026-05-24

### Added
- `agent_core.workers.MCPClient` now supports stdio transport alongside the existing Streamable HTTP. Constructor accepts either `endpoint` (HTTP, existing positional) or keyword-only `command` / `args` / `env` / `cwd` (stdio, new). `connect()` branches on whichever is configured.
- `MCPClient.from_spec(spec)` classmethod — picks the right transport from a `WorkerSpec`. Used internally by `MCPClientPool`.
- `agent_core.workers.types.WorkerSpec` gains optional `command`, `args`, `env`, `cwd` fields. A `model_validator` enforces "endpoint required for streamable_http/http_job_api, command required for stdio."
- `assert_stdio_conformance(spec)` — companion to `assert_streamable_http_conformance(endpoint)`. Workers run this against their own stdio configuration.

### Notes
- `worker_contract_version` stays at `1`. v1.4.0 is purely additive: the new transport opens up the community ecosystem of stdio MCP servers (Frida MCP, Ghidra MCP, mitmdump wrappers, etc.) without changing the contract surface.
- Existing Streamable HTTP callers are unaffected. `MCPClient("http://...")` positional usage continues to work.
- PAL consumers can bump the pin transparently; PAL doesn't use MCP workers in v1.
```

Adapt bullet style to match the existing CHANGELOG if conventions differ.

- [ ] **Step 3: Final full-suite check**

```bash
cd /home/edible/Projects/agent_core
.venv/bin/pytest -q 2>&1 | tail -3
```

Expected: all tests pass.

- [ ] **Step 4: Commit, push, tag locally**

```bash
git add pyproject.toml CHANGELOG.md
git commit -m "chore: bump to v1.4.0

Adds stdio transport to MCPClient. Additive — Streamable HTTP path
unchanged; worker_contract_version stays at 1. Opens up the community
MCP ecosystem (Frida MCP, Ghidra MCP, etc.) without touching the
contract surface."

git push -u origin stdio-transport 2>&1 | tail -5

git tag -a v1.4.0 -m "agent_core v1.4.0 — stdio transport"
```

**Do NOT push the tag yet.** Same pattern as Phase 0 / Phase 2: tag pushes after the PR merges to main. The controller (or operator) handles merge + tag push.

---

## Section E: PAL Pin No-Op Verification (separate, small)

### Task 8: Verify PAL still works against agent_core@v1.4.0

**Working directory:** `~/Projects/PAL/`

**Files:**
- Modify: `pyproject.toml`

Same no-op pattern used for v1.2.0/v1.3.0 pin bumps. Do this AFTER the agent_core PR is merged and the v1.4.0 tag is pushed to origin.

- [ ] **Step 1: Branch + bump the pin**

```bash
cd /home/edible/Projects/PAL
git checkout main
git pull --ff-only origin main
git checkout -b agent-core-v1.4.0-pin-bump
```

Edit `pyproject.toml`. Find the current pin (likely `@v1.3.1`):
```toml
"agent_core @ git+https://github.com/EdibleTuber/agent_core.git@v1.3.1",
```
Change the version suffix to `v1.4.0`.

- [ ] **Step 2: Reinstall PAL with the new pin**

```bash
.venv/bin/pip install -e ".[dev]" 2>&1 | tail -5
```
Expected: `Successfully installed agent_core-1.4.0 ...`.

- [ ] **Step 3: Run the PAL test suite**

```bash
.venv/bin/pytest -q --ignore=tests/test_chat_research_integration.py --ignore=tests/test_client.py --ignore=tests/test_daemon.py --ignore=tests/test_integration.py --ignore=tests/test_prompt_injection.py 2>&1 | tail -5
```
(Same ignore list as prior PAL pin bumps — those have pre-existing collection errors unrelated to this work.)

Expected: PAL's current baseline passes.

- [ ] **Step 4: Commit and push**

```bash
git add pyproject.toml
git commit -m "chore: bump agent_core pin to v1.4.0

No code changes required — agent_core v1.4.0 ships purely additive
changes (stdio transport on MCPClient). PAL doesn't use MCP workers
in v1, so the bump is a no-op functionally."

git push -u origin agent-core-v1.4.0-pin-bump
```

Open and merge the PR via `gh pr create` + `gh pr merge --rebase --delete-branch` per the established pattern.

---

## Phase Exit Verification

- [ ] All `agent_core` tests pass: `cd ~/Projects/agent_core && .venv/bin/pytest -q` — baseline + ~11 new stdio tests.
- [ ] Both conformance helpers work against their fixtures: HTTP via `assert_streamable_http_conformance(url)`, stdio via `assert_stdio_conformance(spec)`.
- [ ] agent_core `stdio-transport` branch is pushed; PR opened, reviewed, merged.
- [ ] After merge: `cd ~/Projects/agent_core && git push origin v1.4.0` to publish the tag.
- [ ] PAL pin bumped to v1.4.0 and PAL tests pass.

stdio transport work is complete when this checklist is fully green. Next: PARE Phase 4 first adoption — pick a community Frida MCP, add to `workers.yaml` with `transport: stdio`, exercise the LLM-generates-scripts loop conversationally.
