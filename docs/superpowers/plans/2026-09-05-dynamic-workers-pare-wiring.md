# PARE Dynamic Worker Wiring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the operator `/worker list|tools|load|unload|reload`, so a worker can be added, dropped, or restarted mid-session — reclaiming the context budget its tool schemas cost — without restarting the daemon or losing live Frida attachments, hooks, and the warm conversation.

**Architecture:** PARE stops discovering workers itself. `setup()` builds an **empty** pool and a `worker_manager = None` sentinel; `astartup()` constructs the `WorkerManager` (by then the tool executor exists) and calls `load_autoload()`; `ashutdown()` closes everything. A thin `/worker` command drives the manager. Two things guard the seams: the executor-bypassing fast-path commands ask the manager before dispatching, and `handle_chat` promotes an unloaded-worker tool call to an operator handback.

**Tech Stack:** Python 3.12, asyncio, `agent_core` v1.8.0, pytest + pytest-asyncio (`asyncio_mode = "auto"`).

**Spec:** `docs/superpowers/specs/2026-09-04-dynamic-worker-loading-design.md` — read §8.1, §8.4, §8.8, §9 and §11 before starting.

**Depends on:** `docs/superpowers/plans/2026-09-05-dynamic-workers-agent-core.md`. **`agent_core` v1.8.0 must be tagged before Task 1.** Nothing here works against v1.7.3.

## Global Constraints

- **Python** `>=3.12`. Run everything with `/mnt/secondary/projects/PARE/.venv/bin/python`.
- **Baseline before any change: `165 passed, 3 skipped`.**
- **`mcp>=1.27,<2`** must be pinned in `pyproject.toml` (spec §8.7) — PARE inherits the same 2.x rename breakage.
- **`autoload:` may not enter `workers.yaml` before the pin bump.** `WorkerSpec` uses pydantic's default `extra="ignore"`, so against v1.7.3 the key is *silently dropped* and the worker autoloads anyway — a fail-open on the trust anchor. Task 1 bumps; Task 2 edits the YAML. Not the other order.
- **`mitm` stays `autoload: true`.** Commit `e9edf9f` deliberately made it unconditional.
- **Every new `ctx.agent.worker_manager` access must be `getattr`-guarded.** Four existing test files build fake agents as `type("A", (), {"tool_pool": pool})()`; an unguarded attribute access breaks ~22 cases and is also wrong in production against a partially-constructed agent.
- **The `system.md` change is static prose only.** Rendering live worker state into `system_prompt` breaks `tests/test_system_prompt.py` (bare `PareAgent()`, no `setup()`) *and* would let round *N* of a turn contradict the round-0 `schemas` snapshot.

---

## File Structure

**Created:**
- `pare/commands/worker.py` — the `/worker` command. Subcommand dispatch, `render_table` output. Mirrors `pare/commands/mitm.py`'s shape.
- `tests/test_worker_command.py` — command surface.
- `tests/test_unloaded_worker_handback.py` — the `handle_chat` tombstone trigger. Separate file because it tests turn-loop control flow, not the command.

**Modified:**
- `pare/agent.py` — `setup()`, `register_tools()`, new `astartup()`/`ashutdown()`, tombstone trigger in `handle_chat`.
- `pare/commands/_frida.py` — `unavailable_reason` guard.
- `pare/commands/mitm.py` — `unavailable_reason` guard **and** the missing `isError` check.
- `pare/commands/health.py` — worker line.
- `pare/prompts/system.md` — one paragraph.
- `workers.yaml` — `autoload:` keys, `hardware` entry.
- `pyproject.toml` — `agent_core` pin, `mcp` pin.
- Four fast-path test files — add `worker_manager` to their fakes.
- `tests/test_register_tools.py` — rewritten.

**Deleted:**
- `tests/test_register_tools_reconnect.py` — it pins `close_all()` after boot discovery, and its own docstring says it exists *because* discovery ran in a throwaway `asyncio.run` loop. That loop is gone, so it pins a workaround for a problem that no longer exists.

---

### Task 1: Bump to agent_core v1.8.0

**Files:**
- Modify: `pyproject.toml:11`

**Interfaces:**
- Produces: `WorkerManager`, `WorkerOpResult`, `WorkerStatus`, `Agent.astartup`, `WorkerSpec.autoload` available to every later task.

- [ ] **Step 1: Confirm the baseline**

```bash
cd /mnt/secondary/projects/PARE
.venv/bin/python -m pytest -q
```

Expected: `165 passed, 3 skipped`.

- [ ] **Step 2: Bump the pin**

In `pyproject.toml`, change the `agent_core` dependency:

```toml
    "agent_core @ git+https://github.com/EdibleTuber/agent_core.git@v1.8.0",
```

and pin `mcp` for the same reason `agent_core` does:

```toml
    "mcp>=1.27,<2",   # 2.0 renamed streamablehttp_client -> streamable_http_client
```

- [ ] **Step 3: Verify the new API is importable**

The local venv already has `agent_core` installed editable, so this reads the working tree — which is what you want while both repos are in flight.

```bash
.venv/bin/python -c "
from agent_core.workers import WorkerManager, WorkerOpResult, WorkerStatus
from agent_core.agent import Agent
from agent_core.workers.types import WorkerSpec
assert hasattr(Agent, 'astartup') and hasattr(Agent, 'ashutdown')
assert WorkerSpec(name='w', transport='stdio', risk_default='low',
                  command='/bin/true').autoload is True
print('agent_core 1.8.0 API OK')"
```

Expected: `agent_core 1.8.0 API OK`.

- [ ] **Step 4: Run the suite**

```bash
.venv/bin/python -m pytest -q
```

Expected: `165 passed, 3 skipped` — v1.8.0 is additive, so PARE must stay green *before* any wiring.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml
git commit -m "build: require agent_core v1.8.0, pin mcp<2

v1.8.0 adds the runtime worker lifecycle (WorkerManager, Agent.astartup,
WorkerSpec.autoload) this wiring depends on. The mcp pin is the same
issue agent_core has: 2.x renamed streamablehttp_client, so an unpinned
fresh install cannot import the worker client."
```

---

### Task 2: `workers.yaml` — autoload keys and the hardware entry

Must follow Task 1: against v1.7.3 pydantic silently drops `autoload` and the worker loads anyway.

**Files:**
- Modify: `workers.yaml`
- Test: `tests/test_workers_yaml.py` (create)

**Interfaces:**
- Produces: a `hardware` worker declared with `autoload: false` — the catalog entry `/worker load hardware` targets.

- [ ] **Step 1: Write the failing test**

Create `tests/test_workers_yaml.py`:

```python
"""workers.yaml is the trust anchor: every loadable worker is declared here,
with an operator-set risk floor, before anything can connect it.

The autoload assertion doubles as a pin-version canary. WorkerSpec sets no
model_config, so pydantic's extra="ignore" means an agent_core older than
v1.8.0 drops the key silently and autoloads the worker anyway — a fail-open.
This test turns that into a loud failure.
"""
from agent_core.workers.registry import WorkerRegistry


def _reg():
    return WorkerRegistry.load("workers.yaml")


