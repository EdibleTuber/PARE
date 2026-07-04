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
    out = await _collect(Snapshot().run("list", _Ctx(_Agent(_store()))))
    assert "enumerate_processes" in out[0].text


@pytest.mark.asyncio
async def test_snapshot_default_renders_latest_rows():
    out = await _collect(Snapshot().run("", _Ctx(_Agent(_store()))))
    assert "zygote" in out[0].text and "init" in out[0].text


@pytest.mark.asyncio
async def test_snapshot_query_filters_rows():
    out = await _collect(Snapshot().run(" zygote", _Ctx(_Agent(_store()))))  # leading space -> sub="", rest="zygote"
    assert "zygote" in out[0].text and "init" not in out[0].text


@pytest.mark.asyncio
async def test_snapshot_empty_store_is_friendly():
    out = await _collect(Snapshot().run("", _Ctx(_Agent(CaptureStore.open_memory()))))
    assert "nothing captured" in out[0].text.lower()


@pytest.mark.asyncio
async def test_snapshot_renders_real_frida_envelope_as_rows():
    """A wire-captured frida enumerate stores the FULL 2-key envelope
    {"summary": ..., "processes": [...]} — not a bare list. /snapshot must
    unwrap it to per-process rows, not one mangled cell. Regression for the
    infer_rows annotated-list fix."""
    s = CaptureStore.open_memory()
    s.write(CaptureRecord(
        worker="frida", tool="enumerate_processes", session_id=None, launch_ts=1.0,
        summary="2 processes",
        body=json.dumps({"summary": "2 processes",
                         "processes": [{"pid": 1, "name": "init"}, {"pid": 9, "name": "zygote"}]}),
        rows=2, addrs=[]))
    out = await _collect(Snapshot().run("", _Ctx(_Agent(s))))
    text = out[0].text
    assert "· 2 rows" in text           # unwrapped to 2 rows, not "1 rows"
    assert "init" in text and "zygote" in text
    assert "pid" in text                 # process columns, not envelope keys
    assert "summary" not in text.split("\n")[0]  # header is not the envelope
