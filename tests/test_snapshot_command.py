import json
import pytest
from pare.commands.snapshot import Snapshot


class _Block:
    def __init__(self, text): self.type = "text"; self.text = text


class _Result:
    def __init__(self, payload): self.isError = False; self.content = [_Block(json.dumps(payload))]


class _Pool:
    """Fake tool_pool: routes page_capture calls by their arguments."""
    def __init__(self, responses): self._responses = responses; self.calls = []
    async def call_tool(self, worker, tool, args, ctx=None):
        self.calls.append((worker, tool, args))
        for matcher, payload in self._responses:
            if matcher(args):
                return _Result(payload)
        raise AssertionError(f"no canned response for {args}")


class _Ctx:
    def __init__(self, pool):
        self.agent = type("A", (), {"tool_pool": pool})()
        self.channel_id = "cli-default"


async def _collect(cmd, raw):
    return [m async for m in cmd.run(raw, cmd._ctx)]


def _cmd(responses):
    c = Snapshot()
    c._ctx = _Ctx(_Pool(responses))
    return c


@pytest.mark.asyncio
async def test_bare_snapshot_renders_latest():
    c = _cmd([(lambda a: not a.get("list_sources") and not a.get("source"),
               {"store": "@snapshots", "source": "apps:emu", "total": 1, "shown": 1,
                "rows": [{"identifier": "com.bank", "name": "Bank"}]})])
    msgs = await _collect(c, "")
    text = msgs[-1].text
    assert "com.bank" in text and "apps:emu" in text


@pytest.mark.asyncio
async def test_list_subcommand_shows_catalog():
    c = _cmd([(lambda a: a.get("list_sources"),
               {"sources": [{"source": "apps:emu", "count": 21}]})])
    msgs = await _collect(c, "list")
    assert "apps:emu" in msgs[-1].text and "21" in msgs[-1].text


@pytest.mark.asyncio
async def test_substring_key_resolves_then_reads():
    c = _cmd([
        (lambda a: a.get("list_sources"),
         {"sources": [{"source": "enumerate_applications:device=emu", "count": 2}]}),
        (lambda a: a.get("source") == "enumerate_applications:device=emu",
         {"store": "@snapshots", "source": "enumerate_applications:device=emu",
          "total": 2, "shown": 2, "rows": [{"identifier": "com.bank"}]}),
    ])
    msgs = await _collect(c, "applications")
    assert "com.bank" in msgs[-1].text


@pytest.mark.asyncio
async def test_ambiguous_substring_lists_candidates():
    c = _cmd([(lambda a: a.get("list_sources"),
               {"sources": [{"source": "enumerate_exports:module=libart.so", "count": 9},
                            {"source": "enumerate_exports:module=libartbase.so", "count": 3}]})])
    msgs = await _collect(c, "libart")
    text = msgs[-1].text
    assert "libart.so" in text and "libartbase.so" in text


@pytest.mark.asyncio
async def test_no_match_message():
    c = _cmd([(lambda a: a.get("list_sources"), {"sources": []})])
    msgs = await _collect(c, "nope")
    assert "no snapshot matches" in msgs[-1].text.lower()