def test_hardware_is_declared_but_not_autoloaded():
    spec = _reg().get("hardware")
    assert spec.autoload is False
    assert spec.risk_default in ("low", "medium", "high", "critical")
    assert spec.capability_tags, "a catalog entry needs tags to be selectable"


def test_live_workers_autoload():
    reg = _reg()
    for name in ("frida", "static", "mitm"):
        assert reg.get(name).autoload is True, (
            f"{name} must still connect at boot; mitm in particular was made "
            f"unconditional deliberately in e9edf9f"
        )


def test_every_declared_worker_has_a_risk_floor():
    for spec in _reg().all():
        assert spec.risk_default, spec.name
```

- [ ] **Step 2: Run to verify it fails**

```bash
.venv/bin/python -m pytest tests/test_workers_yaml.py -q
```

Expected: FAIL — `WorkerNotFoundError: no worker registered with name 'hardware'`.

- [ ] **Step 3: Edit `workers.yaml`**

Add `autoload: true` to the `frida`, `static` and `mitm` entries (explicit beats implicit here — the file is the operator's control surface), then append a new entry after `mitm`:

```yaml
  # Declared but NOT connected at boot: hardware work is occasional, and its
  # tool schemas cost context in every turn that isn't doing it. Load it when a
  # target actually has hardware in play:  /worker load hardware
  # The worker itself is still being built (../pare-hardware-mcp), so a load
  # today reports spawn_failed — which is the honest answer, and visible in
  # /worker list rather than a silently absent worker.
  hardware:
    command: /mnt/secondary/projects/PARE/.venv/bin/pare-hardware-mcp
    transport: stdio
    risk_default: medium        # FLOOR; per-tool wire tiers and pins escalate
    autoload: false
    capability_tags: [hardware, uart, jtag, glitch]
```

- [ ] **Step 4: Run to verify it passes**

```bash
.venv/bin/python -m pytest tests/test_workers_yaml.py tests/test_risk_overrides_coverage.py -q
```

Expected: PASS. `test_risk_overrides_coverage` must still pass — `hardware` has no contract package installed, so its pins (there are none) are simply not validated.

- [ ] **Step 5: Commit**

```bash
git add workers.yaml tests/test_workers_yaml.py
git commit -m "feat(workers): declare hardware as a catalog entry, add autoload keys

hardware is declared with autoload: false — it exists in the catalog and
can be loaded at runtime, but does not spend context on every turn.
frida/static/mitm are marked autoload: true explicitly; the file is the
operator's control surface, so implicit defaults help no one.

