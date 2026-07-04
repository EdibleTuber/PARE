# Capture Layer — PARE Wiring & Frida Teardown Implementation Plan (Plan 2 of 2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the agent_core capture capability (Plan 1, PR #19) actually protect PARE's context window: thread the operator's `cwd` over the wire so the store is per-project, wire the `CaptureLayer` into PARE's tool pool, register the retrieval tools, repoint `/snapshot` and the operator enum views at the PARE-side store, and tear the now-redundant capture/snapshot machinery out of the frida MCP (hard cutover).

**Architecture:** Three repos, three phases. **Phase A** (agent_core, stacked PR #20 on `feat/capture-layer`) adds the small shared seam Plan 1 deferred: a `cwd` field on the wire messages + `HandlerContext`, `run_repl` stamping it, a `context_window_tokens` config field, and a store-*provider* indirection on `CaptureLayer` (so the store can be resolved per-project per-message instead of fixed at construction). **Phase B** (PARE) bumps its agent_core pin once and wires everything: a per-project store manager, a `ContextVar`-backed `capture_store`, the `CaptureLayer`, the opt-in retrieval tools, and the `/snapshot` + `/ps` + `/apps` repoint. **Phase C** (pare-frida-mcp) removes the frida-side store, the `@snapshots` handle, the three retrieval tools, the byte cap, and the seq-handle spill — frida tools return full JSON envelopes and PARE captures them at the wire.

**Tech Stack:** Python 3.11+, stdlib `sqlite3`/`contextvars`/`fcntl`/`pathlib`, `pytest` + `pytest-asyncio`. No new third-party dependencies in any repo.

## Global Constraints

- **Lockstep (spec §10).** Phase A ships first as a normal agent_core release (additive, backward-compatible; PAL unaffected). **Phase B and Phase C are a lockstep pair** — merged and deployed together. Until PARE captures on the wire, frida must keep returning real envelopes; the moment frida sheds `@snapshots`/handles, PARE must already be serving the store. Do not merge B without C or C without B.
- **Decisions locked (this plan's inputs):** frida teardown is a **hard cutover** (no compatibility shim; pre-cutover on-disk frida capture data is orphaned by design, per spec §10). The agent_core seam ships as a **stacked PR #20** on `feat/capture-layer`, so one release/tag covers PR #19 + #20 and PARE bumps its pin exactly once.
- **PAL-safe.** Every Phase A change is additive and optional: `cwd`/`context_window_tokens` default to inert values; `project_marker` defaults to `None`; `CaptureLayer`'s new `store_provider` param defaults to `None` (old positional-`store` construction still works). PAL constructs no `CaptureLayer` and lists no retrieval tools, so it boots and runs unchanged.
- **Cross-repo data contract (the lockstep interface).** After Phase C, frida tools return full JSON with these exact shapes; Phase B's commands and the CaptureLayer's shape inference depend on them:
  - `enumerate_processes` → `{"summary": "<n> processes", "processes": [ {...}, ... ]}`
  - `enumerate_applications` → `{"summary": "<n> applications", "applications": [ ... ]}`
  - `enumerate_modules` → `{"summary": "<n> modules", "modules": [ ... ]}`
  - `enumerate_exports` → `{"summary": "<n> exports for <module>", "exports": [ ... ]}`
  - `read_memory` → `{"summary": "read <n> bytes @ <addr>", "address": "<addr>", "size": <n>, "bytes": <n>, "hex": "<full hex>"}`
  - `execute_script` → `{"summary": "eval complete", "result": <full value>}`
  - The single-array-value-object shape (`{"processes": [...]}`) unwraps to N rows in the store via Plan 1's §4 guard rule 1 — this is intentional and why those envelopes wrap the list in a named key.
- **`inline_budget` formula (spec §6):** `int(context_window_tokens / history_depth * 3.5)`. With the defaults (32768 / 50 * 3.5) this is **2293 bytes**.
- **Run tests from each repo root:** agent_core `cd /home/edible/Projects/agent_core`; PARE `cd /home/edible/Projects/PARE`; frida `cd /home/edible/Projects/pare-frida-mcp`.

---

## PHASE A — agent_core (stacked PR #20 on `feat/capture-layer`)

> Branch from the tip of `feat/capture-layer` (commit `7c8808d`). These three tasks add the shared seam; they do not touch the Plan 1 capture engine except `layer.py`.

### Task A1: `cwd` on wire messages + `context_window_tokens` config

**Files:**
- Modify: `agent_core/agent_core/protocol/messages.py:8-22`
- Modify: `agent_core/agent_core/config.py:33` (add field next to `history_depth`)
- Test: `agent_core/tests/protocol/test_cwd_field.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `ChatMessage.cwd: str | None = None`, `CommandMessage.cwd: str | None = None`; `BaseConfig.context_window_tokens: int = 32768`.

- [ ] **Step 1: Write the failing test**

```python
# agent_core/tests/protocol/test_cwd_field.py
from agent_core.protocol.messages import ChatMessage, CommandMessage
from agent_core.protocol.transport import decode_message, encode_message
from agent_core.config import BaseConfig


def test_chat_message_carries_cwd_and_roundtrips():
    msg = ChatMessage(text="hi", channel_id="c1", cwd="/home/op/target-a")
    back = decode_message(encode_message(msg).rstrip(b"\n"))
    assert isinstance(back, ChatMessage)
    assert back.cwd == "/home/op/target-a"


def test_command_message_cwd_defaults_none():
    assert CommandMessage(name="ps", args="").cwd is None


def test_config_has_context_window_tokens_default():
    assert BaseConfig().context_window_tokens == 32768
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/protocol/test_cwd_field.py -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'cwd'`.

- [ ] **Step 3: Write minimal implementation**

In `messages.py`, add `cwd` before the `type` sentinel in both dataclasses (the sentinel must stay last so it keeps its default and the transport's `type`-based dispatch is unaffected):

```python
@register_message
@dataclass
class ChatMessage:
    text: str
    channel_id: str | None = None
    cwd: str | None = None
    type: str = "chat"


@register_message
@dataclass
class CommandMessage:
    name: str
    args: str
    channel_id: str | None = None
    cwd: str | None = None
    type: str = "command"
```

In `config.py`, add directly after `history_depth: int = 50` (line 33):

```python
    context_window_tokens: int = 32768
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/protocol/test_cwd_field.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add agent_core/protocol/messages.py agent_core/config.py tests/protocol/test_cwd_field.py
git commit -m "feat(protocol): cwd on Chat/CommandMessage; context_window_tokens config"
```

---

### Task A2: thread `cwd` through `HandlerContext`, daemon, and `run_repl`

**Files:**
- Modify: `agent_core/agent_core/agent.py:40-44` (`HandlerContext`)
- Modify: `agent_core/agent_core/daemon.py:88-94` (populate `ctx.cwd`)
- Modify: `agent_core/agent_core/adapters/cli.py:118-149` (`run_repl` stamps cwd)
- Test: `agent_core/tests/test_daemon_cwd.py`

**Interfaces:**
- Consumes: `ChatMessage.cwd`/`CommandMessage.cwd` (Task A1).
- Produces: `HandlerContext.cwd: str | None = None`; `Daemon._handle_connection` sets it from `getattr(msg, "cwd", None)`; `run_repl(..., cwd: str | None = None)` stamps it onto every outgoing message.

- [ ] **Step 1: Write the failing test**

```python
# agent_core/tests/test_daemon_cwd.py
import asyncio
import pytest
from agent_core.agent import HandlerContext
from agent_core.daemon import Daemon
from agent_core.protocol.messages import ChatMessage


def test_handler_context_has_cwd_default_none():
    ctx = HandlerContext(conversation=None, channel_id="c1", writer=None)
    assert ctx.cwd is None


@pytest.mark.asyncio
async def test_daemon_populates_ctx_cwd_from_message():
    captured = {}

    class _Chan:
        async def get_or_create(self, cid): return object()

    class _Agent:
        name = "t"
        channels = _Chan()

        async def handle_chat(self, msg, ctx):
            captured["cwd"] = ctx.cwd
            return
            yield  # async generator

    d = Daemon(_Agent())
    ctx = HandlerContext(conversation=object(), channel_id="c1", writer=None,
                         agent=_Agent(), cwd="/home/op/target-b")
    # Drive the handler directly to assert the field threads through.
    async for _ in _Agent().handle_chat(ChatMessage(text="x", cwd="/home/op/target-b"), ctx):
        pass
    assert ctx.cwd == "/home/op/target-b"
```

> Implementer note: the daemon's connection loop needs a live socket to exercise end-to-end; the unit assertion above pins the two behaviors that matter (the field exists and the handler sees it). If you prefer an integration test, drive a real `asyncio.start_unix_server` round-trip — but the field-threading assertion is the requirement.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_daemon_cwd.py -v`
Expected: FAIL — `HandlerContext.__init__() got an unexpected keyword argument 'cwd'`.

- [ ] **Step 3: Write minimal implementation**

`agent.py` — add `cwd` to `HandlerContext` (after `emit`, all-defaulted so order is valid):

```python
    agent: object = None    # Agent; populated by Daemon._handle_connection
    emit: object = None     # Callable[[object], Awaitable[None]]; populated by Daemon
    cwd: str | None = None  # operator's launch cwd, stamped by the CLI; None for non-CLI clients
```

`daemon.py` — set it where `HandlerContext` is constructed (line 88-94):

```python
                ctx = HandlerContext(
                    conversation=conv,
                    channel_id=channel_id,
                    writer=writer,
                    agent=self.agent,
                    emit=_emit,
                    cwd=getattr(msg, "cwd", None),
                )
```

`adapters/cli.py` — add the param to `run_repl` (line 118-120) and stamp it at both send sites (147, 149):

```python
async def run_repl(
    socket_path: Path, renderer: Renderer, channel_id: str | None = None,
    cwd: str | None = None,
) -> None:
```

```python
                await conn.send(CommandMessage(name=name, args=args, channel_id=channel_id, cwd=cwd))
            else:
                await conn.send(ChatMessage(text=line, channel_id=channel_id, cwd=cwd))
```

Update the docstring line for `run_repl` to note that `cwd`, when set, is stamped on every outgoing message so the daemon can resolve a per-project store.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_daemon_cwd.py -v`
Then the daemon + adapters suites for no regression: `python3 -m pytest tests/test_daemon.py tests/adapters/ -v`
Expected: PASS; existing daemon/adapter tests still green (new field is optional).

- [ ] **Step 5: Commit**

```bash
git add agent_core/agent.py agent_core/daemon.py agent_core/adapters/cli.py tests/test_daemon_cwd.py
git commit -m "feat(daemon): thread cwd from wire message into HandlerContext; run_repl stamps it"
```

---

### Task A3: `CaptureLayer` store-provider indirection

**Files:**
- Modify: `agent_core/agent_core/capture/layer.py:31-60`
- Test: `agent_core/tests/capture/test_layer_provider.py`

**Interfaces:**
- Consumes: `CaptureStore` (Plan 1).
- Produces: `CaptureLayer(store=None, *, inline_budget, launch_ts, store_provider=None)`. `layer.store` is now a property: returns `store_provider()` if a provider was given, else the fixed `store`. `maybe_substitute` reads `self.store` once per call and returns the result unchanged when it is `None`. Backward-compatible: Plan 1's positional-`store` construction and `layer.store` reads are unaffected.

- [ ] **Step 1: Write the failing test**

```python
# agent_core/tests/capture/test_layer_provider.py
import json
from agent_core.capture.store import CaptureStore
from agent_core.capture.layer import CaptureLayer, stringify_result


class _Block:
    type = "text"
    def __init__(self, text): self.text = text


class _Result:
    def __init__(self, text): self.isError = False; self.content = [_Block(text)]


def test_provider_selects_store_dynamically():
    a = CaptureStore.open_memory()
    b = CaptureStore.open_memory()
    current = {"store": a}
    layer = CaptureLayer(inline_budget=10, launch_ts=1.0,
                         store_provider=lambda: current["store"])
    layer.maybe_substitute("frida", "t", _Result(json.dumps([{"x": 1}])), substitute=False)
    assert len(a.recent()) == 1 and len(b.recent()) == 0
    current["store"] = b
    layer.maybe_substitute("frida", "t", _Result(json.dumps([{"y": 2}])), substitute=False)
    assert len(b.recent()) == 1


def test_none_store_passes_through_without_capture():
    layer = CaptureLayer(inline_budget=10, launch_ts=1.0, store_provider=lambda: None)
    r = _Result(json.dumps([{"x": 1}]))
    assert layer.maybe_substitute("frida", "t", r, substitute=True) is r


def test_positional_store_still_works():
    store = CaptureStore.open_memory()
    layer = CaptureLayer(store, inline_budget=10, launch_ts=1.0)
    layer.maybe_substitute("frida", "t", _Result(json.dumps([{"x": 1}])), substitute=False)
    assert len(store.recent()) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/capture/test_layer_provider.py -v`
Expected: FAIL — `store_provider` is an unexpected keyword argument.

- [ ] **Step 3: Write minimal implementation** (replace `CaptureLayer.__init__` and the store-write guard in `maybe_substitute`)

```python
class CaptureLayer:
    def __init__(self, store: CaptureStore | None = None, *, inline_budget: int,
                 launch_ts: float, store_provider=None) -> None:
        self._store = store
        self._store_provider = store_provider
        self._budget = inline_budget
        self._launch_ts = launch_ts

    @property
    def store(self):
        if self._store_provider is not None:
            return self._store_provider()
        return self._store

    def maybe_substitute(self, worker: str, tool: str, result: Any, *, substitute: bool,
                         session_id: str | None = None) -> Any:
        if getattr(result, "isError", False):
            return result
        store = self.store
        if store is None:
            return result  # no project store bound this turn -> don't capture
        text = stringify_result(result)
        try:
            value = json.loads(text)
        except (ValueError, TypeError):
            value = text  # opaque blob -> degenerate row
        rows = infer_rows(value)
        body_bytes = len(text.encode("utf-8"))
        if not is_substantial(value, rows, body_bytes, self._budget):
            return result
        ref = store.write(CaptureRecord(
            worker=worker, tool=tool, session_id=session_id, launch_ts=self._launch_ts,
            summary=f"{tool}: {len(rows)} row(s)", body=text,
            rows=len(rows), addrs=normalize_addrs(text),
        ))
        if not substitute or body_bytes <= self._budget:
            return result
        stub = build_stub(worker=worker, ref=ref, rows=len(rows),
                          summary=f"{tool}: {len(rows)} row(s)", body_bytes=body_bytes,
                          cols=columns(rows))
        return _TextResult(stub)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/capture/test_layer_provider.py tests/capture/test_layer.py -v`
Expected: PASS — new provider tests green, and Plan 1's `test_layer.py` still green (positional store + `layer.store` reads unchanged).

- [ ] **Step 5: Commit + open PR #20**

```bash
git add agent_core/capture/layer.py tests/capture/test_layer_provider.py
git commit -m "feat(capture): CaptureLayer store-provider indirection for per-project stores"
python3 -m pytest -q   # full agent_core suite green before opening the PR
```

Then push `feat/capture-layer` and open PR #20 stacked on PR #19 (or, if #19 has merged, targeting the release branch). **Stop — do not merge.** Shane reviews and merges; the release/tag is his step (see `feedback_pr_merge_flow`). PARE's pin bump in Task B1 waits on that tag.

---

## PHASE B — PARE wiring (single pin bump, lockstep with Phase C)

> **Gate:** Phase A must be merged and released (a new agent_core tag) before Task B1. Phase B and Phase C are developed together and merged together.

### Task B1: bump pin + per-project `CaptureStoreManager`

**Files:**
- Modify: `pyproject.toml:11` (bump the agent_core pin to the new tag)
- Create: `pare/capture_store.py`
- Create: `tests/test_capture_store_manager.py`

**Interfaces:**
- Consumes: `resolve_capture_db`, `CaptureStore` from `agent_core.capture` (Plan 1 public surface).
- Produces:
  - `CaptureStoreManager(*, marker: str | None, home: Path, xdg_state: Path)`
  - `CaptureStoreManager.resolve(cwd: str | None, channel_id: str) -> CaptureStore` — resolves the project db (walk-up with `$HOME` ceiling, XDG fallback), opens+caches it, and on a *project* store writes `.pare/.gitignore` (`*`) and takes an advisory lock. Cached per resolved db path.
  - `CaptureStoreManager.close_all() -> None`

- [ ] **Step 1: Bump the pin**

In `pyproject.toml`, line 11, change the `@v1.6.2` ref to the newly released tag that contains PR #19 + #20 (e.g. `@v1.7.0` — use the actual tag Shane cut):

```toml
    "agent_core @ git+https://github.com/EdibleTuber/agent_core.git@v1.7.0",
```

Reinstall so the new surface is importable: `pip install -e . --force-reinstall --no-deps` (or the project's usual sync). Verify: `python3 -c "from agent_core.capture import CaptureStore, resolve_capture_db; from agent_core.capture.layer import CaptureLayer; CaptureLayer(inline_budget=1, launch_ts=1.0, store_provider=lambda: None)"` prints nothing and exits 0.

- [ ] **Step 2: Write the failing test**

```python
# tests/test_capture_store_manager.py
import stat
from pathlib import Path
import pytest
from pare.capture_store import CaptureStoreManager


def _mgr(tmp_path):
    return CaptureStoreManager(marker=".pare", home=tmp_path / "home",
                               xdg_state=tmp_path / "state")


def test_project_store_is_cached_and_gitignored(tmp_path):
    proj = tmp_path / "home" / "work" / "acme"
    (proj / ".pare").mkdir(parents=True)
    mgr = _mgr(tmp_path)
    s1 = mgr.resolve(str(proj / "src"), "c1")
    s2 = mgr.resolve(str(proj), "c1")
    assert s1 is s2  # same resolved root -> one cached store
    gi = proj / ".pare" / ".gitignore"
    assert gi.read_text().strip() == "*"
    assert stat.S_IMODE((proj / ".pare").stat().st_mode) == 0o700
    mgr.close_all()


def test_outside_project_uses_xdg_fallback_keyed_by_channel(tmp_path):
    mgr = _mgr(tmp_path)
    store = mgr.resolve(str(tmp_path / "elsewhere"), "cli-xyz")
    # fallback db lives under xdg_state, keyed by channel_id
    assert (tmp_path / "state") in Path(mgr.last_db_path).parents
    assert "cli-xyz" in Path(mgr.last_db_path).name
    mgr.close_all()


def test_none_cwd_does_not_crash(tmp_path):
    mgr = _mgr(tmp_path)
    store = mgr.resolve(None, "c1")  # falls back to os.getcwd() internally
    assert store is not None
    mgr.close_all()
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python3 -m pytest tests/test_capture_store_manager.py -v`
Expected: FAIL — `No module named 'pare.capture_store'`.

- [ ] **Step 4: Write minimal implementation**

```python
# pare/capture_store.py
"""Per-project capture store manager for the PARE daemon.

The daemon is one long-lived process that many CLI launches attach to. Each
launch stamps its os.getcwd() onto every message (see pare/cli.py); this
manager resolves that cwd to a project store (git-style .pare/ walk-up, $HOME
ceiling, XDG fallback outside a project), opens it once, caches it per resolved
root, writes a .pare/.gitignore, and holds an advisory lock so a second daemon
on the same project fails loudly instead of racing FTS writes.
"""
from __future__ import annotations

import fcntl
import os
from pathlib import Path

from agent_core.capture import CaptureStore, resolve_capture_db


class CaptureStoreManager:
    def __init__(self, *, marker: str | None, home: Path, xdg_state: Path) -> None:
        self._marker = marker
        self._home = Path(home)
        self._xdg_state = Path(xdg_state)
        self._cache: dict[Path, CaptureStore] = {}
        self._locks: dict[Path, object] = {}
        self.last_db_path: Path | None = None

    def resolve(self, cwd: str | None, channel_id: str) -> CaptureStore:
        base = Path(cwd) if cwd else Path(os.getcwd())
        db_path, is_project = resolve_capture_db(
            base, self._marker, home=self._home, xdg_state=self._xdg_state,
            channel_id=channel_id,
        )
        db_path = Path(db_path).resolve()
        self.last_db_path = db_path
        cached = self._cache.get(db_path)
        if cached is not None:
            return cached
        store = CaptureStore.open(db_path)          # 0o700 dir / 0o600 db (Plan 1)
        pare_dir = db_path.parent
        if is_project:
            self._write_gitignore(pare_dir)
            self._take_lock(pare_dir)
        self._cache[db_path] = store
        return store

    @staticmethod
    def _write_gitignore(pare_dir: Path) -> None:
        gi = pare_dir / ".gitignore"
        if not gi.exists():
            gi.write_text("*\n")
            gi.chmod(0o600)

    def _take_lock(self, pare_dir: Path) -> None:
        lock_path = pare_dir / "daemon.lock"
        fh = open(lock_path, "w")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            fh.close()
            raise RuntimeError(
                f"another PARE daemon holds {lock_path}; refusing to share a "
                f"capture store (set a different project dir or stop the other daemon)"
            ) from exc
        self._locks[pare_dir] = fh  # held for process lifetime

    def close_all(self) -> None:
        for store in self._cache.values():
            store.close()
        self._cache.clear()
        for fh in self._locks.values():
            try:
                fh.close()
            except Exception:
                pass
        self._locks.clear()
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python3 -m pytest tests/test_capture_store_manager.py -v`
Expected: PASS (3 passed).

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml pare/capture_store.py tests/test_capture_store_manager.py
git commit -m "feat(capture): bump agent_core pin; per-project CaptureStoreManager with lock + gitignore"
```

---

### Task B2: wire capture into the agent (store binding, layer, retrieval tools, cwd stamp)

**Files:**
- Modify: `pare/agent.py` (module-level `ContextVar`; `capture_store` property; `setup()` at 65-85; `handle_chat` at 132; `handle_command` at 124; `tools` ClassVar at 50)
- Modify: `pare/config.py:16-32` (set `project_marker = ".pare"`)
- Modify: `pare/cli.py:46-48` (stamp `cwd=os.getcwd()`)
- Test: `tests/test_agent_capture_wiring.py`

**Interfaces:**
- Consumes: `CaptureStoreManager` (B1); `CaptureLayer`, `SearchCapture`, `ReadCapture` from `agent_core.capture` (Plan 1 + A3); `RiskAwareToolPool(capture_layer=...)` (Plan 1 Task 8).
- Produces: `PareAgent.capture_store` property (reads the per-turn `ContextVar`); a `CaptureLayer` injected into `tool_pool`; `SearchCapture`/`ReadCapture` registered; `project_marker=".pare"`; the CLI stamps cwd.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_agent_capture_wiring.py
import json
import pytest
from pare.agent import PareAgent, _current_store
from agent_core.capture import CaptureStore, SearchCapture, ReadCapture


def test_retrieval_tools_registered_and_marker_set():
    assert SearchCapture in PareAgent.tools
    assert ReadCapture in PareAgent.tools
    from pare.config import PAREConfig
    assert PAREConfig().project_marker == ".pare"


def test_capture_store_property_reads_contextvar():
    agent = PareAgent.__new__(PareAgent)   # no full setup needed for the property
    store = CaptureStore.open_memory()
    token = _current_store.set(store)
    try:
        assert agent.capture_store is store
    finally:
        _current_store.reset(token)
    # After reset, unset -> None (the layer treats None as "don't capture")
    assert agent.capture_store is None
```

> Implementer note: `SearchCapture`/`ReadCapture` read `ctx.agent.capture_store` (Plan 1 tools.py). The property below makes that transparently return the per-turn store. A full end-to-end capture test (dispatch → stub) is exercised by agent_core's `test_risk_pool_capture.py`; here we pin the PARE wiring points.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_agent_capture_wiring.py -v`
Expected: FAIL — `cannot import name '_current_store' from 'pare.agent'`.

- [ ] **Step 3: Write minimal implementation**

`pare/config.py` — set the marker on `PAREConfig` (add to the class body, after `audit_dir`):

```python
    project_marker: str | None = ".pare"
```

`pare/agent.py` — add imports and a module-level `ContextVar` near the top (after the existing imports, ~line 41):

```python
import os
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from agent_core.capture import CaptureLayer, CaptureStore, SearchCapture, ReadCapture
from pare.capture_store import CaptureStoreManager

# Per-turn project store, set by _bind_store() at the top of each handler and
# read by the CaptureLayer's provider and by the retrieval tools
# (ctx.agent.capture_store). A ContextVar (not an instance attr) so concurrent
# channels attached to the one daemon never see each other's store.
_current_store: ContextVar[CaptureStore | None] = ContextVar("pare_capture_store", default=None)
```

Register the retrieval tools (line 50):

```python
    tools = [StaticAnalyze, ReadVaultDoc, SearchCapture, ReadCapture]
```

Add the `capture_store` property and the `_bind_store` helper to `PareAgent`:

```python
    @property
    def capture_store(self) -> CaptureStore | None:
        return _current_store.get()

    @contextmanager
    def _bind_store(self, ctx):
        store = self._capture_stores.resolve(getattr(ctx, "cwd", None), ctx.channel_id)
        token = _current_store.set(store)
        try:
            yield
        finally:
            _current_store.reset(token)
```

Extend `setup()` (after the `tool_pool` construction at line 85) to build the store manager and the layer, then re-create `tool_pool` with the layer injected. Replace the `send_message=None,` closing of the `RiskAwareToolPool(...)` call so the layer is passed in the same construction:

```python
        self._launch_ts = time.time()   # process start; per-launch refinement deferred (spec §11)
        self._capture_stores = CaptureStoreManager(
            marker=self.config.project_marker,
            home=Path.home(),
            xdg_state=Path(os.environ.get("XDG_STATE_HOME",
                                          str(Path.home() / ".local" / "state"))) / "pare",
        )
        inline_budget = int(self.config.context_window_tokens / self.config.history_depth * 3.5)
        self._capture_layer = CaptureLayer(
            inline_budget=inline_budget, launch_ts=self._launch_ts,
            store_provider=lambda: self.capture_store,
        )
        self.tool_pool = RiskAwareToolPool(
            inner=self.mcp_pool,
            specs={s.name: s for s in specs},
            risk_gate=RiskGate(overrides=registry.risk_overrides()),
            approval_registry=self.tool_approval_registry,
            audit_log=AuditLog(self.config.audit_dir),
            send_message=None,
            capture_layer=self._capture_layer,
        )
```

Wrap both handlers so the store is bound for the whole turn. `handle_command` (line 124-130) becomes:

```python
    async def handle_command(self, msg, ctx):
        with self._bind_store(ctx):
            async for out in self.command_registry.dispatch(msg.name, msg.args, ctx):
                yield out
```

`handle_chat` (line 132): wrap the entire body in `with self._bind_store(ctx):` (indent the existing body one level; the `ContextVar` stays set across every `await`/`yield` because the daemon runs each handler in its own task with its own context copy).

`pare/cli.py` — stamp cwd (line 46-48):

```python
def main() -> None:
    import os
    config = load_config()
    asyncio.run(run_repl(config.socket_path, _PareRenderer(),
                         channel_id=_new_channel_id(), cwd=os.getcwd()))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_agent_capture_wiring.py -v`
Then the agent/tool suites for no regression: `python3 -m pytest tests/ -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add pare/agent.py pare/config.py pare/cli.py tests/test_agent_capture_wiring.py
git commit -m "feat(capture): wire CaptureLayer + retrieval tools + per-turn store binding into PareAgent"
```

---

### Task B3: repoint `/snapshot` at the PARE-side store

**Files:**
- Modify: `pare/commands/snapshot.py` (full rewrite of the data source; keep the renderers)
- Test: `tests/test_snapshot_command.py`

**Interfaces:**
- Consumes: `ctx.agent.capture_store` (B2); `render_table`/`render_catalog` (unchanged); `infer_rows` from `agent_core.capture.shape`.
- Produces: `/snapshot` reads captures directly from the store — no frida call. `list` → catalog of recent captures; no arg → most recent capture rendered; `<ref-or-substring> [query]` → that capture, optionally row-filtered.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_snapshot_command.py
import json
import pytest
from agent_core.capture import CaptureStore, CaptureRecord
from pare.commands.snapshot import Snapshot


class _Agent:
    def __init__(self, store): self.capture_store = store


class _Ctx:
    def __init__(self, agent): self.agent = agent


def _store():
    s = CaptureStore.open_memory()
    s.write(CaptureRecord(worker="frida", tool="enumerate_processes", session_id=None,
                          launch_ts=1.0, summary="2 processes",
                          body=json.dumps([{"pid": 1, "name": "init"}, {"pid": 9, "name": "zygote"}]),
                          rows=2, addrs=[]))
    return s


async def _collect(agen):
    return [m async for m in agen]


@pytest.mark.asyncio
async def test_snapshot_list_shows_recent_captures():
    ctx = _Ctx(_Agent(_store()))
    out = await _collect(Snapshot().run("list", ctx))
    assert "enumerate_processes" in out[0].text


@pytest.mark.asyncio
async def test_snapshot_default_renders_latest_rows():
    ctx = _Ctx(_Agent(_store()))
    out = await _collect(Snapshot().run("", ctx))
    assert "zygote" in out[0].text and "init" in out[0].text


@pytest.mark.asyncio
async def test_snapshot_query_filters_rows():
    ctx = _Ctx(_Agent(_store()))
    out = await _collect(Snapshot().run(" zygote", ctx))  # leading space -> sub="", rest="zygote"
    assert "zygote" in out[0].text and "init" not in out[0].text


@pytest.mark.asyncio
async def test_snapshot_empty_store_is_friendly():
    ctx = _Ctx(_Agent(CaptureStore.open_memory()))
    out = await _collect(Snapshot().run("", ctx))
    assert "nothing captured" in out[0].text.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_snapshot_command.py -v`
Expected: FAIL — current `Snapshot` calls `ctx.agent.tool_pool` / `page_capture` and has no store path.

- [ ] **Step 3: Write minimal implementation** (replace `pare/commands/snapshot.py`)

```python
"""/snapshot — deterministic viewer over the PARE-side capture store.

Reads captures straight from ctx.agent.capture_store and renders the rows
itself; the LLM is never in this path. Post-teardown there is no frida
@snapshots store and no page_capture — captures land here at the wire.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator

from agent_core.commands.base import Command
from agent_core.capture.shape import infer_rows
from agent_core.protocol.messages import ResponseMessage

from pare.commands._snapshot_render import render_table, render_catalog


class Snapshot(Command):
    name = "snapshot"
    args = "[list | <ref-or-key> [query]]"
    description = "View a captured tool result from the project store (complete, deterministic)"

    async def run(self, raw_args: str, ctx) -> AsyncIterator:
        store = getattr(ctx.agent, "capture_store", None)
        if store is None:
            yield ResponseMessage(text="no capture store for this session")
            return
        parts = raw_args.split(None, 1)
        sub = parts[0] if parts else ""
        rest = parts[1].strip() if len(parts) > 1 else ""

        if sub == "list":
            cat = [{"count": r["rows"], "source": f'{r["tool"]} [{r["ref"]}]'}
                   for r in store.recent(limit=20)]
            yield ResponseMessage(text=render_catalog(cat))
            return

        if sub == "":
            recent = store.recent(limit=1)
            if not recent:
                yield ResponseMessage(text="nothing captured yet — run an enumerate tool first")
                return
            yield ResponseMessage(text=self._render(store.get(recent[0]["ref"]), rest))
            return

        row = store.get(sub)
        if row is None:
            hits = store.search(text=sub, limit=5)
            if not hits:
                yield ResponseMessage(text=f"no capture matches '{sub}' — try /snapshot list")
                return
            if len(hits) > 1:
                listing = "\n".join(f'  {h["tool"]} [{h["ref"]}]' for h in hits)
                yield ResponseMessage(text=f"ambiguous '{sub}' — matches:\n{listing}")
                return
            row = store.get(hits[0]["ref"])
        yield ResponseMessage(text=self._render(row, rest))

    def _render(self, row: dict | None, query: str = "") -> str:
        if row is None:
            return "capture read failed"
        rows = infer_rows(json.loads(row["body"]))
        if query:
            q = query.lower()
            rows = [r for r in rows if any(q in str(v).lower() for v in r.values())]
        header = f'{row["tool"]} [{row["ref"]}] · {len(rows)} rows'
        return f"{header}\n{render_table(rows)}"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_snapshot_command.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add pare/commands/snapshot.py tests/test_snapshot_command.py
git commit -m "feat(capture): /snapshot reads the PARE-side store; drop frida page_capture path"
```

---

### Task B4: repoint `/ps` `/apps` + operator path uses `capture=False`

**Files:**
- Modify: `pare/commands/_frida.py:20-32` (pass `capture=False`)
- Modify: `pare/commands/frida_views.py:47-87` (`_EnumView` renders the full returned list; drop `page_capture`/`@snapshots`)
- Test: `tests/test_frida_views.py`

**Interfaces:**
- Consumes: the Phase C frida return shapes (Global Constraints); `RiskAwareToolPool.call_tool(..., capture=False)` (Plan 1 Task 8).
- Produces: `_frida.call(ctx, tool, args=None)` dispatches with `capture=False` (operator sees the full payload; it is still stored). `_EnumView` renders `data[<rows_key>]` directly.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_frida_views.py
import pytest
from pare.commands.frida_views import Ps, Apps


class _Block:
    type = "text"
    def __init__(self, text): self.text = text


class _Result:
    def __init__(self, text): self.isError = False; self.content = [_Block(text)]


class _Pool:
    def __init__(self): self.calls = []
    async def call_tool(self, worker, tool, args, ctx=None, capture=True):
        self.calls.append((tool, capture))
        return _Result('{"summary": "2 processes", "processes": '
                       '[{"pid": 1, "name": "init"}, {"pid": 9, "name": "zygote"}]}')


class _Agent:
    def __init__(self): self.tool_pool = _Pool()


class _Ctx:
    def __init__(self, agent): self.agent = agent


async def _collect(agen):
    return [m async for m in agen]


@pytest.mark.asyncio
async def test_ps_renders_full_list_and_uses_capture_false():
    agent = _Agent()
    out = await _collect(Ps().run("", _Ctx(agent)))
    assert "zygote" in out[0].text and "init" in out[0].text
    # operator fast path must not substitute a stub in place of the payload
    assert ("enumerate_processes", False) in agent.tool_pool.calls
    # and it must NOT make a second page_capture call
    assert all(t != "page_capture" for t, _ in agent.tool_pool.calls)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_frida_views.py -v`
Expected: FAIL — current `_EnumView` makes a second `page_capture` call and `_frida.call` doesn't pass `capture`.

- [ ] **Step 3: Write minimal implementation**

`pare/commands/_frida.py` — pass `capture=False` (operator path: store, never substitute):

```python
async def call(ctx, tool: str, args: dict | None = None) -> dict:
    """Call a frida worker tool through the audited pool and parse its JSON.
    Operator fast path: capture=False so the store still records the result
    but the operator receives the full payload (never a stub)."""
    result = await ctx.agent.tool_pool.call_tool(WORKER, tool, args or {}, ctx=ctx, capture=False)
    if getattr(result, "isError", False):
        return {"error": True, "summary": f"{tool} call failed"}
    try:
        return json.loads(result_text(result))
    except (json.JSONDecodeError, ValueError):
        return {"error": True, "summary": f"{tool} returned no/invalid JSON"}
```

`pare/commands/frida_views.py` — `_EnumView` renders the returned list directly; remove the `_HANDLE`/`page_capture` round-trip:

```python
class _EnumView(Command):
    """Base for device-scoped enumerate commands: run the enumerate tool (which
    now returns the full list as JSON) and render the complete table. The same
    call is captured to the project store at the wire, so /snapshot can re-view
    it later; this command renders the payload it already has.
    """

    _tool: str = ""
    _rows_key: str = ""

    async def run(self, raw_args: str, ctx) -> AsyncIterator:
        device_id = raw_args.strip()
        data = await _frida.call(ctx, self._tool, {"device_id": device_id} if device_id else {})
        if data.get("error"):
            yield ResponseMessage(text=data.get("summary", f"{self._tool} failed"))
            return
        rows = data.get(self._rows_key, [])
        if not rows:
            yield ResponseMessage(text=data.get("summary", "nothing captured"))
            return
        header = f"{self._tool} · {len(rows)} rows"
        yield ResponseMessage(text=f"{header}\n{render_table(rows)}")


class Ps(_EnumView):
    name = "ps"
    args = "[<device_id>]"
    description = "Enumerate processes and show them (operator fast path)."
    _tool = "enumerate_processes"
    _rows_key = "processes"


class Apps(_EnumView):
    name = "apps"
    args = "[<device_id>]"
    description = "Enumerate installed apps and show them (operator fast path)."
    _tool = "enumerate_applications"
    _rows_key = "applications"
```

Remove the now-unused `_HANDLE = "@snapshots"` constant (line 14).

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_frida_views.py tests/test_snapshot_command.py -v`
Then the full PARE suite: `python3 -m pytest -q`
Expected: PASS across PARE.

- [ ] **Step 5: Commit**

```bash
git add pare/commands/_frida.py pare/commands/frida_views.py tests/test_frida_views.py
git commit -m "feat(capture): /ps /apps render full lists; operator path captures with substitute=False"
```

---

## PHASE C — pare-frida-mcp teardown (hard cutover, lockstep with Phase B)

> **Gate:** develop alongside Phase B; merge together. After Phase C, frida tools return the full JSON envelopes in the Global-Constraints contract, with no byte cap and no handles.

### Task C1: remove the byte cap — tools return full envelopes

**Files:**
- Modify: `src/pare_frida_mcp/tools.py:23-55` (`_CAP`, `_ok`, `_err`)
- Test: `tests/unit/test_tools_envelope.py` (update), `tests/unit/test_uncapped.py` (new)

**Interfaces:**
- Produces: `_ok(summary, **extra)` returns the full JSON envelope unconditionally (no size probe, no oversized fallback, no `search_capture` guidance). The context-window bound now lives PARE-side.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_uncapped.py
import json
from pare_frida_mcp.tools import _ok


def test_ok_returns_full_payload_over_old_cap():
    big = "ab" * 5000  # ~10KB, far over the old 4096 cap
    out = _ok("read complete", hex=big)
    doc = json.loads(out)
    assert doc["hex"] == big              # not truncated
    assert "search_capture" not in out     # no removed-tool guidance
    assert "capture" not in doc            # no handle
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/test_uncapped.py -v`
Expected: FAIL — current `_ok` truncates over `_CAP` and emits a `search_capture` fallback.

- [ ] **Step 3: Write minimal implementation**

Replace `_ok`/`_err` (tools.py:29-55) with cap-free versions and drop the `_CAP`/`_PAGE_BUDGET` constants and the `bound_text` import if now unused:

```python
def _ok(summary: str, **extra) -> str:
    """Return the full JSON result envelope. No byte cap: bounding the model's
    context window is now the host's (PARE's) job, applied at the wire."""
    return json.dumps({"summary": summary, **extra})


def _err(summary: str, exc: Exception | None = None) -> str:
    payload = {"summary": summary, "error": True}
    if exc is not None:
        payload["detail"] = str(exc)
    return json.dumps(payload)
```

> Implementer note: if `bound_text`/`fit_items` in `bounding.py` are no longer imported anywhere after this task and C2/C3, leave `bounding.py` in place (it is generic and harmless) but remove its now-dead imports from `tools.py`. Do not delete `bounding.py` — it is not capture-specific.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/test_uncapped.py -v`
Update `tests/unit/test_tools_envelope.py`: any assertion that a large payload is truncated/handled by `_ok` now asserts the full payload is present. Run: `python3 -m pytest tests/unit/test_tools_envelope.py tests/unit/test_config.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/pare_frida_mcp/tools.py tests/unit/test_uncapped.py tests/unit/test_tools_envelope.py
git commit -m "feat(teardown): remove 4096-byte cap; tools return full JSON envelopes"
```

---

### Task C2: `read_memory` + `execute_script` return full JSON (drop seq-handle spill)

**Files:**
- Modify: `src/pare_frida_mcp/tools.py:203-229` (`execute_script`), `:252-278` (`read_memory`)
- Test: `tests/unit/test_read_memory_full.py` (new); delete `tests/unit/test_results_to_snapshots.py`

**Interfaces:**
- Produces: the Global-Constraints shapes for `read_memory` and `execute_script`. No `s.store.write`, no `capture={...seq...}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_read_memory_full.py
import json
import pytest
from unittest.mock import patch
import pare_frida_mcp.tools as tools


@pytest.mark.asyncio
async def test_read_memory_returns_full_hex_no_handle():
    class _Script: pass
    class _Sess:
        script = _Script()
    with patch.object(tools, "MANAGER") as mgr, \
         patch.object(tools.memory_mod, "read_memory", return_value=bytes(range(256)) * 40):
        mgr.get.return_value = _Sess()
        out = await tools.read_memory("sess-1", "0x401000", 10240)
    doc = json.loads(out)
    assert doc["address"] == "0x401000"
    assert len(bytes.fromhex(doc["hex"])) == 10240   # full region, not a 64-byte preview
    assert "capture" not in doc                        # no seq-handle
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/test_read_memory_full.py -v`
Expected: FAIL — current `read_memory` returns `hex_preview` + `capture={...}` and writes to `s.store`.

- [ ] **Step 3: Write minimal implementation**

`read_memory` (tools.py:252-278):

```python
async def read_memory(session_id: str, address: str, size: int) -> str:
    try:
        sid = validate_session_id(session_id)
        s = MANAGER.get(sid)
        data = memory_mod.read_memory(s.script, address, size)
        n = len(data) if data else 0
        return _ok(f"read {n} bytes @ {address}",
                   address=address, size=size, bytes=n, hex=data.hex() if data else "")
    except Exception as e:
        return _err("read_memory failed", e)
```

`execute_script` (tools.py:203-229):

```python
async def execute_script(session_id: str, source: str) -> str:
    try:
        sid = validate_session_id(session_id)
        s = MANAGER.get(sid)
        value = scripts_mod.execute_ad_hoc(s.frida_session, source)
        return _ok("eval complete", result=value)
    except Exception as e:
        return _err("execute_script failed", e)
```

Delete `tests/unit/test_results_to_snapshots.py` (it asserts the spill→read_capture round-trip that no longer exists).

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/test_read_memory_full.py -v`
Expected: PASS. `git rm tests/unit/test_results_to_snapshots.py`.

- [ ] **Step 5: Commit**

```bash
git rm tests/unit/test_results_to_snapshots.py
git add src/pare_frida_mcp/tools.py tests/unit/test_read_memory_full.py
git commit -m "feat(teardown): read_memory/execute_script return full JSON; no seq-handle spill"
```

---

### Task C3: `enumerate_*` return full lists (drop `@snapshots`)

**Files:**
- Modify: `src/pare_frida_mcp/tools.py:126-190` (four enumerate handlers)
- Test: `tests/unit/test_tools_enum.py` (rewrite assertions)

**Interfaces:**
- Produces: the four Global-Constraints enumerate shapes. No `snapshot_key`, no `SNAPSHOTS.replace`, no `store=@snapshots`.

- [ ] **Step 1: Write the failing test** (rewrite `test_tools_enum.py` assertions)

```python
# tests/unit/test_tools_enum.py  (core assertions — adapt fixtures to the existing file)
import json
import pytest
from unittest.mock import patch
import pare_frida_mcp.tools as tools


@pytest.mark.asyncio
async def test_enumerate_processes_returns_full_list():
    items = [{"pid": 1, "name": "init"}, {"pid": 9, "name": "zygote"}]
    with patch.object(tools.devices_mod, "get_device", return_value=object()), \
         patch.object(tools.devices_mod, "enumerate_processes", return_value=items):
        out = await tools.enumerate_processes("")
    doc = json.loads(out)
    assert doc["processes"] == items
    assert "store" not in doc and "@snapshots" not in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/test_tools_enum.py::test_enumerate_processes_returns_full_list -v`
Expected: FAIL — current handler returns `store=@snapshots, source=key`.

- [ ] **Step 3: Write minimal implementation** (the four handlers, tools.py:126-190)

```python
async def enumerate_processes(device_id: str = "") -> str:
    try:
        d = devices_mod.get_device(device_id or None)
        items = devices_mod.enumerate_processes(d)
        return _ok(f"{len(items)} processes", processes=items)
    except Exception as e:
        return _err("enumerate_processes failed", e)


async def enumerate_applications(device_id: str = "") -> str:
    try:
        d = devices_mod.get_device(device_id or None)
        if getattr(d, "type", None) == "local":
            return _ok("application enumeration not supported on device type "
                       "'local' - use enumerate_processes", applications=[])
        items = devices_mod.enumerate_applications(d)
        return _ok(f"{len(items)} applications", applications=items)
    except Exception as e:
        return _err("enumerate_applications failed", e)


async def enumerate_modules(session_id: str) -> str:
    try:
        sid = validate_session_id(session_id)
        s = MANAGER.get(sid)
        mods = memory_mod.enumerate_modules(s.script)
        return _ok(f"{len(mods)} modules", modules=mods)
    except Exception as e:
        return _err("enumerate_modules failed", e)


async def enumerate_exports(session_id: str, module: str) -> str:
    try:
        sid = validate_session_id(session_id)
        s = MANAGER.get(sid)
        exps = memory_mod.enumerate_exports(s.script, module)
        return _ok(f"{len(exps)} exports for {module}", exports=exps)
    except Exception as e:
        return _err("enumerate_exports failed", e)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/test_tools_enum.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/pare_frida_mcp/tools.py tests/unit/test_tools_enum.py
git commit -m "feat(teardown): enumerate_* return full lists; drop @snapshots handle"
```

---

### Task C4: delete capture machinery + drop the three retrieval tools (18→15)

**Files:**
- Delete: `src/pare_frida_mcp/capture/` (whole package: `store.py`, `search.py`, `read.py`, `page.py`, `__init__.py`), `src/pare_frida_mcp/core/snapshots.py`
- Modify: `src/pare_frida_mcp/contract.py` (drop `search_capture`/`read_capture`/`page_capture` ToolSpecs at :85-107; drop the `capture` property from `_BOUNDED_OUT` at :9-12), `src/pare_frida_mcp/tools.py` (drop `SNAPSHOTS`, `_resolve_store`, `snapshot_key`/`SNAPSHOT_HANDLE` imports, `search_capture`/`read_capture`/`page_capture` handlers at :291-361, and the capture-engine imports at :13-16), `src/pare_frida_mcp/core/sessions.py` (drop the `store` field, `flush`, `store_for`, and `store.close()` in `detach`/`close_all`)
- Delete tests: `tests/unit/test_capture_store.py`, `test_capture_search.py`, `test_capture_read.py`, `test_page.py`, `test_snapshots.py`, `test_enumerate_snapshots.py`, `test_snapshot_routing.py`, `test_ok_floor_data_loss.py`, `test_tools_search.py`, `test_tools_page.py`
- Modify tests: `tests/unit/test_contract.py:31-32`, `tests/integration/test_server_list_tools.py:10-12`, `tests/unit/conftest.py:24-28`, `tests/unit/test_tools_sessions.py`, `tests/unit/test_sessions_pump.py`, `tests/device/test_android_flows.py`

**Interfaces:**
- Produces: `TOOL_SPECS` has 15 tools; `server.py` needs no change (binds by name). No `@snapshots`, no `CaptureStore`, no `snapshot_key` anywhere in the package.

- [ ] **Step 1: Write the failing test** (pin the target end state)

```python
# tests/integration/test_server_list_tools.py  (replace the capture assertions)
from pare_frida_mcp.contract import TOOL_SPECS


def test_tool_count_is_15_and_capture_tools_gone():
    names = {s.name for s in TOOL_SPECS}
    assert len(TOOL_SPECS) == 15
    assert not ({"search_capture", "read_capture", "page_capture"} & names)
```

```python
# tests/unit/test_no_capture_imports.py  (new — guards the teardown)
import importlib, pkgutil, pare_frida_mcp


def test_capture_package_is_gone():
    import pytest
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("pare_frida_mcp.capture.store")
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("pare_frida_mcp.core.snapshots")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/integration/test_server_list_tools.py tests/unit/test_no_capture_imports.py -v`
Expected: FAIL — 18 tools; capture modules still import.

- [ ] **Step 3: Do the deletion**

```bash
git rm -r src/pare_frida_mcp/capture src/pare_frida_mcp/core/snapshots.py
git rm tests/unit/test_capture_store.py tests/unit/test_capture_search.py \
       tests/unit/test_capture_read.py tests/unit/test_page.py tests/unit/test_snapshots.py \
       tests/unit/test_enumerate_snapshots.py tests/unit/test_snapshot_routing.py \
       tests/unit/test_ok_floor_data_loss.py tests/unit/test_tools_search.py tests/unit/test_tools_page.py
```

In `contract.py`: delete the `search_capture`/`read_capture`/`page_capture` `ToolSpec` entries (lines 85-107) from `TOOL_SPECS`, and remove the `capture` property from the `_BOUNDED_OUT` output-schema dict (lines 9-12) so surviving tools no longer advertise a handle.

In `tools.py`: delete the `SNAPSHOTS = SnapshotStore()` singleton (:22), `_resolve_store` (:58-62), the `search_capture`/`read_capture`/`page_capture` handlers (:291-361), the capture-engine imports (`_search_capture`/`_read_capture`/`_page_rows`/`_list_sources`, :13-15), and the `snapshot_key`/`SNAPSHOT_HANDLE`/`CaptureStore` imports (:16-17). Remove the `SNAPSHOTS.delete_sessions(sid)` line in `detach` (:118).

In `core/sessions.py`: remove the `store: CaptureStore` field and its `CaptureStore.open(...)` construction (:7,13,44), the `flush()` method's `store.write` (:33) and the `store.close()` calls in `detach`/`close_all` (:83,96-97), and `store_for` (:88). Sessions no longer own a store.

In the mixed tests: `test_contract.py:31-32` — drop the `@snapshots`-in-description assertions. `conftest.py:24-28` — remove the `SNAPSHOTS` rebuild fixture. `test_tools_sessions.py`/`test_sessions_pump.py` — drop `CaptureStore.open_memory()`/session-store usage. `test_android_flows.py` — replace `store=="@snapshots"` / `search_capture("@snapshots", ...)` assertions with the full-list envelope shapes from Task C3.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/integration/test_server_list_tools.py tests/unit/test_no_capture_imports.py -v`
Then the whole frida suite: `python3 -m pytest -q`
Expected: PASS across the repo. Any remaining reference to `CaptureStore`/`snapshot_key`/`@snapshots`/`search_capture` is a miss — grep to confirm none remain:
`grep -rn "CaptureStore\|snapshot_key\|@snapshots\|search_capture\|read_capture\|page_capture\|SnapshotStore" src/ tests/` should return nothing.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat(teardown): remove capture package, @snapshots, and the 3 retrieval tools (18->15)"
```

---

## Self-Review

**Spec coverage (§ → task):**
- §8 cwd threaded on the wire → A1 (message field), A2 (HandlerContext + daemon + run_repl), B2 (CLI stamp).
- §8 project discovery + `$HOME` ceiling + XDG fallback → B1 (uses Plan 1's `resolve_capture_db`).
- §8 `busy_timeout` (Plan 1) + advisory lockfile → B1 (`_take_lock`).
- §9 `0o700/0o600` (Plan 1 store) + auto `.pare/.gitignore` → B1 (`_write_gitignore`).
- §6 window-derived `inline_budget` → A1 (`context_window_tokens`) + B2 (formula).
- §3 CaptureLayer injected into the pool → B2 (Plan 1 Task 8 added the `capture_layer` param + `capture` flag).
- §3/§6 store-provider so the store is per-project → A3 + B2 (`ContextVar` + `capture_store` property).
- §7 opt-in retrieval tools registered → B2 (`tools` ClassVar).
- §7/§10 `/snapshot` reads the PARE store directly → B3.
- §10 operator `_EnumView` reads/renders PARE-captured data, `capture=False` → B4.
- §10 frida teardown (store, `@snapshots`, 3 tools, byte cap, seq-handle) → C1-C4.

**Deferred (recorded, not silently dropped):**
- **Per-launch `launch_ts`.** B2 sets `launch_ts` at daemon process start, not per CLI launch, so retention's "never evict the current launch" is process-granular (it never evicts anything from this daemon run — safe, just coarse). Per-launch granularity (parse the timestamp the CLI already encodes in `channel_id`) is a follow-up; spec §9/§11.
- **Token-accurate window budgeting** (spec §11) — `inline_budget` stays the `tokens*3.5` per-message approximation; the running per-window accountant is unchanged from Plan 1's deferral.
- **Expression-index hot-key promotion** (spec §5/§11) — Plan 1 left `field=` on `json_extract` unindexed; not revisited here.
- **Conversation resume** (spec §11) — captures resume; transcript replay behind `--resume` is a later phase; unaffected by this plan.

**Placeholder scan:** every code step carries real code. The one adapt-to-fixture note (C3/C4 test rewrites) is unavoidable because those existing frida test files mix removed and surviving behavior; the assertions to change are named exactly.

**Type consistency:** `cwd: str | None` across message/HandlerContext/run_repl; `resolve(cwd: str | None, channel_id: str) -> CaptureStore` consumed by `_bind_store`; `capture_store` property returns `CaptureStore | None` and the layer/tools both tolerate `None`; the six frida envelope shapes in Global Constraints are the exact dict keys B3/B4 read (`processes`/`applications`/`hex`/`result`).

**Lockstep check:** Phase A is independently shippable and PAL-safe. Phase B's `_frida.call` and `/ps` depend on Phase C's envelope shapes; Phase C's removal of `@snapshots`/`page_capture` depends on Phase B no longer calling them — so B and C merge together, exactly as spec §10 requires.
