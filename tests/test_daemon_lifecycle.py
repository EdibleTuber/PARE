"""Unit tests for pare.tui.daemon_lifecycle (spec 2026-09-20 §7.1).

Safety rules these tests follow:
- Nothing spawns the real `pare-daemon`: every call passes `spawn_cmd`
  (a harmless command) and/or `_spawn_hook`.
- Every path that can reach `os.killpg` is driven by a REAL child started
  with `start_new_session=True` (the module's own Popen kwargs). No fake PIDs.
- Every child started is reaped by the `children` fixture's finalizer.
- Socket paths live in a short /tmp dir (AF_UNIX's ~108-byte limit);
  logs and locks live under tmp_path. Nothing touches ~/.local/state/pare.
"""
from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from pare.tui import daemon_lifecycle
from pare.tui.daemon_lifecycle import DaemonSpawnResult, detect_or_spawn, reap

# A stand-in daemon: binds whatever PARE_SOCKET_PATH says (the value the
# module must pass), optionally records its env, then idles.
FAKE_DAEMON = r"""
import os, socket, sys, time
delay = float(sys.argv[1]) if len(sys.argv) > 1 else 0.0
env_out = sys.argv[2] if len(sys.argv) > 2 else ""
if env_out:
    with open(env_out, "w") as f:
        f.write(os.environ.get("PARE_SOCKET_PATH", "<unset>"))
print("fake-daemon starting", flush=True)
time.sleep(delay)
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.bind(os.environ["PARE_SOCKET_PATH"])
s.listen(8)
time.sleep(30)
"""


def fake_daemon_cmd(delay: float = 0.0, env_out: Path | None = None) -> list[str]:
    return [sys.executable, "-c", FAKE_DAEMON, str(delay), str(env_out or "")]


@pytest.fixture
def sock_dir():
    d = tempfile.mkdtemp(prefix="pdl-", dir="/tmp")
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def children():
    """Records every Popen a test starts; kills and reaps leftovers."""
    started: list[subprocess.Popen] = []
    yield started
    for child in started:
        if child.poll() is None:
            try:
                if os.getpgid(child.pid) == child.pid:
                    os.killpg(child.pid, signal.SIGKILL)
                else:
                    child.kill()  # never killpg a group that may be ours
            except ProcessLookupError:
                pass
        child.wait(timeout=5)
        daemon_lifecycle._owned.pop(child.pid, None)


@pytest.fixture
def real_hook(children):
    """A _spawn_hook that does exactly what production does, and records."""
    calls: list[tuple[list[str], dict]] = []

    def hook(cmd, popen_kwargs):
        calls.append((cmd, popen_kwargs))
        child = subprocess.Popen(cmd, **popen_kwargs)
        children.append(child)
        return child

    hook.calls = calls
    hook.children = children
    return hook


def never_called(*a, **kw):
    raise AssertionError("_spawn_hook must not be called here")


def make_stale_socket(path: Path) -> None:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(path))
    s.close()  # file stays, nothing listens -> connect() is refused


# --- detect lane -------------------------------------------------------------


def test_attach_lane_when_socket_is_alive(sock_dir, tmp_path):
    sock_path = sock_dir / "alive.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock_path))
    server.listen(1)
    try:
        result = detect_or_spawn(sock_path, tmp_path / "logs", _spawn_hook=never_called)
    finally:
        server.close()
    assert result == DaemonSpawnResult(mode="attached", pid=None, log_path=None, error=None)


def test_permission_denied_returns_failed_without_spawning(tmp_path, monkeypatch):
    class DeniedSocket:
        def __init__(self, *a, **kw): pass
        def settimeout(self, t): pass
        def connect(self, path): raise PermissionError(13, "Permission denied")
        def close(self): pass

    monkeypatch.setattr(daemon_lifecycle.socket, "socket", DeniedSocket)
    result = detect_or_spawn(tmp_path / "denied.sock", tmp_path / "logs",
                             _spawn_hook=never_called)
    assert result.mode == "failed"
    assert "permission denied" in result.error.lower()


