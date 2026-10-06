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


def _run_with_exit(fakebin: Path, tmp_path: Path,
                   env_extra: dict | None = None) -> tuple[int, str]:
    env = {
        "PATH": f"{fakebin}:/usr/bin:/bin",
        "PARE_ARTIFACT_ROOT": str(tmp_path / "no-drive"),
        "HOME": str(tmp_path),
    }
    env.update(env_extra or {})
    out = subprocess.run(
        ["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=30,
    )
    return out.returncode, out.stdout + out.stderr


def _run(fakebin: Path, tmp_path: Path, env_extra: dict | None = None) -> str:
    _rc, text = _run_with_exit(fakebin, tmp_path, env_extra)
    return text


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


# --- orphaned partial writes (open_artifact's {name}+partial, the R11 spelling) ---

# The shared fakebin curl shim answers EVERY curl with "200", which makes the
# heartbeat probe fail ("no heartbeat object") for reasons that have nothing to
# do with the drive. These tests assert exit codes, so they need a drive-rooted
# setup where every OTHER check passes and the code can only be the orphan
# check's. Returns the drive root to plant files in.
def _healthy_drive(fakebin: Path, tmp_path: Path) -> Path:
    _shim(fakebin, "curl",
          'case "$*" in\n'
          '  *api/health*) echo 200 ;;\n'
          '  *) echo \'{"objects": [{"title": "heartbeat", "age_s": 4}]}\' ;;\n'
          'esac')
    _shim(fakebin, "systemctl", 'echo "AGENT_WORKER_PORT=9102"')
    _shim(fakebin, "ss", 'echo "LISTEN 0 2048 0.0.0.0:9102 0.0.0.0:*"')
    _shim(fakebin, "mountpoint", 'exit 0')
    drive = tmp_path / "drive"
    drive.mkdir()
    (drive / ".bench-store-id").write_text("cafe1234\n")
    return drive


def test_a_clean_drive_has_no_orphaned_partials(fakebin, tmp_path):
    drive = _healthy_drive(fakebin, tmp_path)
    (drive / "run-01").mkdir()
    (drive / "run-01" / "foo.bin").write_bytes(b"\x00" * 32)
    rc, out = _run_with_exit(fakebin, tmp_path, {"PARE_ARTIFACT_ROOT": str(drive)})
    assert rc == 0, out
    assert "no abandoned partial writes" in out
    assert "FAIL" not in out
    # the null-glob trap: with nothing to match, the pattern must not be printed
    assert "*+partial" not in out


def test_an_orphaned_partial_is_named_and_the_doctor_fails(fakebin, tmp_path):
    drive = _healthy_drive(fakebin, tmp_path)
    (drive / "run-01").mkdir()
    (drive / "run-01" / "foo.bin+partial").write_bytes(b"\x00" * 64)
    rc, out = _run_with_exit(fakebin, tmp_path, {"PARE_ARTIFACT_ROOT": str(drive)})
    assert rc != 0, out
    assert "foo.bin+partial" in out
    assert "1 abandoned partial write" in out
    # every other check passes, so exactly one FAIL -- the orphan check's
    assert sum(1 for line in out.splitlines() if "FAIL" in line) == 1, out


def test_the_spelling_is_plus_partial_not_partial(fakebin, tmp_path):
    """R11 mutant: a matcher on "partial" instead of "+partial". The decoy ends
    with the literal word partial, so it must NOT be reported."""
    drive = _healthy_drive(fakebin, tmp_path)
    (drive / "run-01").mkdir()
    (drive / "run-01" / "summary_partial").write_text("nothing to do with R11\n")
    (drive / "run-01" / "notes.txt").write_text("clean\n")
    rc, out = _run_with_exit(fakebin, tmp_path, {"PARE_ARTIFACT_ROOT": str(drive)})
    assert rc == 0, out
    assert "summary_partial" not in out
    assert "no abandoned partial writes" in out


def test_only_files_one_level_down_are_orphans(fakebin, tmp_path):
    """The walk is `$ARTIFACT_ROOT/*/` and the match is any FILE -- three rules,
    three mutants this pins:
    - walk-mindepth-widened: a `+partial` file lying at the drive root is out of
      the walk, because a root-level file is not any slug's abandoned write;
    - walk-maxdepth-widened: nothing under a slug dir is anyone else's business,
      so a nested `run-01/sub/foo2.bin+partial` is not reported;
    - match-any-file-or-dir: a DIRECTORY named `run-01/abandoned_run+partial` is
      not an abandoned write -- open_artifact renames a file, never a directory.
    """
    drive = _healthy_drive(fakebin, tmp_path)
    (drive / "run-01").mkdir()
    (drive / "stray+partial").write_bytes(b"\x00" * 16)
    (drive / "run-01" / "sub").mkdir()
    (drive / "run-01" / "sub" / "foo2.bin+partial").write_bytes(b"\x00" * 48)
    (drive / "run-01" / "abandoned_run+partial").mkdir()
    (drive / "run-01" / "note.md").write_text("clean artifact\n")
    rc, out = _run_with_exit(fakebin, tmp_path, {"PARE_ARTIFACT_ROOT": str(drive)})
    assert rc == 0, out
    assert "no abandoned partial writes" in out
    assert "FAIL" not in out
    for planted in ("stray+partial", "foo2.bin+partial", "abandoned_run+partial"):
        assert planted not in out, out
