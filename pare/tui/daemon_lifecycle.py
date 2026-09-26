"""Detect a running `pare-daemon`, or spawn one; reap the one we spawned.

Spec: docs/superpowers/specs/2026-09-20-daemon-auto-spawn-design.md (§2-§5).

Pure Python, stdlib only -- no Textual, no asyncio. Both public functions
block; the TUI awaits them on a worker thread.

`detect_or_spawn(socket_path, log_dir)`:

1. Probe `socket_path` with `connect()`. Success -> `attached`.
   `FileNotFoundError` / `ConnectionRefusedError` -> absent or stale, go on.
   `PermissionError` or anything else -> `failed`, never spawn.
2. Take a non-blocking `flock` on `<lock_dir>/spawn.lock`. If another process
   (or thread) holds it, do not reap or spawn: poll the socket up to
   `startup_timeout` and return `attached`, or `failed` ("concurrent spawn
   timed out").
3. Under the lock: probe again. A connect that now succeeds means a
   concurrent spawner finished between our probe and our lock -> `attached`.
   `ConnectionRefusedError` -> the file is a dead daemon's leftover; unlink it.
   The unlink happens ONLY here: lock held AND a fresh connect just refused.
4. Open a temp log, Popen the daemon in its own session
   (`start_new_session=True`) with `PARE_SOCKET_PATH` set to `socket_path`,
   rename the log to `daemon-<pid>.log`. If the log can't be opened, the
   child's output goes to a PIPE and the first 4KB is returned in `error`
   on failure.
5. Poll for readiness: connect succeeds -> `spawned`; child exits -> `failed`
   ("exited during startup, code N"); timeout -> SIGTERM/SIGKILL the child's
   process group, `failed` ("did not accept a connection within Xs").

The flock is released and its fd closed on every return path.

`reap(pid)`: SIGTERM the process group, escalate to SIGKILL after
`sigterm_timeout`, give up after `sigkill_timeout`. An already-gone PID is
`exited_clean`.

Signalling safety: every `killpg` in this module first checks that the target
is the leader of its own process group (`getpgid(pid) == pid`) and that the
group is not our own. A child that was not started with
`start_new_session=True` gets a plain `kill(pid)` instead, so a mistake can
never signal the TUI's own process group.
"""
from __future__ import annotations

import fcntl
import os
import signal
import socket
import stat
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

__all__ = ["DaemonSpawnResult", "detect_or_spawn", "reap"]

_POLL_INTERVAL = 0.1
_REAP_POLL_INTERVAL = 0.05
_TIMEOUT_TERM_WAIT = 2.0
_TIMEOUT_KILL_WAIT = 2.0
_PIPE_TAIL_BYTES = 4096

# Popen objects for children we spawned and have not reaped yet. Holding the
# reference stops `subprocess`'s own `_cleanup()` from reaping the child
# behind our back (it does that for dropped, still-running Popen objects),
# which would free the PID for reuse before `reap()` signals it.
_owned: dict[int, subprocess.Popen] = {}
_owned_lock = threading.Lock()

SpawnHook = Callable[[list[str], dict[str, Any]], subprocess.Popen]
"""`_spawn_hook(cmd, popen_kwargs) -> Popen`. The default is
`subprocess.Popen(cmd, **popen_kwargs)`; `popen_kwargs` carries stdin,
stdout, stderr, start_new_session and env exactly as production uses them."""


@dataclass(frozen=True)
class DaemonSpawnResult:
    mode: Literal["attached", "spawned", "failed"]
    pid: int | None
    log_path: Path | None
    error: str | None  # None when mode != "failed"


def _attached() -> DaemonSpawnResult:
    return DaemonSpawnResult(mode="attached", pid=None, log_path=None, error=None)


def _failed(error: str, log_path: Path | None = None) -> DaemonSpawnResult:
    return DaemonSpawnResult(mode="failed", pid=None, log_path=log_path, error=error)


