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

    async def call_tool(self, worker, tool, args, ctx=None, capture=True):
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
