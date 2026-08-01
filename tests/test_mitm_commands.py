import json
import pytest
from pare.commands.mitm import Mitm


class _Block:
    def __init__(self, text):
        self.type = "text"; self.text = text


class _Result:
    def __init__(self, payload):
        self.isError = False; self.content = [_Block(json.dumps(payload))]


class _Pool:
    def __init__(self, payload):
        self._payload = payload; self.calls = []

    async def call_tool(self, worker, tool, args, ctx=None, capture=True):
        self.calls.append((worker, tool, args))
        return _Result(self._payload)


class _Agent:
    def __init__(self, pool, enable_mitm=True):
        self.tool_pool = pool
        self.config = type("C", (), {"enable_mitm": enable_mitm})()


class _Ctx:
    def __init__(self, agent):
        self.agent = agent


async def _run(raw, agent):
    return [m async for m in Mitm().run(raw, _Ctx(agent))]


async def test_status_calls_capture_health():
    agent = _Agent(_Pool({"summary": "proxy reachable — 3 flows, 1 tls-errors",
                          "reachable": True, "flows": 3, "tls_errors": 1}))
    msgs = await _run("status", agent)
    assert "3 flows" in msgs[-1].text
    assert ("mitm", "capture_health", {}) in agent.tool_pool.calls


async def test_status_when_disabled_hints_flag():
    agent = _Agent(_Pool({}), enable_mitm=False)
    msgs = await _run("status", agent)
    assert "PARE_ENABLE_MITM" in msgs[-1].text
    assert agent.tool_pool.calls == []  # no worker call when disabled


async def test_up_invokes_launcher(monkeypatch):
    calls = {}

    async def fake_launch():
        calls["ran"] = True
        return 0, "mitm daemon up (proxy :8080, ui :8081, control :8788)"

    monkeypatch.setattr("pare.commands.mitm._launch_daemon", fake_launch)
    agent = _Agent(_Pool({}))
    msgs = await _run("up", agent)
    assert calls.get("ran") and "daemon up" in msgs[-1].text