The autoload assertion is also a pin-version canary: pydantic's
extra='ignore' means an agent_core older than v1.8.0 drops the key and
autoloads anyway, which would fail open on the trust anchor."
```

---

### Task 3: Rewire `pare/agent.py`

**Files:**
- Modify: `pare/agent.py:101-142` (`setup`), `:144-171` (`register_tools`), imports at `:34`
- Rewrite: `tests/test_register_tools.py`
- Delete: `tests/test_register_tools_reconnect.py`

**Interfaces:**
- Consumes: `WorkerManager(registry, tool_pool, executor)` from plan 1 Task 6.
- Produces: `self.worker_manager` (None until `astartup`), `PareAgent.astartup()`, `PareAgent.ashutdown()`.

- [ ] **Step 1: Rewrite the test that pins the old behaviour**

Replace the body of `tests/test_register_tools.py` (keep the file's existing `test_setup_populates_worker_specs_from_all_declared_workers` intent, restated for the new shape):

```python
"""PareAgent's worker wiring after the move to agent_core's WorkerManager.

register_tools() no longer discovers anything: discovery moved into
astartup(), where it runs in the serving loop and in a task that outlives the
connections it opens. What remains here is the declarative half.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from pare.agent import PareAgent
from pare.config import PAREConfig
from pare.tools import StaticAnalyze

_FRAMEWORK_ATTRS = ("profile", "wisdom", "channels", "learning", "allowlist",
                    "approval_registry", "tool_approval_registry", "inference",
                    "retrieval", "websearch", "fetcher")


def _agent(tmp_path, **cfg_overrides):
    wy = tmp_path / "workers.yaml"
    wy.write_text(
        "workers:\n"
        "  stub:\n"
        "    command: /bin/true\n"
        "    transport: stdio\n"
        "    risk_default: low\n"
        "  later:\n"
        "    command: /bin/true\n"
        "    transport: stdio\n"
        "    risk_default: low\n"
        "    autoload: false\n"
    )
    cfg = PAREConfig()
    cfg.workers_yaml_path = str(wy)
    cfg.audit_dir = tmp_path
    for k, v in cfg_overrides.items():
        setattr(cfg, k, v)
    agent = PareAgent()
    agent.config = cfg
    for attr in _FRAMEWORK_ATTRS:
        setattr(agent, attr, MagicMock())
    return agent


def test_setup_leaves_the_pool_empty(tmp_path):
    """The pool must hold LOADED specs, not DECLARED ones.

    If setup() seeds every declared spec, the first dispatch after an unload
    lazily reconnects the worker you just unloaded — silently undoing it.
    """
    agent = _agent(tmp_path)
    agent.setup()
    assert agent.mcp_pool.names() == []
    assert agent.worker_manager is None, "sentinel until astartup builds it"


def test_setup_loads_the_registry_without_connecting(tmp_path):
    agent = _agent(tmp_path)
    agent.setup()
    assert {s.name for s in agent.worker_registry.all()} == {"stub", "later"}


@pytest.mark.parametrize("enabled", [False, True])
def test_register_tools_is_declarative_only(tmp_path, enabled):
    """No discovery, no asyncio.run, no close_all — just the gated tool."""
    agent = _agent(tmp_path, enable_apk_re_agents=enabled)
    agent.setup()
    classes = agent.register_tools()
    assert classes == ([StaticAnalyze] if enabled else [])
    assert (agent.apk_re_agents_client is not None) is enabled


async def test_astartup_builds_the_manager_and_autoloads(tmp_path):
    agent = _agent(tmp_path)
    agent.setup()
    agent.tool_executor = MagicMock()
    with pytest.MonkeyPatch.context() as mp:
        loaded = []

        async def fake_autoload(self):
            loaded.append(True)
            return []
        mp.setattr("agent_core.workers.manager.WorkerManager.load_autoload",
                   fake_autoload)
        await agent.astartup()
    assert agent.worker_manager is not None
    assert loaded == [True]


async def test_astartup_survives_a_worker_that_cannot_spawn(tmp_path):
    """A missing binary must be a last_error in /worker list, not a boot crash —
    systemd Restart=on-failure would respawn the daemon every 5s."""
    agent = _agent(tmp_path)
    agent.setup()
    agent.tool_executor = MagicMock()
    agent.tool_executor.add_all = MagicMock()
    await agent.astartup()          # /bin/true exits immediately: load fails
    statuses = {s.name: s for s in agent.worker_manager.status()}
    assert statuses["stub"].loaded is False
    assert statuses["stub"].last_error
    assert statuses["later"].loaded is False, "autoload: false must be skipped"
```

- [ ] **Step 2: Run to verify it fails**

```bash
.venv/bin/python -m pytest tests/test_register_tools.py -q
```

Expected: FAIL — `AttributeError: 'PareAgent' object has no attribute 'worker_manager'`.

- [ ] **Step 3: Rewire `setup()`**

In `pare/agent.py`, change the import at line 34:

```python
from agent_core.workers import MCPClientPool, WorkerManager
```

(`discover_and_register` is no longer used here. It stays exported from `agent_core` for `tests/test_phase3_smoke.py:13`.)

In `setup()`, replace the registry/pool block:

```python
        registry = WorkerRegistry.load(self.config.workers_yaml_path)
        self.worker_registry = registry
        # Empty, deliberately. The pool holds LOADED specs, not declared ones:
        # if it were seeded with every declaration, the first dispatch after an
        # unload would lazily reconnect the worker just unloaded, silently
        # undoing it. WorkerManager owns all population.
        self.mcp_pool = MCPClientPool([])
        # Sentinel so the /worker command's requires=("worker_manager",) check
        # passes in _attach_registries, which runs BEFORE astartup. Same pattern
        # the framework uses for command_registry (agent_core/runtime.py:55-58).
        # The real manager is built in astartup(), once tool_executor exists.
        self.worker_manager = None
```

and in the `RiskAwareToolPool(...)` construction, pass `specs={}` instead of `{s.name: s for s in specs}`. Delete the now-unused `specs = registry.all()` / `self._worker_specs = specs` lines.

- [ ] **Step 4: Replace `register_tools()` and add the lifecycle hooks**

Delete the entire `register_tools` body — the docstring about throwaway event loops, the `_discover` closure, the `asyncio.run`, and the `close_all` — and replace with:

```python
    def register_tools(self):
        """Declarative tools only. Worker tools are registered by astartup().

        Discovery used to run here inside a throwaway asyncio.run loop, which
        forced a close_all() afterwards because the connections were bound to
        that dead loop. astartup() runs in the serving loop instead, so that
        whole dance is gone.
        """
        return [StaticAnalyze] if self.config.enable_apk_re_agents else []

    async def astartup(self) -> None:
        """Build the worker manager and connect every autoload worker.

        Runs after _attach_registries (so tool_executor exists) and before the
        daemon accepts a connection.
        """
        self.worker_manager = WorkerManager(
            self.worker_registry, self.tool_pool, self.tool_executor)
        results = await self.worker_manager.load_autoload()
        ok = [r for r in results if r.ok]
        bad = [r for r in results if not r.ok]
        logger.info("workers loaded: %s%s",
                    ", ".join(f"{r.name}({r.tool_count})" for r in ok) or "none",
                    "".join(f" | {r.name} FAILED: {r.error}" for r in bad))

    async def ashutdown(self) -> None:
        """Close worker connections and project capture stores."""
        if self.worker_manager is not None:
            await self.worker_manager.close_all()
        close = getattr(self._capture_stores, "close_all", None)
        if close is not None:
            close()
```

- [ ] **Step 5: Delete the obsolete test**

```bash
git rm tests/test_register_tools_reconnect.py
```

Its subject no longer exists: it asserts `tool_pool.close_all` is awaited once from `register_tools()`, which is now a two-line declarative method. The property it was really protecting — a dispatch not failing because its client outlived its context — is covered by plan 1's `tests/workers/test_client_pool_owner_task.py` and by `test_astartup_survives_a_worker_that_cannot_spawn` above.

- [ ] **Step 6: Run the tests**

```bash
.venv/bin/python -m pytest tests/test_register_tools.py tests/test_smoke.py tests/test_phase1_smoke.py -q
```

Expected: PASS.

- [ ] **Step 7: Run the full suite**

```bash
.venv/bin/python -m pytest -q
```

Expected: PASS. `tests/test_fast_path_registered.py` and `tests/test_disabled_builtins.py` both build agents through `setup()` — if either fails on `_worker_specs`, that attribute was removed; update the assertion to read `agent.worker_registry.all()`.

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "refactor(agent): move worker discovery from register_tools to astartup

setup() now builds an EMPTY pool: it must hold loaded specs, not declared
ones, or the first dispatch after an unload lazily reconnects the worker
just unloaded. worker_manager is a None sentinel until astartup, matching
the framework's own command_registry pattern, so the /worker command's
requires check passes in _attach_registries.

register_tools() collapses to the declarative half. The throwaway
asyncio.run loop and its mandatory close_all are gone — astartup runs in
the serving loop, which is where the connections belong.

Deletes test_register_tools_reconnect.py, whose docstring says it exists
because discovery ran in a throwaway loop. That loop no longer exists."
```

---

### Task 4: The `/worker` command

**Files:**
- Create: `pare/commands/worker.py`
- Modify: `pare/agent.py` (`commands` ClassVar)
- Create: `tests/test_worker_command.py`

**Interfaces:**
- Consumes: `WorkerManager.status()`, `.tools_of(name)`, `.load/unload/reload(name)`.
- Produces: the `/worker` command; `Worker` in `PareAgent.commands`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_worker_command.py`:

```python
"""The /worker operator surface."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_core.workers.manager import WorkerOpResult, WorkerStatus
from pare.commands.worker import Worker


def _ctx(manager):
    agent = MagicMock()
    agent.worker_manager = manager
    return MagicMock(agent=agent)


def _status(name, loaded, count, err=None, autoload=True):
    return WorkerStatus(name=name, loaded=loaded, tool_count=count,
                        transport="stdio", risk_default="low",
                        capability_tags=["a", "b"], autoload=autoload,
                        last_error=err)


async def _run(cmd, args, ctx):
    return "\n".join([m.text async for m in cmd.run(args, ctx)])


async def test_list_shows_loaded_and_unloaded():
    mgr = MagicMock()
    mgr.status.return_value = [
        _status("frida", True, 19),
        _status("hardware", False, 0, err="No such file: pare-hardware-mcp",
                autoload=False),
    ]
    out = await _run(Worker(), "list", _ctx(mgr))
    assert "frida" in out and "19" in out
    assert "hardware" in out and "No such file" in out


async def test_bare_worker_defaults_to_list():
    mgr = MagicMock()
    mgr.status.return_value = [_status("frida", True, 19)]
    assert await _run(Worker(), "", _ctx(mgr)) == await _run(Worker(), "list", _ctx(mgr))


async def test_tools_lists_the_workers_tools():
    mgr = MagicMock()
    mgr.tools_of.return_value = ["frida_attach", "frida_detach"]
    out = await _run(Worker(), "tools frida", _ctx(mgr))
    assert "frida_attach" in out and "frida_detach" in out


async def test_load_reports_the_tool_count():
    mgr = MagicMock()
    mgr.load = AsyncMock(return_value=WorkerOpResult(
        "load", "hardware", True, tool_count=6, tools=["hardware_scan"]))
    out = await _run(Worker(), "load hardware", _ctx(mgr))
    assert "hardware" in out and "6" in out


async def test_load_failure_surfaces_the_reason():
    mgr = MagicMock()
    mgr.load = AsyncMock(return_value=WorkerOpResult(
        "load", "hardware", False, error="No such file or directory",
        error_kind="spawn_failed"))
    out = await _run(Worker(), "load hardware", _ctx(mgr))
    assert "spawn_failed" in out and "No such file" in out


