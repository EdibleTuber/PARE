"""The /worker operator surface."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_core.workers.manager import WorkerOpResult, WorkerStatus
from pare.commands.worker import Worker


def _ctx(manager):
    agent = MagicMock()
    agent.worker_manager = manager
    return MagicMock(agent=agent)


def _status(name, loaded, count, err=None, autoload=True):
    return WorkerStatus(name=name, loaded=loaded, tool_count=count,
                        transport="stdio", risk_default="low",
                        capability_tags=["a", "b"], autoload=autoload,
                        last_error=err)


async def _run(cmd, args, ctx):
    return "\n".join([m.text async for m in cmd.run(args, ctx)])


async def test_list_shows_loaded_and_unloaded():
    mgr = MagicMock()
    mgr.status.return_value = [
        _status("frida", True, 19),
        _status("hardware", False, 0, err="No such file: pare-hardware-mcp",
                autoload=False),
    ]
    out = await _run(Worker(), "list", _ctx(mgr))
    assert "frida" in out and "19" in out
    assert "hardware" in out and "No such file" in out


async def test_bare_worker_defaults_to_list():
    mgr = MagicMock()
    mgr.status.return_value = [_status("frida", True, 19)]
    assert await _run(Worker(), "", _ctx(mgr)) == await _run(Worker(), "list", _ctx(mgr))


async def test_tools_lists_the_workers_tools():
    mgr = MagicMock()
    mgr.tools_of.return_value = ["frida_attach", "frida_detach"]
    out = await _run(Worker(), "tools frida", _ctx(mgr))
    assert "frida_attach" in out and "frida_detach" in out


async def test_load_reports_the_tool_count():
    mgr = MagicMock()
    mgr.load = AsyncMock(return_value=WorkerOpResult(
        "load", "hardware", True, tool_count=6, tools=["hardware_scan"]))
    out = await _run(Worker(), "load hardware", _ctx(mgr))
    assert "hardware" in out and "6" in out


async def test_load_failure_surfaces_the_reason():
    mgr = MagicMock()
    mgr.load = AsyncMock(return_value=WorkerOpResult(
        "load", "hardware", False, error="No such file or directory",
        error_kind="spawn_failed"))
    out = await _run(Worker(), "load hardware", _ctx(mgr))
    assert "spawn_failed" in out and "No such file" in out


async def test_unload_names_what_it_destroyed():
    """D6 makes the consequence documented rather than guarded; this output IS
    the documentation."""
    mgr = MagicMock()
    mgr.unload = AsyncMock(return_value=WorkerOpResult(
        "unload", "frida", True, tool_count=19))
    out = await _run(Worker(), "unload frida", _ctx(mgr))
    assert "19" in out
    assert "attach" in out.lower() or "hook" in out.lower(), (
        "must warn that live attachments and hooks are gone")
    assert "captures" in out.lower(), "must say captured findings survive"


async def test_unknown_subcommand_shows_usage():
    out = await _run(Worker(), "frobnicate", _ctx(MagicMock()))
    assert "usage" in out.lower()


async def test_load_without_a_name_shows_usage():
    out = await _run(Worker(), "load", _ctx(MagicMock()))
    assert "usage" in out.lower()


async def test_command_requires_the_manager():
    assert "worker_manager" in Worker.requires


def test_worker_is_registered():
    from pare.agent import PareAgent
    assert Worker in PareAgent.commands


async def test_reload_reports_the_tool_count():
    """Sanity check the reload branch: not exercised by the brief's own test
    list, but reload shares _render_load with load and must format the same
    way (verb agreement: "reloaded", not "loadeded")."""
    mgr = MagicMock()
    mgr.reload = AsyncMock(return_value=WorkerOpResult(
        "reload", "frida", True, tool_count=19, tools=["frida_attach"]))
    out = await _run(Worker(), "reload frida", _ctx(mgr))
    assert "reloaded frida" in out and "19" in out


async def test_reload_without_a_name_shows_usage():
    out = await _run(Worker(), "reload", _ctx(MagicMock()))
    assert "usage" in out.lower()


async def test_load_and_unload_note_the_reprocess_cost():
    """D7: any load/unload changes the tool list, which changes the prompt
    prefix and forces a slower reply next turn. Without a note, an operator
    reading a slow reply after a totally unrelated command would reasonably
    read it as a hang."""
    mgr = MagicMock()
    mgr.load = AsyncMock(return_value=WorkerOpResult(
        "load", "hardware", True, tool_count=6, tools=["hardware_scan"]))
    mgr.unload = AsyncMock(return_value=WorkerOpResult(
        "unload", "frida", True, tool_count=19))
    load_out = await _run(Worker(), "load hardware", _ctx(mgr))
    unload_out = await _run(Worker(), "unload frida", _ctx(mgr))
    assert "slower" in load_out.lower() or "reprocess" in load_out.lower()
    assert "slower" in unload_out.lower() or "reprocess" in unload_out.lower()


async def test_failed_load_does_not_claim_a_reprocess_is_coming():
    """A failed load changed nothing about the tool list, so it must not
    carry the reprocess note — that would tell the operator to expect a slow
    reply that isn't coming."""
    mgr = MagicMock()
    mgr.load = AsyncMock(return_value=WorkerOpResult(
        "load", "hardware", False, error="boom", error_kind="spawn_failed"))
    out = await _run(Worker(), "load hardware", _ctx(mgr))
    assert "slower" not in out.lower() and "reprocess" not in out.lower()