def test_regular_file_at_socket_path_is_not_unlinked(sock_dir, tmp_path):
    # connect() to a regular file raises ConnectionRefusedError, the same as a
    # stale socket. It must be reported, not deleted.
    sock_path = sock_dir / "notasocket"
    sock_path.write_text("precious")
    result = detect_or_spawn(sock_path, tmp_path / "logs", lock_dir=tmp_path,
                             _spawn_hook=never_called)
    assert result.mode == "failed"
    assert "not a socket" in result.error
    assert sock_path.read_text() == "precious"


def test_stale_socket_is_reaped_before_spawn(sock_dir, tmp_path, real_hook):
    sock_path = sock_dir / "stale.sock"
    make_stale_socket(sock_path)
    assert sock_path.exists()
    existed_at_spawn: list[bool] = []

    def hook(cmd, popen_kwargs):
        existed_at_spawn.append(sock_path.exists())
        return real_hook(cmd, popen_kwargs)

    result = detect_or_spawn(sock_path, tmp_path / "logs",
                             spawn_cmd=fake_daemon_cmd(), _spawn_hook=hook)
    try:
        assert result.mode == "spawned", result.error
        assert existed_at_spawn == [False]  # unlinked before Popen ran
        assert "reaped stale socket" in result.log_path.read_text()
    finally:
        if result.pid:
            reap(result.pid, sigterm_timeout=3)


def test_absent_socket_spawn_success_and_child_env(sock_dir, tmp_path, real_hook):
    sock_path = sock_dir / "absent.sock"
    log_dir = tmp_path / "logs"
    env_out = tmp_path / "child-env.txt"
    result = detect_or_spawn(sock_path, log_dir,
                             spawn_cmd=fake_daemon_cmd(0.1, env_out),
                             _spawn_hook=real_hook)
    try:
        assert result.mode == "spawned", result.error
        assert result.error is None
        (_cmd, kwargs), = real_hook.calls
        assert kwargs["start_new_session"] is True
        assert result.pid is not None and os.getpgid(result.pid) == result.pid
        assert result.log_path == log_dir / f"daemon-{result.pid}.log"
        assert result.log_path.is_file()
        assert list(log_dir.glob("daemon-spawning-*")) == []  # temp log renamed
        # C2: the child was told where to bind, and bound exactly there.
        assert env_out.read_text() == str(sock_path)
        assert "fake-daemon starting" in result.log_path.read_text()
    finally:
        if result.pid:
            reap(result.pid, sigterm_timeout=3)


def test_production_spawn_path_passes_socket_path_to_child(sock_dir, tmp_path):
    # No hook: exercises the module's own Popen call. The fake daemon binds
    # only $PARE_SOCKET_PATH, so "spawned" is itself proof the env was set.
    sock_path = sock_dir / "prod.sock"
    env_out = tmp_path / "child-env.txt"
    result = detect_or_spawn(sock_path, tmp_path / "logs",
                             spawn_cmd=fake_daemon_cmd(0.0, env_out))
    try:
        assert result.mode == "spawned", result.error
        assert env_out.read_text() == str(sock_path)
    finally:
        if result.pid:
            assert reap(result.pid, sigterm_timeout=3) == "exited_clean"


def test_spawn_exits_during_startup(sock_dir, tmp_path, real_hook):
    result = detect_or_spawn(sock_dir / "x.sock", tmp_path / "logs",
                             spawn_cmd=["sh", "-c", "echo boom; exit 3"],
                             _spawn_hook=real_hook)
    assert result.mode == "failed"
    assert "exited during startup" in result.error
    assert "code 3" in result.error
    assert result.log_path is not None and result.log_path.is_file()
    assert str(result.log_path) in result.error
    assert "boom" in result.log_path.read_text()


