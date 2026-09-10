"""The beat rides the sweep — §8.2's actual requirement, at the wiring level."""
from __future__ import annotations

import pytest

from pare.agent import PareAgent
from pare.config import PAREConfig


class _Manager:
    def __init__(self, raises=False, result=None):
        self.raises = raises
        self.result = result if result is not None else {}
        self.probes = 0

    async def probe_all(self):
        self.probes += 1
        if self.raises:
            raise RuntimeError("the sweep is wedged")
        return self.result


class _Beat:
    def __init__(self):
        self.calls = []
        self.boot_id = "b"
        self.last_error = None
        self.last_beat_at = None

    def beat(self, *, active_slug, workers=None):
        self.calls.append({"active_slug": active_slug, "workers": workers})
        return True


def _agent(url="http://wb.invalid:2929"):
    a = PareAgent()
    cfg = PAREConfig()
    cfg.arcticbase_url = url
    a.config = cfg
    return a


@pytest.mark.asyncio
async def test_a_completed_sweep_produces_a_beat():
    a = _agent()
    a.worker_manager = _Manager(result={"frida": True})
    a._heartbeat = _Beat()
    assert await a._sweep_and_beat() is True
    assert a._heartbeat.calls[0]["workers"] == {"frida": True}


@pytest.mark.asyncio
async def test_a_FAILED_sweep_produces_no_beat_at_all():
    """The whole point. A beat that keeps ticking through a wedged sweep is
    exactly the false signal §8.2 exists to prevent, so the beat is skipped and
    the reader sees it go stale."""
    a = _agent()
    a.worker_manager = _Manager(raises=True)
    a._heartbeat = _Beat()
    assert await a._sweep_and_beat() is False
    assert a._heartbeat.calls == []


@pytest.mark.asyncio
async def test_the_sweep_still_runs_when_there_is_no_heartbeat_configured():
    """Worker liveness must not depend on a workbench host being configured."""
    a = _agent(url="")
    a.worker_manager = _Manager()
    a._heartbeat = None
    assert await a._sweep_and_beat() is True
    assert a.worker_manager.probes == 1


@pytest.mark.asyncio
async def test_a_beat_that_throws_cannot_kill_the_sweep_loop():
    """Heartbeat.beat() swallows its own errors, but the loop must not depend on
    that: a bug there would otherwise stop worker probing entirely."""
    class _Exploding(_Beat):
        def beat(self, **kw):
            raise RuntimeError("boom")

    a = _agent()
    a.worker_manager = _Manager()
    a._heartbeat = _Exploding()
    assert await a._sweep_and_beat() is True     # sweep succeeded; beat did not


@pytest.mark.asyncio
async def test_the_first_beat_does_not_wait_a_whole_interval():
    """A freshly started daemon must not look wedged for a minute.

    The loop slept BEFORE its first iteration, so for up to BEAT_INTERVAL_SECONDS
    after startup the bench screen showed `heartbeat FAIL -- the daemon is
    wedged or stopped` about a daemon that had just started perfectly. The beat
    still rides a completed sweep (§8.2); only the ordering of sleep and sweep
    within the loop changes.
    """
    import asyncio as _asyncio

    order: list[str] = []
    a = _agent()
    a.worker_manager = _Manager()
    a._heartbeat = _Beat()

    real_sweep = a._sweep_and_beat

    async def _record_sweep():
        order.append("swept")
        return await real_sweep()

    async def _record_sleep(_seconds):
        order.append("slept")
        if order.count("slept") >= 2:
            raise _asyncio.CancelledError

    a._sweep_and_beat = _record_sweep
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_asyncio, "sleep", _record_sleep)
        with pytest.raises(_asyncio.CancelledError):
            await a._sweep_loop()

    assert order[0] == "swept", (
        f"the loop slept before its first sweep: {order[:4]}")
    # and it still paces itself afterwards rather than spinning
    assert order[:3] == ["swept", "slept", "swept"], order[:4]
