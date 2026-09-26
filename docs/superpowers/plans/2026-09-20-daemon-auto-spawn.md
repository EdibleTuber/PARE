# Daemon Auto-Spawn for pare-tui — Implementation Plan

> **HISTORICAL RECORD — NOT A SOURCE OF TRUTH.** This plan was executed on 2026-09-26 and is kept only as a record of what was planned. Parts of it were superseded during execution, and some of its text describes bugs that were fixed. **Do not implement or review from it.** The authoritative sources are the living spec, [`docs/superpowers/specs/2026-09-20-daemon-auto-spawn-design.md`](../specs/2026-09-20-daemon-auto-spawn-design.md), and the code: `pare/tui/daemon_lifecycle.py`, `pare/tui/app.py`, `pare/tui/widgets/statusbar.py`. Superseded statements in the Task 1 contract are marked inline with **SUPERSEDED**; other stale text (for example the test counts and the "awaited in `on_mount`" wiring) is not marked.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `pare-tui` auto-spawn a `pare-daemon` on launch when none is running, kill it on exit, and attach cleanly (no reap) when one is already running.

**Architecture:** A new pure-Python `daemon_lifecycle` module handles detect/spawn/reap outside Textual (unit-testable in isolation). `PareTUI.on_mount` calls into it before `session.start()`; `on_unmount` reaps the child if we spawned it. `StatusBar` grows a third state (`spawn-failed`) distinct from `up`/`down`. Linux-only (fcntl.flock, start_new_session, killpg).

**Tech Stack:** Python 3.12+, Textual 8.x, stdlib subprocess/socket/fcntl/pathlib, pytest.

**Spec:** `docs/superpowers/specs/2026-09-20-daemon-auto-spawn-design.md`

## Global Constraints

- Linux-only: `fcntl.flock`, `subprocess.Popen(start_new_session=True)`, `os.killpg`. No non-Linux fallback path.
- The attach lane's behavior is unchanged from today — if a live daemon is present at `config.socket_path`, `session.start()` proceeds as it does today and the TUI does NOT touch the daemon process on exit.
- `pare-daemon`'s own code is not modified.
- Reaping uses `killpg` on the child's process group (created via `start_new_session=True`); a SIGTERM child that exits removes its own socket, a SIGKILL'd child leaves a stale socket that the NEXT TUI's detect lane reaps. Do NOT unlink the socket in the cleanup path — that races the daemon's own cleanup.
- Only the spawner reaps. Attach-lane TUIs never SIGTERM the daemon under any condition.
- Log file paths and formats are exactly `~/.local/state/pare/daemon-<PID>.log`; the `~/.local/state/pare/` directory is created with mode 0700 if missing.
- The status bar's third state is spelled `daemon:spawn-failed` (with hyphen), distinct from `daemon:up` and `daemon:DOWN`.
- Plan-supplied code below is a sketch. Deviating to fix a defect is correct — say so in your task report.

## Pre-flight corrections (2026-09-26, supersede task text where they conflict)

- **C1 — spawning is opt-in on the app.** `PareTUI.__init__` gains `auto_spawn: bool = False` and `daemon_log_dir: Path | None = None`. Only `main()` passes `auto_spawn=True`, and not when `PARE_TUI_NO_AUTO_SPAWN=1`. Without this, every existing test that mounts `PareTUI(Path("/nonexistent/pare.sock"), ...)` via `run_test()` would Popen a real `pare-daemon`, wait out the startup timeout, and write into the real `~/.local/state/pare/`.
- **C2 — the child is told where to bind.** `detect_or_spawn` sets `PARE_SOCKET_PATH=str(socket_path)` in the child's env on top of `os.environ.copy()`, so the path it polls and the path the daemon binds are the same by construction rather than by shared defaults.
- **C3 — Task 1's test code is a sketch.** The `_spawn_hook` signature is the implementer's call (the Interfaces block and the test bodies disagree, and the Shim is referenced outside its scope). Any test path that reaches `killpg` uses a real subprocess started with `start_new_session=True`; a shim with a fake PID must never reach `killpg`.
- **C4 — Task 3's tests never reap a stranger.** Tests monkeypatch both `detect_or_spawn` and `reap` at the `pare.tui.app` import site and pass a tmp `daemon_log_dir`. A faked `pid=12345` reaching the real `reap` would `killpg` whatever process group owns that PID.
- **C5 — skip auto-spawn on a systemd-managed host (user ruling, 2026-09-26).** In `main()`, if `systemctl --user is-enabled pare-daemon` exits 0, do not auto-spawn: pass `auto_spawn=False`, and if the attach fails, write `[systemd-managed pare-daemon is not accepting connections — systemctl --user status pare-daemon]` to the transcript. Reason: agent_core unlinks and rebinds the socket on every start, so a systemd restart would orphan a TUI-spawned daemon. A missing `systemctl` binary counts as "no unit".