async def test_unload_names_what_it_destroyed():
    """D6 makes the consequence documented rather than guarded; this output IS
    the documentation."""
    mgr = MagicMock()
    mgr.unload = AsyncMock(return_value=WorkerOpResult(
        "unload", "frida", True, tool_count=19))
    out = await _run(Worker(), "unload frida", _ctx(mgr))
    assert "19" in out
    assert "attach" in out.lower() or "hook" in out.lower(), (
        "must warn that live attachments and hooks are gone")
    assert "captures" in out.lower(), "must say captured findings survive"


async def test_unknown_subcommand_shows_usage():
    out = await _run(Worker(), "frobnicate", _ctx(MagicMock()))
    assert "usage" in out.lower()


async def test_load_without_a_name_shows_usage():
    out = await _run(Worker(), "load", _ctx(MagicMock()))
    assert "usage" in out.lower()


async def test_command_requires_the_manager():
    assert "worker_manager" in Worker.requires


def test_worker_is_registered():
    from pare.agent import PareAgent
    assert Worker in PareAgent.commands
```

- [ ] **Step 2: Run to verify they fail**

```bash
.venv/bin/python -m pytest tests/test_worker_command.py -q
```

Expected: FAIL — `ModuleNotFoundError: No module named 'pare.commands.worker'`.

- [ ] **Step 3: Write the command**

Create `pare/commands/worker.py`:

```python
"""/worker — operator control over which MCP workers are loaded.

Unloading is how you reclaim the context a worker's tool schemas cost in every
turn. It is blunt by design: the client disconnects and, for a stdio worker,
its process ends — so live Frida attachments and installed hooks go with it.
The command output says so, because that warning is the only guard there is.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

from agent_core.commands.base import Command
from agent_core.protocol.messages import ResponseMessage

from pare.commands._snapshot_render import render_table

_SUBCOMMANDS = ("list", "tools", "load", "unload", "reload")

_REPROCESS_NOTE = (
    "note: the tool list changed, so the next turn reprocesses the "
    "conversation prefix — expect one slower reply.")


class Worker(Command):
    name = "worker"
    args = "[list | tools <name> | load <name> | unload <name> | reload <name>]"
    description = "List, load, unload or reload MCP workers without restarting."
    requires = ("worker_manager",)

    async def run(self, raw_args: str, ctx) -> AsyncIterator:
        parts = raw_args.split()
        sub = parts[0] if parts else "list"
        target = parts[1] if len(parts) > 1 else None
        mgr = ctx.agent.worker_manager

        if sub not in _SUBCOMMANDS:
            yield ResponseMessage(
                text=f"unknown /worker subcommand {sub!r} — usage: /worker {self.args}")
            return
        if sub != "list" and target is None:
            yield ResponseMessage(text=f"usage: /worker {sub} <name>")
            return

        if sub == "list":
            yield ResponseMessage(text=self._render_list(mgr))
        elif sub == "tools":
            names = mgr.tools_of(target)
            yield ResponseMessage(
                text="\n".join(names) if names
                else f"{target} is not loaded (or exposes no tools) — /worker list")
        elif sub == "load":
            yield ResponseMessage(text=self._render_load(await mgr.load(target)))
        elif sub == "unload":
            yield ResponseMessage(text=self._render_unload(await mgr.unload(target)))
        else:
            yield ResponseMessage(text=self._render_load(await mgr.reload(target)))

    @staticmethod
    def _render_list(mgr) -> str:
        rows = []
        for s in mgr.status():
            rows.append({
                "worker": s.name,
                "state": "loaded" if s.loaded else "unloaded",
                "tools": str(s.tool_count) if s.loaded else "-",
                "transport": s.transport,
                "floor": s.risk_default,
                "boot": "auto" if s.autoload else "manual",
                "tags": ", ".join(s.capability_tags),
                "last error": s.last_error or "",
            })
        return render_table(rows)

    @staticmethod
    def _render_load(res) -> str:
        if not res.ok:
            return f"{res.op} {res.name} failed [{res.error_kind}]: {res.error}"
        return (f"{res.op}ed {res.name} — {res.tool_count} tools available.\n"
                f"{_REPROCESS_NOTE}")

    @staticmethod
    def _render_unload(res) -> str:
        head = f"unloaded {res.name} — {res.tool_count} tools removed, client disconnected."
        body = ("any live attachments, sessions and installed hooks for this worker "
                "are gone; captures of earlier results remain searchable, though "
                "session ids in them are now stale.")
        if not res.ok:
            return f"{head}\n{body}\nWARNING [{res.error_kind}]: {res.error}"
        return f"{head}\n{body}\n{_REPROCESS_NOTE}"
```

- [ ] **Step 4: Register it**

In `pare/agent.py`, import and add to the `commands` ClassVar:

```python
from pare.commands.worker import Worker
```

```python
    commands = [
        Hello, Health, Snapshot,
        Devices, Ps, Apps, Sessions,   # operator fast-path views
        Select, Attach, Detach,        # operator fast-path actions
        Mitm,                          # HTTPS-traffic daemon control
        Worker,                        # runtime worker lifecycle
    ]
```

- [ ] **Step 5: Run to verify they pass**

```bash
.venv/bin/python -m pytest tests/test_worker_command.py tests/test_commands_metadata.py -q
```

Expected: PASS. `test_commands_metadata` parametrizes over `PareAgent.commands`, so it covers the new command automatically.

- [ ] **Step 6: Commit**

```bash
git add pare/commands/worker.py pare/agent.py tests/test_worker_command.py
git commit -m "feat(commands): add /worker for runtime worker lifecycle

list/tools/load/unload/reload over agent_core's WorkerManager, rendered
through the house render_table so a long spawn_failed path clips instead
of wrecking the line.

Unload output names what it destroyed — live attachments and hooks are
gone, captures survive but their session ids are stale — because unload
is deliberately blunt and this output is the only warning there is. Both
load and unload flag the one-turn context reprocess that follows a change
to the tool list."
```

---

### Task 5: Guard the executor-bypassing commands

`pare/commands/_frida.py:30` and `pare/commands/mitm.py:72` are the only two places in PARE that call the pool directly by hard-coded worker name. After an unload, `risk.py:63-64` resolves an absent spec to `"high"`, so `risk_pool.py:122` prompts the operator for approval on a call that then dies with `KeyError` — an approval round-trip and an audit row for a dispatch that cannot happen.

**Files:**
- Modify: `pare/commands/_frida.py`, `pare/commands/mitm.py`
- Modify: `tests/test_frida_command_helper.py`, `tests/test_frida_views_commands.py`, `tests/test_frida_actions_commands.py`, `tests/test_mitm_commands.py`
- Test: append to `tests/test_worker_command.py`

**Interfaces:**
- Consumes: `WorkerManager.unavailable_reason(name) -> str | None`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_worker_command.py`:

```python
async def test_frida_fast_path_short_circuits_when_unloaded():
    """No approval prompt for a call that cannot succeed."""
    from pare.commands import _frida

    agent = MagicMock()
    agent.worker_manager.unavailable_reason.return_value = (
        "worker 'frida' is not loaded — Ask the operator to run /worker load frida.")
    agent.tool_pool.call_tool = AsyncMock()
    ctx = MagicMock(agent=agent)

    data = await _frida.call(ctx, "list_devices")
    assert data["error"] is True
    assert "not loaded" in data["summary"]
    agent.tool_pool.call_tool.assert_not_awaited(), "must not reach the risk pool"


async def test_frida_fast_path_works_without_a_manager():
    """The four fast-path test files build agents with only tool_pool; an
    unguarded attribute access would break ~22 existing cases."""
    from pare.commands import _frida

    result = MagicMock(isError=False)
    result.content = [MagicMock(type="text", text='{"devices": []}')]
    agent = type("A", (), {"tool_pool": MagicMock(call_tool=AsyncMock(return_value=result))})()
    data = await _frida.call(MagicMock(agent=agent), "list_devices")
    assert data == {"devices": []}


async def test_mitm_status_handles_an_error_result():
    """Pre-existing bug, independent of unload: mitm.py json.loads()es the
    result with no isError check, so any failure — including the mitm daemon
    simply being down — raises JSONDecodeError out of the command."""
    from pare.commands.mitm import Mitm

    err = MagicMock(isError=True)
    err.content = [MagicMock(type="text", text="mitm.capture_health call failed: boom")]
    agent = MagicMock()
    agent.worker_manager.unavailable_reason.return_value = None
    agent.tool_pool.call_tool = AsyncMock(return_value=err)

    out = "\n".join([m.text async for m in Mitm().run("status", MagicMock(agent=agent))])
    assert "call failed" in out or "unavailable" in out.lower()


async def test_mitm_status_short_circuits_when_unloaded():
    from pare.commands.mitm import Mitm

    agent = MagicMock()
    agent.worker_manager.unavailable_reason.return_value = "worker 'mitm' is not loaded"
    agent.tool_pool.call_tool = AsyncMock()
    out = "\n".join([m.text async for m in Mitm().run("status", MagicMock(agent=agent))])
    assert "not loaded" in out
    agent.tool_pool.call_tool.assert_not_awaited()
```

- [ ] **Step 2: Run to verify they fail**

```bash
.venv/bin/python -m pytest tests/test_worker_command.py -q -k "fast_path or mitm_status"
```

Expected: FAIL — `_frida.call` dispatches regardless, and `Mitm.run` raises `JSONDecodeError`.

- [ ] **Step 3: Add a shared guard helper**

In `pare/commands/_frida.py`, above `call`:

```python
def unavailable(ctx, worker: str = WORKER) -> str | None:
    """Operator-facing reason this worker cannot serve a call, or None.

    Advisory only — enforcement stays in RiskAwareToolPool.call_tool. Its value
    here is avoiding a pointless approval prompt: with the spec gone from the
    pool, resolve_declared_tier returns "high" for an unknown worker, so the
    operator would be asked to approve a call that then fails with KeyError.

    getattr-guarded because several test fakes (and any partially-constructed
    agent) have a tool_pool but no worker_manager.
    """
    mgr = getattr(ctx.agent, "worker_manager", None)
    return mgr.unavailable_reason(worker) if mgr is not None else None
```

and at the top of `call`:

```python
    why = unavailable(ctx)
    if why:
        return {"error": True, "summary": why}
```

Note: the message goes into `summary`, which all six render sites already read (`frida_views.py:22,35,58`, `frida_actions.py:26,47,65`) — so no per-command changes.

- [ ] **Step 4: Fix `/mitm status`**

In `pare/commands/mitm.py`, replace the status branch:

```python
        # status (default)
        from pare.commands._frida import unavailable
        why = unavailable(ctx, "mitm")
        if why:
            yield ResponseMessage(text=why)
            return
        result = await ctx.agent.tool_pool.call_tool("mitm", "capture_health", {}, ctx=ctx)
        if getattr(result, "isError", False):
            yield ResponseMessage(text=_result_text(result))
            return
        try:
            payload = json.loads(_result_text(result))
        except (json.JSONDecodeError, ValueError):
            yield ResponseMessage(
                text="mitm capture_health returned no/invalid JSON — is the "
                     "daemon running? (/mitm up)")
            return
        yield ResponseMessage(text=payload.get("summary", "no status"))
```

The `isError` check is a fix in its own right: it fires today whenever the mitm daemon is down, and without it `json.loads` gets plain error text and raises out of the command, reaching the operator as a raw `JSONDecodeError` from `daemon.py:146`.

- [ ] **Step 5: Add `worker_manager` to the four fake agents**

In each of `tests/test_frida_command_helper.py`, `tests/test_frida_views_commands.py`, `tests/test_frida_actions_commands.py`, `tests/test_mitm_commands.py`, find the fake-agent construction (`type("A", (), {"tool_pool": pool})()` or the `_Agent` class) and add a manager whose guard is a no-op:

```python
_LOADED = type("M", (), {"unavailable_reason": staticmethod(lambda name: None)})()
```

then include `"worker_manager": _LOADED` in the fake's namespace (or `self.worker_manager = _LOADED` in `_Agent.__init__`). The `getattr` guard means the tests would pass without this, but being explicit keeps them honest about the collaborator the code now consults.

- [ ] **Step 6: Run to verify**

```bash
.venv/bin/python -m pytest tests/test_worker_command.py tests/test_frida_command_helper.py tests/test_frida_views_commands.py tests/test_frida_actions_commands.py tests/test_mitm_commands.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "fix(commands): short-circuit fast-path calls to an unloaded worker

_frida.call and /mitm status bypass the tool executor and dispatch by
hard-coded worker name. With the spec gone from the pool,
resolve_declared_tier returns 'high' for an unknown worker, so the
operator got an approval prompt for a call that then died with KeyError,
plus an audit row for a dispatch to a worker that does not exist. Both
now ask the manager first; the message rides the existing error-dict
contract, so the six frida render sites are unchanged.

Also fixes a pre-existing bug in /mitm status, which json.loads()ed the
result with no isError check — so any failure, including the mitm daemon
merely being down, escaped as a raw JSONDecodeError."
```

---

### Task 6: Promote an unloaded-worker call to an operator handback

The highest-value task here. `ToolExecutor.run` returns `Unknown tool: frida_attach` for a tool the conversation *successfully used* earlier in the same session, and the model has no way to act on that. Worse, the existing escape hatches do not fire:

- distinct `frida_*` tools produce distinct `RepeatGuard` signatures, so the guard never trips and the loop runs all 50 rounds;
- `pare/agent.py:310` reads `if tc.name not in POLL_TOOLS and guard.tripped(...)`, and `POLL_TOOLS` (`handback.py:18`) holds `frida_read_hook_events` — which `system.md` explicitly tells the model to poll repeatedly. The one mechanism that would return control to the operator is switched off by design for exactly the tool the model will spam.

**Files:**
- Modify: `pare/agent.py` (`handle_chat`, around `:286-296`)
- Create: `tests/test_unloaded_worker_handback.py`

**Interfaces:**
- Consumes: `WorkerManager.worker_of(tool_name)`, `.unavailable_reason(worker)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_unloaded_worker_handback.py`:

```python
"""A turn must not burn its round budget on an unloaded worker.

