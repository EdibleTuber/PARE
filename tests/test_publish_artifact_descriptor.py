"""Descriptor publication — PARE writes the artifact's reference to the bench.

Spec: docs/superpowers/specs/2026-09-06-arcticbase-artifacts-design.md, and
the P4 wiring plan (docs/superpowers/plans/2026-10-06-artifact-wiring-p4.md
T5): when a worker tool result lands `ctx.artifact_descriptor`, PARE publishes
it to the project's workbench as kind="file" carrying a retrieval command —
once per landing — and a failure is reported into the chat, never raised.

Everything here runs without a server. The real client is replaced at
`pare.agent.ArcticBaseClient`, the module attribute pare/agent.py:69 binds,
with a fake that records calls (the fakes in tests/test_publish_finding.py
run the real client against a port nothing listens on; that is too blunt
here, because these tests must inspect the exact arguments handed to
ensure_workbench and publish_descriptor). The live round-trip for the client
itself lives in tests/test_arcticbase_client_live.py.

Each test is written to kill one of the six named mutants from the brief;
the docstrings say which.
"""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_core.agent import HandlerContext
from agent_core.capture import CaptureStore
from agent_core.conversation import Conversation
from agent_core.inference import CompletionResult, ToolCall
from agent_core.protocol import ErrorMessage, ResponseMessage

import pare.agent
from pare.agent import PareAgent
from pare.arcticbase import ArcticBaseError
from pare.tools.publish_finding import PublishFinding

SLUG = "bench-store-abc123"

# The eight validated descriptor fields exactly as the pool lands them on
# the ctx: agent_core/workers/artifacts.py's seven, plus produced_by. The
# host here is the daemon-substituted spec.artifact_host, which is why it
# is the ONLY host the retrieval line may be built from.
DESCRIPTOR = {
    "host": "bench.example",
    "path": "/mnt/bench-store/dump.bin",
    "size": 2097152,
    "sha256": "ab" * 32,
    "hashed_at": "2026-10-06T12:00:00Z",
    "media_type": "application/octet-stream",
    "drive_id": "nvme0",
    "produced_by": "frida.dump_proc_mem",
}

# The fake tool's result body. Its host is a DIFFERENT string from the
# descriptor's on purpose: retrieval built from the tool-result body
# instead of the validated descriptor says "body-side.example" and fails
# the exact-string pins below.
TOOL_BODY = json.dumps({
    "host": "body-side.example",
    "path": DESCRIPTOR["path"],
    "data": "....",
})

EXPECTED_RETRIEVAL = f"scp {DESCRIPTOR['host']}:{DESCRIPTOR['path']} ."


# --- the fake client ----------------------------------------------------------

class _FakeClient:
    """Stands in for ArcticBaseClient; touches no network, records every call.

    The signatures mirror the real ones positionally, so a caller that
    reached for keyword arguments or a wrong arity breaks here, not in prod.
    """

    def __init__(self, base_url):
        self.base_url = base_url
        self.ensure_calls = []
        self.publish_calls = []

    def ensure_workbench(self, slug, title):
        self.ensure_calls.append((slug, title))
        return True

    def publish_descriptor(self, slug, title, descriptor):
        self.publish_calls.append((slug, title, descriptor))
        return "desc-oid-1"


def _fake_clients(monkeypatch, *, ensure_raises=None, publish_raises=None):
    """Replace pare.agent.ArcticBaseClient with a factory handing out armed
    fakes; returns the list of clients the code under test constructed."""
    made: list[_FakeClient] = []

    def factory(base_url):
        client = _FakeClient(base_url)
        if ensure_raises is not None:
            def ensure(slug, title):
                raise ensure_raises
            client.ensure_workbench = ensure
        if publish_raises is not None:
            def publish(slug, title, descriptor):
                raise publish_raises
            client.publish_descriptor = publish
        made.append(client)
        return client

    monkeypatch.setattr(pare.agent, "ArcticBaseClient", factory)
    return made


