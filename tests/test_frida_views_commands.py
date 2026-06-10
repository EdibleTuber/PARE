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