RepeatGuard cannot save us here: different frida_* tools are different guard
signatures, and POLL_TOOLS deliberately exempts frida_read_hook_events from
handback because polling is supposed to repeat.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_core.protocol import ChatMessage, ResponseMessage
from pare.agent import PareAgent


class _Call:
    def __init__(self, name, args=None, id_=None):
        self.id = id_ or f"c-{name}"
        self.name = name
        self.arguments = args or {}


class _Completion:
    def __init__(self, tool_calls):
        self.type = "tool_calls"
        self.tool_calls = tool_calls
        self.content = None
        self.reasoning = None
        self.usage = None


def _agent(tool_call_script):
    """An agent whose model emits `tool_call_script` batches, one per round."""
    agent = PareAgent()
    agent.config = MagicMock(context_window_tokens=8000, history_depth=10)
    agent.worker_manager = MagicMock()
    agent.worker_manager.worker_of = lambda n: n.split("_", 1)[0] if "_" in n else None
    agent.worker_manager.unavailable_reason = lambda w: (
        f"worker {w!r} is not loaded — run /worker load {w}." if w == "frida" else None)

    agent.tool_executor = MagicMock()
    agent.tool_executor.schemas.return_value = []
    agent.tool_executor.run = AsyncMock(return_value="Unknown tool")
    agent.inference = MagicMock()
    agent.inference.complete = AsyncMock(
        side_effect=[_Completion(b) for b in tool_call_script[1:]])
    agent.record_usage = MagicMock()
    agent.decide_mode = MagicMock(return_value="on")
    agent.system_prompt = MagicMock(return_value="")
    agent._first_batch = tool_call_script[0]
    return agent