# --- the agent-under-test ------------------------------------------------------

def _make_agent(url="http://127.0.0.1:59999"):
    """A PareAgent with the framework-populated attrs stubbed (the
    test_handle_chat.py pattern), plus the arcticbase_url this feature's
    gate reads. The port is one nothing listens on; no test here may let
    the real client run."""
    agent = PareAgent()
    agent.decide_mode = lambda conv: "off"
    agent.system_prompt = lambda ctx: "SYSTEM"
    agent.tool_executor = MagicMock()
    agent.tool_executor.schemas = MagicMock(return_value=[])
    agent.inference = MagicMock()
    agent._capture_stores = MagicMock()
    agent._capture_stores.resolve.return_value = CaptureStore.open_memory()
    agent._disambig_resolved = {}
    agent.worker_manager = None
    agent.config = SimpleNamespace(project_marker=".pare", arcticbase_url=url)
    return agent


def _ctx(cwd):
    """A real HandlerContext — not a MagicMock — because these tests read
    and write ctx.artifact_descriptor through the publish path."""
    return HandlerContext(conversation=Conversation(history_depth=50),
                          channel_id="test", writer=None, cwd=str(cwd))


def _project(tmp_path):
    marker = tmp_path / ".pare"
    marker.mkdir()
    (marker / "project").write_text(SLUG + "\n")
    return tmp_path


class _Stream:
    """Async-iterable returning the given items in order."""
    def __init__(self, items):
        self._items = items

    def __aiter__(self):
        async def gen():
            for it in self._items:
                yield it
        return gen()


def _artifact_turn(agent, *, rounds: int):
    """Model makes `rounds` tool calls; the first one lands a fresh copy of
    DESCRIPTOR on the ctx, exactly as
    RiskAwareToolPool._execute_and_audit does, and returns a body whose host
    differs from the descriptor's. Later calls land nothing.

    rounds=1: one artifact call, then a text completion. rounds=2: a second
    round runs a vault search, then the text completion."""
    call = ToolCall(id="t1", name="dump_proc_mem", arguments={"pid": 121})
    agent.inference.stream = MagicMock(return_value=_Stream([[call]]))
    followups = []
    if rounds >= 2:
        followups.append(CompletionResult(
            type="tool_calls",
            tool_calls=[ToolCall(id="t2", name="search_vault",
                                 arguments={"query": "dump"})],
            usage=None))
    followups.append(CompletionResult(type="text", content="final answer",
                                      usage=None))
    agent.inference.complete = AsyncMock(side_effect=followups)

    def run(name, args, ctx):
        if name == "dump_proc_mem":
            ctx.artifact_descriptor = copy.deepcopy(DESCRIPTOR)
            return TOOL_BODY
        return "vault hits"

    agent.tool_executor.run = AsyncMock(side_effect=run)


async def _collect(agent, ctx):
    msg = MagicMock()
    msg.text = "dig"
    return [m async for m in agent.handle_chat(msg, ctx)]


def _notices(out):
    """The text of every ResponseMessage the turn yielded."""
    return [m.text for m in out if isinstance(m, ResponseMessage)]


def _assert_toolcalls_paired(msgs):
    """Every assistant message carrying `tool_calls` must be followed by a
    `tool` message for each of its ids (the test_handle_chat.py helper) —
    the publish notice must not break pairing."""
    for i, m in enumerate(msgs):
        if m.get("role") != "assistant" or not m.get("tool_calls"):
            continue
        ids = {tc["id"] for tc in m["tool_calls"]}
        seen = set()
        j = i + 1
        while j < len(msgs) and msgs[j].get("role") == "tool":
            seen.add(msgs[j]["tool_call_id"])
            j += 1
        missing = ids - seen
        assert not missing, f"tool_calls {missing} at index {i} have no tool result"


# --- the publish itself ---------------------------------------------------------