---

## File Structure

**New files:**
- `pare/tui/daemon_lifecycle.py` — pure module. Exports `DaemonSpawnResult` (dataclass), `detect_or_spawn(...)`, `reap(...)`. No Textual imports; no asyncio imports required by the module surface (spawn is subprocess-blocking; the caller — `on_mount`/`on_unmount` — is async and can `await asyncio.to_thread(detect_or_spawn, ...)`).
- `tests/test_daemon_lifecycle.py` — 7 unit tests for `detect_or_spawn`, 1 for `reap`.

**Modified files:**
- `pare/tui/widgets/statusbar.py` — `daemon_connected: bool` becomes `daemon_state: Literal["up", "down", "spawn-failed"]`. Backwards-compat property keeps `daemon_connected: bool` writable for callers that haven't migrated. `render_for` picks the label from `daemon_state`.
- `pare/tui/app.py` — `on_mount` calls `detect_or_spawn` before `session.start()`; transcript writes on each outcome; `_daemon_owned_pid` field; `on_unmount` reaps when we own the PID; `_refresh_status_bar` writes `daemon_state` instead of `daemon_connected`.
- `tests/test_tui_app_integration.py` — one new integration test for the transcript+statusbar wiring; existing `test_status_bar_reflects_source_failure` may need a one-word update if it constructs `StatusBar(daemon_connected=...)` explicitly.

---

## Task 1: `daemon_lifecycle.py` module + unit tests

**Files:**
- Create: `pare/tui/daemon_lifecycle.py`
- Create: `tests/test_daemon_lifecycle.py`

**Interfaces:**
- Produces:
  ```python
  @dataclass(frozen=True)
  class DaemonSpawnResult:
      mode: Literal["attached", "spawned", "failed"]
      pid: int | None
      log_path: Path | None
      error: str | None    # None when mode != "failed"

  def detect_or_spawn(
      socket_path: Path,
      log_dir: Path,
      *,
      connect_timeout: float = 0.25,
      startup_timeout: float = 5.0,
      lock_dir: Path | None = None,        # default: log_dir; lock lives at lock_dir / "spawn.lock"   <-- SUPERSEDED — see spec §4: (lock_dir or socket_path.parent) / "pare-spawn.lock"
      spawn_cmd: list[str] = ["pare-daemon"],   # <-- SUPERSEDED — see spec §2.2/§4.4: default is None (= ["pare-daemon"]); the app always passes pare-daemon beside sys.executable
      _spawn_hook: Callable[[list[str], Path], subprocess.Popen] | None = None,   # <-- SUPERSEDED — see spec §2.2: Callable[[list[str], dict[str, Any]], Popen]; also a spawn_cwd kwarg exists
  ) -> DaemonSpawnResult:
      """Blocking. Callers await it on a thread."""

  def reap(
      pid: int,
      *,
      sigterm_timeout: float = 5.0,
      sigkill_timeout: float = 2.0,
  ) -> Literal["exited_clean", "killed", "abandoned"]:   # <-- SUPERSEDED — see spec §5.2: four values, adds "not_owned"; only PIDs this module spawned are signalled
      """Blocking. SIGTERM the process group; escalate to SIGKILL on timeout;
      return the outcome. Never raises for a stale PID (already-exited)."""
  ```
- Consumes: stdlib only (`socket`, `subprocess`, `fcntl`, `os`, `pathlib`, `time`, `uuid`, `errno`).

**Context:** Spec §2-§4. This module is the whole design's mechanically-testable core. No Textual, no async, no PARE-specific imports — everything else layers on top.

The plan below describes the FUNCTION CONTRACT and the TESTABLE PROPERTIES. It does NOT include full source: this is process-management + flock + Popen-ordering + socket-state-transitions code, which per CLAUDE.md's "Scale code-in-plans to the risk of the code" is the class where code in prose gets transcribed past defects. The implementer writes the module against the real files.

**Behavioral contract of `detect_or_spawn`:**

1. **Probe:** try `socket.socket(AF_UNIX).connect(str(socket_path))` with `settimeout(connect_timeout)`. On success, `close()` the probe socket and return `DaemonSpawnResult(mode="attached", pid=None, log_path=None, error=None)`.
2. **Categorize the failure:**
   - ~~`FileNotFoundError` OR `ConnectionRefusedError` → socket is absent or stale; proceed to spawn.~~ **SUPERSEDED — see spec §3. Refused ≠ stale:** agent_core binds before `listen()`, so a starting daemon refuses too. Refused + path listed in `/proc/net/unix` (or unreadable) = starting: wait, never spawn, never unlink. Refused + not a socket = failed. Only refused + a socket + not listed = stale. Implementing this line as written reintroduces the bug that unlinks a restarting daemon's socket.
   - `PermissionError` → return `mode="failed", error=f"permission denied: {socket_path}"`; do NOT spawn.
   - Anything else (e.g. `IsADirectoryError`) → return `mode="failed", error=f"{type(exc).__name__}: {exc}"`; do NOT spawn.
