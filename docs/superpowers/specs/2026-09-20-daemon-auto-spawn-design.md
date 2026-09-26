# Daemon auto-spawn for `pare-tui`

**Status:** design, 2026-09-20. Awaiting spec review before writing-plans.
**Repos touched:** PARE only.
**Related:** [`2026-09-18-pare-tui-design.md`](2026-09-18-pare-tui-design.md), [`2026-09-20-bench-integration-design.md`](2026-09-20-bench-integration-design.md).

The motivating problem: launching `pare-tui` today, if no `pare-daemon` is running, gives the operator a UI with the UART pane working (its source connects directly to the deployed worker) and everything else broken — chat is dead, `/worker list` fails, the status bar reads `daemon:DOWN` in a way most operators will miss until they've typed something and seen `[send failed: ...]` in the transcript. The user's own reaction 2026-09-20, after the first live launch: "it worked! I couldn't chat or anything but it worked."

The chain today is: `PareTUI.__init__` builds a `DaemonSession(socket_path, ...)`, `on_mount` awaits `session.start()`, `start()` raises on connection refused, the `except` block calls `logger.exception` (which goes to stderr, invisible under a Textual TUI redirecting stdio), and sets `self._daemon_connected = False`. Nothing else happens. No hint. No path to recovery from the UI.

This spec adds an auto-spawn lane. If no daemon is running, the TUI spawns one and kills it on exit. If one IS running, the TUI attaches to it (unchanged path) and doesn't touch it on exit.

---

## 1. Decisions

