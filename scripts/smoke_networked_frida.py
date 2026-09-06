"""Smoke-test a frida worker running on another machine.

    python scripts/smoke_networked_frida.py http://100.x.y.z:9101/mcp

Drives agent_core directly -- no inference server, no PARE daemon -- so a
failure here is the worker or the link, never the model. Every check reports
what it actually observed; nothing is inferred from a check passing.

Read docs/superpowers/2026-09-06-networked-frida-smoke-test.md first: the
laptop side has to be set up before any of this means anything.
"""
from __future__ import annotations

import asyncio
import re
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent_core.tools.builtin as _builtin
import agent_core.tools.executor as _executor
_builtin.BUILTIN_TOOLS = []          # this harness has no agent to satisfy
_executor.BUILTIN_TOOLS = []

from agent_core.tools.executor import ToolExecutor          # noqa: E402
from agent_core.workers.audit import AuditLog               # noqa: E402
from agent_core.workers.client_pool import MCPClientPool     # noqa: E402
from agent_core.workers.manager import WorkerManager         # noqa: E402
from agent_core.workers.registry import WorkerRegistry       # noqa: E402
from agent_core.workers.risk import RiskGate                 # noqa: E402
from agent_core.workers.risk_pool import RiskAwareToolPool   # noqa: E402
from agent_core.workers.tool_approval import ToolApprovalRegistry  # noqa: E402
from agent_core.workers.types import WorkerSpec              # noqa: E402

POLL_SAMPLES = 20


class _Agent:
    pass


