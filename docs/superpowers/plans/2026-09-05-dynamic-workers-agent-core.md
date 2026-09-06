# agent_core Dynamic Worker Lifecycle (v1.8.0) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give `agent_core` a runtime worker lifecycle — load, unload, and reload an MCP worker without restarting the daemon — with the risk-gating, approval, and audit guarantees holding *across* those transitions.

**Architecture:** A new `WorkerManager` owns the lifecycle and is the only thing that mutates the three registries that currently freeze at boot (`WorkerRegistry`, `RiskAwareToolPool`/`MCPClientPool`, `ToolExecutor`). Underneath it, `MCPClientPool` gains a **connection-owner task per worker**, because anyio binds a cancel scope to the task that entered it — a client connected in the daemon's startup task cannot be closed from a per-message command task. Boot becomes "load every `autoload: true` worker" through a new `Agent.astartup()` hook, so boot and runtime share one code path.

**Tech Stack:** Python 3.12, asyncio, anyio (via the `mcp` SDK), pydantic v2, pytest + pytest-asyncio (`asyncio_mode = "auto"`).

**Spec:** `docs/superpowers/specs/2026-09-04-dynamic-worker-loading-design.md` (in the PARE repo). Read it first — this plan argues from it and cites its section numbers.

**Repo:** All work is in `/mnt/secondary/projects/agent_core`. Nothing in this plan touches PARE; that is plan 2.

> **STATUS: EXECUTED** (2026-09-05) — shipped as `agent_core` v1.8.0, 17 commits,
> 852 passed / 2 skipped. Execution deviated from this document in several places;
> the deviations, the rulings behind them, and the defects this plan's own code
> carried are recorded in
> [`../2026-09-05-dynamic-workers-build-record.md`](../2026-09-05-dynamic-workers-build-record.md).
> **Do not re-run this plan as written** — Task 2's and Task 5's code blocks contain
> defects found in review (a subprocess leak on the failure path, a short-circuiting
> `or` in `close_all`, and an unguarded hash of untrusted input). The shipped code is
> the authority.

## Global Constraints

- **Python** `>=3.12`. Target interpreter is 3.12.3.
- **`mcp` must be pinned `>=1.27,<2`.** `mcp` 2.x renamed `streamablehttp_client` → `streamable_http_client`, so an unpinned install breaks `agent_core/workers/client.py:35` at import (spec §8.7). Working pair: `mcp` 1.29.1 + `fastmcp` 2.11.3.
- **`fastmcp` must be pinned `>=2.11,<2.12`** in the `dev` extra. 2.12+ imports `IdentityAssertionParams`, which no `mcp` 1.x provides, and its metadata does not declare the conflict.
- **Version target: `1.8.0`.** Additive only. Do **not** make `ToolExecutor.build` raise on duplicate tool names — that is a 2.0.0 change (spec §6.8). `build()` warns; `add()` raises.
- **`worker_contract_version` stays at `1`.** No wire fields change. Say so explicitly in the CHANGELOG, as every prior worker-touching release has.
- **`escalate-only` is inviolable.** No change may cause a tool to dispatch at a *lower* effective tier than it previously resolved to in the same daemon session (spec §6.4.1).
- **Results, never raises.** `WorkerManager` public methods return result objects; a failed load must never propagate into a caller's turn or kill the daemon.
- **Run the suite with the PARE venv:** `/mnt/secondary/projects/PARE/.venv/bin/python -m pytest`. Baseline before any change: **793 passed, 2 skipped**.

---

## File Structure

**Created:**
- `agent_core/workers/manager.py` — `WorkerManager`, `WorkerOpResult`, `WorkerStatus`. The lifecycle coordinator; the only place that mutates pool + executor together.
- `tests/workers/test_manager.py` — lifecycle tests, including the cross-task teardown regression test.
- `tests/workers/test_manager_security.py` — the tier ratchet, generation/approval, cancellation-audit tests. Split from the above because these pin *security invariants* rather than mechanics, and a reviewer should be able to read them without wading through connection plumbing.

**Modified:**
- `agent_core/workers/client_pool.py` — rewritten around owner tasks. The largest single change.
- `agent_core/workers/risk_pool.py` — read-through specs, generations, tier high-water mark, cancellation audit.
- `agent_core/workers/tool_factory.py` — one line: `worker` provenance attribute.
- `agent_core/workers/types.py` — `WorkerSpec.autoload`; two new `Outcome` values; `AuditEntry.tool` nullable.
- `agent_core/workers/discovery.py` — reimplemented over `WorkerManager`; drop the `CancelledError` swallow.
- `agent_core/workers/__init__.py` — export `WorkerManager`, `WorkerRegistry`, `RiskAwareToolPool`.
- `agent_core/tools/executor.py` — mutable registry, agent stored at build, stable `schemas()` order.
- `agent_core/agent.py` — `astartup()` / `ashutdown()` no-op hooks.
- `agent_core/daemon.py` — `start_serving=False`, await `astartup()`, await `ashutdown()` on exit.
- `pyproject.toml` — version 1.8.0, `mcp` and `fastmcp` pins.
- `CHANGELOG.md` — backfill 1.7.0–1.7.3, add 1.8.0.

**Task order is dependency order.** Task 2 (owner tasks) unblocks everything else; do not reorder it.

---

### Task 1: Pin the dependencies and establish the baseline

Nothing else in this plan is verifiable until the suite runs green, and a fresh clone currently cannot install (spec §8.7).

**Files:**
- Modify: `pyproject.toml:10-19` (dependencies), `pyproject.toml:25-31` (dev extra)

**Interfaces:**
- Consumes: nothing.
- Produces: a green baseline every later task compares against.

- [ ] **Step 1: Confirm the baseline suite passes**

```bash
cd /mnt/secondary/projects/agent_core
/mnt/secondary/projects/PARE/.venv/bin/python -m pytest -q
```

Expected: `793 passed, 2 skipped`. If it does not pass, stop — something in the environment differs from what this plan assumes, and every later "expected: PASS" becomes meaningless.

- [ ] **Step 2: Add the pins**

In `pyproject.toml`, change the `mcp` dependency line:

```toml
    "mcp>=1.27.0,<2",   # 2.0 renamed streamablehttp_client -> streamable_http_client
```

and in `[project.optional-dependencies].dev`:

```toml
    "fastmcp>=2.11,<2.12",   # 2.12+ imports IdentityAssertionParams, absent from mcp 1.x
```

- [ ] **Step 3: Verify the pins resolve against what is installed**

```bash
/mnt/secondary/projects/PARE/.venv/bin/pip install -e "/mnt/secondary/projects/agent_core[dev]" --dry-run 2>&1 | tail -5
/mnt/secondary/projects/PARE/.venv/bin/python -c "from fastmcp import FastMCP; from mcp.client.streamable_http import streamablehttp_client; print('OK')"
```

Expected: `OK`, and no resolution conflict. (If you ever need to downgrade `fastmcp` across a major version, `pip uninstall` it and delete `site-packages/fastmcp` first — the 4.x tree shares the directory and leaves stale modules that produce import errors naming modules the installed version never had.)

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml
git commit -m "build: pin mcp<2 and fastmcp<2.12

mcp 2.x renamed streamablehttp_client, so an unpinned fresh install
cannot import agent_core.workers.client. fastmcp 2.12+ imports a symbol
no mcp 1.x provides while its metadata claims mcp<2, so pip's resolver
cannot see the conflict."
```

---

### Task 2: `MCPClientPool` — connection-owner tasks

This is the blocker (spec D7). anyio requires a cancel scope be exited in the task that entered it; `MCPClient.connect()` enters two (the `stdio_client` task group and `ClientSession.__aenter__`'s). Today the pool connects wherever the first caller happens to run and closes wherever the last one does. Measured behaviour: cross-task `close()` raises `RuntimeError` **and the worker subprocess stays alive until the event loop tears down**.

**Files:**
- Modify: `agent_core/workers/client_pool.py` (full rewrite, 53 lines → ~150)
- Test: `tests/workers/test_client_pool_owner_task.py` (create)

**Interfaces:**
- Consumes: `MCPClient.from_spec(spec)`, `.connect()`, `.initialize()`, `.list_tools()`, `.call_tool(name, args)`, `.close()` — all unchanged (`agent_core/workers/client.py`).
- Produces:
  - `MCPClientPool.add_spec(spec: WorkerSpec) -> None`
  - `MCPClientPool.remove_spec(name: str) -> None` (sync; caller disconnects first)
  - `MCPClientPool.spec(name: str) -> WorkerSpec | None`
  - `async MCPClientPool.connect(worker: str, timeout: float | None = None) -> None`
  - `async MCPClientPool.disconnect(worker: str) -> None`
  - `MCPClientPool.is_connected(worker: str) -> bool`
  - existing `list_tools`, `call_tool`, `close_all` keep their signatures.

- [ ] **Step 1: Write the failing regression test**

Create `tests/workers/test_client_pool_owner_task.py`:

```python
"""Connection teardown must not be bound to the task that connected.

anyio binds a cancel scope to the entering task. The daemon connects workers
in its startup task (Agent.astartup) but unloads them from a per-message
handler task (daemon.py:103), so a pool that closes in the caller's task
raises RuntimeError -- and, measured, leaves the worker subprocess running
until the event loop exits. Every pre-existing test in test_client_pool.py
connects and closes inside one coroutine, so none of them can catch this.
"""
import asyncio
import os

import pytest


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


async def test_disconnect_from_a_different_task(stdio_stub_spec):
    from agent_core.workers.client_pool import MCPClientPool

    pool = MCPClientPool([stdio_stub_spec("stub", "low")])

    # Connect in THIS task, the way astartup() would.
    result = await pool.list_tools("stub")
    assert {t.name for t in result.tools} == {"noop_low", "risky_high"}
    pid = pool._owner_pid("stub")
    assert pid is not None and _alive(pid)

    # Dispatch from another task must keep working.
    async def dispatch():
        return await pool.call_tool("stub", "noop_low", {"message": "hi"})

    res = await asyncio.create_task(dispatch())
    assert not getattr(res, "isError", False)

    # Disconnect from a DIFFERENT task, the way /worker unload would.
    async def teardown():
        await pool.disconnect("stub")

    await asyncio.create_task(teardown())

    assert not pool.is_connected("stub")
    for _ in range(50):                 # give the child a moment to reap
        if not _alive(pid):
            break
        await asyncio.sleep(0.1)
    assert not _alive(pid), "worker subprocess survived disconnect"


