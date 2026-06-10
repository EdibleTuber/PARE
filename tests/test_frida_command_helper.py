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