def ok(label, passed, detail=""):
    print(f"  [{'PASS' if passed else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    return bool(passed)


async def main(endpoint: str) -> int:
    spec = WorkerSpec(name="frida", transport="streamable_http", endpoint=endpoint,
                      risk_default="low", connect_timeout=20, read_timeout=60,
                      autoload=False,
                      capability_tags=["mobile", "dynamic", "android", "frida"])
    registry = WorkerRegistry()
    registry.add(spec)
    tmp = Path(tempfile.mkdtemp(prefix="frida-smoke-"))
    pool = RiskAwareToolPool(
        inner=MCPClientPool([]), specs={}, risk_gate=RiskGate(overrides=[]),
        approval_registry=ToolApprovalRegistry(), audit_log=AuditLog(tmp))
    mgr = WorkerManager(registry, pool, ToolExecutor.build(_Agent(), []),
                        liveness_interval=5.0, probe_timeout=5.0)
    failures = 0

    print(f"\nendpoint: {endpoint}\naudit:    {tmp}\n")

    # 1 --------------------------------------------------------------------
    print("1. connect and register")
    t0 = time.monotonic()
    res = await mgr.load("frida")
    dial = time.monotonic() - t0
    failures += not ok("load", res.ok, res.error or f"{res.tool_count} tools in {dial:.2f}s")
    if not res.ok:
        print("\n   Nothing below can run. Check, in this order: the unit is up "
              "(systemctl status), the port is bound to the TAILNET address and not "
              "loopback (ss -tlnp), and the tailnet reaches it (tailscale ping).")
        return 1

    # 2 --------------------------------------------------------------------
    print("\n2. identity — the only provenance a remote worker has")
    info = pool.server_info("frida") or {}
    version = info.get("server_version")
    failures += not ok("worker reports a version", bool(version), str(info))
    failures += not ok(
        "version is the WORKER's, not the mcp SDK's", version not in (None, "1.29.1"),
        "install pare-worker-kit>=0.1.1 on the laptop" if version == "1.29.1" else str(version))

    # 3 --------------------------------------------------------------------
    print("\n3. risk tiers survive the wire")
    listing = await pool.list_tools("frida")
    tools = list(getattr(listing, "tools", []) or [])
    tiered = [t for t in tools
              if (getattr(t, "meta", None) or {}).get("agent_core/risk_tier")]
    failures += not ok("every tool carries a tier", len(tiered) == len(tools) and tools,
                       f"{len(tiered)}/{len(tools)}")
    pinned = {t.name for t in tools} & {"execute_script", "write_memory"}
    print(f"       tools needing an operator pin present here: {sorted(pinned) or 'none'}")

    # 4 --------------------------------------------------------------------
    print("\n4. the device, seen from a machine that cannot see it")
    devices = await pool.call_tool("frida", "list_devices", {})
    text = str(getattr(devices, "content", devices))
    # Keyed on device TYPE, not on a blocklist of names. Measured with nothing
    # attached, frida still reports three pseudo-devices -- local (type local),
    # socket and barebone (both type remote) -- so an "is it not one of these
    # names" test passes vacuously the moment frida adds a fourth. It already
    # did: an earlier draft of this check counted `barebone` as a real device
    # and went green on a machine with no phone anywhere near it.
    entries = re.findall(r'\{"id":\s*"([^"]+)",\s*"name":\s*"[^"]*",\s*"type":\s*"([^"]+)"\}',
                         text)
    ids = sorted(i for i, _ in entries)
    failures += not ok("list_devices answered", bool(ids), f"ids: {ids or 'none'}")
    attached = [i for i, kind in entries
                if kind == "usb" or i.startswith("emulator-")]
    failures += not ok(
        "an actual Android device is attached TO THE LAPTOP", bool(attached),
        f"attached: {attached}" if attached else
        f"only frida's pseudo-devices ({ids}) — start the emulator and "
        f"frida-server on the laptop, then check `adb devices` THERE")

    # 5 --------------------------------------------------------------------
    print(f"\n5. poll latency over the hop ({POLL_SAMPLES} samples)")
    print("   THE number that decides whether this design is usable.")
    print("   Loopback baseline measured on the inference server: ~5 ms median.")
    print("   Compare against that, not against zero.")
    samples = []
    errors = 0
    for _ in range(POLL_SAMPLES):
        t0 = time.monotonic()
        try:
            await pool.call_tool("frida", "list_sessions", {})
            samples.append((time.monotonic() - t0) * 1000)
        except Exception:
            errors += 1
    if samples:
        s = sorted(samples)
        p95 = s[int(len(s) * 0.95) - 1]
        median = statistics.median(s)
        print(f"       median {median:6.1f} ms | p95 {p95:6.1f} ms | "
              f"min {s[0]:.1f} | max {s[-1]:.1f} | errors {errors}")
        failures += not ok("polling is responsive", median < 250,
                           f"median {median:.0f} ms")
        # Reported separately from the median on purpose: a p95 many times the
        # median is an unstable link that looks fine on average, and it is the
        # shape that makes a co-pilot loop feel broken without ever failing.
        failures += not ok("latency is STABLE, not just fast on average",
                           p95 < max(250.0, median * 4),
                           f"p95 {p95:.0f} ms vs median {median:.0f} ms")
    else:
        failures += not ok("polling works at all", False, f"{errors} errors")

    # 6 --------------------------------------------------------------------
    print("\n6. liveness while healthy")
    failures += not ok("probe succeeds", await mgr.probe("frida") is True)
    st = {s.name: s for s in mgr.status()}["frida"]
    failures += not ok("status names the endpoint", st.endpoint == endpoint, str(st.endpoint))
    failures += not ok("reachable is observed, not assumed", st.reachable is True)

    # 7 --------------------------------------------------------------------
    print("\n7. now break the link")
    print("   Stop the worker (systemctl stop pare-frida-mcp) or turn off the")
    print("   laptop's wifi. They are DIFFERENT events -- stopping closes the")
    print("   socket, wifi-off drops the SYN, and only the second resembles a")
    print("   sleeping laptop. Then press Enter.")
    try:
        await asyncio.get_running_loop().run_in_executor(None, input, "   > ")
    except (EOFError, KeyboardInterrupt):
        print("\n   skipped (no tty) — run interactively to check the failure paths")
        await mgr.stop_liveness()
        return 1 if failures else 0

    gen = pool.generation("frida")
    pool.record_session_approval("frida", "frida_execute_script", gen)
    held = pool.is_session_approved("frida", "frida_execute_script")
    print(f"   holding a scope:session approval first: {held}")

    reachable = await mgr.probe("frida")
    failures += not ok("probe now reports unreachable", reachable is False)
    st = {s.name: s for s in mgr.status()}["frida"]
    failures += not ok("last_error names the machine", endpoint.split("//")[-1].split("/")[0]
                       in (st.last_error or ""), st.last_error or "(none)")
    failures += not ok("the session approval was evicted",
                       not pool.is_session_approved("frida", "frida_execute_script"),
                       "a link drop may mean a restarted process")

    print("\n8. unload — what does it claim was destroyed?")
    res = await mgr.unload("frida")
    print(f"   {res.op} ok={res.ok} kind={res.error_kind}")
    print("   Now check in PARE: /worker unload frida must say the remote process")
    print("   KEEPS RUNNING and its attachments survive. They do.")

    await mgr.stop_liveness()
    print(f"\n{'ALL CHECKS PASSED' if not failures else f'{failures} CHECK(S) FAILED'}")
    print(f"audit rows: {tmp}")
    return 1 if failures else 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(asyncio.run(main(sys.argv[1])))