async def test_connect_timeout_leaves_no_residue(stdio_stub_spec):
    """A worker that never completes connect must not wedge the pool."""
    from agent_core.workers.client_pool import MCPClientPool
    from agent_core.workers.types import WorkerSpec

    spec = WorkerSpec(name="slow", transport="streamable_http",
                      risk_default="low", endpoint="http://127.0.0.1:9/mcp")
    pool = MCPClientPool([spec])
    with pytest.raises((asyncio.TimeoutError, OSError, Exception)):
        await pool.connect("slow", timeout=1.0)
    assert not pool.is_connected("slow")
    assert pool._owners.get("slow") is None
```

- [ ] **Step 2: Run it to verify it fails**

```bash
/mnt/secondary/projects/PARE/.venv/bin/python -m pytest tests/workers/test_client_pool_owner_task.py -q
```

Expected: FAIL — `AttributeError: 'MCPClientPool' object has no attribute '_owner_pid'`.

- [ ] **Step 3: Rewrite `client_pool.py`**

Replace the whole file:

```python
"""MCPClientPool — one owner task per worker, lazy connect, reused across calls.

Why an owner task per worker: anyio binds a cancel scope to the task that
entered it. `MCPClient.connect()` enters two nested scopes (the stdio_client
task group and ClientSession.__aenter__), so `close()` MUST run in the task
that ran `connect()`. The daemon connects during `Agent.astartup()` (the serve
task) and disconnects from a per-message handler task, so a pool that closed in
the caller's task would raise

    RuntimeError: Attempted to exit cancel scope in a different task than it
                  was entered in

and leave the worker subprocess running for the life of the daemon. The owner
task connects, parks on a stop event, and closes in its own `finally` — the
same task throughout.

Dispatch is unaffected: calling a client from a foreign task is safe, and only
teardown carries the affinity requirement.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import defaultdict
from typing import Any

from agent_core.workers.client import MCPClient
from agent_core.workers.types import WorkerSpec

logger = logging.getLogger(__name__)

DEFAULT_CONNECT_TIMEOUT = 10.0


class MCPClientPool:
    """Holds one MCPClient per worker name, each owned by its own task."""

    def __init__(self, specs: list[WorkerSpec]) -> None:
        self._specs: dict[str, WorkerSpec] = {s.name: s for s in specs}
        self._clients: dict[str, MCPClient] = {}
        self._owners: dict[str, asyncio.Task] = {}
        self._ready: dict[str, asyncio.Event] = {}
        self._stop: dict[str, asyncio.Event] = {}
        self._errors: dict[str, BaseException] = {}
        # Per-worker, not global: one worker hanging in connect() must not block
        # every other worker's first use.
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    # --- spec bookkeeping -------------------------------------------------
    def add_spec(self, spec: WorkerSpec) -> None:
        """Register a spec. Idempotent; does not connect."""
        self._specs[spec.name] = spec

    def remove_spec(self, name: str) -> None:
        """Forget a spec. The caller disconnects first — this is deliberately
        sync so spec bookkeeping never awaits, and so a wedged teardown cannot
        leave the spec behind (see WorkerManager.unload's ordering)."""
        self._specs.pop(name, None)

    def spec(self, name: str) -> WorkerSpec | None:
        """The single source of truth for worker specs. RiskAwareToolPool reads
        through to this rather than keeping its own copy."""
        return self._specs.get(name)

    def names(self) -> list[str]:
        return list(self._specs)

    def is_connected(self, worker: str) -> bool:
        return worker in self._clients

    def _owner_pid(self, worker: str) -> int | None:
        """The stdio child's pid, for tests and for hard-kill on a wedged close.
        None for non-stdio transports or before connect completes."""
        client = self._clients.get(worker)
        ctx = getattr(client, "_transport_ctx", None)
        gen = getattr(ctx, "gen", None)
        frame = getattr(gen, "ag_frame", None)
        proc = frame.f_locals.get("process") if frame is not None else None
        return getattr(proc, "pid", None)

    # --- connection lifecycle --------------------------------------------
    async def _own(self, worker: str) -> None:
        """Own one worker's connection for its entire lifetime.

        Connect, publish, park, close — all in this one task, which is what
        makes teardown legal.
        """
        client = MCPClient.from_spec(self._specs[worker])
        try:
            await client.connect()
            await client.initialize()
        except BaseException as exc:      # includes CancelledError on timeout
            self._errors[worker] = exc
            self._ready[worker].set()
            raise
        self._clients[worker] = client
        self._ready[worker].set()
        try:
            await self._stop[worker].wait()
        finally:
            self._clients.pop(worker, None)
            # Best-effort: a wedged worker must not keep the daemon from
            # completing the unload. WorkerManager bounds and hard-kills.
            with contextlib.suppress(BaseException):
                await client.close()

    async def connect(self, worker: str, timeout: float | None = None) -> None:
        """Spawn the owner task and wait until the worker is usable.

        Raises whatever connect/initialize raised, or asyncio.TimeoutError.
        Leaves no residue on failure.
        """
        if worker not in self._specs:
            raise KeyError(f"no worker named {worker!r} in this pool")
        async with self._locks[worker]:
            if worker in self._clients:
                return
            self._ready[worker] = asyncio.Event()
            self._stop[worker] = asyncio.Event()
            self._errors.pop(worker, None)
            self._owners[worker] = asyncio.create_task(
                self._own(worker), name=f"mcp-owner:{worker}")
            try:
                await asyncio.wait_for(
                    self._ready[worker].wait(),
                    timeout if timeout is not None else DEFAULT_CONNECT_TIMEOUT,
                )
            except asyncio.TimeoutError:
                # Cancel INSIDE the owner task — the only safe way to abort a
                # partially-entered anyio scope.
                await self._cancel_owner(worker)
                raise
            exc = self._errors.get(worker)
            if exc is not None:
                await self._reap(worker)
                raise exc

    async def _cancel_owner(self, worker: str) -> None:
        task = self._owners.pop(worker, None)
        if task is not None:
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
        self._cleanup(worker)

    async def _reap(self, worker: str) -> None:
        task = self._owners.pop(worker, None)
        if task is not None:
            with contextlib.suppress(BaseException):
                await task
        self._cleanup(worker)

    def _cleanup(self, worker: str) -> None:
        self._clients.pop(worker, None)
        self._ready.pop(worker, None)
        self._stop.pop(worker, None)

    async def disconnect(self, worker: str) -> None:
        """Stop the worker's owner task and wait for its close to finish."""
        stop = self._stop.get(worker)
        if stop is not None:
            stop.set()
        await self._reap(worker)
        self._errors.pop(worker, None)

    async def _ensure_connected(self, worker: str) -> MCPClient:
        if worker not in self._specs:
            raise KeyError(f"no worker named {worker!r} in this pool")
        if worker not in self._clients:
            await self.connect(worker)
        return self._clients[worker]

    # --- dispatch ---------------------------------------------------------
    async def list_tools(self, worker: str):
        client = await self._ensure_connected(worker)
        return await client.list_tools()

    async def call_tool(self, worker: str, tool: str, arguments: dict[str, Any],
                        ctx: Any = None):
        client = await self._ensure_connected(worker)
        return await client.call_tool(tool, arguments)

    async def close_all(self) -> None:
        for worker in list(self._owners):
            with contextlib.suppress(BaseException):
                await self.disconnect(worker)
        self._clients.clear()
```

- [ ] **Step 4: Run the new test and the existing pool tests**

```bash
/mnt/secondary/projects/PARE/.venv/bin/python -m pytest tests/workers/test_client_pool_owner_task.py tests/workers/test_client_pool.py -q
```

Expected: PASS. The existing `test_client_pool.py` connects and closes in one coroutine, which still works — the owner task is transparent to it.

- [ ] **Step 5: Run the full suite**

```bash
/mnt/secondary/projects/PARE/.venv/bin/python -m pytest -q
```

Expected: `795 passed, 2 skipped` (793 + the 2 new). If a conformance test hangs, the likely cause is `close_all` awaiting an owner task that never parks — check that `_own` sets `_ready` on both the success and failure path.

- [ ] **Step 6: Commit**

```bash
git add agent_core/workers/client_pool.py tests/workers/test_client_pool_owner_task.py
git commit -m "fix(workers): own each MCP connection in a dedicated task

anyio binds a cancel scope to the task that entered it, so a client
connected in the daemon's startup task could not be closed from a
per-message handler task: close() raised RuntimeError and the worker
subprocess survived until the event loop exited. Each worker now has an
owner task that connects, parks on a stop event, and closes in its own
finally. Connect is bounded and cancels the owner task on timeout, which
is the only safe way to abort a partially-entered anyio scope.

Also splits the single global connect lock into a per-worker lock, so one
worker hanging in connect() no longer blocks every other worker's first
use."
```

---

### Task 3: Tool provenance

**Files:**
- Modify: `agent_core/workers/tool_factory.py` (after line 51, alongside the other class attribute assignments)
- Test: `tests/workers/test_tool_factory.py` (append)

**Interfaces:**
- Produces: every class from `make_tool_class` carries `cls.worker == spec.name`. Task 4's `ToolExecutor.remove_worker` and Task 6's rollback both key on it.

- [ ] **Step 1: Write the failing test**

Append to `tests/workers/test_tool_factory.py`:

```python
def test_synthesized_tool_carries_worker_provenance():
    """remove_worker() keys on this attribute rather than the name prefix.

    Prefix matching would delete a declarative tool that merely starts with the
    worker's name -- PARE has a live near-miss: StaticAnalyze is named
    "static_analyze" while the `static` worker prefixes its tools "static_".
    """
    from agent_core.workers.tool_factory import make_tool_class
    from agent_core.workers.types import WorkerSpec

    spec = WorkerSpec(name="static", transport="stdio", risk_default="low",
                      command="/bin/true")
    cls = make_tool_class(spec, {"name": "grep_smali", "description": "d",
                                 "inputSchema": {"type": "object", "properties": {}}}, None)
    assert cls.worker == "static"
    assert cls.name == "static_grep_smali"


def test_builtin_tools_have_no_worker_attribute():
    """The absence of the attribute is what makes builtins unremovable."""
    from agent_core.tools.builtin import BUILTIN_TOOLS
    for tool_cls in BUILTIN_TOOLS:
        assert not hasattr(tool_cls, "worker"), tool_cls.name
```

- [ ] **Step 2: Run it to verify it fails**

```bash
/mnt/secondary/projects/PARE/.venv/bin/python -m pytest tests/workers/test_tool_factory.py -q -k provenance
```

Expected: FAIL — `AttributeError: type object 'DynamicTool_static_grep_smali' has no attribute 'worker'`.

- [ ] **Step 3: Add the attribute**

In `agent_core/workers/tool_factory.py`, next to the other assignments (`_DynamicTool.name = prefixed` etc.):

```python
    _DynamicTool.worker = worker.name
```

- [ ] **Step 4: Run to verify it passes**

```bash
/mnt/secondary/projects/PARE/.venv/bin/python -m pytest tests/workers/test_tool_factory.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent_core/workers/tool_factory.py tests/workers/test_tool_factory.py
git commit -m "feat(workers): stamp worker provenance on synthesized tools

remove_worker() keys on this rather than the {worker}_ name prefix, so
unloading a worker cannot remove a declarative tool that happens to share
the prefix. Builtins define no such attribute and are structurally
unremovable."
```

---

### Task 4: `ToolExecutor` — a mutable registry

**Files:**
- Modify: `agent_core/tools/executor.py` (whole class)
- Modify: `agent_core/runtime.py:50-54` (pass `agent=` through `build`)
- Test: `tests/test_tools_executor.py` (append)

**Interfaces:**
- Consumes: `cls.worker` from Task 3.
- Produces:
  - `ToolExecutor(tools: dict[str, Tool], *, agent=None, disabled: frozenset[str] = frozenset())`
  - `.add(tool_cls: type[Tool]) -> None` — raises `ValueError` on name collision, `RuntimeError` on unmet `requires`
  - `.add_all(tool_classes: list[type[Tool]]) -> None` — validate all, then commit (atomic)
  - `.remove(name: str) -> bool`
  - `.remove_worker(worker: str) -> int`
  - `.__contains__(name: str) -> bool`
  - `.schemas()` emits a **stable order**; `.names()` keeps insertion order.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_tools_executor.py`:

```python
class _FakeAgent:
    pass


def _worker_tool(name, worker):
    from agent_core.tools.base import Tool

    class _T(Tool):
        pass
    _T.name = name
    _T.description = "d"
    _T.parameters = {"type": "object", "properties": {}}
    _T.worker = worker
    return _T


def test_add_and_remove_worker_tools():
    from agent_core.tools.executor import ToolExecutor
    ex = ToolExecutor.build(_FakeAgent(), [])
    before = set(ex.names())

    ex.add(_worker_tool("frida_attach", "frida"))
    ex.add(_worker_tool("frida_detach", "frida"))
    ex.add(_worker_tool("static_load_apk", "static"))
    assert "frida_attach" in ex

    removed = ex.remove_worker("frida")
    assert removed == 2
    assert "frida_attach" not in ex
    assert "static_load_apk" in ex

    ex.remove_worker("static")
    assert set(ex.names()) == before, "builtins must be untouched"


def test_add_refuses_to_shadow_an_existing_tool():
    """build() is last-write-wins and silent; add() must not be.

    PARE's StaticAnalyze is named "static_analyze"; if the static worker ever
    ships a tool called "analyze" the synthesized name collides exactly.
    """
    from agent_core.tools.executor import ToolExecutor
    ex = ToolExecutor.build(_FakeAgent(), [_worker_tool("static_analyze", None)])
    with pytest.raises(ValueError, match="static_analyze"):
        ex.add(_worker_tool("static_analyze", "static"))
    assert ex.names().count("static_analyze") == 1


def test_add_all_is_atomic():
    from agent_core.tools.executor import ToolExecutor
    ex = ToolExecutor.build(_FakeAgent(), [_worker_tool("static_analyze", None)])
    batch = [_worker_tool("static_load_apk", "static"),
             _worker_tool("static_analyze", "static")]   # second one collides
    with pytest.raises(ValueError):
        ex.add_all(batch)
    assert "static_load_apk" not in ex, "a rejected batch must add nothing"


def test_add_validates_requires():
    from agent_core.tools.executor import ToolExecutor
    ex = ToolExecutor.build(_FakeAgent(), [])
    cls = _worker_tool("needs_thing", "w")
    cls.requires = ("nonexistent_manager",)
    with pytest.raises(RuntimeError, match="nonexistent_manager"):
        ex.add(cls)


def test_add_honours_disabled():
    from agent_core.tools.executor import ToolExecutor
    ex = ToolExecutor.build(_FakeAgent(), [], disabled=frozenset({"frida_attach"}))
    ex.add(_worker_tool("frida_attach", "frida"))
    assert "frida_attach" not in ex


def test_schemas_order_is_stable_across_mutation():
    """schemas() is a prompt-cache prefix; dict insertion order would reshuffle
    it on every load/unload, and load_autoload() registers concurrently."""
    from agent_core.tools.executor import ToolExecutor
    ex = ToolExecutor.build(_FakeAgent(), [])
    ex.add(_worker_tool("static_load_apk", "static"))
    ex.add(_worker_tool("frida_attach", "frida"))
    first = [s["function"]["name"] for s in ex.schemas()]

    ex2 = ToolExecutor.build(_FakeAgent(), [])
    ex2.add(_worker_tool("frida_attach", "frida"))
    ex2.add(_worker_tool("static_load_apk", "static"))
    assert [s["function"]["name"] for s in ex2.schemas()] == first
```

- [ ] **Step 2: Run to verify they fail**

```bash
/mnt/secondary/projects/PARE/.venv/bin/python -m pytest tests/test_tools_executor.py -q
```

Expected: FAIL — `AttributeError: 'ToolExecutor' object has no attribute 'add'`.

- [ ] **Step 3: Rewrite the executor**

Replace the body of `agent_core/tools/executor.py` from `class ToolExecutor:` onward:

```python
class ToolExecutor:
    def __init__(self, tools: dict[str, Tool], *, agent=None,
                 disabled: frozenset[str] = frozenset()) -> None:
        self._tools = tools
        # Retained so add() can run the same `requires` validation build() does
        # and honour the same `disabled` set. The executor is already stored on
        # the agent (runtime.py:50) and every tool receives it via ctx.agent, so
        # this reference adds no lifetime coupling that did not already exist.
        self._agent = agent
        self._disabled = disabled

    @classmethod
    def build(
        cls,
        agent,
        agent_tool_classes: list[type[Tool]],
        disabled: frozenset[str] = frozenset(),
    ) -> "ToolExecutor":
        all_classes = [
            t for t in BUILTIN_TOOLS + list(agent_tool_classes) if t.name not in disabled
        ]
        instances: dict[str, Tool] = {}
        for tool_cls in all_classes:
            cls._validate_requires(agent, tool_cls)
            if tool_cls.name in instances:
                # 1.8.0 warns; raising here would break a consumer that
                # deliberately shadows a builtin, which is a 2.0.0 change.
                logger.warning(
                    "tool name collision: %r from %s replaces %s — the earlier "
                    "tool is now unreachable",
                    tool_cls.name, tool_cls.__name__,
                    type(instances[tool_cls.name]).__name__,
                )
            instances[tool_cls.name] = tool_cls()
        return cls(instances, agent=agent, disabled=disabled)

    @staticmethod
    def _validate_requires(agent, tool_cls: type[Tool]) -> None:
        for attr in tool_cls.requires:
            if not hasattr(agent, attr):
                raise RuntimeError(
                    f"Tool {tool_cls.name!r} requires agent.{attr!r}, "
                    f"but {type(agent).__name__} has no such attribute. "
                    f"Add it in setup(), or remove {tool_cls.name!r} from tools / disabled_builtins."
                )

    # --- runtime mutation -------------------------------------------------
    def add(self, tool_cls: type[Tool]) -> None:
        """Register one tool. Raises on collision or unmet requires."""
        if tool_cls.name in self._disabled:
            return
        if tool_cls.name in self._tools:
            existing = type(self._tools[tool_cls.name])
            raise ValueError(
                f"tool name collision: {tool_cls.name!r} is already registered by "
                f"{existing.__name__}; refusing to shadow it silently"
            )
        self._validate_requires(self._agent, tool_cls)
        self._tools[tool_cls.name] = tool_cls()

    def add_all(self, tool_classes: list[type[Tool]]) -> None:
        """Validate every class, then commit — all or nothing.

        Atomicity lives here rather than in the caller because the dict lives
        here; a worker advertising one shadowing tool among ten is exactly the
        partial-add this prevents.
        """
        pending = [t for t in tool_classes if t.name not in self._disabled]
        seen: set[str] = set()
        for tool_cls in pending:
            if tool_cls.name in self._tools or tool_cls.name in seen:
                raise ValueError(
                    f"tool name collision: {tool_cls.name!r} is already registered"
                )
            self._validate_requires(self._agent, tool_cls)
            seen.add(tool_cls.name)
        for tool_cls in pending:
            self._tools[tool_cls.name] = tool_cls()

    def remove(self, name: str) -> bool:
        return self._tools.pop(name, None) is not None

    def remove_worker(self, worker: str) -> int:
        """Remove every tool synthesized for `worker`, by provenance."""
        doomed = [n for n, t in self._tools.items()
                  if getattr(type(t), "worker", None) == worker]
        for name in doomed:
            del self._tools[name]
        return len(doomed)

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    async def run(self, name: str, arguments: dict, ctx: "HandlerContext") -> str:
        tool = self._tools.get(name)
        if tool is None:
            return f"Unknown tool: {name}"
        try:
            return await tool.run(arguments, ctx)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return f"Error in {name}: {exc}"

    def schemas(self) -> list[dict]:
        """Stable order: non-worker tools in insertion order, then worker tools
        sorted by (worker, name).

        This is a prompt prefix. Dict insertion order would reshuffle it on
        every load/unload, and load_autoload() registers workers concurrently,
        so registration order is not even deterministic. names() deliberately
        keeps insertion order — it has no consumer-visible meaning.
        """
        plain, owned = [], []
        for tool in self._tools.values():
            (owned if getattr(type(tool), "worker", None) else plain).append(tool)
        owned.sort(key=lambda t: (type(t).worker, type(t).name))
        return [type(t).to_openai_schema() for t in plain + owned]

    def names(self) -> list[str]:
        return list(self._tools)
```

Add `import logging` and `logger = logging.getLogger(__name__)` at the top of the file, below the existing imports.

- [ ] **Step 4: Run to verify they pass**

```bash
/mnt/secondary/projects/PARE/.venv/bin/python -m pytest tests/test_tools_executor.py -q
```

Expected: PASS, including the pre-existing `test_names_preserves_insertion_order` at line 118 — `names()` is deliberately untouched.

- [ ] **Step 5: Run the full suite**

```bash
/mnt/secondary/projects/PARE/.venv/bin/python -m pytest -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add agent_core/tools/executor.py tests/test_tools_executor.py
git commit -m "feat(tools): make ToolExecutor mutable at runtime

add/add_all/remove/remove_worker, with the agent and disabled set stored
at build time so add() runs the same requires validation build() does.
add() refuses to shadow an existing name (build() only warns -- raising
there would break a consumer that intentionally shadows a builtin, which
is a 2.0.0 change). add_all validates the whole batch before committing
any of it. schemas() now emits a stable order, since it is a prompt-cache
prefix and concurrent autoload makes registration order nondeterministic."
```

---

### Task 5: `RiskAwareToolPool` — the security invariants

Four changes, all in one class, all in service of one property: **no load/unload/reload sequence may lower a tool's effective tier or launder an approval.**

**Files:**
- Modify: `agent_core/workers/risk_pool.py`
- Modify: `agent_core/workers/types.py` (Outcome values, nullable `tool`)
- Test: `tests/workers/test_risk_pool_lifecycle.py` (create)

**Interfaces:**
- Consumes: `MCPClientPool.spec(name)` from Task 2.
- Produces:
  - `RiskAwareToolPool.add_spec(spec)` / `.remove_spec(name)` — bump generation, evict approvals
  - `.generation(worker) -> int`
  - `_tier_highwater` — never evicted
  - `AuditEntry` accepts `outcome="cancelled"`, `"worker_loaded"`, `"worker_unloaded"`, and `tool=None`
  - `.emit_lifecycle(worker, action, detail)` — used by Task 6

- [ ] **Step 1: Write the failing tests**

Create `tests/workers/test_risk_pool_lifecycle.py`:

```python
"""Security invariants that must hold ACROSS load/unload/reload.

Each test here corresponds to a way the boot-frozen daemon was safe by
construction and the runtime-mutable one is not (spec section 6.4).
"""
import asyncio

import pytest

from agent_core.workers.audit import AuditLog
from agent_core.workers.client_pool import MCPClientPool
from agent_core.workers.risk import RiskGate
from agent_core.workers.risk_pool import RiskAwareToolPool
from agent_core.workers.tool_approval import ToolApprovalRegistry, ToolDecision
from agent_core.workers.types import WorkerSpec


def _pool(tmp_path, spec):
    inner = MCPClientPool([spec])
    return inner, RiskAwareToolPool(
        inner=inner, specs={spec.name: spec}, risk_gate=RiskGate(overrides=[]),
        approval_registry=ToolApprovalRegistry(), audit_log=AuditLog(tmp_path),
    )


def _spec(name="frida", floor="low"):
    return WorkerSpec(name=name, transport="stdio", risk_default=floor,
                      command="/bin/true")


class _Tool:
    def __init__(self, name, tier):
        self.name = name
        self.meta = {"agent_core/risk_tier": tier} if tier else None


class _Listing:
    def __init__(self, tools):
        self.tools = tools


async def test_tier_never_ratchets_down_on_reload(tmp_path, monkeypatch):
    """A reload against a build that stops advertising must NOT fall to the floor.

    frida's risk_default is low and only execute_script/write_memory are pinned,
    so read_memory and java_hook are protected solely by the wire tier. Without
    a high-water mark, a reload silently makes them auto-execute.
    """
    spec = _spec()
    inner, pool = _pool(tmp_path, spec)

    async def listing_high(worker):
        return _Listing([_Tool("read_memory", "high")])
    monkeypatch.setattr(inner, "list_tools", listing_high)
    await pool.list_tools("frida")
    assert pool.resolve_effective("frida", "read_memory") == "high"

    # Reload: the new build advertises nothing.
    pool.remove_spec("frida")
    pool.add_spec(spec)

    async def listing_silent(worker):
        return _Listing([_Tool("read_memory", None)])
    monkeypatch.setattr(inner, "list_tools", listing_silent)
    await pool.list_tools("frida")

    assert pool.resolve_effective("frida", "read_memory") == "high", (
        "reload lowered the effective tier — a wire downgrade must fail closed"
    )


async def test_session_approval_does_not_survive_a_reload(tmp_path):
    """An approval resolving AFTER the reload must not apply to the new process.

    Evicting on unload and load is not enough: the operator answers the prompt
    on their own schedule, and that can land after the load completes.
    """
    spec = _spec()
    inner, pool = _pool(tmp_path, spec)

    gen_before = pool.generation("frida")
    # Simulate the approval being granted against the pre-reload generation.
    pool.record_session_approval("frida", "java_hook", gen_before)
    assert pool.is_session_approved("frida", "java_hook")

    pool.remove_spec("frida")
    pool.add_spec(spec)

    assert not pool.is_session_approved("frida", "java_hook")
    # And a late-resolving approval stamped with the OLD generation is dropped.
    pool.record_session_approval("frida", "java_hook", gen_before)
    assert not pool.is_session_approved("frida", "java_hook"), (
        "an approval granted before the reload was applied to the new worker"
    )


async def test_close_all_also_clears_approvals(tmp_path):
    """close_all lazily reconnects fresh subprocesses; approvals must not carry."""
    spec = _spec()
    inner, pool = _pool(tmp_path, spec)
    pool.record_session_approval("frida", "java_hook", pool.generation("frida"))
    await pool.close_all()
    assert not pool.is_session_approved("frida", "java_hook")


async def test_cancelled_dispatch_is_audited(tmp_path, monkeypatch):
    """Unload mid-dispatch surfaces as CancelledError, a BaseException that every
    guard on this path misses — so the dispatch executed with no audit row."""
    spec = _spec(floor="low")
    inner, pool = _pool(tmp_path, spec)

    async def boom(worker, tool, arguments):
        raise asyncio.CancelledError()
    monkeypatch.setattr(inner, "call_tool", boom)

    with pytest.raises(asyncio.CancelledError):
        await pool.call_tool("frida", "list_devices", {})

    rows = pool._audit.read_all() if hasattr(pool._audit, "read_all") else None
    assert rows, "expected an audit row for the cancelled dispatch"
    assert rows[-1].outcome == "cancelled"


async def test_specs_are_read_through_not_duplicated(tmp_path):
    """One source of truth: a spec removed from the inner pool is gone here too.

    If the two dicts drift with the inner cleared and the outer kept, dispatch
    resolves at the worker's floor and skips HITL entirely.
    """
    spec = _spec()
    inner, pool = _pool(tmp_path, spec)
    inner.remove_spec("frida")
    assert pool.spec_for("frida") is None
```

- [ ] **Step 2: Run to verify they fail**

```bash
/mnt/secondary/projects/PARE/.venv/bin/python -m pytest tests/workers/test_risk_pool_lifecycle.py -q
```

Expected: FAIL — `AttributeError: 'RiskAwareToolPool' object has no attribute 'resolve_effective'`.

- [ ] **Step 3: Extend `types.py`**

In `agent_core/workers/types.py`, extend the `Outcome` literal (currently ending `"approval_undeliverable",`):

```python
Outcome = Literal[
    "ok",
    "error",
    "hitl_approved",
    "hitl_denied",
    "validation_failed",
    "timeout",
    "cancelled",
    "approval_undeliverable",
    "worker_loaded",
    "worker_unloaded",
]
```

and make `AuditEntry.tool` nullable, since a lifecycle row names no tool:

```python
    tool: str | None = None
    """None for control-plane rows (worker_loaded / worker_unloaded)."""
```

- [ ] **Step 4: Modify `risk_pool.py`**

In `__init__`, replace `self._specs = specs` with generation and high-water state, seeding the inner pool so `specs=` keeps working:

```python
        self._inner = inner
        for spec in (specs or {}).values():
            inner.add_spec(spec)
        self._gate = risk_gate
        self._registry = approval_registry
        self._audit = audit_log
        self._send = send_message
        self._capture = capture_layer
        # (worker, tool, generation) — an approval is scoped to the exact worker
        # instance it was granted against.
        self._session_approved: set[tuple[str, str, int]] = set()
        self._tool_tiers: dict[tuple[str, str], str | None] = {}
        # Highest tier ever observed for a tool this session. NEVER evicted:
        # escalate-only must be monotonic across time, not just within one
        # resolution, or a reload becomes a downgrade channel (spec 6.4.1).
        self._tier_highwater: dict[tuple[str, str], str] = {}
        self._generations: dict[str, int] = {}
```

Add the lifecycle surface:

```python
    def spec_for(self, worker: str):
        """Read through to the inner pool — one source of truth for specs."""
        return self._inner.spec(worker)

    def generation(self, worker: str) -> int:
        return self._generations.get(worker, 0)

    def _bump(self, worker: str) -> None:
        self._generations[worker] = self.generation(worker) + 1
        self._session_approved = {
            e for e in self._session_approved if e[0] != worker
        }
        self._tool_tiers = {
            k: v for k, v in self._tool_tiers.items() if k[0] != worker
        }
        # _tier_highwater is deliberately NOT cleared.

    def add_spec(self, spec) -> None:
        self._inner.add_spec(spec)
        self._bump(spec.name)

    def remove_spec(self, worker: str) -> None:
        self._inner.remove_spec(worker)
        self._bump(worker)

    def record_session_approval(self, worker: str, tool: str, generation: int) -> None:
        """Record only if the worker has not been reloaded since the approval
        was requested. An operator answering a prompt after a reload must not
        pre-approve the new process."""
        if generation == self.generation(worker):
            self._session_approved.add((worker, tool, generation))

    def is_session_approved(self, worker: str, tool: str) -> bool:
        return (worker, tool, self.generation(worker)) in self._session_approved

    def resolve_effective(self, worker: str, tool: str) -> str:
        """The tier a dispatch would resolve to right now. Extracted so tests
        and the tier-ratchet check can ask without dispatching."""
        advertised = self._tool_tiers.get((worker, tool))
        high = self._tier_highwater.get((worker, tool))
        declared, _ = resolve_declared_tier(self.spec_for(worker),
                                            _max_tier(advertised, high))
        return self._gate.evaluate(worker=worker, tool=tool,
                                   declared_tier=declared).effective_tier

    def emit_lifecycle(self, worker: str, action: str, detail: str | None = None,
                       args: dict | None = None) -> None:
        """Control-plane audit row. Load/unload changes the enforcement config
        itself, which is the first thing an audit log exists for."""
        self._audit.append(AuditEntry(
            request_id=uuid.uuid4().hex, worker=worker, tool=None,
            args=args or {}, declared_tier="low", effective_tier="low",
            override_reason=None, detail=detail, outcome=action,
            latency_ms=0, session_guid="pending", worker_contract_version=1,
            tier_source=None,
        ))
```

Add the module-level helper next to the imports:

```python
_TIER_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}


def _max_tier(a: str | None, b: str | None) -> str | None:
    """The higher of two tiers; None only if both are None/unknown."""
    known = [t for t in (a, b) if t in _TIER_ORDER]
    return max(known, key=lambda t: _TIER_ORDER[t]) if known else None
```

In `list_tools`, record the high-water mark alongside the advertised tier:

```python
            self._tool_tiers[(worker, name)] = tier
            hw = _max_tier(tier, self._tier_highwater.get((worker, name)))
            if hw is not None:
                self._tier_highwater[(worker, name)] = hw
```

In `call_tool`, resolve against the high-water mark and capture the generation:

```python
        spec = self.spec_for(worker)
        advertised = _max_tier(self._tool_tiers.get((worker, tool)),
                               self._tier_highwater.get((worker, tool)))
        declared, tier_source = resolve_declared_tier(spec, advertised)
        gen = self.generation(worker)
```

Change the session-approval read (was `(worker, tool) in self._session_approved`):

```python
            if effective != "critical" and self.is_session_approved(worker, tool):
```

and pass `gen` into `_await_operator`, whose signature gains a `generation` parameter. Replace the approval-record line (`self._session_approved.add((worker, tool))`) with:

```python
        if decision.scope == "session" and effective != "critical":
            self.record_session_approval(worker, tool, generation)
        if generation != self.generation(worker):
            return _ErrorResult(
                f"{worker} was reloaded while approval was pending; re-issue the call")
```

Fix the stale comment at the top of `call_tool` — it currently claims the `None` case "fails safe to 'high' for internal workers", which `risk.py:68` contradicts and which is what made the original design reason wrongly:

```python
        # A tool with no advertised tier resolves to the worker's risk_default
        # FLOOR (risk.py:68) -- not a fail-safe to high. Safety for dangerous
        # tools that fail to advertise comes from operator pins plus the
        # session high-water mark below, never from a dispatch-time fallback.
```

In `_execute_and_audit`, add the cancellation row **before** the existing `except Exception`:

```python
        except asyncio.CancelledError:
            self._emit(worker, tool, snapshot, declared, effective,
                       int((time.monotonic() - start) * 1000),
                       "cancelled", gate_override, "worker disconnected mid-dispatch",
                       tier_source)
            raise
```

and add `import asyncio` at the top of the file.

Finally, in `close_all`, bump every generation so a lazy reconnect cannot inherit approvals:

```python
    async def close_all(self) -> None:
        for worker in list(self._generations) or self._inner.names():
            self._bump(worker)
        await self._inner.close_all()
```

- [ ] **Step 5: Run to verify they pass**

```bash
/mnt/secondary/projects/PARE/.venv/bin/python -m pytest tests/workers/test_risk_pool_lifecycle.py -q
```

Expected: PASS. If `test_cancelled_dispatch_is_audited` fails on `read_all`, check `agent_core/workers/audit.py` for the reader's actual name and use it (the assertion, not the accessor, is the point).

- [ ] **Step 6: Run the full suite**

```bash
/mnt/secondary/projects/PARE/.venv/bin/python -m pytest -q
```

Expected: PASS. `tests/workers/test_risk_pool*.py` exercise `_session_approved` — if any assert on the old 2-tuple shape, update them to use `is_session_approved()`.

- [ ] **Step 7: Commit**

```bash
git add agent_core/workers/risk_pool.py agent_core/workers/types.py tests/workers/test_risk_pool_lifecycle.py
git commit -m "feat(workers): hold risk invariants across worker reloads

Four changes, one property: no load/unload/reload may lower a tool's
effective tier or launder an approval.

- Session tier high-water mark, never evicted. escalate-only was monotonic
  within a resolution but not across time; a reload against a build that
  stops advertising would drop frida_read_memory to the low floor.
- Approvals keyed by worker generation, so one resolving after a reload
  does not apply to the new process, and a call approved before a reload
  is refused rather than dispatched against a different binary.
- CancelledError is audited before re-raising. Unload mid-dispatch is a
  BaseException that every guard on this path missed, so a dispatch could
  execute leaving no record. Outcome had a 'cancelled' slot nothing had
  ever emitted.
- Specs read through to MCPClientPool instead of a second dict, whose
  drift could resolve at the floor and skip HITL entirely.

Also corrects a stale comment claiming a missing wire tier fails safe to
high; risk.py uses the floor, and that comment is what made the original
lifecycle design reason wrongly."
```

---

### Task 6: `WorkerManager`

**Files:**
- Create: `agent_core/workers/manager.py`
- Create: `tests/workers/test_manager.py`

**Interfaces:**
- Consumes: everything from Tasks 2–5.
- Produces:
  - `WorkerOpResult(op, name, ok, tool_count, tools, error, error_kind)`
  - `WorkerStatus(name, loaded, tool_count, transport, risk_default, capability_tags, autoload, last_error)`
  - `WorkerManager(registry, tool_pool, executor, *, connect_timeout=10.0)`
  - `async load(name)`, `async unload(name)`, `async reload(name)`, `async load_autoload()`, `async close_all()`
  - `status() -> list[WorkerStatus]`, `unavailable_reason(name) -> str | None`

- [ ] **Step 1: Write the failing tests**

Create `tests/workers/test_manager.py`:

```python
"""WorkerManager lifecycle against a real stdio worker subprocess."""
import asyncio

import pytest

from agent_core.tools.executor import ToolExecutor
from agent_core.workers.audit import AuditLog
from agent_core.workers.client_pool import MCPClientPool
from agent_core.workers.manager import WorkerManager
from agent_core.workers.registry import WorkerRegistry
from agent_core.workers.risk import RiskGate
from agent_core.workers.risk_pool import RiskAwareToolPool
from agent_core.workers.tool_approval import ToolApprovalRegistry
from agent_core.workers.types import WorkerSpec


class _Agent:
    pass


def _manager(tmp_path, specs):
    reg = WorkerRegistry()
    for s in specs:
        reg.add(s)
    inner = MCPClientPool([])
    pool = RiskAwareToolPool(
        inner=inner, specs={}, risk_gate=RiskGate(overrides=[]),
        approval_registry=ToolApprovalRegistry(), audit_log=AuditLog(tmp_path))
    ex = ToolExecutor.build(_Agent(), [])
    return WorkerManager(reg, pool, ex), pool, ex, inner


async def test_load_registers_prefixed_tools(tmp_path, stdio_stub_spec):
    mgr, pool, ex, inner = _manager(tmp_path, [stdio_stub_spec("stub", "low")])
    res = await mgr.load("stub")
    assert res.ok, res.error
    assert res.tool_count == 2
    assert "stub_noop_low" in ex and "stub_risky_high" in ex


async def test_unload_removes_only_its_own_tools(tmp_path, stdio_stub_spec):
    mgr, pool, ex, inner = _manager(tmp_path, [stdio_stub_spec("stub", "low")])
    builtins = set(ex.names())
    await mgr.load("stub")
    res = await mgr.unload("stub")
    assert res.ok and res.tool_count == 2
    assert set(ex.names()) == builtins
    assert not inner.is_connected("stub")


async def test_unload_from_a_different_task(tmp_path, stdio_stub_spec):
    """The daemon loads in astartup's task and unloads in a handler task."""
    mgr, pool, ex, inner = _manager(tmp_path, [stdio_stub_spec("stub", "low")])
    await mgr.load("stub")
    res = await asyncio.create_task(mgr.unload("stub"))
    assert res.ok, res.error
    assert not inner.is_connected("stub")


async def test_dispatch_after_unload_does_not_resurrect_the_worker(
        tmp_path, stdio_stub_spec):
    """If the pool keeps declared (not loaded) specs, the next call lazily
    respawns the worker you just unloaded, silently undoing the unload."""
    mgr, pool, ex, inner = _manager(tmp_path, [stdio_stub_spec("stub", "low")])
    await mgr.load("stub")
    await mgr.unload("stub")
    with pytest.raises(KeyError):
        await inner.list_tools("stub")
    assert not inner.is_connected("stub")


async def test_reload_yields_a_fresh_process(tmp_path, stdio_stub_spec):
    mgr, pool, ex, inner = _manager(tmp_path, [stdio_stub_spec("stub", "low")])
    await mgr.load("stub")
    first = inner._owner_pid("stub")
    res = await mgr.reload("stub")
    assert res.ok, res.error
    assert inner._owner_pid("stub") != first


async def test_failed_load_leaves_no_residue(tmp_path):
    spec = WorkerSpec(name="broken", transport="stdio", risk_default="low",
                      command="/nonexistent/binary")
    mgr, pool, ex, inner = _manager(tmp_path, [spec])
    res = await mgr.load("broken")
    assert not res.ok
    assert res.error_kind == "spawn_failed"
    assert inner.spec("broken") is None
    assert not any(n.startswith("broken_") for n in ex.names())
    assert mgr.status()[0].last_error


async def test_load_of_unknown_worker(tmp_path, stdio_stub_spec):
    mgr, *_ = _manager(tmp_path, [stdio_stub_spec("stub", "low")])
    res = await mgr.load("nope")
    assert not res.ok and res.error_kind == "unknown_worker"
    assert "stub" in res.error


async def test_load_and_unload_are_idempotent(tmp_path, stdio_stub_spec):
    mgr, pool, ex, inner = _manager(tmp_path, [stdio_stub_spec("stub", "low")])
    assert (await mgr.load("stub")).ok
    assert (await mgr.load("stub")).ok
    assert (await mgr.unload("stub")).ok
    assert (await mgr.unload("stub")).ok


async def test_collision_fails_the_whole_load(tmp_path, stdio_stub_spec):
    """One shadowing tool among many must not half-register the worker."""
    from agent_core.tools.base import Tool

    class _Clash(Tool):
        name = "stub_noop_low"
        description = "d"
        parameters = {"type": "object", "properties": {}}

    reg = WorkerRegistry()
    reg.add(stdio_stub_spec("stub", "low"))
    inner = MCPClientPool([])
    pool = RiskAwareToolPool(
        inner=inner, specs={}, risk_gate=RiskGate(overrides=[]),
        approval_registry=ToolApprovalRegistry(), audit_log=AuditLog(tmp_path))
    ex = ToolExecutor.build(_Agent(), [_Clash])
    mgr = WorkerManager(reg, pool, ex)

    res = await mgr.load("stub")
    assert not res.ok and res.error_kind == "tool_collision"
    assert "stub_noop_low" in res.error
    assert "stub_risky_high" not in ex, "partial registration"


async def test_load_autoload_skips_autoload_false(tmp_path, stdio_stub_spec):
    on = stdio_stub_spec("on", "low")
    off = stdio_stub_spec("off", "low")
    off = off.model_copy(update={"autoload": False})
    mgr, pool, ex, inner = _manager(tmp_path, [on, off])
    results = await mgr.load_autoload()
    assert {r.name for r in results} == {"on"}
    assert inner.is_connected("on") and not inner.is_connected("off")


async def test_unavailable_reason(tmp_path, stdio_stub_spec):
    mgr, pool, ex, inner = _manager(tmp_path, [stdio_stub_spec("stub", "low")])
    assert "not loaded" in mgr.unavailable_reason("stub")
    await mgr.load("stub")
    assert mgr.unavailable_reason("stub") is None
    assert "not declared" in mgr.unavailable_reason("ghost")


async def test_lifecycle_rows_are_audited(tmp_path, stdio_stub_spec):
    mgr, pool, ex, inner = _manager(tmp_path, [stdio_stub_spec("stub", "low")])
    await mgr.load("stub")
    await mgr.unload("stub")
    rows = pool._audit.read_all()
    outcomes = [r.outcome for r in rows]
    assert "worker_loaded" in outcomes and "worker_unloaded" in outcomes
```

- [ ] **Step 2: Run to verify they fail**

```bash
/mnt/secondary/projects/PARE/.venv/bin/python -m pytest tests/workers/test_manager.py -q
```

Expected: FAIL — `ModuleNotFoundError: No module named 'agent_core.workers.manager'`.

- [ ] **Step 3: Write `manager.py`**

Create `agent_core/workers/manager.py`:

```python
"""WorkerManager — the runtime worker lifecycle.

The only component that mutates the worker registry, the connection pool and
the tool executor together. Boot and runtime share one path: `load_autoload()`
calls the same `load()` an operator command does, so the two cannot drift.

Every public method returns a result object rather than raising. A worker that
fails to load must not take down the daemon or the caller's turn — the failure
belongs in `/worker list`, not in a traceback.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Literal

from agent_core.tools.base import Tool
from agent_core.workers.registry import WorkerNotFoundError, WorkerRegistry
from agent_core.workers.tool_factory import make_tool_class

logger = logging.getLogger(__name__)

DEFAULT_CONNECT_TIMEOUT = 10.0
DEFAULT_DISCONNECT_TIMEOUT = 5.0

ErrorKind = Literal[
    "unknown_worker", "spawn_failed", "connect_timeout", "protocol_mismatch",
    "tool_collision", "list_tools_failed", "disconnect_timeout",
]


@dataclass
class WorkerOpResult:
    """One result shape for load/unload/reload.

    Reload needs somewhere to say "the unload half failed" — which is exactly
    the state a wedged teardown produces — so it cannot be a LoadResult.
    """
    op: Literal["load", "unload", "reload"]
    name: str
    ok: bool
    tool_count: int = 0
    tools: list[str] = field(default_factory=list)
    error: str | None = None
    error_kind: ErrorKind | None = None


@dataclass
class WorkerStatus:
    name: str
    loaded: bool
    tool_count: int
    transport: str
    risk_default: str
    capability_tags: list[str]
    autoload: bool
    last_error: str | None = None


class WorkerManager:
    def __init__(self, registry: WorkerRegistry, tool_pool, executor,
                 *, connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
                 disconnect_timeout: float = DEFAULT_DISCONNECT_TIMEOUT) -> None:
        self._registry = registry
        self._pool = tool_pool
        self._executor = executor
        self._connect_timeout = connect_timeout
        self._disconnect_timeout = disconnect_timeout
        self._loaded: dict[str, list[str]] = {}
        self._errors: dict[str, str] = {}
        # Held across a whole load/unload/reload body. The pool's per-worker
        # lock only guards connect+initialize; without this, a concurrent
        # unload's remove_worker can land after a load's add_all, leaving a
        # connected worker whose tools are invisible.
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    # --- queries ----------------------------------------------------------
    def is_loaded(self, name: str) -> bool:
        return name in self._loaded

    def status(self) -> list[WorkerStatus]:
        out = []
        for spec in self._registry.all():
            out.append(WorkerStatus(
                name=spec.name,
                loaded=self.is_loaded(spec.name),
                tool_count=len(self._loaded.get(spec.name, [])),
                transport=spec.transport,
                risk_default=spec.risk_default,
                capability_tags=list(spec.capability_tags),
                autoload=spec.autoload,
                last_error=self._errors.get(spec.name),
            ))
        return out

    def tools_of(self, name: str) -> list[str]:
        return list(self._loaded.get(name, []))

    def unavailable_reason(self, name: str) -> str | None:
        """Advisory only — enforcement stays at RiskAwareToolPool.call_tool.

        Reports "loaded" only once the executor registration has completed, so
        callers that bypass the executor cannot dispatch during the load window
        while the tier table is still empty.
        """
        if self.is_loaded(name):
            return None
        try:
            self._registry.get(name)
        except WorkerNotFoundError:
            return (f"worker {name!r} is not declared in workers.yaml — "
                    f"declared workers: {sorted(s.name for s in self._registry.all())}")
        err = self._errors.get(name)
        tail = f" (last error: {err})" if err else ""
        return (f"worker {name!r} is not loaded — its tools are unavailable this "
                f"session. Ask the operator to run /worker load {name}.{tail}")

    def worker_of(self, tool_name: str) -> str | None:
        """Map a prefixed tool name back to its declared worker, longest first
        so a worker named `x` cannot claim `x_y_z` belonging to `x_y`."""
        for spec in sorted(self._registry.all(), key=lambda s: -len(s.name)):
            if tool_name.startswith(f"{spec.name}_"):
                return spec.name
        return None

    # --- lifecycle --------------------------------------------------------
    async def load(self, name: str) -> WorkerOpResult:
        try:
            spec = self._registry.get(name)
        except WorkerNotFoundError:
            return WorkerOpResult(
                "load", name, False, error_kind="unknown_worker",
                error=(f"no worker named {name!r}; declared: "
                       f"{sorted(s.name for s in self._registry.all())}"))
        async with self._locks[name]:
            if self.is_loaded(name):
                return WorkerOpResult("load", name, True,
                                      tool_count=len(self._loaded[name]),
                                      tools=list(self._loaded[name]))
            return await self._load_locked(spec)

    async def _load_locked(self, spec) -> WorkerOpResult:
        name = spec.name
        self._pool.add_spec(spec)
        try:
            await self._pool._inner.connect(name, timeout=self._connect_timeout)
        except asyncio.TimeoutError:
            return await self._fail(name, "connect_timeout",
                                    f"worker {name!r} did not connect within "
                                    f"{self._connect_timeout}s")
        except FileNotFoundError as exc:
            return await self._fail(name, "spawn_failed", str(exc))
        except Exception as exc:
            kind = "protocol_mismatch" if "version" in str(exc).lower() else "spawn_failed"
            return await self._fail(name, kind, f"{type(exc).__name__}: {exc}")

        try:
            listing = await self._pool.list_tools(name)
        except Exception as exc:
            return await self._fail(name, "list_tools_failed",
                                    f"{type(exc).__name__}: {exc}")

        classes: list[type[Tool]] = []
        for tool in getattr(listing, "tools", []) or []:
            tool_name = getattr(tool, "name", None)
            if tool_name is None:
                continue
            classes.append(make_tool_class(spec, {
                "name": tool_name,
                "description": getattr(tool, "description", "") or "",
                "inputSchema": getattr(tool, "inputSchema", None)
                or {"type": "object", "properties": {}},
            }, self._pool))
        try:
            self._executor.add_all(classes)
        except ValueError as exc:
            return await self._fail(name, "tool_collision", str(exc))
        except RuntimeError as exc:
            return await self._fail(name, "spawn_failed", str(exc))

        names = [c.name for c in classes]
        self._loaded[name] = names
        self._errors.pop(name, None)
        self._pool.emit_lifecycle(name, "worker_loaded",
                                  args={"transport": spec.transport,
                                        "command": spec.command or spec.endpoint,
                                        "tool_count": len(names)})
        logger.info("loaded worker %s (%d tools)", name, len(names))
        return WorkerOpResult("load", name, True, tool_count=len(names), tools=names)

    async def _fail(self, name: str, kind: ErrorKind, message: str) -> WorkerOpResult:
        """Roll a partial load back to the unloaded state."""
        with contextlib.suppress(BaseException):
            await self._pool._inner.disconnect(name)
        self._executor.remove_worker(name)
        self._pool.remove_spec(name)
        self._loaded.pop(name, None)
        self._errors[name] = message
        logger.warning("worker %s load failed (%s): %s", name, kind, message)
        return WorkerOpResult("load", name, False, error=message, error_kind=kind)

    async def unload(self, name: str) -> WorkerOpResult:
        async with self._locks[name]:
            if not self.is_loaded(name):
                return WorkerOpResult("unload", name, True)
            # Order matters: everything before the disconnect is unconditional
            # and cannot hang, so a wedged teardown can never leave a worker
            # whose tools are gone but whose spec and approvals remain.
            removed = self._executor.remove_worker(name)
            self._pool.remove_spec(name)          # bumps generation, evicts approvals
            self._loaded.pop(name, None)
            err = kind = None
            try:
                await asyncio.wait_for(self._pool._inner.disconnect(name),
                                       timeout=self._disconnect_timeout)
            except asyncio.TimeoutError:
                kind, err = "disconnect_timeout", (
                    f"worker {name!r} did not shut down within "
                    f"{self._disconnect_timeout}s; its process may still be running")
                self._errors[name] = err
                logger.warning(err)
            self._pool.emit_lifecycle(name, "worker_unloaded",
                                      detail=err, args={"tools_removed": removed})
            return WorkerOpResult("unload", name, err is None,
                                  tool_count=removed, error=err, error_kind=kind)

    async def reload(self, name: str) -> WorkerOpResult:
        un = await self.unload(name)
        if not un.ok:
            return WorkerOpResult("reload", name, False,
                                  error=f"unload half failed: {un.error}",
                                  error_kind=un.error_kind)
        res = await self.load(name)
        return WorkerOpResult("reload", name, res.ok, res.tool_count, res.tools,
                              res.error, res.error_kind)

    async def load_autoload(self) -> list[WorkerOpResult]:
        """Boot path. Gathers so boot costs max(), not sum()."""
        targets = [s.name for s in self._registry.all() if s.autoload]
        if not targets:
            return []
        return list(await asyncio.gather(*(self.load(n) for n in targets)))

    async def close_all(self) -> None:
        for name in list(self._loaded):
            with contextlib.suppress(BaseException):
                await self.unload(name)
        with contextlib.suppress(BaseException):
            await self._pool.close_all()
```

- [ ] **Step 4: Run to verify they pass**

```bash
/mnt/secondary/projects/PARE/.venv/bin/python -m pytest tests/workers/test_manager.py -q
```

Expected: PASS (13 tests).

- [ ] **Step 5: Commit**

```bash
git add agent_core/workers/manager.py tests/workers/test_manager.py
git commit -m "feat(workers): add WorkerManager for runtime load/unload/reload

The only component that mutates registry, pool and executor together.
Boot goes through the same load() an operator command does, so the two
paths cannot drift. Load is transactional and bounded: a failure rolls
back to the unloaded state and reports a discriminated error_kind rather
than free text, so callers do not string-match prose. Unload does every
unconditional step -- executor removal, spec removal, approval eviction --
before the disconnect, so a wedged teardown cannot leave a half-unloaded
worker with standing approvals. A manager-level lock spans the whole
transaction; the pool's per-worker lock only covers connect."
```

---

### Task 7: `astartup()` / `ashutdown()` and `Daemon.serve()`

**Files:**
- Modify: `agent_core/agent.py` (add two methods to `Agent`)
- Modify: `agent_core/daemon.py:48-61` (`serve`)
- Test: `tests/test_daemon_startup.py` (create)

**Interfaces:**
- Produces: `async Agent.astartup() -> None` and `async Agent.ashutdown() -> None`, both no-op by default; `Daemon.serve()` awaits `astartup()` after binding but **before** accepting, and `ashutdown()` on exit.

- [ ] **Step 1: Write the failing test**

Create `tests/test_daemon_startup.py`:

```python
"""The daemon must finish astartup() before it dispatches anything.

start_unix_server defaults to start_serving=True, so the naive "bind, then
astartup" ordering already accepts connections and spawns handler tasks during
startup -- a chat turn can land against a half-populated tool executor.
"""
import asyncio

import pytest

from agent_core.agent import Agent
from agent_core.daemon import Daemon


class _Agent(Agent):
    name = "startup-probe"

    def __init__(self, socket_path):
        super().__init__()
        self.events = []
        self._socket_path = socket_path
        self.config = type("C", (), {"socket_path": socket_path})()

    async def astartup(self):
        self.events.append("astartup-begin")
        await asyncio.sleep(0.2)
        self.events.append("astartup-end")

    async def ashutdown(self):
        self.events.append("ashutdown")

    def system_prompt(self, ctx):
        return ""


async def test_astartup_completes_before_first_connection(tmp_path):
    sock = tmp_path / "probe.sock"
    agent = _Agent(sock)
    daemon = Daemon(agent)
    server_task = asyncio.create_task(daemon.serve())

    # Wait for the socket file to exist, then connect immediately.
    for _ in range(100):
        if sock.exists():
            break
        await asyncio.sleep(0.01)
    assert sock.exists(), "socket should be bound before astartup finishes"

    reader, writer = await asyncio.open_unix_connection(str(sock))
    agent.events.append("connected")
    writer.close()

    server_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await server_task

    assert agent.events.index("astartup-end") < agent.events.index("connected"), (
        f"a connection was serviced before astartup finished: {agent.events}")


def test_default_hooks_are_noops():
    class _Bare(Agent):
        name = "bare"
        def system_prompt(self, ctx):
            return ""

    a = _Bare()
    asyncio.run(a.astartup())
    asyncio.run(a.ashutdown())
```

- [ ] **Step 2: Run to verify it fails**

```bash
/mnt/secondary/projects/PARE/.venv/bin/python -m pytest tests/test_daemon_startup.py -q
```

Expected: FAIL — `AttributeError: 'Agent' object has no attribute 'astartup'`.

- [ ] **Step 3: Add the hooks to `Agent`**

In `agent_core/agent.py`, after `setup()`:

```python
    async def astartup(self) -> None:
        """Async startup, awaited by Daemon.serve() after the socket is bound
        but before any connection is serviced.

        This is where worker discovery belongs: it runs in the serving loop and
        in a task that outlives the connections it opens, which is what the
        MCP client's anyio cancel scopes require. Default no-op.
        """

    async def ashutdown(self) -> None:
        """Async teardown, awaited when the daemon stops serving. Default no-op."""
```

- [ ] **Step 4: Rewrite `Daemon.serve`**

```python
    async def serve(self) -> None:
        """Bind the socket, run agent startup, then accept connections."""
        socket_path = self.agent.config.socket_path
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        if socket_path.exists():
            socket_path.unlink()
        # start_serving=False: the socket exists immediately (so a client's
        # connect() succeeds and queues in the listen backlog) but nothing is
        # dispatched until astartup() has populated the tool executor.
        server = await asyncio.start_unix_server(
            self._handle_connection,
            path=str(socket_path),
            limit=STREAM_BUFFER_LIMIT,
            start_serving=False,
        )
        try:
            await self.agent.astartup()
        except Exception:
            # A raising astartup would exit the process, and systemd's
            # Restart=on-failure would respawn it every RestartSec, spawning
            # worker subprocesses each cycle. Log and serve degraded instead.
            logger.exception("agent %s astartup failed; serving degraded",
                             self.agent.name)
        logger.info("agent %s listening on %s", self.agent.name, socket_path)
        try:
            async with server:
                await server.serve_forever()
        finally:
            with contextlib.suppress(Exception):
                await self.agent.ashutdown()
```

Add `import contextlib` to the imports at the top of `daemon.py`.

- [ ] **Step 5: Run to verify it passes**

```bash
/mnt/secondary/projects/PARE/.venv/bin/python -m pytest tests/test_daemon_startup.py -q
```

Expected: PASS.

- [ ] **Step 6: Run the full suite**

```bash
/mnt/secondary/projects/PARE/.venv/bin/python -m pytest -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add agent_core/agent.py agent_core/daemon.py tests/test_daemon_startup.py
git commit -m "feat(daemon): add astartup/ashutdown hooks around serve()

Worker discovery needs to run in the serving loop, in a task that
outlives the connections it opens. serve() now binds with
start_serving=False, awaits astartup(), and only then accepts -- so the
socket exists immediately (a client connect queues in the backlog) while
no request is dispatched against a half-populated executor. The naive
'bind then astartup' ordering would already be accepting.

astartup failures are logged rather than propagated: an exception would
exit the process, and systemd Restart=on-failure would respawn it every
RestartSec, spawning worker subprocesses each cycle.

ashutdown gives the pool a teardown path; nothing closed it in the
serving loop before."
```

---

### Task 8: `WorkerSpec.autoload` and `discover_and_register` over the manager

**Files:**
- Modify: `agent_core/workers/types.py` (`WorkerSpec`)
- Modify: `agent_core/workers/discovery.py` (reimplement)
- Modify: `agent_core/workers/__init__.py` (exports)
- Test: `tests/workers/test_registry.py` (append), `tests/workers/test_discovery.py` (adjust)

**Interfaces:**
- Produces: `WorkerSpec.autoload: bool = True`; `discover_and_register` keeps its signature but no longer swallows `CancelledError`; `WorkerManager`, `WorkerRegistry`, `RiskAwareToolPool` exported from `agent_core.workers`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/workers/test_registry.py`:

```python
def test_autoload_defaults_true_and_parses(tmp_path):
    """Default True keeps every existing workers.yaml behaving as it does now."""
    from agent_core.workers.registry import WorkerRegistry

    p = tmp_path / "workers.yaml"
    p.write_text(
        "workers:\n"
        "  a:\n"
        "    command: /bin/true\n"
        "    transport: stdio\n"
        "    risk_default: low\n"
        "  b:\n"
        "    command: /bin/true\n"
        "    transport: stdio\n"
        "    risk_default: low\n"
        "    autoload: false\n"
    )
    reg = WorkerRegistry.load(p)
    assert reg.get("a").autoload is True
    assert reg.get("b").autoload is False
```

Append to `tests/workers/test_discovery.py`:

```python
async def test_discovery_does_not_swallow_cancellation():
    """`except (TimeoutError, CancelledError, Exception)` absorbed a cancellation
    aimed at the discovery task and marched on to the next worker inside a task
    that was already cancelling -- the best available explanation for the
    'one dead worker killed its siblings' report in PARE's workers.yaml."""
    import asyncio
    from agent_core.workers.discovery import discover_and_register
    from agent_core.workers.types import WorkerSpec

    class _Pool:
        async def list_tools(self, worker):
            raise asyncio.CancelledError()

    spec = WorkerSpec(name="w", transport="stdio", risk_default="low",
                      command="/bin/true")
    with pytest.raises(asyncio.CancelledError):
        await discover_and_register([spec], _Pool())
```

- [ ] **Step 2: Run to verify they fail**

```bash
/mnt/secondary/projects/PARE/.venv/bin/python -m pytest tests/workers/test_registry.py tests/workers/test_discovery.py -q
```

Expected: FAIL — `autoload` is dropped by pydantic (`extra="ignore"`), and the cancellation test returns `[]` instead of raising.

- [ ] **Step 3: Add the field**

In `agent_core/workers/types.py`, inside `WorkerSpec` alongside `capability_tags`:

```python
    autoload: bool = True
    """Connect this worker at daemon startup. False means declared-but-not-
    loaded: it appears in the catalog and can be loaded at runtime.

    NOTE: WorkerSpec does not set model_config, so pydantic's default
    extra="ignore" applies — an OLDER agent_core reading a workers.yaml that
    sets autoload: false silently drops the field and autoloads the worker
    anyway. Consumers must bump their agent_core pin before adding the key.
    """
```

- [ ] **Step 4: Fix the cancellation swallow**

In `agent_core/workers/discovery.py`, change the except clause:

```python
        except asyncio.CancelledError:
            # Never absorb a cancellation: continuing the loop inside a task
            # that is already cancelling makes every later await re-raise, which
            # presents as one dead worker killing its siblings' discovery.
            raise
        except Exception as exc:
```

(`asyncio.TimeoutError` is `TimeoutError`, an `Exception` subclass on 3.11+, so the bare `except Exception` still covers the `wait_for` timeout.)

- [ ] **Step 5: Export the new names**

In `agent_core/workers/__init__.py`:

```python
from agent_core.workers.manager import (
    WorkerManager, WorkerOpResult, WorkerStatus,
)
from agent_core.workers.registry import WorkerRegistry
from agent_core.workers.risk_pool import RiskAwareToolPool

__all__ = [
    "MCPClient",
    "MCPClientPool",
    "RiskAwareToolPool",
    "WorkerManager",
    "WorkerOpResult",
    "WorkerRegistry",
    "WorkerStatus",
    "discover_and_register",
    "make_tool_class",
]
```

`discover_and_register` stays exported — PARE's `tests/test_phase3_smoke.py:13` imports it directly, and an ImportError at collection is not skipped by its env gate.

- [ ] **Step 6: Run to verify they pass**

```bash
/mnt/secondary/projects/PARE/.venv/bin/python -m pytest tests/workers/ -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add agent_core/workers/types.py agent_core/workers/discovery.py agent_core/workers/__init__.py tests/workers/test_registry.py tests/workers/test_discovery.py
git commit -m "feat(workers): add WorkerSpec.autoload; stop swallowing cancellation

autoload defaults True, so every existing workers.yaml behaves unchanged.
Documents the forward-compat hazard: pydantic's extra='ignore' means an
older agent_core silently drops the key and autoloads anyway, so a
consumer must bump its pin before adding it.

discover_and_register no longer catches CancelledError. Continuing the
loop inside an already-cancelling task makes every later await re-raise,
which is the best code-supported explanation for the sibling-discovery
cascade recorded in PARE's workers.yaml."
```

---

### Task 9: Version, CHANGELOG, and release

**Files:**
- Modify: `pyproject.toml:7`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Backfill the missing releases**

`CHANGELOG.md`'s top entry is `[1.6.2]` while `pyproject.toml` says `1.7.3` — four undocumented releases, including the one PARE pins. Reconstruct them:

```bash
git log --oneline v1.6.2..v1.7.3
git log v1.6.2..v1.7.0 --format='%s%n%b'
```

Write one `## [1.7.x] - <date from `git log -1 --format=%ai <tag>`>` section per tag, with `### Added` / `### Changed` / `### Fixed` entries drawn from the commits. Keep the house style: prose entries that explain the *why*, not bare bullet points.

- [ ] **Step 2: Write the 1.8.0 entry**

```markdown
## [1.8.0] - 2026-09-05

### Added
- `WorkerManager` (`agent_core.workers.manager`) — runtime worker lifecycle: `load`, `unload`, `reload`, `load_autoload`, `status`, `unavailable_reason`. The only component that mutates the registry, connection pool and tool executor together, so boot (`load_autoload`) and an operator's runtime load are literally the same code path.
- `Agent.astartup()` / `Agent.ashutdown()` — async lifecycle hooks, both no-op by default. `Daemon.serve()` now binds with `start_serving=False`, awaits `astartup()`, and only then accepts, so the socket exists immediately while no request is dispatched against a half-populated executor.
- `WorkerSpec.autoload` (default `True`) — declared-but-not-loaded workers.
- `ToolExecutor.add/add_all/remove/remove_worker/__contains__` — the registry is mutable at runtime. `add()` refuses to shadow an existing name; `add_all()` validates the whole batch before committing any of it.
- Synthesized worker tools carry a `worker` class attribute, so `remove_worker` keys on provenance rather than the name prefix.
- `AuditEntry` gains the outcomes `worker_loaded` / `worker_unloaded` and a nullable `tool`, for control-plane rows.

### Changed
- `MCPClientPool` now owns each connection in a dedicated task. anyio binds a cancel scope to the task that entered it, so a client connected during startup could not be closed from a per-message handler task: `close()` raised `RuntimeError` and the worker subprocess survived until the event loop exited. The single global connect lock is now per-worker.
- `RiskAwareToolPool` reads specs through to `MCPClientPool` instead of keeping a second dict, and keeps a **session tier high-water mark that lifecycle never evicts**. Escalate-only was monotonic within one resolution but not across time; a reload against a build that stopped advertising would drop a tool to its `risk_default` floor. A re-advertised lower tier now fails closed at the previously observed tier.
- Session approvals are keyed by a per-worker generation, so an approval resolving after a reload does not apply to the new process, and a call approved before a reload is refused rather than dispatched against a different binary. `close_all` bumps every generation.
- `ToolExecutor.schemas()` emits a stable order (non-worker tools in insertion order, then worker tools sorted by `(worker, name)`). It is a prompt-cache prefix, and concurrent autoload makes registration order nondeterministic. `names()` keeps insertion order.
- `ToolExecutor.build()` now logs a warning on a duplicate tool name instead of silently overwriting. It still overwrites — raising would break a consumer that intentionally shadows a builtin, so that is scheduled for 2.0.0.

### Fixed
- `RiskAwareToolPool` audits `asyncio.CancelledError` before re-raising. Disconnecting mid-dispatch tears the task down with a `BaseException` that every guard on the audited path missed, so a dispatch could execute leaving no record at all. `Outcome` had a `"cancelled"` value nothing had ever emitted.
- `discover_and_register` no longer catches `CancelledError`. Absorbing it and continuing the loop inside an already-cancelling task makes every subsequent await re-raise — the best code-supported explanation for the sibling-discovery cascade reported by consumers.
- A stale comment in `risk_pool.call_tool` claimed a missing wire tier "fails safe to high"; `resolve_declared_tier` uses the worker's floor. The comment is corrected — it was actively misleading.

### Notes
- `worker_contract_version` stays at `1`. No wire fields change; workers need no edits.
- Dependencies pinned: `mcp>=1.27,<2` (2.x renamed `streamablehttp_client`) and `fastmcp>=2.11,<2.12` in the dev extra.
```

- [ ] **Step 3: Bump the version**

`pyproject.toml`: `version = "1.8.0"`.

- [ ] **Step 4: Full verification**

```bash
/mnt/secondary/projects/PARE/.venv/bin/python -m pytest -q
cd /mnt/secondary/projects/PARE && /mnt/secondary/projects/PARE/.venv/bin/python -m pytest -q
```

Expected: `agent_core` all pass; PARE's **165 passed, 3 skipped still passes** — this release is additive, so PARE must be green *before* its own wiring lands. If PARE fails here, something in this plan was not additive; find it before tagging.

- [ ] **Step 5: Commit and tag**

```bash
git add pyproject.toml CHANGELOG.md
git commit -m "chore: release 1.8.0 — runtime worker lifecycle

Also backfills the CHANGELOG for 1.7.0-1.7.3, which were released
without entries; the pinned version had no record of what it contained."
git tag v1.8.0
```

---

## Self-Review

**Spec coverage:**

| Spec section | Task |
|---|---|
| §6.1 `autoload` + forward-compat hazard | 8 |
| §6.2 provenance | 3 |
| §6.3 owner tasks, per-worker locks | 2 |
| §6.4.1 tier ratchet | 5 |
| §6.4.2 generations | 5 |
| §6.4.3 one spec dict | 5 |
| §6.4.4 pins untouched | 5 (no code — asserted by the untouched `RiskGate`) |
| §6.5 ToolExecutor | 4 |
| §6.6 WorkerManager | 6 |
| §6.7 astartup/ashutdown, `start_serving=False`, systemd | 7 |
| §6.8 versioning, exports, CHANGELOG, `discover_and_register` | 8, 9 |
| §7 transactional load, manager lock, timeouts, unload ordering, cancellation audit, close_all | 2, 5, 6 |
| §8.2 collision refusal | 4 |
| §8.3 cascade hypothesis | 8 (verification test) |
| §8.7 dependency pins | 1 |
| §10 `agent_core` test table | 2, 4, 5, 6, 7 |

§8.1, §8.4, §8.8, §9, §11 are PARE-side — plan 2.

**Not covered here, deliberately:** the §11 precondition-1 check (warn when a worker advertises above its `risk_default` with no covering pin). It needs the operator pin list, which `WorkerManager` does not hold — `RiskGate` does, privately. Adding it means a `RiskGate.overrides()` accessor. It is a §11 precondition, not a v1 requirement, so it is recorded in plan 2's follow-ups rather than built speculatively here.

**Type consistency check:** `WorkerOpResult` fields are identical in Task 6's dataclass and every test. `unavailable_reason` (never `require`) throughout. `remove_worker` (never `remove_tools`) in Tasks 4 and 6. `spec_for` on `RiskAwareToolPool` vs `spec` on `MCPClientPool` — deliberately different names for different objects; Task 5's test asserts the read-through relationship between them.

**Known rough edge for the implementer:** Task 6 reaches `self._pool._inner` to call `connect`/`disconnect`. That is a private attribute. The alternative is proxying both through `RiskAwareToolPool`, which is cleaner but widens its surface for no behavioural gain. If the reviewer objects, add `RiskAwareToolPool.connect/disconnect` one-line proxies and update the three call sites in `manager.py` — do not change the ordering in `unload`.
