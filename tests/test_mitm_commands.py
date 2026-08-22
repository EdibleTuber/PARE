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
    def __init__(self, pool):
        self.tool_pool = pool


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


async def test_up_invokes_launcher(monkeypatch):
    calls = {}

    async def fake_launch(subcommand):
        calls["ran"] = subcommand
        return 0, "mitm daemon up (proxy :8080, ui :8081, control :8788)"

    monkeypatch.setattr("pare.commands.mitm._launch_daemon", fake_launch)
    agent = _Agent(_Pool({}))
    msgs = await _run("up", agent)
    assert calls.get("ran") == "up" and "daemon up" in msgs[-1].text


async def test_up_when_daemon_not_on_path_is_friendly(monkeypatch):
    async def fake_launch(subcommand):
        raise FileNotFoundError("pare-mitm-daemon")

    monkeypatch.setattr("pare.commands.mitm._launch_daemon", fake_launch)
    agent = _Agent(_Pool({}))
    msgs = await _run("up", agent)  # must not raise
    assert len(msgs) == 1
    assert "install" in msgs[-1].text.lower()
    assert "pare-mitm-mcp" in msgs[-1].text


async def test_down_invokes_launcher(monkeypatch):
    calls = {}

    async def fake_launch(subcommand):
        calls["ran"] = subcommand
        return 0, "mitm daemon down"

    monkeypatch.setattr("pare.commands.mitm._launch_daemon", fake_launch)
    agent = _Agent(_Pool({}))
    msgs = await _run("down", agent)
    assert calls.get("ran") == "down" and "daemon down" in msgs[-1].text


async def test_unknown_subcommand_yields_usage_and_makes_no_calls(monkeypatch):
    called = {}

    async def fake_launch(subcommand):
        called["ran"] = subcommand
        return 0, "should not run"

    monkeypatch.setattr("pare.commands.mitm._launch_daemon", fake_launch)
    agent = _Agent(_Pool({}))
    msgs = await _run("foo", agent)
    assert len(msgs) == 1
    assert "unknown" in msgs[-1].text.lower()
    assert "up" in msgs[-1].text and "down" in msgs[-1].text and "status" in msgs[-1].text
    assert agent.tool_pool.calls == []  # no worker call
    assert "ran" not in called  # no subprocess launch