@pytest.mark.asyncio
async def test_a_landed_descriptor_is_published_exactly_once(tmp_path,
                                                             monkeypatch):
    """Brief test 1. The descriptor lands off a tool result and must reach
    the workbench exactly once, under the project's slug, titled by
    produced_by, carrying its eight fields plus the retrieval line built
    from the DESCRIPTOR's host and produced_at_project.

    Kills: 'drop the publish' (nothing recorded); 'title from the wrong
    field' (title pinned to DESCRIPTOR['produced_by'] only); 'retrieval
    from the tool-result body' (the body's host is body-side.example and
    appears nowhere in the pinned content)."""
    agent = _make_agent()
    made = _fake_clients(monkeypatch)
    root = _project(tmp_path)
    ctx = _ctx(root)
    _artifact_turn(agent, rounds=1)

    out = await _collect(agent, ctx)

    assert len(made) == 1, "exactly one fresh client per publish"
    assert made[0].base_url == agent.config.arcticbase_url
    client = made[0]
    assert client.ensure_calls == [(SLUG, SLUG)]
    assert len(client.publish_calls) == 1
    slug, title, content = client.publish_calls[0]
    assert slug == SLUG
    assert title == DESCRIPTOR["produced_by"]
    assert content == {
        **DESCRIPTOR,
        "retrieval": EXPECTED_RETRIEVAL,
        "produced_at_project": SLUG,
    }
    # The stored descriptor must not have grown publication keys in place.
    assert "retrieval" not in DESCRIPTOR
    # And the turn completes normally, its pairing intact.
    assert out[-1].text == "final answer"
    assert ctx.artifact_descriptor is None
    _assert_toolcalls_paired(
        ctx.conversation.get_messages_for_api(system_prompt="S"))


@pytest.mark.asyncio
async def test_the_descriptor_is_consumed_not_copied(tmp_path, monkeypatch):
    """Brief test 2. Publishing is a CONSUME: the ctx field is cleared
    immediately, so a second tool call in the same turn — one that lands
    nothing new — publishes nothing more.

    Kills: 'publish twice (no clear)' — with the field left set, the
    second call's gate check republishes and publish_calls holds two."""
    agent = _make_agent()
    made = _fake_clients(monkeypatch)
    root = _project(tmp_path)
    ctx = _ctx(root)
    _artifact_turn(agent, rounds=2)

    await _collect(agent, ctx)

    assert len(made) == 1, "one client, one publish, for one landed descriptor"
    assert made[0].publish_calls and len(made[0].publish_calls) == 1
    assert ctx.artifact_descriptor is None


# --- the gate --------------------------------------------------------------------

@pytest.mark.asyncio
async def test_an_unset_workbench_url_constructs_no_client_and_says_nothing(
        tmp_path, monkeypatch):
    """Brief test 3. No arcticbase_url means the workbench channel does
    not exist for this agent: not a failed publish to report, just the
    feature being off — no client constructed, no notice, turn completes.

    Kills: a publish that ignores the config gate would construct a
    client (recorded), or would report a failure notice into the chat."""
    agent = _make_agent(url="")
    made = _fake_clients(monkeypatch)
    root = _project(tmp_path)
    ctx = _ctx(root)
    _artifact_turn(agent, rounds=1)

    out = await _collect(agent, ctx)

    assert made == []
    assert not any(isinstance(m, ErrorMessage) for m in out)
    for text in _notices(out):
        assert "descriptor" not in text.lower()
    assert out[-1].text == "final answer"


@pytest.mark.asyncio
async def test_publish_without_a_project_slug_is_reported_not_raised(
        tmp_path, monkeypatch):
    """A descriptor can land while the stamp found no project (the cwd has
    no marker): with no slug there is no workbench to write to, and that
    is a publish failure to report, not an exception — and not a client
    constructed with a None slug either."""
    agent = _make_agent()
    made = _fake_clients(monkeypatch)
    ctx = _ctx(tmp_path)  # no .pare/ anywhere: ctx.project_slug stays None
    _artifact_turn(agent, rounds=1)

    out = await _collect(agent, ctx)

    assert made == [], "no slug means no client — nothing to name a workbench"
    assert any("descriptor" in t.lower() and "slug" in t.lower()
               for t in _notices(out))
    assert ctx.artifact_descriptor is None
    assert out[-1].text == "final answer"