3. **Ensure `log_dir` exists** (`mkdir(mode=0o700, parents=True, exist_ok=True)`). If this fails, use `subprocess.PIPE` fallback path (see step 6 log-open behavior).
4. **Acquire flock:** ~~open `(lock_dir or log_dir) / "spawn.lock"`~~ **SUPERSEDED — see spec §4: the lock is `(lock_dir or socket_path.parent) / "pare-spawn.lock"`.** Open it for writing (`O_CREAT | O_WRONLY`); attempt `fcntl.flock(fd, LOCK_EX | LOCK_NB)`.
   - Got lock: proceed as spawner (steps 5-7).
   - `BlockingIOError`: someone else is spawning. Fall through to the *concurrent-wait* path (step 8).
5. **Under lock — reap stale socket if present:** ~~if `socket_path.exists()` AND another `connect()` attempt still fails, `os.unlink(socket_path)`.~~ **SUPERSEDED — see spec §4 step 3:** unlink only if a fresh connect was refused AND the path is a socket AND `/proc/net/unix` does not list it (re-checked here, immediately before the unlink); a listed path is a starting daemon and is waited on, never unlinked.
6. **Under lock — open log:** temp path `log_dir / f"daemon-spawning-{uuid.uuid4().hex}.log"`, `open(temp_path, "w")`. If open fails (`OSError`), null the log_fh and set `pipe_fallback=True`.
7. **Under lock — Popen:**
   ```python
   child = subprocess.Popen(
       spawn_cmd,
       stdin=subprocess.DEVNULL,
       stdout=(log_fh or subprocess.PIPE),
       stderr=subprocess.STDOUT,
       start_new_session=True,
       env=os.environ.copy(),
   )
   ```
   Immediately rename `temp_path` → `log_dir / f"daemon-{child.pid}.log"` and update `log_fh` if we opened one.
8. **Poll socket readiness** every 100ms up to `startup_timeout`:
   - Success (`connect()` succeeds against `socket_path`): release flock, return `DaemonSpawnResult(mode="spawned", pid=child.pid, log_path=<renamed>, error=None)`.
   - `child.poll()` returns non-None: exit code known; if `pipe_fallback`, read up to 4KB from `child.stderr` for the error payload. Release flock, return `mode="failed"` with error string naming exit code and log path (or captured tail).
   - Timeout with child still alive: `os.killpg(os.getpgid(child.pid), SIGTERM)`; wait bounded 2s; if still alive, `SIGKILL`. Release flock, return `mode="failed"` with error naming "did not accept a connection within Xs" and the log path.
9. **Concurrent-wait path** (step 4 fallthrough): with the flock owned by someone else, do NOT reap or spawn. Poll `socket_path.connect()` every 100ms up to `startup_timeout`. Success → return `mode="attached"`. Timeout → return `mode="failed", error="concurrent spawn timed out"`. Do not hold the flock lookup fd open — release its `open(...)` handle before returning.

**Behavioral contract of `reap(pid, sigterm_timeout, sigkill_timeout)`:**

**SUPERSEDED — see spec §5.2:** `reap` signals only a PID this module spawned and still holds the `Popen` for; any other PID returns `"not_owned"` with no signal (a bare `killpg(getpgid(pid))` can hit a reused PID). Group signals are sent only to a child leading its own group that is not ours.

1. `os.killpg(os.getpgid(pid), SIGTERM)` — if `ProcessLookupError` or `PermissionError` (already dead / not ours), return `"exited_clean"` (idempotent — reap is called on shutdown, "already gone" is success).
2. Poll `os.waitpid(pid, os.WNOHANG)` every 50ms up to `sigterm_timeout`. Non-zero return → return `"exited_clean"`.
3. Timeout: `os.killpg(..., SIGKILL)`. Poll for another `sigkill_timeout`. Non-zero → return `"killed"`. Still-alive after SIGKILL → return `"abandoned"`.

- [ ] **Step 1: Write the module skeleton**

Create `pare/tui/daemon_lifecycle.py` with:
- Module docstring naming the spec (`docs/superpowers/specs/2026-09-20-daemon-auto-spawn-design.md`) and the contract summary above.
- `DaemonSpawnResult` dataclass exactly as in the Interfaces block.
- `detect_or_spawn` and `reap` signatures exactly as above, bodies raising `NotImplementedError`.
- Type hints throughout. `from __future__ import annotations` at the top for consistency with the rest of `pare/tui/`.