| | Decision | Why |
|---|---|---|
| **D1** | TUI auto-spawns `pare-daemon` on launch if no daemon is running. | The operator's expected model of a "chat-capable TUI" is that chat works when the TUI is open. Requiring a second shell for `pare-daemon` is friction that the user named on the first live launch. |
| **D2** | TUI owns the daemon it spawned and kills it on TUI exit. | Simplest possible lifecycle. Multiple TUI sessions: second and later ones detect the alive socket, attach as read/write clients, and do NOT kill on exit. Only the spawner reaps. |
| **D3** | Attach if a daemon is already running (socket alive AND `connect()` succeeds). | Non-competitive: the second TUI does not replace the first's daemon. Attaching doesn't fight for ownership — attachers are never reapers. |
| **D4** | Stale socket file (present, but nothing accepts a `connect()`) is reaped inside the spawn path, under an `flock` guard so two concurrent TUI launches don't race the reap-and-spawn. | A crashed daemon leaves its socket file behind; without reaping we'd never recover without operator intervention. `flock` at `~/.local/state/pare/spawn.lock` is Linux-idiomatic and matches the rest of PARE's assumptions. |
| **D5** | Daemon stderr/stdout goes to a per-spawn log at `~/.local/state/pare/daemon-<PID>.log`. TUI prints the log path in the transcript. | Post-mortem for spawn failures needs a file, not stderr swallowed by Textual. Per-PID naming is `grep`-friendly for the operator's typical `pgrep pare-daemon` workflow. Log-open failure has a PIPE fallback so filesystem trouble doesn't silently discard the diagnostic. |
| **D6** | Three status-bar states: `daemon:up`, `daemon:DOWN`, `daemon:spawn-failed`. | `spawn-failed` (we tried and it didn't come up — read the log) is meaningfully different from `DOWN` (attach-lane disconnect — retry might help). Merging them loses actionability. |

### What this does NOT change

- **`pare-daemon` itself.** No changes to the daemon's own code, config surface, or startup semantics.
- **The attach lane's behavior.** If a daemon is running when the TUI launches, everything runs today's path unchanged.
- **The status bar's `daemon:up` and `daemon:DOWN` render logic.** Only adds a third state.
- **In-session recovery.** If the daemon dies mid-session, the existing `_on_daemon_message` / `_daemon_connected = False` path handles it. This spec does not auto-respawn a daemon that died in-session.
- **Non-Linux support.** `fcntl.flock`, `start_new_session=True`, `os.killpg` are Linux-idiomatic; PARE is Linux-only today.

---

## 2. Architecture

### 2.1 Two-lane launch path

`PareTUI.on_mount` gains one call BEFORE `session.start()`: `detect_or_spawn(socket_path, log_dir)`. It returns one of three outcomes:

- `attached` — daemon was already running. `session.start()` proceeds unchanged.
- `spawned(pid, log_path)` — we launched a daemon; `pid` is recorded on `self._daemon_owned_pid` for cleanup on exit. `session.start()` proceeds against the just-spawned daemon.
- `failed(reason, log_path | None)` — spawn attempted and did not produce a live daemon. `session.start()` is skipped (or proceeds and fails, same visible surface); `self._daemon_owned_pid` stays `None`.

The `session.start()` call and its `except` block stay. Their behavior on `attached` is unchanged. On `spawned`, they succeed against the fresh daemon. On `failed`, they either aren't called or fail through — either way, the transcript already carries the failure line from `detect_or_spawn`.

### 2.2 Files

- **New:** `pare/tui/daemon_lifecycle.py` — pure Python module, no Textual imports, no async framework dependencies beyond `subprocess`, `socket`, `fcntl`, `pathlib`. Exports:
  - `@dataclass DaemonSpawnResult` with fields `mode: Literal["attached", "spawned", "failed"]`, `pid: int | None`, `log_path: Path | None`, `error: str | None`.
  - `def detect_or_spawn(socket_path: Path, log_dir: Path, *, connect_timeout: float = 0.25, startup_timeout: float = 5.0, spawn_cmd: list[str] | None = None, _spawn_hook: Callable | None = None) -> DaemonSpawnResult`.
  - `_spawn_hook` is a test seam, per the pattern established in `agent_core.CaptureStore._pre_write_hook`. Production callers never pass it.
- **Modified:** `pare/tui/app.py` — `on_mount` gains the detect-or-spawn call and transcript writes; `on_unmount` gains the reap block; new `self._daemon_owned_pid` field.
- **Modified:** `pare/tui/widgets/statusbar.py` — the render logic gains a `daemon:spawn-failed` case. Field on `StatusBar` becomes `daemon_state: Literal["up", "down", "spawn-failed"]` (backwards-compatible via a property that maps the old `daemon_connected: bool` to `up`/`down`).

### 2.3 State lives on the app

`PareTUI` grows one field: `self._daemon_owned_pid: int | None = None`. Set only when `detect_or_spawn` returned `spawned`. `on_unmount` checks it and reaps only when set. No global state, no PID file — the PID lives in the app's memory for exactly the lifetime the app owns.

---

## 3. The detect step

```
1. Try to connect() to socket_path with a short timeout (250ms).
   - Success: mode=attached. Close probe socket. Return.
   - ConnectionRefused OR FileNotFoundError: socket absent or stale.
     Fall through to spawn.
   - Any other error (permission, path is a regular file, etc): mode=failed
     with a specific errno-named reason ("socket_path is not a socket" or
     "permission denied opening <path>"). Do NOT spawn — the problem isn't
     "no daemon."
```

The `connect()` probe is what distinguishes a live daemon from a crashed one's leftover socket file. A stale unix socket accepts `stat()` but refuses `connect()` — that's the reap signal.

---

## 4. The spawn step

Only reached when the detect step returned "socket absent" or "socket stale."

```
1. Acquire flock on ~/.local/state/pare/spawn.lock (non-blocking; mkdir -p the
   parent as needed with mode 0700).
   - Got lock: continue as the spawner.
   - Lock contended: another TUI is spawning. Wait up to startup_timeout for
     socket_path to accept a connection (poll every 100ms). If it comes up,
     return mode=attached. If not, return mode=failed with
     "concurrent spawn timed out."

2. Inside the lock:
   a. If socket_path exists AND connect() still fails: unlink it. Note in
      the daemon log ("reaped stale socket at <path> before spawn") once we
      have a log to write to.
   b. Open a temp log at daemon-spawning-<uuid>.log in log_dir. Popen the
      daemon with stdin=DEVNULL, stdout=stderr=that log, start_new_session=True,
      env=os.environ.copy(). start_new_session gives the child its own
      process group so terminal signals don't cascade and killpg targets
      only the daemon.
   c. Rename the temp log to daemon-<child.pid>.log.
   d. Poll socket_path + connect() every 100ms up to startup_timeout.
      - Ready (connect succeeds): return mode=spawned, pid=child.pid,
        log_path=<renamed path>. Drop the flock.
      - child.poll() returns non-None before ready: daemon exited during
        startup. Return mode=failed with the exit code named
        ("daemon exited during startup, code <N> -- see <log_path>").
      - Timeout: SIGTERM the child (we own it), wait bounded 2s, SIGKILL if
        needed. Return mode=failed with "daemon did not accept a connection
        within Xs -- see <log_path>."
   e. Drop the flock.
```

### 4.1 Log-open fallback

If `open(temp_log, "w")` itself fails (log_dir unwritable, filesystem full), fall back to `stdout=stderr=subprocess.PIPE`. On any failure return path, read up to 4KB of the PIPE'd stderr into memory and include it verbatim in the `error` field of the result. The transcript writes the captured tail directly with no log path referenced. This path is rare but must not silently discard the diagnostic — a broken filesystem is exactly when the operator needs the trace most.

### 4.2 Concurrent-spawn safety

Two TUI processes racing to spawn against the same `socket_path` never both spawn: the `flock` serializes them, and the second wakes up to find a socket ready and takes the attach lane. If both are attempting the spawn at exactly the same wall-clock time, one blocks on `flock` for at most `startup_timeout` before failing over to "concurrent spawn timed out" — a diagnosable state, not a mystery.

---

## 5. Cleanup on TUI exit

`PareTUI.on_unmount` (existing method) gains a tail block guarded by `self._daemon_owned_pid is not None`:

```
1. os.killpg(os.getpgid(pid), SIGTERM).
2. Wait bounded (default 5s) for the child to exit.
   - Exits cleanly: done.
   - Timeout: os.killpg(..., SIGKILL); wait bounded 2s.
   - Still alive after SIGKILL: log a warning (goes to whatever log the
     TUI's own logger writes to) naming the abandoned PID. Do not hang the
     TUI's exit path -- Textual's on_unmount can't afford to wait forever.
3. Do NOT unlink the socket file. Cleanly-exited daemons remove their own
   socket. A SIGKILL'd daemon leaves a stale socket; the NEXT TUI's detect
   lane reaps it (that's what §3-§4 are for). This is deliberate: it keeps
   the state machine simple and avoids racing the daemon's own cleanup.
```

**Attach-lane cleanup** (`_daemon_owned_pid is None`): `on_unmount` skips the reap block entirely. Never touches the daemon process. Matches today's behavior for an externally-started daemon.

**Crash-during-session** (TUI's Textual loop raises uncaught, `on_unmount` doesn't run): the daemon becomes an orphan. It stays alive because `start_new_session=True` protects it from any SIGHUP cascade. The next TUI launch's detect probe finds a live socket and attaches — the orphan is transparently reclaimed. It's now unowned by anyone; the operator can `kill` it by PID if they want to force a re-spawn. Named in §9 as a known operational property, not a fix candidate.

---

## 6. Visible feedback (the operator's view)

Three states, three transcript surfaces. All lines are `[bracketed]` for visual distinction from chat content.

**Attach lane:**
```
[attached to running pare-daemon at /run/user/1000/pare.sock]
```
No PID (we don't know it; attachers never `pgrep`). Status bar: `daemon:up`.

**Spawn lane, success:**
```
[pare-daemon not running; spawning...]
[pare-daemon ready — PID 12345, logs at ~/.local/state/pare/daemon-12345.log]
```
Status bar: `daemon:up`.

**Spawn lane, failure:**
```
[daemon spawn failed: <specific reason> — see <log path or "no log; captured tail below">]
[<up to 4KB of captured tail, when the log-open fallback fired>]
```
Status bar: `daemon:spawn-failed`.

Specific reasons the `<specific reason>` slot resolves to:
- `exited during startup, code <N>` — the Popen'd child ran and died before binding the socket.
- `did not accept a connection within Xs` — child is still alive but the socket isn't ready.
- `concurrent spawn timed out` — flock contention persisted past `startup_timeout`.
- `<errno name>: <errno string>` — for cases like EACCES on the socket_path parent, or ENOSPC on the log dir.

**Status bar** gains one visible state (`daemon:spawn-failed`); `daemon:up` and `daemon:DOWN` render identically to today. The bar's daemon column is one of the operator's primary at-a-glance signals — the new state is what makes it actionable ("read the log") vs. the existing state ("maybe transient, maybe not").

---

## 7. Testing strategy

### 7.1 Unit tests (new file `tests/test_daemon_lifecycle.py`)

Seven cases against `detect_or_spawn`:

1. **Attach lane** — bind a real `AF_UNIX` socket at a temp path, call `detect_or_spawn`. Assert `mode == "attached"`, PID is None, `_spawn_hook` never invoked. Discriminating: bug that makes the code spawn when a socket is already alive would fail this.
2. **Stale-socket reap** — pre-create a socket file at a temp path WITHOUT a listener. Assert the sequence: connect fails → path exists → unlink → `_spawn_hook` fires. Assert the unlinked path was the exact stale one.
3. **Absent-socket spawn** — no file at the temp path. `_spawn_hook` stub binds a real socket after 100ms. Assert `mode == "spawned"`, `pid` returned, `log_path` returned. Total elapsed under 500ms.
4. **Spawn exits during startup** — `_spawn_hook` Popens `sh -c 'exit 1'`. Assert `mode == "failed"`, error names "exited during startup", log_path is a real file.
5. **Spawn times out** — `_spawn_hook` Popens `sleep 30`. Assert `mode == "failed"` within `startup_timeout + slack`, error names "did not accept a connection", the sleep child has been SIGTERM'd (poll() returns non-None inside 1s).
6. **Concurrent-spawn flock contention** — two threads call `detect_or_spawn` on the same socket_path. Assert exactly one spawn (`_spawn_hook` invoked exactly once). The other returns `attached` after the flock releases.
7. **Log-open failure fallback** — pass an unwritable `log_dir`. Assert spawn still runs (using PIPE), first 4KB of the child's stderr comes back in the failure record. Discriminating: silently discarding the tail would fail the assertion.

### 7.2 Integration test (new case in `tests/test_tui_app_integration.py`)

- `test_transcript_receives_spawn_status_line` — mount `PareTUI` against a `MockDaemonLifecycle` returning `mode="spawned", pid=12345, log_path=<tmp>` synchronously. Assert the transcript's RichLog content contains `spawned pare-daemon PID 12345` and `logs at <tmp>`. Assert status bar reads `daemon:up`. Mutation-verified by flipping mode to `failed` and re-asserting the status bar reads `daemon:spawn-failed`.

### 7.3 What's deliberately un-tested

- **SIGKILL escalation timing.** Test 5 covers the SIGTERM path. Mocking "child ignores SIGTERM" needs a signal-eating subprocess and gets flaky. A code-review of the escalation branch stands in for a test.
- **Orphaned-daemon-reclamation on next launch.** Covered by composition of test 1 (attach lane) and test 2 (stale-socket reap). Not a distinct case.
- **Textual UI rendering beyond string-content assertion.** Textual has its own snapshot testing; not this spec's layer.

### 7.4 Suite target

481 passed / 3 skipped today → 488 passed / 3 skipped after this spec (7 unit tests + 1 integration case).

---

## 8. Rollback

- **Full rollback:** revert the PR that lands this spec's implementation. `pare-tui` returns to today's launch behavior (no auto-spawn; operator runs `pare-daemon` in another shell). No lingering state — the `~/.local/state/pare/` dir and any leftover log files are harmless.
- **Partial rollback:** feature-flag via env var (`PARE_TUI_NO_AUTO_SPAWN=1`) — if a specific operator hits an edge case, they can disable the spawn lane without a code change. Detect-lane still runs; failure falls through to today's behavior. Not strictly required to ship, but the flag costs nothing.

---

## 9. Not in scope

- **PARE grows a heartbeat / long-lived duties.** If the daemon later gains background responsibilities that should outlive any single TUI (arcticbase publishing, keepalive contracts with pare-hardware-mcp, scheduled workers), the "TUI owns, kills on exit" call from D2 becomes wrong. Fix at that time: switch to shared/PID-file ownership. Isolated to `daemon_lifecycle.py`'s spawn/cleanup logic — everything else in this design (detect probe, log file shape, transcript surfaces) survives.
- **In-session daemon crash recovery.** Detected today (`_on_daemon_message` sets `_daemon_connected = False`), rendered today (status bar), but NOT auto-respawned. Belongs to the heartbeat spec above.
- **`pare-daemon stop` command / clean shutdown protocol.** If a persistent daemon ever ships, there needs to be a way to stop it that isn't `kill`. Not this spec.
- **Log rotation / cleanup.** Old `daemon-<PID>.log` files accumulate in `~/.local/state/pare/`. First operator complaint drives a `--keep-logs=N` flag or a periodic sweep.
- **Non-Linux platforms.** `fcntl.flock`, `start_new_session`, `os.killpg` are Linux-idiomatic. PARE is Linux-only today; spec explicitly commits to that.
- **Multi-machine daemon spawning.** Spawn is same-host only.
- **Configurable spawn command.** Hardcoded `["pare-daemon"]`; respects PATH. Operators wanting custom flags start the daemon manually and use the attach lane.
- **Direction 1: interactive UART pane.** Separate spec after this one ships.

---

## 10. Code in this document

None, deliberately. This is a lifecycle + concurrency design (Popen ordering, flock semantics, signal propagation, socket-state transitions) — exactly the class where CLAUDE.md says code in prose is unexecuted, remote from real files, and authoritative-looking enough to be transcribed past defects. The plan lands the code against `pare/tui/daemon_lifecycle.py` and its neighbors, with real imports and real tests running.

---

## Correction 2026-09-26 (supersedes §3, §4 step 1 and step 2a, §4.1 lock location)

Task 1's review found that "`connect()` refused means stale" is wrong for this daemon. agent_core's `DaemonServer.serve` (`agent_core/daemon.py`) binds the socket and deliberately defers `listen()` until `agent.astartup()` returns, so a *starting* daemon refuses connections too. It also unlinks the socket path unconditionally before binding. On agenthost `pare-daemon` runs as a user systemd unit with `Restart=always`, so a TUI launched during a restart would have unlinked the starting daemon's socket and spawned a competitor. The first daemon would keep running with its workers loaded, and nothing could reach it.

Corrected rule:
- **Refused, and the path IS listed in `/proc/net/unix`:** a daemon has bound but not yet started listening, so it is starting. Do not spawn and do not unlink. Poll `connect()` up to `startup_timeout`. Success means `attached`; timeout means `failed` ("daemon at <path> is starting but did not accept connections within Xs").
- **Refused, and the path is NOT listed in `/proc/net/unix`:** stale leftover. Reap under the flock and spawn, as before.
- The same distinction is re-checked under the flock, immediately before any unlink.

The lock file moves from `log_dir` to `socket_path.parent / "pare-spawn.lock"`, next to the socket it guards. That keeps an unwritable `log_dir` on the §4.1 pipe fallback instead of failing the lock.

`reap()` signals only PIDs this process spawned (tracked in the module); any other PID is a no-op returning `"not_owned"`. The pgid guard alone does not protect against PID reuse.