# --- failure is reported into the chat, never raised -------------------------------

@pytest.mark.asyncio
async def test_a_failed_ensure_is_reported_into_the_chat(tmp_path,
                                                         monkeypatch):
    """Brief test 4. ensure_workbench raises ArcticBaseError (the §10 voice
    from publish_finding: '{TypeName}: {message}') — the turn keeps going,
    the text lands in the yielded messages AND the conversation, the
    descriptor is still cleared, nothing is raised.

    Kills: 're-raise on client error'."""
    agent = _make_agent()
    _fake_clients(monkeypatch,
                  ensure_raises=ArcticBaseError("ArcticBase did not answer"))
    root = _project(tmp_path)
    ctx = _ctx(root)
    _artifact_turn(agent, rounds=1)

    out = await _collect(agent, ctx)  # must not raise

    failure = "ArcticBaseError: ArcticBase did not answer"
    assert any(failure in t for t in _notices(out))
    assert out[-1].text == "final answer"
    assert ctx.artifact_descriptor is None
    msgs = ctx.conversation.get_messages_for_api(system_prompt="S")
    assert any(m["role"] == "assistant" and failure in (m.get("content") or "")
               for m in msgs)
    _assert_toolcalls_paired(msgs)


@pytest.mark.asyncio
async def test_an_oserror_from_the_publish_is_caught_too(tmp_path, monkeypatch):
    """The catch is (ArcticBaseError, OSError) around the WHOLE
    ensure+publish, not just the client's own error family: an OSError
    from publish_descriptor is reported in the same voice, the descriptor
    is cleared, the chat continues.

    Kills: 'narrow the except back' to ArcticBaseError (the OSError
    escapes handle_chat's contract through the tool loop)."""
    agent = _make_agent()
    _fake_clients(monkeypatch,
                  publish_raises=OSError("read-only filesystem"))
    root = _project(tmp_path)
    ctx = _ctx(root)
    _artifact_turn(agent, rounds=1)

    out = await _collect(agent, ctx)  # must not raise

    failure = "OSError: read-only filesystem"
    assert any(failure in t for t in _notices(out))
    assert out[-1].text == "final answer"
    assert ctx.artifact_descriptor is None


# --- publish_finding's widened except (P3-R15 precedent) ---------------------------

def _finding_ctx(cwd):
    cfg = SimpleNamespace(arcticbase_url="http://127.0.0.1:59999",
                          project_marker=".pare")
    return SimpleNamespace(cwd=str(cwd),
                           agent=SimpleNamespace(config=cfg))


@pytest.mark.asyncio
@pytest.mark.parametrize("break_the_project_file", [
    pytest.param("non-utf8",
                 id="UnicodeDecodeError_from_binary_project_file"),
    pytest.param("directory",
                 id="OSError_project_file_is_a_directory"),
])
async def test_publish_finding_survives_a_project_file_it_cannot_read(
        tmp_path, break_the_project_file):
    """The slug read is a file read, and a file read raises: non-UTF-8
    bytes give UnicodeDecodeError, a directory named 'project' gives
    IsADirectoryError — neither is a ProjectSlugError, and today neither
    is caught (this is the widening's own RED). The tool must answer with
    the error JSON, not with a traceback through the tool loop."""
    marker = tmp_path / ".pare"
    marker.mkdir()
    if break_the_project_file == "directory":
        (marker / "project").mkdir()
    else:
        (marker / "project").write_bytes(b"\xff\xfe\x00not-utf8")

    out = json.loads(await PublishFinding().run(
        {"title": "t", "markdown": "x"}, _finding_ctx(tmp_path)))

    assert out["status"] == "error"
