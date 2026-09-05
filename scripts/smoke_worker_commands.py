"""Smoke test the operator surface: /worker and /health on a real daemon.

Not a test — an execution. `/worker` and `/health` are commands: they flow
through handle_command, never handle_chat, so they never touch inference.
This script starts the real `pare-daemon` (which autoloads the real
frida/static/mitm workers over real stdio MCP handshakes, same as
scripts/live_worker_lifecycle.py does at a lower level) and drives the real
wire protocol against it with agent_core's own DaemonConnection helper — no
inference server running, none needed.

It proves the parts of the Task 8 brief's manual CLI smoke test that don't
require reading a model's reply: worker discovery, /health agreement, unload
(with its destruction warning), a fast-path command surviving a sibling's
unload, reload, and the hardware spawn_failed path. The remaining piece of
that smoke test — asking the model something that needs an unloaded worker
and watching it hand back cleanly — needs an inference server and a human
reading the transcript, and is deliberately not attempted here.

    .venv/bin/python scripts/smoke_worker_commands.py

Exits non-zero if any check fails. The daemon subprocess is always terminated
before exit, including on failure, so a failed run does not leave a process
behind.
"""
from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import tempfile
import time

from agent_core.client import DaemonConnection
from agent_core.workers.registry import WorkerRegistry
from pare.config import load_config

SOCKET_WAIT_TIMEOUT = 15.0

# Phrases _frida.call() and WorkerManager.unavailable_reason() actually use on
# a failed dispatch. A /devices check that doesn't scan for these can't tell
# "frida still works" from "frida silently became unavailable" -- see section
# 5 below.
_FRIDA_FAILURE_PHRASES = ("not loaded", "call failed", "invalid json")


def check(label: str, got, want=None, *, predicate=None) -> bool:
    if predicate is not None:
        ok = predicate(got)
    else:
        ok = got == want
    shown = got if not isinstance(got, str) or len(got) < 200 else got[:200] + "…"
    print(f"  [{'OK ' if ok else 'FAIL'}] {label}: {shown!r}"
          + ("" if ok or predicate is not None else f"  (expected {want!r})"))
    return ok