def _probe(socket_path: Path, timeout: float) -> None:
    """connect() once; return on success, raise the connect error otherwise."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        sock.connect(str(socket_path))
    finally:
        sock.close()


def _not_a_socket(socket_path: Path) -> bool:
    """True if something other than a socket sits at socket_path. connect()
    to a regular file or a directory raises ConnectionRefusedError, exactly
    like a stale socket, so the refusal alone must never license an unlink."""
    try:
        return not stat.S_ISSOCK(os.lstat(socket_path).st_mode)
    except FileNotFoundError:
        return False


def _probe_ok(socket_path: Path, timeout: float) -> bool:
    try:
        _probe(socket_path, timeout)
    except OSError:
        return False
    return True


def _default_spawn(cmd: list[str], popen_kwargs: dict[str, Any]) -> subprocess.Popen:
    return subprocess.Popen(cmd, **popen_kwargs)


def _signal_group(pid: int, sig: int) -> bool:
    """Signal pid's process group if pid leads its own group (and it isn't
    ours); otherwise signal pid alone. Return False if the process is gone."""
    try:
        pgid = os.getpgid(pid)
        if pgid == pid and pgid != os.getpgrp():
            os.killpg(pgid, sig)
        else:
            os.kill(pid, sig)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def _read_pipe_head(pipe) -> str:
    """Read up to _PIPE_TAIL_BYTES already written to `pipe` without blocking
    (a grandchild holding the write end open must not hang us)."""
    if pipe is None:
        return ""
    fd = pipe.fileno()
    os.set_blocking(fd, False)
    chunks: list[bytes] = []
    remaining = _PIPE_TAIL_BYTES
    try:
        while remaining > 0:
            try:
                data = os.read(fd, remaining)
            except BlockingIOError:
                break
            if not data:
                break
            chunks.append(data)
            remaining -= len(data)
    finally:
        pipe.close()
    return b"".join(chunks).decode("utf-8", errors="replace")


def _drain_forever(pipe) -> None:
    """Pipe-fallback success path: keep the daemon's stdout pipe from filling
    (a full pipe would block the daemon's writes)."""
    def _run() -> None:
        try:
            while pipe.read(65536):
                pass
        except (OSError, ValueError):
            pass
        finally:
            try:
                pipe.close()
            except OSError:
                pass

    threading.Thread(target=_run, name="pare-daemon-drain", daemon=True).start()


def _describe_log(log_path: Path | None, tail: str | None) -> str:
    if log_path is not None:
        return f"see {log_path}"
    if tail:
        return f"no log; captured tail below\n{tail}"
    return "no log; no output captured"


def detect_or_spawn(
    socket_path: Path,
    log_dir: Path,
    *,
    connect_timeout: float = 0.25,
    startup_timeout: float = 5.0,
    lock_dir: Path | None = None,
    spawn_cmd: list[str] | None = None,
    _spawn_hook: SpawnHook | None = None,
) -> DaemonSpawnResult:
    """Blocking. Callers await it on a thread.

    `spawn_cmd` defaults to `["pare-daemon"]`. The lock lives at
    `(lock_dir or log_dir) / "spawn.lock"`.
    """
    socket_path = Path(socket_path)
    log_dir = Path(log_dir)
    cmd = list(spawn_cmd) if spawn_cmd is not None else ["pare-daemon"]
    spawn = _spawn_hook or _default_spawn

    # 1-2. Probe and categorize.
    try:
        _probe(socket_path, connect_timeout)
        return _attached()
    except FileNotFoundError:
        pass
    except ConnectionRefusedError:
        if _not_a_socket(socket_path):
            return _failed(f"socket_path is not a socket: {socket_path}")
    except PermissionError:
        return _failed(f"permission denied: {socket_path}")
    except OSError as exc:
        return _failed(f"{type(exc).__name__}: {exc}")

    # 3. Ensure log_dir. Failure here just means the log open below fails
    # and we take the PIPE fallback.
    try:
        log_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError:
        pass

    # 4. Lock. Without a lock we must not unlink anything, so an unopenable
    # lock file is a failure, not a reason to spawn unserialized.
    lock_path = (Path(lock_dir) if lock_dir is not None else log_dir) / "spawn.lock"
    if lock_dir is not None:
        try:
            Path(lock_dir).mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError:
            pass
    try:
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_WRONLY, 0o600)
    except OSError as exc:
        return _failed(f"cannot open spawn lock {lock_path}: {type(exc).__name__}: {exc}")

    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return _wait_for_concurrent_spawn(
                socket_path, connect_timeout, startup_timeout,
            )
        except OSError as exc:
            return _failed(f"cannot lock {lock_path}: {type(exc).__name__}: {exc}")
        try:
            return _spawn_locked(
                socket_path, log_dir, cmd, spawn,
                connect_timeout=connect_timeout,
                startup_timeout=startup_timeout,
            )
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
    finally:
        os.close(lock_fd)


def _wait_for_concurrent_spawn(
    socket_path: Path, connect_timeout: float, startup_timeout: float,
) -> DaemonSpawnResult:
    """Someone else holds the lock: never reap, never spawn; just wait."""
    deadline = time.monotonic() + startup_timeout
    while True:
        if _probe_ok(socket_path, connect_timeout):
            return _attached()
        if time.monotonic() >= deadline:
            return _failed("concurrent spawn timed out")
        time.sleep(_POLL_INTERVAL)