def test_spawn_timeout_kills_the_child(sock_dir, tmp_path, real_hook):
    start = time.monotonic()
    result = detect_or_spawn(sock_dir / "never.sock", tmp_path / "logs",
                             startup_timeout=0.5,
                             spawn_cmd=["sleep", "30"],
                             _spawn_hook=real_hook)
    elapsed = time.monotonic() - start
    assert result.mode == "failed"
    assert "did not accept a connection within 0.5s" in result.error
    assert result.log_path is not None and str(result.log_path) in result.error
    assert elapsed < 0.5 + 2.0
    (child,) = real_hook.children
    assert child.returncode == -signal.SIGTERM  # SIGTERM'd and reaped


def test_log_open_failure_falls_back_to_pipe(sock_dir, tmp_path, real_hook):
    if os.geteuid() == 0:
        pytest.skip("root ignores directory permissions")
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o000)
    try:
        result = detect_or_spawn(
            sock_dir / "x.sock", locked / "logs", lock_dir=tmp_path / "lock",
            spawn_cmd=["sh", "-c", "echo oops >&2; exit 2"],
            _spawn_hook=real_hook,
        )
    finally:
        locked.chmod(0o700)
    assert result.mode == "failed"
    assert result.log_path is None
    assert "code 2" in result.error
    assert "no log" in result.error
    assert "oops" in result.error


def test_unopenable_lock_fails_without_touching_stale_socket(sock_dir, tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root ignores directory permissions")
    sock_path = sock_dir / "stale.sock"
    make_stale_socket(sock_path)
    lock_dir = tmp_path / "lock"
    lock_dir.mkdir()
    lock_dir.chmod(0o500)
    try:
        result = detect_or_spawn(sock_path, tmp_path / "logs", lock_dir=lock_dir,
                                 _spawn_hook=never_called)
    finally:
        lock_dir.chmod(0o700)
    assert result.mode == "failed"
    assert "spawn lock" in result.error
    assert sock_path.exists()  # no unlink without the lock


# --- flock contention --------------------------------------------------------


def _hold_lock(lock_dir: Path) -> int:
    lock_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_dir / "spawn.lock", os.O_CREAT | os.O_WRONLY, 0o600)
    import fcntl
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


def test_contended_lock_waits_and_attaches(sock_dir, tmp_path):
    sock_path = sock_dir / "race.sock"
    make_stale_socket(sock_path)  # the waiter must NOT reap this
    fd = _hold_lock(tmp_path)
    try:
        server_holder: list[socket.socket] = []

        def other_spawner_finishes():
            time.sleep(0.3)
            # Stand-in for the lock holder: it reaps and binds.
            os.unlink(sock_path)
            srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            srv.bind(str(sock_path))
            srv.listen(1)
            server_holder.append(srv)

        t = threading.Thread(target=other_spawner_finishes)
        t.start()
        result = detect_or_spawn(sock_path, tmp_path / "logs", lock_dir=tmp_path,
                                 startup_timeout=3.0, _spawn_hook=never_called)
        t.join()
        for s in server_holder:
            s.close()
    finally:
        os.close(fd)
    assert result.mode == "attached"


def test_contended_lock_times_out_without_reaping(sock_dir, tmp_path):
    sock_path = sock_dir / "race.sock"
    make_stale_socket(sock_path)
    fd = _hold_lock(tmp_path)
    try:
        result = detect_or_spawn(sock_path, tmp_path / "logs", lock_dir=tmp_path,
                                 startup_timeout=0.4, _spawn_hook=never_called)
    finally:
        os.close(fd)
    assert result.mode == "failed"
    assert result.error == "concurrent spawn timed out"
    assert sock_path.exists()  # only the lock holder may unlink


