"""Black-box tests for scripts/bench_doctor.sh: run the real script with fake
system tools on PATH, so the probes' decisions are exercised without a Pi."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "bench_doctor.sh"


def _shim(bindir: Path, name: str, body: str) -> None:
    p = bindir / name
    p.write_text("#!/usr/bin/env bash\n" + body + "\n")
    p.chmod(0o755)


@pytest.fixture
def fakebin(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _shim(bindir, "tailscale", 'echo "100.1.1.1 peer linux -"')
    _shim(bindir, "curl", 'echo 200')
    return bindir


def _run(fakebin: Path, tmp_path: Path, env_extra: dict | None = None) -> str:
    env = {
        "PATH": f"{fakebin}:/usr/bin:/bin",
        "PARE_ARTIFACT_ROOT": str(tmp_path / "no-drive"),
        "HOME": str(tmp_path),
    }
    env.update(env_extra or {})
    out = subprocess.run(
        ["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=30,
    )
    return out.stdout + out.stderr


def _listening_on(fakebin: Path, port: int) -> None:
    _shim(fakebin, "ss", f'echo "LISTEN 0 2048 100.97.133.126:{port} 0.0.0.0:*"')


def test_port_comes_from_the_installed_unit(fakebin, tmp_path):
    """The deployed worker listens on the port its systemd unit declares
    (AGENT_WORKER_PORT). The doctor must check that port, not a hardcoded
    default -- a hardcoded 9100 reported a healthy worker as down."""
    _shim(fakebin, "systemctl",
          'echo "AGENT_WORKER_TRANSPORT=http AGENT_WORKER_HOST=tailscale0 '
          'AGENT_WORKER_PORT=9555 PYTHONUNBUFFERED=1"')
    _listening_on(fakebin, 9555)
    out = _run(fakebin, tmp_path)
    assert "4. worker on this Pi (port 9555" in out
    assert "something is listening on :9555" in out
    assert "from the pare-hardware-mcp unit" in out


def test_explicit_worker_port_overrides_the_unit(fakebin, tmp_path):
    _shim(fakebin, "systemctl", 'echo "AGENT_WORKER_PORT=9555"')
    _listening_on(fakebin, 9777)
    out = _run(fakebin, tmp_path, {"WORKER_PORT": "9777"})
    assert "something is listening on :9777" in out
    assert "from WORKER_PORT" in out


def test_no_unit_falls_back_to_9102_and_says_it_is_a_guess(fakebin, tmp_path):
    _shim(fakebin, "systemctl", 'exit 1')
    _listening_on(fakebin, 9102)
    out = _run(fakebin, tmp_path)
    assert "something is listening on :9102" in out
    assert "default" in out and "guess" in out


def test_nothing_listening_is_still_a_failure(fakebin, tmp_path):
    _shim(fakebin, "systemctl", 'echo "AGENT_WORKER_PORT=9102"')
    _shim(fakebin, "ss", 'echo "LISTEN 0 128 0.0.0.0:22 0.0.0.0:*"')
    out = _run(fakebin, tmp_path)
    assert "nothing is listening on :9102" in out
    assert "FAIL" in out