async def _drive(agent, monkeypatch):
    """Run one turn, returning every yielded message."""
    conv = MagicMock()
    conv.get_messages_for_api.return_value = []
    ctx = MagicMock(conversation=conv, channel_id="t", cwd=None)
    agent.inference.complete = AsyncMock(
        side_effect=[_Completion(agent._first_batch)] + list(
            agent.inference.complete.side_effect))
    monkeypatch.setattr(agent, "_bind_store", lambda c: __import__("contextlib").nullcontext())
    return [m async for m in agent.handle_chat(ChatMessage(text="go"), ctx)], conv


async def test_three_distinct_unloaded_tools_hand_back_fast(monkeypatch):
    """Not 50 rounds. RepeatGuard never trips on distinct signatures."""
    script = [[_Call("frida_attach")], [_Call("frida_list_devices")],
              [_Call("frida_enumerate_processes")]] + [[_Call("frida_attach")]] * 50
    agent = _agent(script)
    msgs, conv = await _drive(agent, monkeypatch)
    text = "\n".join(m.text for m in msgs if isinstance(m, ResponseMessage))
    assert "not loaded" in text
    assert agent.inference.complete.await_count <= 3, (
        f"handback took {agent.inference.complete.await_count} rounds")


async def test_poll_tool_still_hands_back(monkeypatch):
    """frida_read_hook_events is in POLL_TOOLS, exempt from the spin handback —
    so without this trigger an unloaded frida would poll to the round cap."""
    script = [[_Call("frida_read_hook_events")]] * 51
    agent = _agent(script)
    msgs, conv = await _drive(agent, monkeypatch)
    text = "\n".join(m.text for m in msgs if isinstance(m, ResponseMessage))
    assert "not loaded" in text
    assert agent.inference.complete.await_count <= 3


async def test_every_tool_call_id_is_settled(monkeypatch):
    """A dangling tool_call id with no matching result makes the NEXT turn's
    API request invalid."""
    batch = [_Call("frida_attach", id_="a"), _Call("frida_detach", id_="b")]
    script = [batch, batch, batch]
    agent = _agent(script)
    await _drive(agent, monkeypatch)
    settled = {c.args[0] for c in agent_conv_calls(agent)}
    assert {"a", "b"} <= settled


def agent_conv_calls(agent):
    return []  # replaced below by the real assertion helper


async def test_loaded_worker_is_unaffected(monkeypatch):
    """The trigger must not fire for a worker that is loaded."""
    script = [[_Call("static_grep_smali")], [_Call("static_grep_smali")]]
    agent = _agent(script)
    msgs, conv = await _drive(agent, monkeypatch)
    text = "\n".join(m.text for m in msgs if isinstance(m, ResponseMessage))
    assert "not loaded" not in text
```

Replace the `test_every_tool_call_id_is_settled` / `agent_conv_calls` pair with a real assertion once you see the shape `conv.add_tool_result` is called with — the intent is: after the handback, every `tc.id` in the final batch appears in an `add_tool_result` call. Use `conv.add_tool_result.call_args_list`.

- [ ] **Step 2: Run to verify they fail**

```bash
.venv/bin/python -m pytest tests/test_unloaded_worker_handback.py -q
```

Expected: FAIL — the turn runs to `MAX_TOOL_ROUNDS` and the text is "Reached the tool-call limit".

- [ ] **Step 3: Add the trigger**

In `pare/agent.py`'s `handle_chat`, next to `name_searches` and `resolved`, add the counter:

```python
                # Trigger 3 (unloaded worker): a tool whose worker was unloaded
                # mid-session. Its own trigger because neither existing one
                # fires — distinct frida_* tools are distinct RepeatGuard
                # signatures, and POLL_TOOLS exempts read_hook_events from the
                # spin handback by design.
                unavailable_hits = 0
                UNAVAILABLE_HANDBACK_AFTER = 2
```

and inside `for tc in tool_calls:`, immediately before the `COMMIT_TOOLS` check:

```python
                        mgr = self.worker_manager
                        if mgr is not None:
                            owner = mgr.worker_of(tc.name)
                            why = mgr.unavailable_reason(owner) if owner else None
                            if why:
                                unavailable_hits += 1
                                if unavailable_hits >= UNAVAILABLE_HANDBACK_AFTER:
                                    yield _settle_and_handback(
                                        f"{why}\n\nTell me how to proceed without "
                                        f"it, or load it and say when to retry.",
                                        done_ids)
                                    return
```

`_settle_and_handback` (`pare/agent.py:268`) already fills a synthetic result for every unanswered `tc.id`, which is what keeps the next turn's request valid.

- [ ] **Step 4: Run to verify they pass**

```bash
.venv/bin/python -m pytest tests/test_unloaded_worker_handback.py -q
```

Expected: PASS.

- [ ] **Step 5: Run the full suite**

```bash
.venv/bin/python -m pytest -q
```

Expected: PASS — in particular `tests/test_handle_chat.py` and `tests/test_handback.py`, whose agents have no `worker_manager`; the `self.worker_manager` read returns the `None` sentinel from `setup()`, and the `if mgr is not None` guard covers a bare `PareAgent()`.

If a test fails with `AttributeError: worker_manager`, add a class-level default next to the other ClassVars in `PareAgent`:

```python
    worker_manager = None   # replaced in astartup(); the sentinel setup() sets
```

- [ ] **Step 6: Commit**

```bash
git add pare/agent.py tests/test_unloaded_worker_handback.py
git commit -m "feat(agent): hand back to the operator on an unloaded-worker call