async def test_unload_failure_still_carries_the_destruction_warning():
    """A disconnect_timeout on unload still tore down the executor/pool
    registration first (WorkerManager.unload's ordering: tool removal is
    unconditional and happens BEFORE the timeout-bounded disconnect) — so the
    warning about lost attachments/hooks, and the tool-list-changed reprocess
    note, both still apply even though the op reports failure. And the
    headline must not claim "client disconnected" in the same breath as a
    warning saying the process may still be running — that direct
    contradiction was a defect in the original sketch."""
    mgr = MagicMock()
    mgr.unload = AsyncMock(return_value=WorkerOpResult(
        "unload", "frida", False, tool_count=19,
        error="worker 'frida' did not shut down within 5.0s",
        error_kind="disconnect_timeout"))
    out = await _run(Worker(), "unload frida", _ctx(mgr))
    assert "disconnect_timeout" in out
    assert "did not shut down" in out
    assert "attach" in out.lower() or "hook" in out.lower()
    assert "client disconnected" not in out.lower(), (
        "the manager's own error says the process may still be running; "
        "the headline must not simultaneously claim a clean disconnect")
    assert "slower" in out.lower() or "reprocess" in out.lower(), (
        "tool removal happens unconditionally before the disconnect await, "
        "so the tool list already changed even though the disconnect timed out")


async def test_tools_of_unloaded_worker_points_at_worker_list():
    mgr = MagicMock()
    mgr.tools_of.return_value = []
    out = await _run(Worker(), "tools hardware", _ctx(mgr))
    assert "worker list" in out


async def test_list_shows_the_full_last_error_not_just_the_clipped_column():
    """render_table clips each column to fit an 8-column, 100-char-wide row —
    a realistic last_error (a full spawn_failed path from a stale workers.yaml
    entry, per spec 8.4) is 114 chars on its own, so the table cell alone
    shows only the error class, not the path an operator needs to fix it.
    The command output is the only place this shows up, so the full string
    must appear somewhere in it even though the table cell is clipped."""
    long_error = (
        "FileNotFoundError: [Errno 2] No such file or directory: "
        "'/mnt/secondary/projects/PARE/.venv/bin/pare-hardware-mcp'")
    mgr = MagicMock()
    mgr.status.return_value = [
        _status("frida", True, 19),
        _status("hardware", False, 0, err=long_error, autoload=False),
    ]
    out = await _run(Worker(), "list", _ctx(mgr))
    assert long_error in out, (
        "the full last_error string must appear in the output somewhere "
        "(e.g. a footer), even though the table column itself is clipped")


async def test_list_has_no_error_footer_when_the_fleet_is_healthy():
    """A healthy fleet (no worker has a last_error) must print exactly the
    table — no trailing footer lines — so the common case stays clean."""
    from pare.commands._snapshot_render import render_table

    mgr = MagicMock()
    mgr.status.return_value = [_status("frida", True, 19)]
    out = await _run(Worker(), "list", _ctx(mgr))
    expected_rows = [{
        "worker": "frida", "state": "loaded", "tools": "19",
        "transport": "stdio", "floor": "low", "boot": "auto",
        "tags": "a, b", "last error": "",
    }]
    assert out == render_table(expected_rows), (
        "with no last_error on any worker, /worker list must be exactly the "
        "table — no footer appended")
