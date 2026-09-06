"""Step 4: what the operator sees once workers live on other machines.

Three separate problems, all of them copy or state that was true only while
every worker was a local subprocess.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_core.workers.manager import WorkerOpResult, WorkerStatus
from pare.commands.health import _worker_lines
from pare.commands.worker import (Worker, _destruction_body, _state_of,
                                  _where_of)
from pare.handback import (POLL_FAILURE_LIMIT, is_worker_failure,
                           poll_failure_question)


def _status(name="frida", **kw):
    base = dict(name=name, loaded=True, tool_count=19, transport="stdio",
                risk_default="low", capability_tags=[], autoload=False,
                last_error=None, endpoint=None, reachable=None)
    base.update(kw)
    return WorkerStatus(**base)


def _ctx(mgr):
    ctx = MagicMock()
    ctx.agent.worker_manager = mgr
    return ctx


async def _run(cmd, args, ctx):
    return "\n".join([m.text async for m in cmd.run(args, ctx)])


# --- 5.6: copy that claims state was destroyed when it was not ------------

def test_unloading_a_local_worker_still_says_the_attachments_died():
    body = _destruction_body("stdio")
    assert "are gone" in body
    assert "REMOTE" not in body


def test_unloading_a_remote_worker_must_not_claim_the_attachments_died():
    """The dangerous direction. Unloading an HTTP worker is a client-side
    disconnect: the remote process keeps running and a frida session it holds
    keeps holding whatever it was attached to. An operator told 'attachments
    are gone' may walk away from a live hook on a target."""
    body = _destruction_body("streamable_http")
    assert "keeps running" in body
    assert "must be cleaned up there" in body
    assert "are gone" not in body


def test_an_unknown_transport_over_warns_rather_than_under_warns():
    """Over-warning about state that might survive is recoverable.
    Under-warning about a live attachment is not."""
    assert _destruction_body(None) == _destruction_body("streamable_http")


async def test_a_failed_remote_reload_says_which_machine_to_look_on():
    """`its process may still be running` sent the operator hunting locally
    for a process on another host."""
    mgr = MagicMock()
    mgr.is_loaded.return_value = True
    mgr.tools_of.return_value = ["frida_attach"] * 19
    mgr.status.return_value = [_status(transport="streamable_http",
                                       endpoint="http://100.64.0.7:9101/mcp")]
    mgr.reload = AsyncMock(return_value=WorkerOpResult(
        "reload", "frida", False,
        error="unload half failed: worker 'frida' did not shut down within 5.0s",
        error_kind="disconnect_timeout"))
    out = await _run(Worker(), "reload frida", _ctx(mgr))
    assert "may still be running" in out, "must not imply the process died"
    assert "REMOTE" in out, "must say the process is on another host"


# --- 7.4: which machine, and is it answering ------------------------------

def test_the_table_shows_the_host_not_just_the_word_http():
    assert _where_of(_status(transport="streamable_http",
                             endpoint="http://100.64.0.7:9101/mcp")) == "http 100.64.0.7:9101"
    assert _where_of(_status(transport="stdio")) == "stdio"


def test_an_unreachable_worker_is_not_reported_as_merely_loaded():
    """`loaded` alone reads as healthy. It means the daemon registered the
    tools, not that the machine is answering."""
    assert _state_of(_status(reachable=False)) == "UNREACHABLE"
    assert _state_of(_status(reachable=True)) == "loaded"
    assert _state_of(_status(reachable=None)) == "loaded"
    assert _state_of(_status(loaded=False)) == "unloaded"


def test_health_carries_what_worker_list_had_and_health_did_not():
    """/health is the command an operator types first, and it was strictly
    less informative than /worker list."""
    lines = "\n".join(_worker_lines([
        _status("frida", transport="streamable_http",
                endpoint="http://100.64.0.7:9101/mcp", reachable=False,
                last_error="unreachable at http://100.64.0.7:9101/mcp: TimeoutError"),
        _status("static", reachable=None),
    ]))
    assert "LOADED but UNREACHABLE" in lines
    assert "100.64.0.7:9101" in lines, "must name the host"
    assert "last error:" in lines
    assert "TimeoutError" in lines


def test_health_does_not_claim_a_liveness_observation_it_never_made():
    """stdio workers are never probed, so `reachable` is None. Printing
    'reachable' there would be an assertion nobody checked."""
    lines = "\n".join(_worker_lines([_status("static", reachable=None)]))
    assert "reachable" not in lines


# --- 7.2: the POLL_TOOLS trap --------------------------------------------

def test_an_empty_poll_is_not_a_failure():
    """The distinction the whole trigger rests on. POLL_TOOLS are exempt from
    the spin guard because empty polls are normal and may repeat all turn."""
    assert is_worker_failure("") is False
    assert is_worker_failure(None) is False
    assert is_worker_failure('{"events": []}') is False


def test_both_worker_failure_shapes_are_recognised():
    assert is_worker_failure("frida_read_hook_events call failed: ConnectError")
    assert is_worker_failure("frida_read_hook_events returned an error: boom")


def test_the_failure_markers_still_match_what_agent_core_actually_emits():
    """Pins the text this predicate depends on to the real producer.

    A wording change in agent_core's make_tool_class would otherwise disable
    the handback silently -- polling would go back to failing invisibly, which
    is the exact bug this exists to fix.
    """
    import asyncio

    from agent_core.workers.tool_factory import make_tool_class
    from agent_core.workers.types import WorkerSpec

    class _FailingPool:
        def emit_lifecycle(self, *a, **k): ...
        async def call_tool(self, *a, **k):
            raise ConnectionError("no route to host")

    spec = WorkerSpec(name="frida", transport="streamable_http",
                      endpoint="http://h:1/mcp", risk_default="low")
    tool_cls = make_tool_class(spec, {"name": "read_hook_events",
                                      "description": "", "inputSchema": {}},
                               _FailingPool())
    out = asyncio.run(tool_cls().run({}, None))
    assert is_worker_failure(out), (
        f"agent_core now emits {out!r}, which pare.handback no longer "
        f"recognises as a failure")


def test_the_handback_names_the_worker_and_the_command_that_diagnoses_it():
    q = poll_failure_question("frida_read_hook_events", 3,
                              "frida_read_hook_events call failed: ConnectError")
    assert "frida" in q
    assert "/worker list" in q, "the useful next action is checking the link"
    assert "3 times in a row" in q
    assert POLL_FAILURE_LIMIT == 3