A tool call to an unloaded worker needed its own handback trigger.
Neither existing one fires: distinct frida_* tools produce distinct
RepeatGuard signatures so the guard never trips, and POLL_TOOLS exempts
frida_read_hook_events from the spin handback by design -- which is
exactly the tool system.md tells the model to poll repeatedly. An
unloaded frida therefore burned all 50 rounds, each a full-context
inference call, and ended on the round cap with no escalation.

Hands back on the second such call in a turn, settling every outstanding
tool_call id so the next turn's request stays valid."
```

---

### Task 7: Prompt, `/health`, and the handback-prefix guard

**Files:**
- Modify: `pare/prompts/system.md`, `pare/commands/health.py`
- Test: `tests/test_health.py` (append), `tests/test_handback_schema.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_health.py`:

```python
def test_health_survives_an_agent_without_a_manager():
    """/health must never be the thing that crashes."""
    from pare.commands.health import Health
    import asyncio

    agent = type("A", (), {"config": _cfg(), "name": "pare"})()
    out = asyncio.run(_collect(Health(), "", type("C", (), {"agent": agent})()))
    assert "agent: pare" in out


def test_health_lists_workers_when_a_manager_is_present():
    from unittest.mock import MagicMock
    from agent_core.workers.manager import WorkerStatus
    from pare.commands.health import Health
    import asyncio

    agent = MagicMock()
    agent.config = _cfg()
    agent.name = "pare"
    agent.worker_manager.status.return_value = [
        WorkerStatus("frida", True, 19, "stdio", "low", [], True),
        WorkerStatus("hardware", False, 0, "stdio", "medium", [], False),
    ]
    out = asyncio.run(_collect(Health(), "", type("C", (), {"agent": agent})()))
    assert "frida(19)" in out and "hardware" in out
```

(Reuse the file's existing `_cfg` / collection helpers; if it has none, add a small `_collect` that joins `m.text` across the async generator, and a `_cfg()` returning the same MagicMock config the existing test builds.)

Append to `tests/test_handback_schema.py`:

```python
def test_handback_tool_prefixes_name_declared_workers():
    """COMMIT_TOOLS / NAME_SEARCH_TOOLS / POLL_TOOLS hardcode {worker}_ prefixes.
    Renaming a worker in workers.yaml would silently disarm a handback trigger —
    a bug class runtime loading makes much easier to hit."""
    from agent_core.workers.registry import WorkerRegistry
    from pare.handback import COMMIT_TOOLS, NAME_SEARCH_TOOLS, POLL_TOOLS

    declared = {s.name for s in WorkerRegistry.load("workers.yaml").all()}
    for tool in COMMIT_TOOLS | NAME_SEARCH_TOOLS | POLL_TOOLS:
        prefix = tool.split("_", 1)[0]
        assert prefix in declared, (
            f"{tool!r} names worker {prefix!r}, which is not declared in "
            f"workers.yaml (declared: {sorted(declared)})")
```

- [ ] **Step 2: Run to verify they fail**

```bash
.venv/bin/python -m pytest tests/test_health.py tests/test_handback_schema.py -q
```

Expected: FAIL on the `/health` worker line (the prefix test should already pass — it is a guard against future drift, and a passing new guard is fine).

- [ ] **Step 3: Add the `/health` line**

In `pare/commands/health.py`, after the existing lines:

```python
        mgr = getattr(ctx.agent, "worker_manager", None)
        if mgr is not None:
            statuses = mgr.status()
            loaded = ", ".join(f"{s.name}({s.tool_count})" for s in statuses if s.loaded)
            idle = ", ".join(s.name for s in statuses if not s.loaded)
            lines.append(f"workers: {loaded or 'none'}"
                         + (f" · unloaded: {idle}" if idle else ""))
```

- [ ] **Step 4: Add the prompt paragraph**

Append to `pare/prompts/system.md`, in the section describing tools:

```markdown
## Your toolset can change mid-session

The operator can load and unload workers while you work (`/worker`), usually to
free up context. A tool disappearing is a deliberate operator action, not a
failure and not something you did wrong. If you call a tool whose worker is
unloaded you will be told so plainly.

When that happens, **say what you need and why, then stop** — do not route
around the gap. Losing `static_*` mid-investigation does not mean reconstructing
the answer from `frida`; it means telling the operator you need the static
worker back. Findings you already captured stay searchable
(`search_capture` / `read_capture`) even when the worker that produced them is
gone, but live state — session ids, hook events — does not survive a reload.
```

- [ ] **Step 5: Run to verify**

```bash
.venv/bin/python -m pytest tests/test_health.py tests/test_handback_schema.py tests/test_system_prompt.py -q
```

Expected: PASS. `test_system_prompt.py` builds a bare `PareAgent()` and asserts substrings; static prose is safe. If you were tempted to render live worker state here instead — don't; see this plan's Global Constraints.

- [ ] **Step 6: Commit**

```bash
git add pare/prompts/system.md pare/commands/health.py tests/test_health.py tests/test_handback_schema.py
git commit -m "feat(prompt,health): tell the model and the operator about worker state

system.md gets a static paragraph: the toolset can change mid-session, a
vanished tool is an operator decision, and the right response is to say
what you need rather than route around it — without that last clause a
model that loses static_* tries to reconstruct the answer from frida.
Deliberately static prose: rendering live state here would let round N of
a turn contradict the round-0 schemas snapshot.

/health gains a worker line, getattr-guarded so it can never be the thing
that crashes. Adds a guard that every handback tool prefix names a
declared worker, so a rename cannot silently disarm a trigger."
```

---

### Task 8: Prove the capture claim end to end

The spec promises that unloading a worker drops its *tools*, not its *findings* — the property that makes context-budget unloading safe. It is asserted in `agent_core` by construction (the capture layer takes the worker as a string and holds no reference), but never proven where the store actually lives.

**Files:**
- Test: `tests/test_capture_survives_unload.py` (create)

- [ ] **Step 1: Write the test**

```python
"""Unloading a worker must not cost you its findings.

CaptureLayer.maybe_substitute takes `worker` as a plain string and holds no
pool or client reference, and SearchCapture/ReadCapture are declarative
PareAgent.tools with no `worker` attribute — so remove_worker cannot remove
them. This pins that end to end, in the repo where the store lives.
"""
from unittest.mock import MagicMock

import pytest

from agent_core.tools.executor import ToolExecutor
from pare.agent import PareAgent


class _Agent:
    pass


def test_retrieval_tools_are_not_worker_owned():
    from agent_core.capture import ReadCapture, SearchCapture

    for cls in (SearchCapture, ReadCapture):
        assert not hasattr(cls, "worker"), (
            f"{cls.name} would be removed by remove_worker()")
    assert SearchCapture in PareAgent.tools and ReadCapture in PareAgent.tools