- [ ] **Step 2: Write the first failing test — attach lane**

Create `tests/test_daemon_lifecycle.py`. First test:

```python
import socket
import tempfile
from pathlib import Path

import pytest

from pare.tui.daemon_lifecycle import detect_or_spawn, DaemonSpawnResult


def test_attach_lane_when_socket_is_alive(tmp_path):
    sock_path = tmp_path / "alive.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock_path))
    server.listen(1)
    try:
        # Poison _spawn_hook to prove it was not called
        def never_called(*a, **kw):
            raise AssertionError("spawn_hook must not be called when a live daemon is present")

        result = detect_or_spawn(
            sock_path, tmp_path / "logs",
            _spawn_hook=never_called,
        )
        assert result.mode == "attached"
        assert result.pid is None
        assert result.log_path is None
        assert result.error is None
    finally:
        server.close()
```

- [ ] **Step 3: Verify test fails**

Run: `.venv/bin/pytest tests/test_daemon_lifecycle.py::test_attach_lane_when_socket_is_alive -v`
Expected: FAIL with `NotImplementedError`.

- [ ] **Step 4: Implement `detect_or_spawn`'s probe + attach path**

Steps 1-2 of the contract above. Return the probe result; do not implement the spawn path yet. Test should now pass.

- [ ] **Step 5: Test — permission-denied error path**

```python
def test_permission_denied_returns_failed_without_spawning(tmp_path, monkeypatch):
    sock_path = tmp_path / "denied.sock"
    # Force connect() to raise PermissionError
    class DeniedSocket:
        def __init__(self, *a, **kw): pass
        def settimeout(self, t): pass
        def connect(self, path): raise PermissionError(13, "Permission denied")
        def close(self): pass
    monkeypatch.setattr("socket.socket", DeniedSocket)

    def never_called(*a, **kw):
        raise AssertionError("must not spawn on permission errors")

    result = detect_or_spawn(sock_path, tmp_path / "logs", _spawn_hook=never_called)
    assert result.mode == "failed"
    assert "permission denied" in result.error.lower()
```

Run, expect fail. Implement categorize-failure logic (step 2 of contract). Run, expect pass.

- [ ] **Step 6: Test — stale-socket reap**

```python
def test_stale_socket_is_reaped_before_spawn(tmp_path):
    sock_path = tmp_path / "stale.sock"
    sock_path.touch()  # dead file, no listener
    log_dir = tmp_path / "logs"

    called_with_paths = []
    def hook(cmd, log_fh_or_pipe):
        # Verify the stale file was unlinked BEFORE spawn
        called_with_paths.append(sock_path.exists())
        # Simulate a daemon binding the socket after 50ms
        # (In real use this is a Popen; the hook returns a Popen-like object.)
        import subprocess
        # Return a shim that fakes readiness by binding the socket
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(str(sock_path))
        s.listen(1)
        class Shim:
            pid = 99999
            def poll(self): return None
            stderr = None
        # Leak the socket by design; the test cleanup handles it
        Shim._sock = s
        return Shim()

    result = detect_or_spawn(sock_path, log_dir, _spawn_hook=hook)
    assert result.mode == "spawned"
    assert called_with_paths == [False]  # socket was NOT there when spawn ran → reaped
    Shim._sock.close()  # cleanup, not asserted
```

If the API doesn't fit this test shape exactly, the implementer adjusts the seam (either the hook's signature, or how the test observes the reap) — the property "the stale file was unlinked before Popen ran" is what matters, not the exact call shape.

- [ ] **Step 7: Test — absent-socket spawn success**

Similar to test 6 but no pre-existing socket file. Assert `mode == "spawned"`, `pid == 99999` (or whatever the hook records), `log_path` is a real file under `log_dir` matching `daemon-<pid>.log`.

- [ ] **Step 8: Test — spawn exits during startup**

Hook returns a Popen that has already exited (`poll()` returns 1). Assert `mode == "failed"`, error message contains "exited during startup" and the exit code.

- [ ] **Step 9: Test — spawn times out (child alive, socket not bound)**

Hook returns a Popen that stays alive forever without binding the socket. Assert `mode == "failed"` within `startup_timeout + 1s`, error mentions "did not accept a connection". Assert the hook's Popen received a `terminate()` call (implementer wires this by having the shim record the call).

- [ ] **Step 10: Test — concurrent-spawn flock contention**

Two threads call `detect_or_spawn` on the same socket_path with a hook that binds the socket after 100ms. Assert exactly one thread reports `mode="spawned"`; the other reports `mode="attached"`.