async def wait_for_socket(path, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            # Give the server a beat past the file's creation (serve() unlinks
            # then creates it, but start_unix_server needs to actually be
            # listening before a connect attempt is guaranteed to succeed).
            for _ in range(20):
                try:
                    conn = DaemonConnection(path)
                    await conn.connect()
                    await conn.close()
                    return
                except (ConnectionRefusedError, FileNotFoundError):
                    await asyncio.sleep(0.1)
            return
        await asyncio.sleep(0.1)
    raise TimeoutError(f"daemon socket {path} did not appear within {timeout}s")


async def run_checks(cfg) -> bool:
    ok = True
    conn = DaemonConnection(cfg.socket_path)
    await conn.connect()
    try:
        print("1. /worker list shows frida/static/mitm loaded, hardware unloaded")
        r = await conn.command("worker", "list")
        print(r.text)
        lines = {ln.split()[0]: ln for ln in r.text.splitlines()
                 if ln.split() and ln.split()[0] in
                 ("frida", "static", "mitm", "hardware")}
        for name in ("frida", "static", "mitm"):
            row = lines.get(name, "")
            ok &= check(f"{name} loaded", "loaded" in row, predicate=lambda v: v)
            ok &= check(f"{name} tool count nonzero", row.split()[2] if len(row.split()) > 2 else "",
                        predicate=lambda v: v.isdigit() and int(v) > 0)
        ok &= check("hardware unloaded", "unloaded" in lines.get("hardware", ""),
                    predicate=lambda v: v)

        print("\n2. /health reports the same workers")
        r = await conn.command("health", "")
        print(r.text)
        health_line = next((ln for ln in r.text.splitlines() if ln.startswith("workers:")), "")
        for name in ("frida", "static", "mitm"):
            ok &= check(f"health mentions {name} loaded", f"{name}(" in health_line,
                        predicate=lambda v: v)
        ok &= check("health mentions hardware unloaded", "hardware" in health_line,
                    predicate=lambda v: v)

        print("\n3. /worker unload static succeeds with the destruction warning")
        r = await conn.command("worker", "unload static")
        print(r.text)
        ok &= check("mentions tools removed", "tools removed" in r.text,
                    predicate=lambda v: v)
        ok &= check("carries the destruction warning",
                    "attach" in r.text.lower() and "gone" in r.text.lower(),
                    predicate=lambda v: v)

        print("\n4. /worker list now shows static unloaded")
        r = await conn.command("worker", "list")
        print(r.text)
        static_row = next((ln for ln in r.text.splitlines() if ln.split()[:1] == ["static"]), "")
        ok &= check("static unloaded", "unloaded" in static_row, predicate=lambda v: v)

        print("\n5. /devices (frida fast-path) still works — unloading static "
              "did not disturb frida")
        r = await conn.command("devices", "")
        print(r.text)
        lines = r.text.splitlines()
        header = lines[0].split() if lines else []
        # Positive: the response must actually have the shape render_table
        # produces for /devices — a header naming its columns, plus at least
        # one data row (header + separator + >=1 row == 3 lines).
        ok &= check("devices header has id/name/type columns",
                    {"id", "name", "type"} <= set(header), predicate=lambda v: v)
        ok &= check("devices has at least one data row", len(lines) >= 3,
                    predicate=lambda v: v)
        # Negative: none of _frida.call()'s / unavailable_reason()'s actual
        # failure phrasings appear. "error" alone doesn't discriminate --
        # every one of those failure strings passes a bare "not empty, no
        # 'error' substring" check, which is exactly how this section's
        # original assertions missed a silently-unavailable frida (see the
        # discrimination proof below and the fix-round report).
        lower = r.text.lower()
        ok &= check("devices carries no known failure phrase",
                    not any(p in lower for p in _FRIDA_FAILURE_PHRASES),
                    predicate=lambda v: v)

        print("\n6. /worker load static restores it with the same tool count")
        r = await conn.command("worker", "load static")
        print(r.text)
        ok &= check("load reports 10 tools", "10 tools" in r.text,
                    predicate=lambda v: v)
        r = await conn.command("worker", "list")
        static_row = next((ln for ln in r.text.splitlines() if ln.split()[:1] == ["static"]), "")
        print(static_row)
        ok &= check("static loaded again with 10 tools",
                    "loaded" in static_row and "10" in static_row.split(),
                    predicate=lambda v: v)

        print("\n7. /worker load hardware fails with spawn_failed, visible in /worker list")
        r = await conn.command("worker", "load hardware")
        print(r.text)
        ok &= check("reports spawn_failed", "spawn_failed" in r.text,
                    predicate=lambda v: v)
        r = await conn.command("worker", "list")
        print(r.text)
        hw_row = next((ln for ln in r.text.splitlines() if ln.split()[:1] == ["hardware"]), "")
        ok &= check("hardware still unloaded", "unloaded" in hw_row, predicate=lambda v: v)
        # The table cell alone clips a realistic spawn_failed message down to
        # the error's class (see worker.py's _render_list comment) -- the
        # Task 4 footer below the table is the only place the actual binary
        # path an operator needs to fix surfaces, so assert on that, not on
        # the row's raw width.
        hw_spec = WorkerRegistry.load(cfg.workers_yaml_path).get("hardware")
        footer_line = next((ln for ln in r.text.splitlines()
                             if ln.startswith("hardware:")), "")
        ok &= check("hardware footer names the missing binary",
                    hw_spec.command in footer_line, predicate=lambda v: v)
    finally:
        await conn.close()
    return ok


def main() -> int:
    cfg = load_config()
    env = dict(os.environ)
    log_path = os.path.join(tempfile.gettempdir(), f"pare-daemon-smoke-{os.getpid()}.log")
    logf = open(log_path, "w")
    proc = subprocess.Popen(
        [os.path.join(os.path.dirname(sys.executable), "pare-daemon")],
        stdout=logf, stderr=subprocess.STDOUT, env=env,
    )
    print(f"started pare-daemon pid={proc.pid}, waiting for {cfg.socket_path} ...")
    print(f"daemon output logged to {log_path}")
    ok = False
    try:
        asyncio.run(wait_for_socket(cfg.socket_path, SOCKET_WAIT_TIMEOUT))
        print("daemon socket is up\n")
        ok = asyncio.run(run_checks(cfg))
    except Exception as exc:
        print(f"\nEXCEPTION: {exc!r}")
        ok = False
    finally:
        print("\nterminating daemon...")
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        logf.close()
        with open(log_path) as f:
            out = f.read()
        if out:
            print("--- daemon output ---")
            print(out)
        try:
            os.remove(log_path)
        except OSError:
            pass

    print("\n" + ("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
