# Operator Fast Path Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the operator instant, LLM-free slash commands (`/devices`, `/ps`, `/apps`, `/select`, `/attach`, `/detach`, `/sessions`) that drive the frida worker directly, while the agent and operator share the same worker state.

**Architecture:** New `Command` subclasses in PARE core call the frida worker through the *same* audited `tool_pool` the agent already uses (proven by the existing `/snapshot` command) — the LLM is bypassed as decision-maker, nothing else. Two new worker tools (`list_sessions`, `detach`) fill genuine capability gaps. **No `agent_core` change** (decision 2026-06-09): every fast-path command maps to a low/medium-tier tool, which `RiskAwareToolPool.call_tool` auto-executes today, so there is no approval prompt to suppress and the calls are already audited. Actor-tagging (`actor=operator`) and operator-initiated high/critical commands are deferred to the day operator high/critical lands — they are the same feature.

**Tech Stack:** Python 3.12, `asyncio`, FastMCP (worker), `agent_core` v1.6.0 (unchanged), pytest + pytest-asyncio.

**Repos & sequencing:**
- **PR A — `pare-frida-mcp`** (`~/Projects/pare-frida-mcp`): Tasks W1, W2. Land first. The worker is **editable-installed** into PARE's venv, so once committed PARE picks the tools up live (no reinstall).
- **PR B — PARE core** (`~/Projects/PARE`, branch `design/operator-fast-path`): Tasks P1–P6. Unit tests use fake pools so PR B is independently green; the manual smoke (Task P6) needs PR A merged.

**Decisions locked (2026-06-09):**
- `/detach` gets a new worker tool (not deferred).
- `list_sessions` convention added to the system prompt this round.
- Zero `agent_core` changes; defer actor-tagging + skip-prompt.
- `detach` is tier **medium** (mirrors `attach`; the gate threshold is high/critical, so medium still auto-executes on the fast path and for the agent).

---

## File Structure

**`pare-frida-mcp` (worker):**
- Modify `src/pare_frida_mcp/core/sessions.py` — add `SessionManager.list_sessions()` and `SessionManager.detach()`.
- Modify `src/pare_frida_mcp/tools.py` — add `list_sessions` and `detach` handler functions.
- Modify `src/pare_frida_mcp/contract.py` — add two `ToolSpec` entries.
- Create `tests/unit/test_tools_sessions.py` — worker tool tests.

**PARE core:**
- Create `pare/commands/_frida.py` — shared call/parse helper for all fast-path commands.
- Create `pare/commands/frida_views.py` — `Devices`, `Ps`, `Apps`, `Sessions` (read → table).
- Create `pare/commands/frida_actions.py` — `Select`, `Attach`, `Detach` (action → status line).
- Modify `pare/agent.py` — import and register the seven commands.
- Modify `pare/prompts/system.md` — session-liveness convention.
- Create `tests/test_frida_command_helper.py`, `tests/test_frida_views_commands.py`, `tests/test_frida_actions_commands.py`, `tests/test_fast_path_registered.py`.
- Modify `tests/test_system_prompt.py` — assert the new guidance.

---

# PR A — `pare-frida-mcp` worker

Work in `~/Projects/pare-frida-mcp`. Run tests with:
`python -m pytest tests/unit/test_tools_sessions.py -v` (the worker repo's dev env, or PARE's `.venv` which has the package editable + pytest + the frida wheel).

## Task W1: `list_sessions` tool

**Files:**
- Modify: `src/pare_frida_mcp/core/sessions.py` (add method to `SessionManager`)
- Modify: `src/pare_frida_mcp/tools.py` (add handler)
- Modify: `src/pare_frida_mcp/contract.py:30-34` (add `ToolSpec` after `attach`)
- Test: `tests/unit/test_tools_sessions.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_tools_sessions.py`:

```python
import json

import pytest

from pare_frida_mcp import tools as T
from pare_frida_mcp.capture.store import CaptureStore
from pare_frida_mcp.ids import new_session_id


class _FakeFridaSession:
    def __init__(self, detached=False):
        self.is_detached = detached
        self.detached_calls = 0

    def detach(self):
        self.detached_calls += 1
        self.is_detached = True


class _FakeSession:
    """Mirrors the attrs SessionManager.list_sessions/detach read off a Session."""

    def __init__(self, sid, pid, name, fs):
        self.id = sid
        self.pid = pid
        self.name = name
        self.frida_session = fs
        self.store = CaptureStore.open_memory()
        self.flushed = False

    def flush(self):
        self.flushed = True


@pytest.mark.asyncio
async def test_list_sessions_empty():
    T.MANAGER._sessions.clear()
    res = json.loads(await T.list_sessions())
    assert res.get("error") is not True
    assert res["sessions"] == []


@pytest.mark.asyncio
async def test_list_sessions_reports_liveness():
    sid_live, sid_dead = new_session_id(), new_session_id()
    T.MANAGER._sessions[sid_live] = _FakeSession(sid_live, 100, "com.live", _FakeFridaSession(False))
    T.MANAGER._sessions[sid_dead] = _FakeSession(sid_dead, 200, "com.dead", _FakeFridaSession(True))
    try:
        res = json.loads(await T.list_sessions())
        by_id = {r["session_id"]: r for r in res["sessions"]}
        assert by_id[sid_live]["live"] is True
        assert by_id[sid_live]["pid"] == 100 and by_id[sid_live]["name"] == "com.live"
        assert by_id[sid_dead]["live"] is False
    finally:
        T.MANAGER._sessions.pop(sid_live, None)
        T.MANAGER._sessions.pop(sid_dead, None)


@pytest.mark.asyncio
async def test_list_sessions_none_frida_session_is_not_live():
    sid = new_session_id()
    T.MANAGER._sessions[sid] = _FakeSession(sid, 1, "x", None)
    try:
        res = json.loads(await T.list_sessions())
        assert res["sessions"][0]["live"] is False
    finally:
        T.MANAGER._sessions.pop(sid, None)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_tools_sessions.py -v`
Expected: FAIL — `AttributeError: module 'pare_frida_mcp.tools' has no attribute 'list_sessions'`.

- [ ] **Step 3: Add `SessionManager.list_sessions()`**

In `src/pare_frida_mcp/core/sessions.py`, add this method to `SessionManager` (after `get`, around line 50):

```python
    def list_sessions(self) -> list[dict]:
        """Snapshot of live sessions with a real per-session liveness probe.

        Liveness reads frida.Session.is_detached - a cheap property, no RPC to
        the target. A missing frida_session, or a session object lacking
        is_detached, is treated as NOT live: we must never report a dead
        session as alive.
        """
        rows = []
        for s in self._sessions.values():
            fs = s.frida_session
            detached = True if fs is None else bool(getattr(fs, "is_detached", True))
            rows.append({"session_id": s.id, "pid": s.pid,
                         "name": s.name, "live": not detached})
        return rows
```

- [ ] **Step 4: Add the `list_sessions` handler**

In `src/pare_frida_mcp/tools.py`, add after `attach` (after line 101):

```python
async def list_sessions() -> str:
    try:
        rows = MANAGER.list_sessions()
        return _ok(f"{len(rows)} sessions", sessions=rows)
    except Exception as e:
        return _err("list_sessions failed", e)
```

- [ ] **Step 5: Register the tool in the contract**

In `src/pare_frida_mcp/contract.py`, insert into `TOOL_SPECS` immediately after the `attach` entry (line 34):