```python
import threading

def test_concurrent_spawn_only_one_spawns(tmp_path):
    sock_path = tmp_path / "race.sock"
    log_dir = tmp_path / "logs"
    results = []
    hook_count = [0]
    hook_lock = threading.Lock()
    def hook(cmd, log_fh_or_pipe):
        with hook_lock:
            hook_count[0] += 1
        import socket as s
        server = s.socket(s.AF_UNIX, s.SOCK_STREAM)
        server.bind(str(sock_path))
        server.listen(1)
        class Shim:
            pid = 12345
            def poll(self): return None
            stderr = None
        Shim._sock = server
        return Shim()

    def run(): results.append(detect_or_spawn(sock_path, log_dir, _spawn_hook=hook))

    t1 = threading.Thread(target=run)
    t2 = threading.Thread(target=run)
    t1.start(); t2.start()
    t1.join(); t2.join()

    modes = sorted(r.mode for r in results)
    assert modes == ["attached", "spawned"]
    assert hook_count[0] == 1
```

- [ ] **Step 11: Test — log-open failure fallback**

Pass a `log_dir` inside a chmod-000 parent so `mkdir` fails. Hook Popens `sh -c 'echo oops >&2; exit 2'` and returns the real Popen (this test uses a real subprocess, not a shim). Assert `mode == "failed"`, `error` contains "oops" (the captured stderr tail) and mentions "no log", `log_path is None`.

- [ ] **Step 12: Test — `reap` SIGTERM path**

```python
def test_reap_sigterm_terminates_a_normal_child():
    import subprocess, time
    child = subprocess.Popen(
        ["sleep", "30"],
        start_new_session=True,
    )
    from pare.tui.daemon_lifecycle import reap
    result = reap(child.pid, sigterm_timeout=2.0)
    assert result == "exited_clean"
    assert child.poll() is not None
```

- [ ] **Step 13: Run full test file**

`.venv/bin/pytest tests/test_daemon_lifecycle.py -v` — all pass.

- [ ] **Step 14: Full suite**

`.venv/bin/pytest -q` — expect 481 baseline → 489 (7 new detect tests + 1 reap test, target says 8 new; the concurrent-spawn test contributes 1).

- [ ] **Step 15: Commit**

```bash
git add pare/tui/daemon_lifecycle.py tests/test_daemon_lifecycle.py
git commit -m "$(cat <<'EOF'
feat(tui): daemon_lifecycle module (detect_or_spawn + reap)

Pure Python, no Textual, no async framework -- callers await it on
a thread. Three-lane detect (attached / spawn / failed), spawn
guarded by flock so concurrent TUI launches don't race the
reap-and-spawn. reap() SIGTERM-with-SIGKILL-escalation on a
process group.

Spec: docs/superpowers/specs/2026-09-20-daemon-auto-spawn-design.md §2-§4

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01VatXHkDJSMKf57UVL76KbN
EOF
)"
```

---

## Task 2: `StatusBar` third state

**Files:**
- Modify: `pare/tui/widgets/statusbar.py`
- Modify: `tests/test_tui_app_integration.py:69` — `test_status_bar_reflects_source_failure` if it constructs `StatusBar(daemon_connected=...)`; grep to confirm.

**Interfaces:**
- Consumes: nothing new from other tasks.
- Produces: `StatusBar.daemon_state: Literal["up", "down", "spawn-failed"]`, with a backwards-compat property `daemon_connected` that setter maps `True → "up"` / `False → "down"`.

**Context:** Spec §6. `daemon:spawn-failed` renders in the same slot as `daemon:up` / `daemon:DOWN`. The `daemon_connected: bool` field on `StatusBar` becomes a compatibility property; Task 3 will migrate `_refresh_status_bar` to write `daemon_state` directly.

- [ ] **Step 1: Write the failing test — spawn-failed renders distinctly**

Add to `tests/test_tui_app_integration.py` (near the existing `test_status_bar_reflects_source_failure`):

```python
def test_status_bar_renders_spawn_failed_distinctly():
    from pare.tui.widgets.statusbar import StatusBar
    up = StatusBar(); up.daemon_state = "up"
    down = StatusBar(); down.daemon_state = "down"
    failed = StatusBar(); failed.daemon_state = "spawn-failed"

    up_text = up.render_for([])
    down_text = down.render_for([])
    failed_text = failed.render_for([])

    # Three distinct labels
    assert up_text != down_text
    assert up_text != failed_text
    assert down_text != failed_text
    assert "spawn-failed" in failed_text.lower() or "spawn_failed" in failed_text.lower()


def test_status_bar_daemon_connected_backwards_compat():
    """Legacy callers still writing daemon_connected: bool get the same
    render they did before (up when True, down when False)."""
    from pare.tui.widgets.statusbar import StatusBar
    sb = StatusBar()
    sb.daemon_connected = True
    assert "up" in sb.render_for([]).lower()
    sb.daemon_connected = False
    assert "down" in sb.render_for([]).lower()
```