def test_daemon_that_came_up_before_we_locked_is_attached_not_respawned(
        sock_dir, tmp_path, monkeypatch):
    # Our probe fails; a concurrent spawner then finishes and releases the lock
    # before we take it. Under the lock the re-probe must see the live socket
    # and attach -- not spawn a second daemon.
    import fcntl
    sock_path = sock_dir / "late.sock"
    servers: list[socket.socket] = []
    real_flock = fcntl.flock

    def flock_after_other_spawner(fd, op):
        if op & fcntl.LOCK_EX and not servers:
            srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            srv.bind(str(sock_path))
            srv.listen(1)
            servers.append(srv)
        return real_flock(fd, op)

    monkeypatch.setattr(daemon_lifecycle.fcntl, "flock", flock_after_other_spawner)
    try:
        result = detect_or_spawn(sock_path, tmp_path / "logs",
                                 _spawn_hook=never_called)
    finally:
        for s in servers:
            s.close()
    assert servers, "flock wrapper never ran"
    assert result.mode == "attached"


def test_lock_is_released_after_every_outcome(sock_dir, tmp_path, real_hook):
    import fcntl
    detect_or_spawn(sock_dir / "x.sock", tmp_path / "logs", lock_dir=tmp_path,
                    spawn_cmd=["sh", "-c", "exit 1"], _spawn_hook=real_hook)
    detect_or_spawn(sock_dir / "y.sock", tmp_path / "logs", lock_dir=tmp_path,
                    startup_timeout=0.2, spawn_cmd=["sleep", "30"],
                    _spawn_hook=real_hook)
    fd = os.open(tmp_path / "spawn.lock", os.O_WRONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # raises if still held
    finally:
        os.close(fd)


def test_concurrent_spawn_only_one_spawns(sock_dir, tmp_path, real_hook):
    sock_path = sock_dir / "race.sock"
    results: list[DaemonSpawnResult] = []
    barrier = threading.Barrier(2)

    def run():
        barrier.wait()
        results.append(detect_or_spawn(
            sock_path, tmp_path / "logs",
            spawn_cmd=fake_daemon_cmd(0.2), _spawn_hook=real_hook,
        ))

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    try:
        assert sorted(r.mode for r in results) == ["attached", "spawned"], results
        assert len(real_hook.calls) == 1
    finally:
        for r in results:
            if r.pid:
                reap(r.pid, sigterm_timeout=3)


# --- reap --------------------------------------------------------------------


def test_reap_sigterm_terminates_a_normal_child(children):
    child = subprocess.Popen(["sleep", "30"], start_new_session=True)
    children.append(child)
    assert reap(child.pid, sigterm_timeout=2.0) == "exited_clean"
    assert child.poll() is not None


def test_reap_escalates_to_sigkill(children):
    child = subprocess.Popen(
        [sys.executable, "-c",
         "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
         "print('ready', flush=True); time.sleep(30)"],
        stdout=subprocess.PIPE, start_new_session=True,
    )
    children.append(child)
    assert child.stdout.readline() == b"ready\n"  # handler installed
    child.stdout.close()
    assert reap(child.pid, sigterm_timeout=0.3, sigkill_timeout=2.0) == "killed"
    assert child.poll() is not None


def test_reap_of_already_exited_child_is_clean(children):
    child = subprocess.Popen(["true"], start_new_session=True)
    children.append(child)
    # Wait for exit WITHOUT reaping: the zombie keeps the PID from being reused.
    os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOWAIT)
    assert reap(child.pid, sigterm_timeout=1.0) == "exited_clean"


def test_reap_never_killpgs_a_child_outside_its_own_group(children, monkeypatch):
    # A child left in OUR process group must be signalled alone. killpg is
    # patched to record-and-refuse, so a broken guard can't hit pytest's group.
    killpg_calls: list = []

    def fake_killpg(pgid, sig):
        killpg_calls.append((pgid, sig))
        raise AssertionError("killpg on a non-leader")

    monkeypatch.setattr(daemon_lifecycle.os, "killpg", fake_killpg)
    child = subprocess.Popen(["sleep", "30"])  # no new session: shares our pgid
    children.append(child)
    assert reap(child.pid, sigterm_timeout=2.0) == "exited_clean"
    assert killpg_calls == []
