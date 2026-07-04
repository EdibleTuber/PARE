# tests/test_frida_views_commands.py
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


@pytest.mark.asyncio
async def test_apps_renders_applications_key():
    class _AppsPool(_Pool):
        async def call_tool(self, worker, tool, args, ctx=None, capture=True):
            self.calls.append((tool, capture))
            return _Result('{"summary": "1 applications", "applications": [{"identifier": "com.x"}]}')
    agent = _Agent(); agent.tool_pool = _AppsPool()
    out = await _collect(Apps().run("", _Ctx(agent)))
    assert "com.x" in out[0].text
    assert ("enumerate_applications", False) in agent.tool_pool.calls