- [ ] **Step 2: Verify tests fail**

`.venv/bin/pytest tests/test_tui_app_integration.py::test_status_bar_renders_spawn_failed_distinctly tests/test_tui_app_integration.py::test_status_bar_daemon_connected_backwards_compat -v` — expect both fail (`daemon_state` attribute doesn't exist).

- [ ] **Step 3: Implement `daemon_state` and backwards-compat property**

Edit `pare/tui/widgets/statusbar.py`:

```python
# Replace the __init__ default field and render_for:

from typing import Literal


class StatusBar(Static):
    def __init__(
        self,
        *,
        daemon_state: Literal["up", "down", "spawn-failed"] = "down",
        channel_id: str = "",
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
        # Backwards-compat: accept daemon_connected as a keyword; True → "up".
        daemon_connected: bool | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        if daemon_connected is not None:
            daemon_state = "up" if daemon_connected else "down"
        self.daemon_state = daemon_state
        self.channel_id = channel_id

    @property
    def daemon_connected(self) -> bool:
        return self.daemon_state == "up"

    @daemon_connected.setter
    def daemon_connected(self, value: bool) -> None:
        self.daemon_state = "up" if value else "down"

    def render_for(self, panes: list[Pane]) -> str:
        label = {
            "up": "daemon:up",
            "down": "daemon:DOWN",
            "spawn-failed": "daemon:spawn-failed",
        }[self.daemon_state]
        channel = f"channel:{self.channel_id or '-'}"
        if not panes:
            panes_text = "panes:none"
        else:
            panes_text = " ".join(_pane_summary(pane) for pane in panes)
        return f"{label}  {channel}  {panes_text}"
```

- [ ] **Step 4: Verify the two new tests pass and the existing one still passes**

`.venv/bin/pytest tests/test_tui_app_integration.py -v` — all pass.

- [ ] **Step 5: Full suite**

`.venv/bin/pytest -q` — no regressions.

- [ ] **Step 6: Commit**

```bash
git add pare/tui/widgets/statusbar.py tests/test_tui_app_integration.py
git commit -m "$(cat <<'EOF'
feat(tui): StatusBar third state daemon:spawn-failed

Distinct from daemon:DOWN so the operator knows whether to retry
(DOWN — transient) or read the log (spawn-failed — the TUI tried
and something is wrong). Backwards-compat daemon_connected setter
keeps callers that haven't migrated working.

Spec: docs/superpowers/specs/2026-09-20-daemon-auto-spawn-design.md §6

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01VatXHkDJSMKf57UVL76KbN
EOF
)"
```

---

## Task 3: Wire `detect_or_spawn` + `reap` into `PareTUI`

**Files:**
- Modify: `pare/tui/app.py`
- Modify: `tests/test_tui_app_integration.py` — one new integration test.

**Interfaces:**
- Consumes: `pare.tui.daemon_lifecycle.detect_or_spawn`, `reap`, `DaemonSpawnResult` (from Task 1).
- Consumes: `pare.tui.widgets.statusbar.StatusBar.daemon_state` (from Task 2).
- Produces: `PareTUI._daemon_owned_pid: int | None` (private field, set by spawn lane, checked by on_unmount).

**Context:** Spec §2.1 (two-lane launch path), §5 (cleanup), §6 (transcript surfaces). This is the wiring task — no new logic, only integration of Task 1's module and Task 2's field.

- [ ] **Step 1: Read the current `on_mount` and `on_unmount`**

```bash
sed -n '190,225p' pare/tui/app.py
```

Confirm the anchors: the `subscribe / start` block starting around line 195, the `except Exception: logger.exception(...)` around line 205-210, the `on_unmount` starting around line 217.

- [ ] **Step 2: Write the failing integration test**

Add to `tests/test_tui_app_integration.py`:

```python
async def test_transcript_receives_spawn_status_line(monkeypatch, tmp_path):
    """When detect_or_spawn returns 'spawned', the transcript should show
    the PID + log path, and the status bar reads daemon:up."""
    from pare.tui.daemon_lifecycle import DaemonSpawnResult
    from pare.tui import daemon_lifecycle as dl_module

    def fake_detect(*args, **kwargs):
        return DaemonSpawnResult(
            mode="spawned", pid=12345,
            log_path=tmp_path / "daemon-12345.log",
            error=None,
        )
    monkeypatch.setattr(dl_module, "detect_or_spawn", fake_detect)
    # Also patch the import site in app.py
    from pare.tui import app as app_module
    monkeypatch.setattr(app_module, "detect_or_spawn", fake_detect)

    # ... construct PareTUI, mount, drive it enough that on_mount runs ...
    # (Follow the pattern of existing test_status_bar_reflects_source_failure
    #  or whatever fixture in tests/test_tui_app_integration.py mounts the app.)

    # After mount:
    # assert "spawned pare-daemon PID 12345" in transcript_text
    # assert "logs at" in transcript_text
    # assert status_bar.daemon_state == "up"
```

The implementer looks at existing `test_tui_app_integration.py` cases to find the mounting pattern. If mounting a full `PareTUI` in a test is heavy, an alternative is to construct a minimal harness that calls `on_mount` in isolation — either works as long as the assertion is on the transcript RichLog's captured writes and the status bar's `daemon_state`.

Also add a `mode="failed"` counterpart:

```python
async def test_transcript_and_statusbar_on_spawn_failed(monkeypatch, tmp_path):
    # Same shape, but fake_detect returns:
    # DaemonSpawnResult(mode="failed", pid=None, log_path=tmp_path/"log", 
    #                   error="did not accept a connection within 5s")
    # Assert transcript contains "daemon spawn failed" and the error string
    # Assert status_bar.daemon_state == "spawn-failed"
```

- [ ] **Step 3: Verify tests fail**

Run both new tests, expect assertions to fail (no `detect_or_spawn` wiring in `on_mount` yet).

- [ ] **Step 4: Wire `detect_or_spawn` into `on_mount`**

In `pare/tui/app.py`:

Add imports near the other `pare.tui.*` imports:
```python
from pare.tui.daemon_lifecycle import DaemonSpawnResult, detect_or_spawn, reap
```

Add the field in `__init__`, alongside the existing `self._daemon_connected = False`:
```python
self._daemon_owned_pid: int | None = None
```

Add config-derived log_dir (compute once, at __init__):
```python
self._daemon_log_dir = Path.home() / ".local" / "state" / "pare"
```

In `on_mount`, BEFORE the existing `subscribe / start` block:

```python
# Blocking work (socket probe, flock, Popen) goes on a thread.
result = await asyncio.to_thread(
    detect_or_spawn,
    self.socket_path,
    self._daemon_log_dir,
)
transcript = self.query_one("#transcript", RichLog)
if result.mode == "attached":
    transcript.write(f"[attached to running pare-daemon at {self.socket_path}]")
elif result.mode == "spawned":
    self._daemon_owned_pid = result.pid
    transcript.write(f"[pare-daemon not running; spawning...]")
    transcript.write(
        f"[pare-daemon ready — PID {result.pid}, logs at {result.log_path}]"
    )
else:  # failed
    log_ref = f"see {result.log_path}" if result.log_path else "no log; error inline"
    transcript.write(f"[daemon spawn failed: {result.error} — {log_ref}]")
    # The status bar's spawn-failed state is set below via _refresh_status_bar,
    # once we know result.mode.

# Then let the existing subscribe/start block run. On mode='failed', session.start()
# will fail with the connection error today's code already handles.
```

The subsequent `subscribe / start` block stays. On `attached` and `spawned`, `session.start()` succeeds. On `failed`, it fails through the existing `except` and sets `self._daemon_connected = False` — that's the correct state for the base bar, but we want `spawn-failed` specifically. Update `_refresh_status_bar` to know the difference (step 5).

- [ ] **Step 5: Update `_refresh_status_bar` to write `daemon_state`**

Replace `status_bar.daemon_connected = self._daemon_connected` with:

```python
if not self._daemon_connected and result_mode_was_failed:
    status_bar.daemon_state = "spawn-failed"
else:
    status_bar.daemon_state = "up" if self._daemon_connected else "down"
```

The "result_mode_was_failed" state has to be stored somewhere `_refresh_status_bar` can see. Simplest: add `self._daemon_spawn_failed: bool = False` in `__init__`, set it in `on_mount` when `result.mode == "failed"`, clear it on any successful `_on_daemon_message` (a live message means the daemon is talking to us, spawn-failed is stale).

Rewrite `_refresh_status_bar` as:

```python
def _refresh_status_bar(self) -> None:
    try:
        status_bar = self.query_one("#status-bar", StatusBar)
        pane_dock = self.query_one("#pane-dock", PaneDock)
    except Exception:
        return
    if self._daemon_connected:
        status_bar.daemon_state = "up"
    elif self._daemon_spawn_failed:
        status_bar.daemon_state = "spawn-failed"
    else:
        status_bar.daemon_state = "down"
    status_bar.channel_id = self.channel_id
    status_bar.refresh_for(pane_dock.panes)
```

- [ ] **Step 6: Wire `reap` into `on_unmount`**

Replace `on_unmount` body with:

```python
async def on_unmount(self) -> None:
    stop = getattr(self.session, "stop", None)
    if stop is not None:
        await stop()
    if self._daemon_owned_pid is not None:
        # Blocking (waitpid), goes on a thread.
        try:
            await asyncio.to_thread(reap, self._daemon_owned_pid)
        except Exception:
            # Cleanup must not hang the exit; log and move on.
            logger.exception("failed to reap owned daemon PID %s",
                             self._daemon_owned_pid)
```

- [ ] **Step 7: Verify integration tests pass**

`.venv/bin/pytest tests/test_tui_app_integration.py -v` — all pass, including the two new cases.

- [ ] **Step 8: Live sanity — launch pare-tui once**

Not automated. This is a "verify-the-surface-you-hand-the-user-to" check: launch `pare-tui` with no daemon running. Confirm you see the spawn transcript lines and `daemon:up` on the status bar. Then Ctrl-C to exit; confirm `pgrep pare-daemon` finds nothing (reap worked). Log the outcome in the task report.

- [ ] **Step 9: Full suite**

`.venv/bin/pytest -q` — final count ≈ 490 (481 baseline + 8 from Task 1 + 2 from Task 2 + 2 from Task 3 - 3 for a couple that get absorbed by monkeypatching order).

The actual count is whatever the implementer records; the property that matters is "no regressions from baseline plus the new tests all pass."

- [ ] **Step 10: Commit**

```bash
git add pare/tui/app.py tests/test_tui_app_integration.py
git commit -m "$(cat <<'EOF'
feat(tui): auto-spawn pare-daemon on TUI launch

on_mount calls detect_or_spawn before session.start(). Three
outcomes: attached (existing daemon, unchanged), spawned (we
launched one, will reap on exit), failed (error surface to
transcript + spawn-failed status bar state). on_unmount reaps the
owned daemon via SIGTERM-with-SIGKILL escalation. Attach-lane
TUIs never touch the daemon process.

Spec: docs/superpowers/specs/2026-09-20-daemon-auto-spawn-design.md §2, §5, §6

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01VatXHkDJSMKf57UVL76KbN
EOF
)"
```

---

## Self-Review

**1. Spec coverage.** Each spec section maps to a task:
- Spec §1 D1-D6: D1 (auto-spawn) → Task 3's on_mount wiring; D2 (TUI owns, kill on exit) → Task 3's on_unmount reap; D3 (attach if running) → Task 1's attached path; D4 (stale-socket reap under flock) → Task 1 steps 5-11; D5 (per-PID log at ~/.local/state/pare/) → Task 1 steps 6-7; D6 (three status-bar states) → Task 2.
- Spec §2 architecture: Task 1 (module) + Task 3 (integration).
- Spec §3-§4 detect + spawn: Task 1's behavioral contract + tests.
- Spec §5 cleanup: Task 3's `on_unmount` block using Task 1's `reap`.
- Spec §6 visible feedback: Task 3's transcript writes + Task 2's status bar state.
- Spec §7 testing: Task 1 tests (7 detect + 1 reap) + Task 3 integration tests. Matches the target counts.
- Spec §8 rollback: not in the plan directly — rollback is a git revert of Task 3's commit (isolated wiring), plus optionally Task 1 and Task 2. Named for reviewer awareness.
- Spec §9 not in scope: honored — no heartbeat, no in-session respawn, no stop protocol, no log rotation, no non-Linux, no multi-machine, no configurable spawn command.

**2. Placeholder scan.** No "TBD" or "TODO". The `result_mode_was_failed` phrasing in Task 3 Step 5 resolves to `self._daemon_spawn_failed` in the implementation — spelled out in the same step.

**3. Type consistency.** `DaemonSpawnResult` fields match across the module contract and the integration test. `daemon_state: Literal["up", "down", "spawn-failed"]` used consistently in Task 2 and Task 3. `_daemon_owned_pid: int | None` and `_daemon_spawn_failed: bool` are the two new `PareTUI` fields; both referenced in the tasks that add them.

**4. Task granularity.** Three tasks, each independently testable, each a clear reviewer surface. Task 1 is the biggest (the pure module + 8 tests); Task 2 is small (a widget field + 2 tests); Task 3 is integration (wiring + 2 tests). Ordering matters (1 before 3; 2 before 3), reflected in the Interfaces blocks.

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-09-20-daemon-auto-spawn.md`. Two execution options:

1. **Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution** — Execute tasks in this session using executing-plans.

Which approach?
