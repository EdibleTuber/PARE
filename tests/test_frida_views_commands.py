# tests/test_frida_views_commands.py
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
    """Fake tool_pool routing by tool name to a canned payload. Records every
    call as (worker, tool, args, capture) so tests can assert the operator fast
    path dispatches with capture=False and makes no second page_capture call."""

    def __init__(self, by_tool):
        self._by_tool = by_tool
        self.calls = []

    async def call_tool(self, worker, tool, args, ctx=None, capture=True):
        self.calls.append((worker, tool, args, capture))
        return _Result(self._by_tool[tool])


class _Ctx:
    def __init__(self, pool):
        self.agent = type("A", (), {"tool_pool": pool})()


async def _run(cmd_cls, raw, by_tool):
    cmd = cmd_cls()
    ctx = _Ctx(_Pool(by_tool))
    msgs = [m async for m in cmd.run(raw, ctx)]
    return msgs, ctx.agent.tool_pool


# --- Devices / Sessions (unchanged commands; coverage retained) ---------------

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


# --- Ps / Apps: full-list render, capture=False, no page_capture --------------

@pytest.mark.asyncio
async def test_ps_renders_full_list_and_uses_capture_false():
    msgs, pool = await _run(Ps, "", {
        "enumerate_processes": {"summary": "2 processes", "processes": [
            {"pid": 1, "name": "init"}, {"pid": 9, "name": "zygote"}]},
    })
    text = msgs[-1].text
    assert "zygote" in text and "init" in text
    # operator fast path stores but never substitutes -> capture=False
    assert ("frida", "enumerate_processes", {}, False) in pool.calls
    # single call; no second page_capture round-trip
    assert all(tool != "page_capture" for _, tool, _, _ in pool.calls)


@pytest.mark.asyncio
async def test_apps_renders_applications_key():
    msgs, pool = await _run(Apps, "", {
        "enumerate_applications": {"summary": "1 applications", "applications": [
            {"identifier": "com.x"}]},
    })
    assert "com.x" in msgs[-1].text
    assert ("frida", "enumerate_applications", {}, False) in pool.calls
    assert all(tool != "page_capture" for _, tool, _, _ in pool.calls)
