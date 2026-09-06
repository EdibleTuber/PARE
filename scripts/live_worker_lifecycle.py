"""Drive the real WorkerManager against a real production worker binary.

Not a test — an execution. The suite exercises the lifecycle against a toy stub;
this uses the installed `pare-static-mcp`, the same binary workers.yaml points at,
and asserts on real pids, a real MCP handshake, and real audit rows on disk.

No inference server needed: the worker lifecycle never touches the model, so this
runs anywhere the venv is installed.

    .venv/bin/python scripts/live_worker_lifecycle.py

Exits non-zero if any check fails. Run it after changing anything in the worker
lifecycle — a green pytest run does not prove a real worker still loads.
"""
import asyncio, os, shutil, sys, tempfile

import agent_core.tools.executor as executor_mod
executor_mod.BUILTIN_TOOLS = []          # scratch harness: no agent to satisfy `requires`

from agent_core.tools.executor import ToolExecutor
from agent_core.workers import MCPClientPool, RiskAwareToolPool, WorkerManager
from agent_core.workers.audit import AuditLog
from agent_core.workers.registry import WorkerRegistry
from agent_core.workers.risk import RiskGate
from agent_core.workers.tool_approval import ToolApprovalRegistry

WORKERS_YAML = "/mnt/secondary/projects/PARE/workers.yaml"
# Run-scoped, not a fixed path: AuditLog names its file by calendar date, so a
# fixed directory accumulates rows across same-day reruns and step 10's
# count-based checks (worker_loaded rows == 3) fail spuriously against
# leftover rows from an earlier run rather than anything this run did. A
# fresh tempdir per invocation makes the script idempotent; main() removes it
# on the way out, pass or fail.
AUDIT = tempfile.mkdtemp(prefix="pare-live-lifecycle-audit-")


class _Agent:
    pass


def alive(pid):
    if pid is None:
        return None
    try:
        os.kill(pid, 0); return True
    except OSError:
        return False


def check(label, got, want):
    ok = got == want
    print(f"  [{'OK ' if ok else 'FAIL'}] {label}: {got!r}" + ("" if ok else f"  (expected {want!r})"))
    return ok


async def main():
    reg = WorkerRegistry.load(WORKERS_YAML)
    # `hardware` is declared by plan 2's Task 2, which has not run. Stand in a
    # synthetic declared-but-unbuildable worker so the catalog/failure paths are
    # still exercised against the real manager.
    from agent_core.workers.types import WorkerSpec
    reg.add(WorkerSpec(name="ghost", transport="stdio", risk_default="medium",
                       command="/nonexistent/pare-ghost-mcp", autoload=False,
                       capability_tags=["synthetic"]))
    spec = reg.get("static")
    print(f"target: {spec.command}  floor={spec.risk_default}  autoload={spec.autoload}\n")

    inner = MCPClientPool([])
    pool = RiskAwareToolPool(
        inner=inner, specs={}, risk_gate=RiskGate(overrides=reg.risk_overrides()),
        approval_registry=ToolApprovalRegistry(), audit_log=AuditLog(AUDIT))
    ex = ToolExecutor.build(_Agent(), [])
    mgr = WorkerManager(reg, pool, ex)
    ok = True

    print("1. the constructor's duck-type guard rejects the raw inner pool")
    try:
        WorkerManager(reg, inner, ex)
        ok &= check("raised TypeError", False, True)
    except TypeError as exc:
        ok &= check("raised TypeError", True, True)
        print(f"       -> {str(exc)[:88]}")

    print("\n2. load the real worker")
    res = await mgr.load("static")
    ok &= check("ok", res.ok, True)
    ok &= check("tool_count", res.tool_count, 10)
    pid = inner._owner_pid("static")
    ok &= check("subprocess alive", alive(pid), True)
    print(f"       pid={pid}")
    ok &= check("tools in executor", sum(n.startswith("static_") for n in ex.names()), 10)
    ok &= check("schemas exposed", sum(s["function"]["name"].startswith("static_")
                                       for s in ex.schemas()), 10)
    print(f"       sample: {sorted(n for n in ex.names() if n.startswith('static_'))[:3]}")

    print("\n3. wire tiers were recorded, and resolve through the gate")
    tier = pool.resolve_effective("static", "load_apk")
    ok &= check("static_load_apk effective tier", tier, "low")
    ok &= check("unavailable_reason while loaded", mgr.unavailable_reason("static"), None)

    print("\n4. status() reflects reality")
    st = {s.name: s for s in mgr.status()}
    ok &= check("static loaded", st["static"].loaded, True)
    ok &= check("ghost declared but not loaded", st["ghost"].loaded, False)
    ok &= check("ghost is manual-boot", st["ghost"].autoload, False)

    print("\n5. unload — tools go, process dies")
    res = await mgr.unload("static")
    ok &= check("ok", res.ok, True)
    ok &= check("tools removed", res.tool_count, 10)
    ok &= check("no static_ tools left", [n for n in ex.names() if n.startswith("static_")], [])
    for _ in range(50):
        if not alive(pid):
            break
        await asyncio.sleep(0.1)
    ok &= check("subprocess reaped", alive(pid), False)
    ok &= check("unavailable_reason set", "not loaded" in (mgr.unavailable_reason("static") or ""), True)

    print("\n6. the spec is gone, so a dispatch cannot resurrect the worker")
    ok &= check("spec removed from pool", inner.spec("static"), None)

    print("\n7. reload gives a genuinely fresh process")
    await mgr.load("static")
    pid2 = inner._owner_pid("static")
    ok &= check("new pid differs", pid2 != pid, True)
    print(f"       pid {pid} -> {pid2}")
    r = await mgr.reload("static")
    pid3 = inner._owner_pid("static")
    ok &= check("reload ok", r.ok, True)
    ok &= check("reload respawned", pid3 not in (pid, pid2), True)
    print(f"       pid {pid2} -> {pid3}")

    print("\n8. loading a worker whose binary does not exist fails cleanly")
    res = await mgr.load("ghost")
    ok &= check("ok", res.ok, False)
    ok &= check("error_kind", res.error_kind, "spawn_failed")
    ok &= check("last_error surfaced in status", bool(
        {s.name: s for s in mgr.status()}["ghost"].last_error), True)
    ok &= check("no residue", inner.spec("ghost"), None)

    print("\n9. shutdown closes everything")
    await mgr.close_all()
    ok &= check("static reaped", alive(pid3), False)
    ok &= check("nothing connected", inner.is_connected("static"), False)

    print("\n10. lifecycle rows landed in the audit log")
    import glob, json
    rows = []
    for f in glob.glob(os.path.join(AUDIT, "*.jsonl")):
        rows += [json.loads(l) for l in open(f) if l.strip()]
    outcomes = [r["outcome"] for r in rows]
    ok &= check("worker_loaded rows", outcomes.count("worker_loaded"), 3)
    ok &= check("worker_unloaded rows", outcomes.count("worker_unloaded") >= 2, True)
    swap = [r for r in rows if r["outcome"] == "worker_loaded" and r["args"].get("command_mtime")]
    ok &= check("artifact-swap forensics present", bool(swap), True)
    ok &= check("reload distinguishable from unload+load",
                sorted({r["args"].get("action") for r in rows}), ["load", "reload", "unload"])
    if swap:
        a = swap[0]["args"]
        print(f"       {a.get('command')}  mtime={a.get("command_mtime")}  size={a.get("command_size")}")

    print("\n" + ("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


rc = 1
try:
    rc = asyncio.run(main())
finally:
    shutil.rmtree(AUDIT, ignore_errors=True)
sys.exit(rc)