```python
    ToolSpec("list_sessions", "low",
             "List live attach sessions with a real liveness probe. Returns "
             "session_id, pid, name, and live (bool) per session. Call this at "
             "the start of any turn that will act on a session - never assume a "
             "session_id from earlier in the conversation is still attached.",
             dict(_OBJ)),
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_tools_sessions.py tests/unit/test_contract.py -v`
Expected: PASS (the three `list_sessions` tests + the contract test, which iterates `TOOL_SPECS` and validates the new spec's metadata — it asserts no fixed tool count).

- [ ] **Step 7: Commit**

```bash
git add src/pare_frida_mcp/core/sessions.py src/pare_frida_mcp/tools.py src/pare_frida_mcp/contract.py tests/unit/test_tools_sessions.py
git commit -m "feat(sessions): list_sessions tool with real liveness probe"
```

## Task W2: `detach` tool

**Files:**
- Modify: `src/pare_frida_mcp/core/sessions.py` (add `SessionManager.detach`)
- Modify: `src/pare_frida_mcp/tools.py` (add handler)
- Modify: `src/pare_frida_mcp/contract.py` (add `ToolSpec` after `list_sessions`)
- Test: `tests/unit/test_tools_sessions.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_tools_sessions.py`:

```python
@pytest.mark.asyncio
async def test_detach_tears_down_session():
    sid = new_session_id()
    fs = _FakeFridaSession(False)
    sess = _FakeSession(sid, 1, "x", fs)
    T.MANAGER._sessions[sid] = sess
    res = json.loads(await T.detach(sid))
    assert res.get("error") is not True
    assert res["session_id"] == sid
    assert fs.detached_calls == 1
    assert sess.flushed is True
    assert sid not in T.MANAGER._sessions


@pytest.mark.asyncio
async def test_detach_unknown_session_errors():
    res = json.loads(await T.detach(new_session_id()))
    assert res["error"] is True
    assert "no such session" in res["summary"]


@pytest.mark.asyncio
async def test_detach_survives_dead_frida_session():
    sid = new_session_id()

    class _Boom(_FakeFridaSession):
        def detach(self):
            raise RuntimeError("USB gone")

    sess = _FakeSession(sid, 1, "x", _Boom())
    T.MANAGER._sessions[sid] = sess
    res = json.loads(await T.detach(sid))
    assert res.get("error") is not True   # teardown proceeds despite detach() throwing
    assert sid not in T.MANAGER._sessions
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_tools_sessions.py -k detach -v`
Expected: FAIL — `AttributeError: module 'pare_frida_mcp.tools' has no attribute 'detach'`.

- [ ] **Step 3: Add `SessionManager.detach()`**

In `src/pare_frida_mcp/core/sessions.py`, add to `SessionManager` (after `list_sessions`):

```python
    def detach(self, session_id: str) -> None:
        """Detach the live session and tear down its capture store.

        Raises KeyError if session_id is unknown. If the underlying
        frida.Session is already dead (USB drop), the detach() call may throw -
        we swallow it and tear down our own state regardless.
        """
        s = self._sessions.pop(session_id)  # KeyError if absent - handler maps to _err
        fs = s.frida_session
        if fs is not None:
            try:
                fs.detach()
            except Exception:
                pass
        s.flush()
        s.store.close()
```

- [ ] **Step 4: Add the `detach` handler**

In `src/pare_frida_mcp/tools.py`, add after `list_sessions`:

```python
async def detach(session_id: str) -> str:
    try:
        sid = validate_session_id(session_id)
        MANAGER.detach(sid)
        return _ok(f"detached {sid}", session_id=sid)
    except KeyError:
        return _err(f"no such session {session_id!r}")
    except Exception as e:
        return _err("detach failed", e)
```

- [ ] **Step 5: Register the tool in the contract**

In `src/pare_frida_mcp/contract.py`, insert into `TOOL_SPECS` immediately after the `list_sessions` entry:

```python
    ToolSpec("detach", "medium",
             "Detach a live session and tear down its capture state. Errors "
             "only if the session_id is unknown.",
             _in(session_id={"type": "string"})),
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_tools_sessions.py tests/unit/test_contract.py -v`
Expected: PASS (all six session tests + the contract test, which validates each `TOOL_SPEC`'s metadata; no fixed count is asserted).

- [ ] **Step 7: Run the full worker suite to confirm no regression**

Run: `python -m pytest tests/unit -q`
Expected: PASS (all green).

- [ ] **Step 8: Commit**

```bash
git add src/pare_frida_mcp/core/sessions.py src/pare_frida_mcp/tools.py src/pare_frida_mcp/contract.py tests/unit/test_tools_sessions.py
git commit -m "feat(sessions): detach tool — tear down a live session"
```

> **Cross-repo handoff:** merge PR A. Because the worker is editable-installed in PARE's venv, no reinstall is needed — PARE's next worker connection discovers `list_sessions` and `detach` automatically.

---

# PR B — PARE core

Work in `~/Projects/PARE` on branch `design/operator-fast-path`. Run tests with:
`.venv/bin/python -m pytest tests/<file> -v`.

## Task P1: shared fast-path helper

**Files:**
- Create: `pare/commands/_frida.py`
- Test: `tests/test_frida_command_helper.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_frida_command_helper.py`:

```python
import pytest

from pare.commands import _frida


class _Block:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _Result:
    def __init__(self, text, is_error=False):
        self.isError = is_error
        self.content = [_Block(text)]


class _Pool:
    def __init__(self, result):
        self._result = result
        self.calls = []

    async def call_tool(self, worker, tool, args, ctx=None):
        self.calls.append((worker, tool, args))
        return self._result


class _Ctx:
    def __init__(self, pool):
        self.agent = type("A", (), {"tool_pool": pool})()


@pytest.mark.asyncio
async def test_call_parses_json_envelope():
    ctx = _Ctx(_Pool(_Result('{"summary": "ok", "devices": [1, 2]}')))
    out = await _frida.call(ctx, "list_devices")
    assert out["devices"] == [1, 2]
    assert ctx.agent.tool_pool.calls == [("frida", "list_devices", {})]


@pytest.mark.asyncio
async def test_call_forwards_args():
    ctx = _Ctx(_Pool(_Result('{"summary": "ok"}')))
    await _frida.call(ctx, "attach", {"target": "com.x"})
    assert ctx.agent.tool_pool.calls == [("frida", "attach", {"target": "com.x"})]


@pytest.mark.asyncio
async def test_call_maps_transport_error():
    ctx = _Ctx(_Pool(_Result("denied", is_error=True)))
    out = await _frida.call(ctx, "attach", {"target": "x"})
    assert out["error"] is True


@pytest.mark.asyncio
async def test_call_maps_non_json():
    ctx = _Ctx(_Pool(_Result("not json")))
    out = await _frida.call(ctx, "list_devices")
    assert out["error"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_frida_command_helper.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'pare.commands._frida'`.

- [ ] **Step 3: Write the helper**

Create `pare/commands/_frida.py`:

```python
"""Shared plumbing for operator fast-path commands.

These commands drive the frida worker DIRECTLY through the audited tool_pool -
the LLM is never in this path (commands bypass the model, exactly like
/snapshot). Every call is still risk-gated and audited by RiskAwareToolPool,
identically to an agent-initiated call.
"""
from __future__ import annotations

import json

WORKER = "frida"


def result_text(result) -> str:
    """Concatenate the text blocks of an MCP CallToolResult."""
    return "".join(getattr(b, "text", "") for b in (getattr(result, "content", None) or []))


async def call(ctx, tool: str, args: dict | None = None) -> dict:
    """Call a frida worker tool through the audited pool and parse its JSON
    envelope. Returns the parsed dict, or an error-shaped dict ({"error": True,
    "summary": ...}) on a transport error or non-JSON result so callers render
    failures uniformly.
    """
    result = await ctx.agent.tool_pool.call_tool(WORKER, tool, args or {}, ctx=ctx)
    if getattr(result, "isError", False):
        return {"error": True, "summary": f"{tool} call failed"}
    try:
        return json.loads(result_text(result))
    except (json.JSONDecodeError, ValueError):
        return {"error": True, "summary": f"{tool} returned no/invalid JSON"}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_frida_command_helper.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add pare/commands/_frida.py tests/test_frida_command_helper.py
git commit -m "feat(commands): shared frida fast-path call helper"
```

## Task P2: view commands (`/devices`, `/ps`, `/apps`, `/sessions`)

**Files:**
- Create: `pare/commands/frida_views.py`
- Test: `tests/test_frida_views_commands.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_frida_views_commands.py`:

```python
import json

import pytest

from pare.commands.frida_views import Devices, Ps, Apps, Sessions


class _Block:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _Result:
    def __init__(self, payload):
        self.isError = False
        self.content = [_Block(json.dumps(payload))]


class _Pool:
    """Fake tool_pool routing by tool name to a canned payload."""

    def __init__(self, by_tool):
        self._by_tool = by_tool
        self.calls = []

    async def call_tool(self, worker, tool, args, ctx=None):
        self.calls.append((worker, tool, args))
        return _Result(self._by_tool[tool])


class _Ctx:
    def __init__(self, pool):
        self.agent = type("A", (), {"tool_pool": pool})()


async def _run(cmd_cls, raw, by_tool):
    cmd = cmd_cls()
    ctx = _Ctx(_Pool(by_tool))
    msgs = [m async for m in cmd.run(raw, ctx)]
    return msgs, ctx


@pytest.mark.asyncio
async def test_devices_renders_table():
    msgs, _ = await _run(Devices, "", {
        "list_devices": {"summary": "1 devices", "devices": [
            {"id": "emulator-5554", "name": "Android Emulator", "type": "usb"}]},
    })
    text = msgs[-1].text
    assert "emulator-5554" in text and "Android Emulator" in text


@pytest.mark.asyncio
async def test_devices_error_surfaces():
    msgs, _ = await _run(Devices, "", {"list_devices": {"error": True, "summary": "list_devices failed"}})
    assert "failed" in msgs[-1].text


@pytest.mark.asyncio
async def test_sessions_empty_message():
    msgs, _ = await _run(Sessions, "", {"list_sessions": {"summary": "0 sessions", "sessions": []}})
    assert "no live sessions" in msgs[-1].text.lower()


@pytest.mark.asyncio
async def test_sessions_renders_liveness():
    msgs, _ = await _run(Sessions, "", {
        "list_sessions": {"summary": "1 sessions", "sessions": [
            {"session_id": "s1", "pid": 100, "name": "com.bank", "live": True}]},
    })
    text = msgs[-1].text
    assert "com.bank" in text and "100" in text


@pytest.mark.asyncio
async def test_ps_enumerates_then_pages():
    msgs, ctx = await _run(Ps, "emulator-5554", {
        "enumerate_processes": {"summary": "2 captured", "store": "@snapshots",
                                "source": "enumerate_processes:device=emulator-5554", "total": 2},
        "page_capture": {"store": "@snapshots", "source": "enumerate_processes:device=emulator-5554",
                         "total": 2, "shown": 2,
                         "rows": [{"pid": 1, "name": "zygote"}, {"pid": 2, "name": "system_server"}]},
    })
    text = msgs[-1].text
    assert "zygote" in text and "system_server" in text
    assert ("frida", "enumerate_processes", {"device_id": "emulator-5554"}) in ctx.agent.tool_pool.calls
    assert ("frida", "page_capture",
            {"session_id": "@snapshots", "source": "enumerate_processes:device=emulator-5554"}) in ctx.agent.tool_pool.calls


@pytest.mark.asyncio
async def test_ps_no_device_arg_omits_device_id():
    msgs, ctx = await _run(Ps, "", {
        "enumerate_processes": {"summary": "0 captured", "store": "@snapshots",
                                "source": "enumerate_processes", "total": 0},
        "page_capture": {"store": "@snapshots", "source": "enumerate_processes",
                         "total": 0, "shown": 0, "rows": []},
    })
    assert ("frida", "enumerate_processes", {}) in ctx.agent.tool_pool.calls


@pytest.mark.asyncio
async def test_ps_enumerate_error_surfaces():
    msgs, _ = await _run(Ps, "", {"enumerate_processes": {"error": True, "summary": "enumerate_processes failed"}})
    assert "failed" in msgs[-1].text


@pytest.mark.asyncio
async def test_apps_enumerates_then_pages():
    msgs, _ = await _run(Apps, "", {
        "enumerate_applications": {"summary": "1 apps", "store": "@snapshots",
                                   "source": "enumerate_applications:device=emu", "total": 1},
        "page_capture": {"store": "@snapshots", "source": "enumerate_applications:device=emu",
                         "total": 1, "shown": 1, "rows": [{"identifier": "com.bank", "name": "Bank"}]},
    })
    assert "com.bank" in msgs[-1].text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_frida_views_commands.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'pare.commands.frida_views'`.

- [ ] **Step 3: Write the view commands**

Create `pare/commands/frida_views.py`:

```python
"""Operator fast-path read commands: enumerate and view worker state as tables.
No LLM in the loop - see pare.commands._frida.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

from agent_core.commands.base import Command
from agent_core.protocol.messages import ResponseMessage

from pare.commands import _frida
from pare.commands._snapshot_render import render_table

_HANDLE = "@snapshots"


class Devices(Command):
    name = "devices"
    args = ""
    description = "List Frida devices (operator fast path, no LLM)."

    async def run(self, raw_args: str, ctx) -> AsyncIterator:
        data = await _frida.call(ctx, "list_devices")
        if data.get("error"):
            yield ResponseMessage(text=data.get("summary", "list_devices failed"))
            return
        yield ResponseMessage(text=render_table(data.get("devices", [])))


class Sessions(Command):
    name = "sessions"
    args = ""
    description = "List live attach sessions with liveness (operator fast path)."

    async def run(self, raw_args: str, ctx) -> AsyncIterator:
        data = await _frida.call(ctx, "list_sessions")
        if data.get("error"):
            yield ResponseMessage(text=data.get("summary", "list_sessions failed"))
            return
        rows = data.get("sessions", [])
        if not rows:
            yield ResponseMessage(text="no live sessions — /attach <target> to start one")
            return
        yield ResponseMessage(text=render_table(rows))


class _EnumView(Command):
    """Base for device-scoped enumerate commands: run the enumerate tool (which
    persists rows to @snapshots — the agent's persisted view), then page the
    captured rows and render the complete table for the operator. This is the
    'dual output shape' (human render + persisted record) for free.
    """

    _tool: str = ""

    async def run(self, raw_args: str, ctx) -> AsyncIterator:
        device_id = raw_args.strip()
        cap = await _frida.call(ctx, self._tool, {"device_id": device_id} if device_id else {})
        if cap.get("error"):
            yield ResponseMessage(text=cap.get("summary", f"{self._tool} failed"))
            return
        source = cap.get("source")
        if not source:
            yield ResponseMessage(text=cap.get("summary", "nothing captured"))
            return
        page = await _frida.call(ctx, "page_capture", {"session_id": _HANDLE, "source": source})
        if page.get("error"):
            yield ResponseMessage(text=page.get("summary", "page_capture failed"))
            return
        rows = page.get("rows", [])
        header = f"{source} · {page.get('total', len(rows))} rows"
        yield ResponseMessage(text=f"{header}\n{render_table(rows)}")


class Ps(_EnumView):
    name = "ps"
    args = "[<device_id>]"
    description = "Enumerate processes into @snapshots and show them (operator fast path)."
    _tool = "enumerate_processes"


class Apps(_EnumView):
    name = "apps"
    args = "[<device_id>]"
    description = "Enumerate installed apps into @snapshots and show them (operator fast path)."
    _tool = "enumerate_applications"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_frida_views_commands.py -v`
Expected: PASS (8 tests).

- [ ] **Step 5: Commit**

```bash
git add pare/commands/frida_views.py tests/test_frida_views_commands.py
git commit -m "feat(commands): /devices /ps /apps /sessions fast-path views"
```

## Task P3: action commands (`/select`, `/attach`, `/detach`)

**Files:**
- Create: `pare/commands/frida_actions.py`
- Test: `tests/test_frida_actions_commands.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_frida_actions_commands.py`:

```python
import json

import pytest

from pare.commands.frida_actions import Select, Attach, Detach


class _Block:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _Result:
    def __init__(self, payload):
        self.isError = False
        self.content = [_Block(json.dumps(payload))]


class _Pool:
    def __init__(self, by_tool):
        self._by_tool = by_tool
        self.calls = []

    async def call_tool(self, worker, tool, args, ctx=None):
        self.calls.append((worker, tool, args))
        return _Result(self._by_tool[tool])


class _Ctx:
    def __init__(self, pool):
        self.agent = type("A", (), {"tool_pool": pool})()


async def _run(cmd_cls, raw, by_tool):
    cmd = cmd_cls()
    ctx = _Ctx(_Pool(by_tool))
    msgs = [m async for m in cmd.run(raw, ctx)]
    return msgs, ctx


@pytest.mark.asyncio
async def test_select_reports_selection():
    msgs, ctx = await _run(Select, "emulator-5554", {
        "select_device": {"summary": "selected", "id": "emulator-5554",
                          "name": "Android Emulator", "type": "usb"}})
    assert "Android Emulator" in msgs[-1].text and "emulator-5554" in msgs[-1].text
    assert ("frida", "select_device", {"device_id": "emulator-5554"}) in ctx.agent.tool_pool.calls


@pytest.mark.asyncio
async def test_select_requires_arg():
    msgs, ctx = await _run(Select, "", {})
    assert "usage" in msgs[-1].text.lower()
    assert ctx.agent.tool_pool.calls == []   # no worker call without an id


@pytest.mark.asyncio
async def test_attach_reports_session():
    msgs, ctx = await _run(Attach, "com.bank emulator-5554", {
        "attach": {"summary": "attached", "session_id": "sess-1", "pid": 4242, "name": "com.bank"}})
    text = msgs[-1].text
    assert "sess-1" in text and "4242" in text and "com.bank" in text
    assert ("frida", "attach", {"target": "com.bank", "device_id": "emulator-5554"}) in ctx.agent.tool_pool.calls


@pytest.mark.asyncio
async def test_attach_target_only():
    msgs, ctx = await _run(Attach, "1234", {
        "attach": {"summary": "attached", "session_id": "s", "pid": 1234, "name": "1234"}})
    assert ("frida", "attach", {"target": "1234"}) in ctx.agent.tool_pool.calls


@pytest.mark.asyncio
async def test_attach_requires_arg():
    msgs, ctx = await _run(Attach, "", {})
    assert "usage" in msgs[-1].text.lower()
    assert ctx.agent.tool_pool.calls == []


@pytest.mark.asyncio
async def test_detach_confirms():
    msgs, ctx = await _run(Detach, "sess-1", {"detach": {"summary": "detached sess-1", "session_id": "sess-1"}})
    assert "sess-1" in msgs[-1].text
    assert ("frida", "detach", {"session_id": "sess-1"}) in ctx.agent.tool_pool.calls


@pytest.mark.asyncio
async def test_detach_requires_arg():
    msgs, ctx = await _run(Detach, "", {})
    assert "usage" in msgs[-1].text.lower()
    assert ctx.agent.tool_pool.calls == []


@pytest.mark.asyncio
async def test_detach_error_surfaces():
    msgs, _ = await _run(Detach, "sess-x", {"detach": {"error": True, "summary": "no such session 'sess-x'"}})
    assert "no such session" in msgs[-1].text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_frida_actions_commands.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'pare.commands.frida_actions'`.

- [ ] **Step 3: Write the action commands**

Create `pare/commands/frida_actions.py`:

```python
"""Operator fast-path action commands: device selection and session lifecycle.
No LLM in the loop - see pare.commands._frida.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

from agent_core.commands.base import Command
from agent_core.protocol.messages import ResponseMessage

from pare.commands import _frida


class Select(Command):
    name = "select"
    args = "<device_id>"
    description = "Select a Frida device by id (operator fast path)."

    async def run(self, raw_args: str, ctx) -> AsyncIterator:
        device_id = raw_args.strip()
        if not device_id:
            yield ResponseMessage(text="usage: /select <device_id> — run /devices to list ids")
            return
        data = await _frida.call(ctx, "select_device", {"device_id": device_id})
        if data.get("error"):
            yield ResponseMessage(text=data.get("summary", "select_device failed"))
            return
        yield ResponseMessage(
            text=f"selected {data.get('name')} ({data.get('id')}, {data.get('type')})")


class Attach(Command):
    name = "attach"
    args = "<target> [<device_id>]"
    description = "Attach to a process by pid or name (operator fast path)."

    async def run(self, raw_args: str, ctx) -> AsyncIterator:
        parts = raw_args.split()
        if not parts:
            yield ResponseMessage(text="usage: /attach <pid|name> [<device_id>]")
            return
        args = {"target": parts[0]}
        if len(parts) > 1:
            args["device_id"] = parts[1]
        data = await _frida.call(ctx, "attach", args)
        if data.get("error"):
            yield ResponseMessage(text=data.get("summary", "attach failed"))
            return
        yield ResponseMessage(
            text=f"attached {data.get('name')} pid {data.get('pid')} → session {data.get('session_id')}")


class Detach(Command):
    name = "detach"
    args = "<session_id>"
    description = "Detach a session and tear down its state (operator fast path)."

    async def run(self, raw_args: str, ctx) -> AsyncIterator:
        session_id = raw_args.strip()
        if not session_id:
            yield ResponseMessage(text="usage: /detach <session_id> — run /sessions to list them")
            return
        data = await _frida.call(ctx, "detach", {"session_id": session_id})
        if data.get("error"):
            yield ResponseMessage(text=data.get("summary", "detach failed"))
            return
        yield ResponseMessage(text=f"detached {data.get('session_id')}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_frida_actions_commands.py -v`
Expected: PASS (8 tests).

- [ ] **Step 5: Commit**

```bash
git add pare/commands/frida_actions.py tests/test_frida_actions_commands.py
git commit -m "feat(commands): /select /attach /detach fast-path actions"
```

## Task P4: register the commands

**Files:**
- Modify: `pare/agent.py:35-37` (imports) and `pare/agent.py:49` (`commands` ClassVar)
- Test: `tests/test_fast_path_registered.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_fast_path_registered.py`:

```python
from pare.agent import PareAgent


def test_fast_path_commands_registered():
    names = {c.name for c in PareAgent.commands}
    assert {"devices", "ps", "apps", "sessions", "select", "attach", "detach"} <= names


def test_fast_path_commands_have_metadata():
    fast = {"devices", "ps", "apps", "sessions", "select", "attach", "detach"}
    for c in PareAgent.commands:
        if c.name in fast:
            assert isinstance(c.args, str)            # args ClassVar required by CommandRegistry.metadata()
            assert c.description                       # non-empty description
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_fast_path_registered.py -v`
Expected: FAIL — the seven names are not yet in `PareAgent.commands`.

- [ ] **Step 3: Add imports**

In `pare/agent.py`, after line 37 (`from pare.commands.snapshot import Snapshot`):

```python
from pare.commands.frida_views import Devices, Ps, Apps, Sessions
from pare.commands.frida_actions import Select, Attach, Detach
```

- [ ] **Step 4: Register the commands**

In `pare/agent.py`, replace line 49:

```python
    commands = [Hello, Health, Snapshot]  # framework builtins serve /help, /clear, etc.
```

with:

```python
    commands = [
        Hello, Health, Snapshot,
        Devices, Ps, Apps, Sessions,   # operator fast-path views
        Select, Attach, Detach,        # operator fast-path actions
    ]  # framework builtins serve /help, /clear, etc.
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_fast_path_registered.py tests/test_commands_metadata.py -v`
Expected: PASS (registration + the existing metadata suite, which now covers the new commands).

- [ ] **Step 6: Commit**

```bash
git add pare/agent.py tests/test_fast_path_registered.py
git commit -m "feat(agent): register operator fast-path commands"
```

## Task P5: session-liveness convention in the system prompt

**Files:**
- Modify: `pare/prompts/system.md`
- Test: `tests/test_system_prompt.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_system_prompt.py`:

```python
def test_system_prompt_includes_session_liveness_guidance():
    agent = PareAgent()
    pb = MagicMock()
    pb.render_profile.return_value = ""
    pb.render_wisdom.return_value = ""
    pb.render_scratchpad.return_value = ""
    pb.render_commands_catalog.return_value = ""
    agent.prompt_builder = pb
    ctx = MagicMock(); ctx.channel_id = "c"

    prompt = agent.system_prompt(ctx)

    assert "list_sessions" in prompt
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_system_prompt.py -v`
Expected: FAIL — `assert "list_sessions" in prompt` fails (not yet in the prompt).

- [ ] **Step 3: Add the convention to the prompt**

Append to `pare/prompts/system.md` (after the vault section):

```markdown

## Working with live sessions

Attach sessions (created by the operator's `/attach`, or by you) live in the
worker process, not in this conversation. Their liveness is mutable — the
operator may detach, swap targets, or a USB hiccup may kill a session between
your turns.

Before acting on a session (authoring/running scripts, hooking, reading memory),
call `list_sessions` to confirm the session_id is still live. Never assume a
session_id mentioned earlier in the conversation is still attached — query the
worker, don't trust memory.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_system_prompt.py -v`
Expected: PASS (both vault and session-liveness assertions).

- [ ] **Step 5: Commit**

```bash
git add pare/prompts/system.md tests/test_system_prompt.py
git commit -m "feat(prompt): query list_sessions before acting on a session"
```

## Task P6: full suite + manual smoke + docs

**Files:**
- Modify: `docs/superpowers/specs/2026-06-08-operator-fast-path-design.md` (status + resolved open items)
- Modify: `README.md` (command list, if it enumerates commands)

- [ ] **Step 1: Run the entire PARE test suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS (all green, including the new fast-path tests).

- [ ] **Step 2: Manual smoke against the real (editable) worker**

Requires PR A merged. Start the PARE daemon and CLI per `QUICKSTART.md`, then in the REPL:

```
/sessions          → "no live sessions — /attach <target> to start one"
/devices           → a table of devices (or "(no rows)" with no device attached)
```

With an Android device/emulator on USB:

```
/devices                       → device table incl. the device id
/ps <device_id>                → process table (also persisted to @snapshots; /snapshot shows it)
/apps <device_id>              → application table
/attach <package> <device_id>  → "attached <name> pid <n> → session <sid>"
/sessions                      → table showing that session with live=True
/detach <sid>                  → "detached <sid>"
/sessions                      → "no live sessions ..." again
```

Confirm each renders instantly with no approval prompt, and that an audit row is written for each call under `~/.local/share/pare/audit` (calls are audited even though no prompt fires).

- [ ] **Step 3: Update the design spec status**

In `docs/superpowers/specs/2026-06-08-operator-fast-path-design.md`:
- Change `**Status:** Approved (brainstorm), pending written-spec review` to `**Status:** Implemented (2026-06-09) — see docs/superpowers/plans/2026-06-09-operator-fast-path.md`.
- Under "Open items to verify during planning", append a resolution note:

```markdown
**Resolutions (2026-06-09):**
1. No agent_core change needed for v1 — all fast-path commands map to low/medium
   tiers, which auto-execute and are already audited. Skip-prompt + actor-tagging
   deferred to operator high/critical (same feature).
2. Shared MCP client is safe — both callers are async tasks in one daemon event
   loop, serialized at `await call_tool`. No explicit queue added.
3. Liveness probe: `frida.Session.is_detached` (cheap property, no RPC); a
   missing/None session is reported as not-live.
4. /snapshot stays a separate Command; fast-path commands are sibling Commands
   sharing pare/commands/_frida.py. No monolithic dispatcher.
```

- [ ] **Step 4: Update README if it lists commands**

Check `README.md` for a command list; if present, add `/devices`, `/ps`, `/apps`, `/sessions`, `/select`, `/attach`, `/detach` as the operator fast path. (Skip if README does not enumerate commands.)

- [ ] **Step 5: Commit**

```bash
git add docs/superpowers/specs/2026-06-08-operator-fast-path-design.md README.md
git commit -m "docs(fast-path): mark design implemented; document operator commands"
```

---

## Notes for the implementer

- **Why no agent_core change:** `RiskAwareToolPool.call_tool` only fires a `ToolApprovalRequest` for `high`/`critical` tiers. Every v1 fast-path tool is `low`/`medium` (`list_devices`, `select_device`, `attach`, `enumerate_processes`, `enumerate_applications`, `list_sessions` = low/medium; `detach` = medium). So there is no prompt to suppress, and the existing audit path already records every call. The day you want operator-initiated `/execute_script` (critical) or `/write_memory` (high), add the `actor=operator` field on `AuditEntry` and the skip-prompt branch in `RiskAwareToolPool._await_operator` together — that is one coherent follow-up, not part of this plan.
- **`tool_pool` from a command:** `ctx.agent.tool_pool.call_tool(worker, tool, args, ctx=ctx)` is the proven path — `pare/commands/snapshot.py` already uses it. The serving loop reconnects the stdio worker lazily on first call (commit 28496b4), so commands work even though `register_tools` closes the discovery-time pool.
- **Dual output for `/ps` and `/apps`:** the enumerate tools persist rows to `@snapshots` (the agent's view) and the command pages + renders them (the operator's view) — one invocation, both artifacts, no extra plumbing.
- **`render_table`** lives in `pare/commands/_snapshot_render.py` and is reused verbatim; it handles the empty case (`(no rows)`) and width-clips to 100 cols.
```