def test_remove_worker_leaves_the_capture_tools(tmp_path):
    """The executor-level guarantee: unloading `frida` must not take
    search_capture with it."""
    ex = ToolExecutor.build(_Agent(), [])
    before = set(ex.names())

    from agent_core.workers.tool_factory import make_tool_class
    from agent_core.workers.types import WorkerSpec
    spec = WorkerSpec(name="frida", transport="stdio", risk_default="low",
                      command="/bin/true")
    ex.add(make_tool_class(spec, {"name": "attach", "description": "d",
                                  "inputSchema": {"type": "object", "properties": {}}},
                           MagicMock()))
    ex.remove_worker("frida")
    assert set(ex.names()) == before
```

- [ ] **Step 2: Run it**

```bash
.venv/bin/python -m pytest tests/test_capture_survives_unload.py -q
```

Expected: PASS. If `SearchCapture` is not in `PareAgent.tools`, check `pare/agent.py:70` — the tools ClassVar is `[ReadVaultDoc, SearchCapture, ReadCapture]`.

- [ ] **Step 3: Full verification**

```bash
.venv/bin/python -m pytest -q
cd /mnt/secondary/projects/agent_core && /mnt/secondary/projects/PARE/.venv/bin/python -m pytest -q
```

Expected: both green.

- [ ] **Step 4: Manual smoke test**

The suites cannot prove the operator loop works. Run the daemon and drive it:

```bash
cd /mnt/secondary/projects/PARE
.venv/bin/pare-daemon &
sleep 3
.venv/bin/pare-cli
```

Then, in the CLI:

```
/worker list          → frida/static/mitm loaded with real tool counts;
                        hardware unloaded, boot=manual
/health               → the workers line matches
/worker tools static  → ten static_* names
/worker unload static → "10 tools removed", the attachment warning, the
                        reprocess note
/worker list          → static now unloaded
<ask the model something that needs static_grep_smali>
                      → it must hand back saying static is not loaded,
                        within a round or two — NOT spin to the cap
/worker load static   → 10 tools back
/worker load hardware → spawn_failed (the worker isn't built yet); the
                        error is visible in /worker list
```

Record anything that surprises you before moving on.

- [ ] **Step 5: Commit**

```bash
git add tests/test_capture_survives_unload.py
git commit -m "test: pin that unloading a worker keeps its captured findings

The property that makes context-budget unloading safe: unload drops the
tools, not the evidence. True by construction in agent_core (the capture
layer takes the worker as a string and holds no reference), but never
proven in the repo where the store actually lives."
```

---

---

### Task 9: Update the user-facing documentation

Docs are part of the deliverable, not a follow-up. `README.md:148` currently tells the
operator to "Edit `workers.yaml` and restart the daemon" — which this plan makes false.

**Files:**
- Modify: `README.md` (the "Workers & risk gating" and "Adding a worker" sections, the
  command table, and the extension-points table)
- Modify: `docs/superpowers/specs/2026-09-04-dynamic-worker-loading-design.md` (status header)
- Modify: `docs/superpowers/2026-09-05-dynamic-workers-build-record.md` (add a phase-2 section)

- [ ] **Step 1: Correct "Adding a worker"**

`README.md:146-175` says to edit `workers.yaml` and restart. Replace with: declare the
worker in `workers.yaml` with an `autoload` value, then either restart (if `autoload:
true`) or `/worker load <name>` at runtime. Keep the existing YAML examples and add
`autoload:` to them. State plainly that `workers.yaml` remains the trust anchor —
nothing is loadable that is not declared there with an operator-set `risk_default`.

- [ ] **Step 2: Document the `/worker` command**

Add it wherever `/mitm` is documented, with the same shape: the subcommands, and the
two consequences an operator needs to know before using it — unloading `frida` drops
live attachments and installed hooks, and any load/unload costs one slower turn while
the conversation prefix is reprocessed.

- [ ] **Step 3: Note what unload does NOT lose**

Captured findings stay searchable via `search_capture` / `read_capture` after a worker
is unloaded; live-state captures (session ids, hook events) go stale on reload. This is
the property that makes context-budget unloading safe, and it belongs next to the
command that makes it relevant.

- [ ] **Step 4: Update the status header on the spec**

Change §6/§7-implemented to note that §8.1, §8.4 and §9 are now implemented too.

- [ ] **Step 5: Add a phase-2 section to the build record**

Mirror the phase-1 structure: commit trail, defects found and what caught each,
rulings with their cost-if-wrong, and any spec corrections made during the build.

- [ ] **Step 6: Verify no stale claim survives**

```bash
grep -n "restart the daemon\|restart PARE" README.md docs/*.md
```

Expected: no hit that describes adding or changing a worker. A hit about restarting for
an unrelated reason (a config change that is not worker-related) is fine.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "docs: document runtime worker loading

README told operators to restart the daemon to add a worker, which
/worker makes false. Documents the command, the two consequences an
operator needs before using it (live frida attachments are dropped; the
next turn reprocesses the conversation prefix), and the fact that
captured findings survive an unload while live-state captures do not."
```

## Self-Review

**Spec coverage:**

| Spec section | Task |
|---|---|
| §6.1 `autoload` in the YAML, pin ordering | 1, 2 |
| §8.1 unloaded-worker dispatch, `_frida` + `/mitm` | 5 |
| §8.4 stale command paths | already fixed on this branch (commit `af2e082`); Task 2's `hardware` entry uses the corrected prefix |
| §8.7 `mcp` pin | 1 |
| §8.8 mitm tools/pins | already fixed on this branch (commit `054d970`) |
| §9.1 `/worker` command, `render_table`, unload warning, reprocess note | 4 |
| §9.2 empty pool, sentinel, `register_tools`, `astartup`/`ashutdown` | 3 |
| §9.3 tombstone as handback trigger | 6 |
| §9.4 handback constants | 7 |
| §9.5 prompt, `/health` | 7 |
| §10 PARE test table | 3, 4, 5, 6, 7, 8 |
| Docs kept current with the change | 9 |

**Placeholder scan:** one deliberate gap — `test_every_tool_call_id_is_settled` in Task 6 ships with a stub helper and an instruction to replace it against the real `conv.add_tool_result.call_args_list` shape. It is called out inline rather than left silent. Everything else contains runnable code.

**Type consistency:** `unavailable_reason` everywhere (never `require`). `WorkerStatus` field order in Task 4's and Task 7's constructors matches plan 1 Task 6's dataclass: `(name, loaded, tool_count, transport, risk_default, capability_tags, autoload, last_error)`. `WorkerOpResult(op, name, ok, tool_count, tools, error, error_kind)` likewise. `worker_of` and `tools_of` are the manager methods used in Tasks 4 and 6, both defined in plan 1 Task 6.

**Deliberately deferred to a follow-up, not built here:**
- **The §11 precondition-1 check** — warn when a worker advertises a tool above its `risk_default` with no covering pin. It needs the operator pin list, which `RiskGate` holds privately, so it wants a `RiskGate.overrides()` accessor in `agent_core`. It would fire on four mitm tools today, which is why they were pinned by hand in commit `054d970`.
- **Per-channel tool views** — D4's YAGNI call, whose expiry is model-initiated loading (spec §11 precondition 2), not this plan.
- **Auto-load on an operator fast-path command** (`/attach` silently loading frida). Explicitly rejected for v1; `unavailable_reason` is a one-line upgrade to it later if the refusal turns out to be annoying in practice.