def _spawn_locked(
    socket_path: Path,
    log_dir: Path,
    cmd: list[str],
    spawn: SpawnHook,
    *,
    connect_timeout: float,
    startup_timeout: float,
) -> DaemonSpawnResult:
    """Caller holds the spawn lock."""
    # 5. Re-probe under the lock. A concurrent spawner may have finished
    # between our first probe and our lock -- its socket is live, not stale.
    reaped_stale = False
    try:
        _probe(socket_path, connect_timeout)
        return _attached()
    except FileNotFoundError:
        pass
    except ConnectionRefusedError:
        # Lock held AND a fresh connect was just refused: stale leftover --
        # provided it is actually a socket.
        if _not_a_socket(socket_path):
            return _failed(f"socket_path is not a socket: {socket_path}")
        try:
            os.unlink(socket_path)
            reaped_stale = True
        except FileNotFoundError:
            pass
        except OSError as exc:
            return _failed(
                f"cannot remove stale socket {socket_path}: {type(exc).__name__}: {exc}"
            )
    except PermissionError:
        return _failed(f"permission denied: {socket_path}")
    except OSError as exc:
        return _failed(f"{type(exc).__name__}: {exc}")

    # 6. Open the temp log; fall back to a PIPE if we can't.
    temp_log = log_dir / f"daemon-spawning-{uuid.uuid4().hex}.log"
    log_fh = None
    try:
        log_fh = open(temp_log, "w")
    except OSError:
        log_fh = None
    pipe_fallback = log_fh is None

    if log_fh is not None and reaped_stale:
        try:
            log_fh.write(f"[pare-tui] reaped stale socket at {socket_path} before spawn\n")
            log_fh.flush()
        except OSError:
            pass

    # 7. Popen. The lock fd is non-inheritable (os.open default) and Popen's
    # close_fds=True default keeps it out of the child either way -- a daemon
    # holding spawn.lock would wedge every later launch.
    env = os.environ.copy()
    env["PARE_SOCKET_PATH"] = str(socket_path)
    popen_kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": log_fh if log_fh is not None else subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "start_new_session": True,
        "env": env,
    }
    try:
        try:
            child = spawn(cmd, popen_kwargs)
        except OSError as exc:
            if log_fh is not None:
                try:
                    temp_log.unlink()
                except OSError:
                    pass
            return _failed(f"{type(exc).__name__}: {exc}")
    finally:
        if log_fh is not None:
            log_fh.close()  # the child has its own copy of the fd

    with _owned_lock:
        _owned[child.pid] = child

    log_path: Path | None = None
    if not pipe_fallback:
        final_log = log_dir / f"daemon-{child.pid}.log"
        try:
            os.rename(temp_log, final_log)
            log_path = final_log
        except OSError:
            log_path = temp_log

    # 8. Poll for readiness.
    deadline = time.monotonic() + startup_timeout
    while True:
        if _probe_ok(socket_path, connect_timeout):
            if pipe_fallback:
                _drain_forever(child.stdout)
            return DaemonSpawnResult(
                mode="spawned", pid=child.pid, log_path=log_path, error=None,
            )
        code = child.poll()
        if code is not None:
            with _owned_lock:
                _owned.pop(child.pid, None)
            tail = _read_pipe_head(child.stdout) if pipe_fallback else None
            return _failed(
                f"exited during startup, code {code} -- {_describe_log(log_path, tail)}",
                log_path,
            )
        if time.monotonic() >= deadline:
            break
        time.sleep(_POLL_INTERVAL)

    # Timeout with the child alive: we own it, so kill it.
    _terminate_child(child)
    with _owned_lock:
        _owned.pop(child.pid, None)
    tail = _read_pipe_head(child.stdout) if pipe_fallback else None
    return _failed(
        f"did not accept a connection within {startup_timeout:g}s -- "
        f"{_describe_log(log_path, tail)}",
        log_path,
    )


def _terminate_child(child: subprocess.Popen) -> None:
    _signal_group(child.pid, signal.SIGTERM)
    try:
        child.wait(timeout=_TIMEOUT_TERM_WAIT)
        return
    except subprocess.TimeoutExpired:
        pass
    _signal_group(child.pid, signal.SIGKILL)
    try:
        child.wait(timeout=_TIMEOUT_KILL_WAIT)
    except subprocess.TimeoutExpired:
        pass


def _exited(pid: int) -> bool:
    """True once pid is gone. Reaps it if it is our child."""
    with _owned_lock:
        child = _owned.get(pid)
    if child is not None:
        return child.poll() is not None
    try:
        done, _ = os.waitpid(pid, os.WNOHANG)
        return done != 0
    except ChildProcessError:
        # Not our child: fall back to a liveness check.
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        return False


def _wait_exited(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if _exited(pid):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_REAP_POLL_INTERVAL)


def reap(
    pid: int,
    *,
    sigterm_timeout: float = 5.0,
    sigkill_timeout: float = 2.0,
) -> Literal["exited_clean", "killed", "abandoned"]:
    """Blocking. SIGTERM the process group; escalate to SIGKILL on timeout;
    return the outcome. Never raises for a stale PID (already-exited)."""
    try:
        if not _signal_group(pid, signal.SIGTERM):
            _exited(pid)  # reap a zombie if it is ours
            return "exited_clean"
        if _wait_exited(pid, sigterm_timeout):
            return "exited_clean"
        _signal_group(pid, signal.SIGKILL)
        if _wait_exited(pid, sigkill_timeout):
            return "killed"
        return "abandoned"
    finally:
        with _owned_lock:
            child = _owned.get(pid)
            if child is not None and child.returncode is not None:
                _owned.pop(pid, None)
