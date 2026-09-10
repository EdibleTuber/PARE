"""/health's ArcticBase block — §10.1.

When something does not arrive there are five candidates: tailnet, ArcticBase,
daemon, worker, drive. /health is the command an operator types first, so its
job here is to eliminate candidates -- and, just as importantly, to never claim
an observation it did not make.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from pare.commands.health import Health

UNREACHABLE = "http://127.0.0.1:59999"


async def _collect(ctx, cmd=None) -> str:
    cmd = cmd or Health()
    return "\n".join([m.text async for m in cmd.run("", ctx)])


def _ctx(*, url="", cwd=None, heartbeat=None, specs=None):
    """`specs` are WorkerSpec-shaped: artifact_root lives on the REGISTRY, not
    on WorkerStatus, which carries no such field."""
    cfg = SimpleNamespace(
        inference_url="http://i.invalid", model="m", vault_path="/tmp/v",
        apk_re_agents_url="http://a.invalid", arcticbase_url=url,
        project_marker=".pare")
    agent = SimpleNamespace(name="pare", config=cfg, worker_manager=None,
                            worker_registry=None, _heartbeat=heartbeat)
    if specs is not None:
        agent.worker_registry = SimpleNamespace(all=lambda: specs)
    return SimpleNamespace(agent=agent, cwd=cwd)


# --- not configured ----------------------------------------------------------

@pytest.mark.asyncio
async def test_an_unconfigured_host_says_so_and_names_the_env_var():
    out = await _collect(_ctx(url=""))
    assert "arcticbase" in out.lower()
    assert "PARE_ARCTICBASE_URL" in out
    # It must not imply a reachability check happened.
    assert "unreachable" not in out.lower()


# --- reachability ------------------------------------------------------------

@pytest.mark.asyncio
async def test_an_unreachable_host_is_reported_not_raised():
    out = await _collect(_ctx(url=UNREACHABLE))
    assert UNREACHABLE in out
    assert "UNREACHABLE" in out


# --- the heartbeat -----------------------------------------------------------

@pytest.mark.asyncio
async def test_a_heartbeat_that_has_never_written_says_never():
    """Distinct from 'stale': a daemon that has never beaten and one whose beat
    stopped are different problems with different next steps."""
    hb = SimpleNamespace(boot_id="b1", last_beat_at=None, last_error=None)
    out = await _collect(_ctx(url=UNREACHABLE, heartbeat=hb))
    assert "never" in out.lower()


@pytest.mark.asyncio
async def test_a_heartbeat_error_is_surfaced_verbatim():
    hb = SimpleNamespace(boot_id="b1", last_beat_at=None,
                         last_error="Unreachable: nope did not answer")
    out = await _collect(_ctx(url=UNREACHABLE, heartbeat=hb))
    assert "nope did not answer" in out


@pytest.mark.asyncio
async def test_the_boot_id_is_shown_so_a_restart_is_visible():
    hb = SimpleNamespace(boot_id="deadbeef1234", last_beat_at="2026-09-10T00:00:00Z",
                         last_error=None)
    out = await _collect(_ctx(url=UNREACHABLE, heartbeat=hb))
    assert "deadbeef1234" in out


# --- the project -------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_resolved_slug_and_workbench_url_are_shown(tmp_path):
    (tmp_path / ".pare").mkdir()
    out = await _collect(_ctx(url=UNREACHABLE, cwd=str(tmp_path)))
    assert "/wb/" in out


@pytest.mark.asyncio
async def test_outside_a_project_it_says_so_rather_than_inventing_one(tmp_path):
    out = await _collect(_ctx(url=UNREACHABLE, cwd=str(tmp_path)))
    assert "no project" in out.lower()
    assert "/wb/" not in out


# --- artifact_root: report the declaration, never a fake observation --------

@pytest.mark.asyncio
async def test_a_declared_artifact_root_is_shown_as_declared_not_as_checked():
    """§5.4: the daemon has no view of the worker's filesystem, so resolving
    that path daemon-side resolves against the WRONG namespace -- worse than not
    checking. mounted/free/drive-id needs the worker's own bench_status tool,
    which does not exist yet, so /health must not imply it looked."""
    spec = SimpleNamespace(name="hardware", artifact_root="/mnt/bench-store")
    out = await _collect(_ctx(url=UNREACHABLE, specs=[spec]))
    assert "/mnt/bench-store" in out
    assert "declared" in out.lower()
    # No invented facts about a filesystem it cannot see.
    for forbidden in ("free", "mounted", "drive id"):
        assert forbidden not in out.lower().replace("not checked", "")


@pytest.mark.asyncio
async def test_a_worker_with_no_artifact_root_is_not_listed_as_having_one():
    spec = SimpleNamespace(name="frida", artifact_root=None)
    out = await _collect(_ctx(url=UNREACHABLE, specs=[spec]))
    assert "artifact_root" not in out.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["not-a-url", "://broken", "ftp:/nope", "   "])
async def test_a_malformed_url_degrades_to_a_line_not_a_traceback(bad):
    """/health is typed when something is already wrong. urllib raises
    ValueError -- not an ArcticBaseError -- out of its own url parsing for a
    malformed url, and that killed the whole command."""
    out = await _collect(_ctx(url=bad))
    assert "arcticbase" in out.lower()
    # the rest of the report must still be there
    assert "agent: pare" in out
