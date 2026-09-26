# Daemon auto-spawn for `pare-tui`

**Status:** living design. Implemented. This document is edited in place to match the shipped code; where the two disagree, the code is authoritative and this document is the bug. Changes are listed in the revision history at the end.
**Code:** `pare/tui/daemon_lifecycle.py`, `pare/tui/app.py`, `pare/tui/widgets/statusbar.py`. Tests: `tests/test_daemon_lifecycle.py`, `tests/test_tui_app_integration.py`.
**Historical:** the implementation plan [`../plans/2026-09-20-daemon-auto-spawn.md`](../plans/2026-09-20-daemon-auto-spawn.md) was executed on 2026-09-26 and is kept as a record only. It is not a source of truth.
**Repos touched:** PARE only.
**Related:** [`2026-09-18-pare-tui-design.md`](2026-09-18-pare-tui-design.md), [`2026-09-20-bench-integration-design.md`](2026-09-20-bench-integration-design.md).

The motivating problem: before this change, launching `pare-tui` with no `pare-daemon` running gave the operator a UI whose UART pane worked (its source connects directly to the deployed worker) and whose chat did not. `/worker list` failed, and the status bar read `daemon:DOWN` in a way most operators missed until they typed something and saw `[send failed: ...]`. The user's reaction on 2026-09-20, after the first live launch: "it worked! I couldn't chat or anything but it worked."

The chain was: `PareTUI.__init__` built a `DaemonSession(socket_path, ...)`, `on_mount` awaited `session.start()`, `start()` raised on a refused connection, and the `except` block called `logger.exception` (stderr, invisible under Textual) and left `_daemon_connected = False`. No hint and no way to recover from the UI.

This design adds an auto-spawn lane. If no daemon is running and the host does not supervise one, the TUI spawns one and kills it on exit. If one is running, the TUI attaches to it and does not touch it on exit.

---

## 1. Decisions

| | Decision | Why |
|---|---|---|
| **D1** | `pare-tui` auto-spawns `pare-daemon` on launch if none is running, unless the host supervises the daemon (D7) or the operator opted out (`PARE_TUI_NO_AUTO_SPAWN=1`, §8). | The operator's model of a chat-capable TUI is that chat works when the TUI is open. A second shell for `pare-daemon` is friction the user named on the first live launch. |
| **D2** | The TUI owns the daemon it spawned and kills it on TUI exit. Later TUIs attach and never kill. Only the spawner reaps. | Simplest lifecycle. Consequence named in §9: attached TUIs lose the daemon when the spawner quits. |
| **D3** | Attach if a daemon is already accepting connections (`connect()` succeeds). | Non-competitive: attachers never reap. |
| **D4** | A stale socket file is removed only inside the spawn path, only under an `flock`, and only after the kernel confirms nothing is bound to it (§3). The lock is `pare-spawn.lock` in the socket's directory. | A crashed daemon leaves its socket file behind; without reaping we never recover. But a refused `connect()` alone does not mean stale (D8). The lock sits beside the socket it guards. |
| **D5** | The spawned daemon's stdout and stderr go to a per-spawn log `daemon-<PID>.log` in the TUI's log directory (default `~/.local/state/pare/`). The transcript names the log. If the log cannot be opened, output goes to a pipe and is returned in the failure message. | Post-mortem needs a file, not stderr swallowed by Textual. Filesystem trouble must not discard the diagnostic. |
| **D6** | Three status-bar states: `daemon:up`, `daemon:DOWN`, `daemon:spawn-failed`. | `spawn-failed` (we tried; read the log) is actionable in a way `DOWN` is not. |
| **D7** | On a host where `systemctl --user is-enabled pare-daemon` succeeds, never spawn. If `systemctl`'s answer is unknown, also never spawn (fail closed). | agent_core unlinks and rebinds the socket on every daemon start, so a systemd restart would orphan a TUI-spawned daemon. User ruling C5, 2026-09-26. |
| **D8** | A refused `connect()` means *stale* only if the kernel no longer lists a socket bound to that path in `/proc/net/unix`. If the path is listed, a daemon is starting: wait for it, never spawn, never unlink. If `/proc/net/unix` can't be read, assume starting. | agent_core's `Daemon.serve` (`agent_core/daemon.py`) binds with `asyncio.start_unix_server(..., start_serving=False)` and defers `listen()` until `astartup()` returns, so a starting daemon refuses connections. On agenthost the daemon runs under a user unit with `Restart=always`, so treating refused as stale would unlink a restarting daemon's socket and spawn a competitor. |

### What this does NOT change

- **`pare-daemon` itself.** No change to the daemon's code, config surface or startup semantics.
- **The status bar's `daemon:up` and `daemon:DOWN` labels.** Only a third state is added.
- **In-session recovery.** If the daemon dies mid-session, `_on_daemon_message` handles `DaemonDisconnected` as before (bar goes to `daemon:DOWN`). Nothing respawns it.
- **Non-Linux support.** `fcntl.flock`, `start_new_session=True`, `os.killpg` and `/proc/net/unix` are Linux-only; PARE is Linux-only.

---

## 2. Architecture

### 2.1 Launch lanes

`main()` in `pare/tui/app.py` calls `load_config()`, then `launch_policy(os.environ)`, then constructs `PareTUI(config.socket_path, <fresh channel id>, os.getcwd(), auto_spawn=policy.auto_spawn, systemd_managed=policy.systemd_managed)` and runs it. `main()` is the only code that constructs `PareTUI` with an `auto_spawn` that can be True (it passes `policy.auto_spawn`). The constructor defaults are `auto_spawn=False`, `systemd_managed=False`, so any other construction (every test) never detects, spawns or reaps.

`launch_policy(env, run=subprocess.run) -> LaunchPolicy(auto_spawn: bool, systemd_managed: bool)`:

1. Always runs `systemctl --user is-enabled pare-daemon` first (stdin/stdout/stderr to `/dev/null`, `timeout=5`, `check=False`).
   - Exit 0: `LaunchPolicy(auto_spawn=False, systemd_managed=True)`. This wins even when the opt-out variable is set.
   - `systemctl` binary missing (`FileNotFoundError`): a definite "no unit".
   - Any other non-zero exit: a definite "no unit" (see §9 for the cost of this).
   - Any other `OSError` or `subprocess.SubprocessError` (including the 5s timeout): answer unknown. Logged with `logger.exception`.
2. If `env["PARE_TUI_NO_AUTO_SPAWN"] == "1"` (exactly the string `1`) or the answer was unknown: `LaunchPolicy(auto_spawn=False, systemd_managed=False)`.
3. Otherwise `LaunchPolicy(auto_spawn=True, systemd_managed=False)`.

Three lanes follow from the policy:

| Lane | Policy | What `on_mount` does |
|---|---|---|
| **Systemd** | `systemd_managed=True`, `auto_spawn=False` | Awaits a plain `session.start()`. On success writes the attach line. On failure writes `SYSTEMD_ATTACH_FAILED` (§6). Never calls `detect_or_spawn`. No wait for a starting daemon and no reconnect (§9). |
| **Plain attach** (opt-out, or `systemctl` answer unknown) | both False | Awaits a plain `session.start()`. On success writes the attach line; on failure writes nothing and the bar reads `daemon:DOWN`. Never calls `detect_or_spawn`. |
| **Auto-spawn** | `auto_spawn=True` | Installs the SIGHUP handler (§5.3), then starts a background task (§2.4) that resolves the spawn directory, runs `detect_or_spawn` on a thread, then connects. `on_mount` returns at once. |

### 2.2 Files and signatures

- **`pare/tui/daemon_lifecycle.py`** — stdlib only; no Textual, no asyncio. Both public functions block; the app runs them on a worker thread. Exports:
  - `@dataclass(frozen=True) class DaemonSpawnResult` with `mode: Literal["attached", "spawned", "failed"]`, `pid: int | None`, `log_path: Path | None`, `error: str | None` (None unless `mode == "failed"`). `pid` is set only for `spawned`.
  - `detect_or_spawn(socket_path: Path, log_dir: Path, *, connect_timeout: float = 0.25, startup_timeout: float = 5.0, lock_dir: Path | None = None, spawn_cmd: list[str] | None = None, spawn_cwd: Path | None = None, _spawn_hook: SpawnHook | None = None) -> DaemonSpawnResult`. `spawn_cmd=None` means `["pare-daemon"]`; the app always passes the result of `_daemon_command()` (§4.4).
  - `reap(pid: int, *, sigterm_timeout: float = 5.0, sigkill_timeout: float = 2.0) -> Literal["exited_clean", "killed", "abandoned", "not_owned"]`.
  - `SpawnHook = Callable[[list[str], dict[str, Any]], subprocess.Popen]` — a test seam called as `hook(cmd, popen_kwargs)`, where `popen_kwargs` carries exactly the production `stdin`, `stdout`, `stderr`, `start_new_session`, `env` and (when set) `cwd`. Production never passes it.
- **`pare/tui/app.py`** — `SYSTEMD_ATTACH_FAILED`, `_daemon_command()`, `SpawnCwd` / `resolve_spawn_cwd()`, `LaunchPolicy` / `launch_policy()`, and the `PareTUI` wiring (`on_mount`, `_spawn_then_connect`, `_detect_or_spawn_daemon`, `_record_spawn`, `on_unmount`, `_reap_owned_daemon`, the SIGHUP handler, `_refresh_status_bar`).
- **`pare/tui/widgets/statusbar.py`** — `DaemonState = Literal["up", "down", "spawn-failed"]` and the `daemon_state` field (§6.2).

### 2.3 State on the app

`PareTUI.__init__(socket_path, channel_id, cwd, *, auto_spawn=False, daemon_log_dir=None, systemd_managed=False)`. `daemon_log_dir=None` means `Path.home() / ".local" / "state" / "pare"`; it is only used in the auto-spawn lane.

Lifecycle fields: `_daemon_owned_pid` (set only from a `spawned` result; the only PID ever reaped), `_daemon_spawn_failed` (drives the `spawn-failed` bar state), `_daemon_connect_task` (the background task), `_daemon_spawn_future` (the executor future running `detect_or_spawn`), `_spawn_recorded` (makes `_record_spawn` run once per future), `_sighup_installed`. No PID file and no global app state: the owned PID lives in the app's memory for exactly the time the app owns it. (`daemon_lifecycle` keeps a module-level table of the children it spawned; see §5.2.)

### 2.4 The background spawn task

`detect_or_spawn` blocks for up to its timeouts (about 1.3s on a normal real start, §4). Awaited inside `on_mount` it held the app unready — nothing painted, no key handled, not even quit — so the auto-spawn lane runs as a task created in `on_mount`, and `on_mount` returns immediately. The bar reads `daemon:DOWN` while the task runs.

`_spawn_then_connect`:

1. `resolve_spawn_cwd(os.environ)` (§4.3). If it returns an error, do not spawn: still try a plain `session.start()`, and only if that fails write `[daemon spawn failed: <error>]` and set the bar to `spawn-failed`. A daemon that is already running is attached normally.
2. Otherwise `_detect_or_spawn_daemon(cwd)`: write the "checking" line, submit `detect_or_spawn(Path(socket_path), daemon_log_dir, spawn_cmd=_daemon_command(), spawn_cwd=cwd)` with `loop.run_in_executor`, attach `_record_spawn` as a done-callback, store the future on `_daemon_spawn_future`, and await it through `asyncio.shield` so cancelling the task never cancels the future (a cancelled executor future drops its result, which here would be the PID of a daemon we just started).
3. Then `session.start()`.
4. Any exception escaping steps 1-3 is logged, written as `[daemon spawn failed: <ExceptionType>: <message>]`, and sets `spawn-failed`, so the UI never sits on "checking" forever. `CancelledError` still propagates.

---

## 3. The detect step

Inside `detect_or_spawn`, before any lock:

1. `connect()` an `AF_UNIX` stream socket to `socket_path` with a `connect_timeout` (0.25s) timeout, then close it.
   - **Success:** `attached`.
   - **`FileNotFoundError`:** no socket; go to the spawn step.
   - **`ConnectionRefusedError`:**
     - If something other than a socket sits at the path (`lstat` is not `S_ISSOCK`): `failed`, `socket_path is not a socket: <path>`. `connect()` to a regular file or directory is also refused, so the refusal alone must never license an unlink.
     - Otherwise check `/proc/net/unix`. If the path is **listed** (a daemon has bound but not listened) or the file **can't be read** (fail closed): wait for a starting daemon (below). Never spawn, never unlink, no lock needed.
     - If the path is **not listed**: stale; go to the spawn step.
   - **`PermissionError`:** `failed`, `permission denied: <path>`. Never spawn.
   - **Any other `OSError`:** `failed`, `<ExceptionType>: <message>`. Never spawn.

**`/proc/net/unix` matching.** Each line after the header has seven fixed columns and then the bound path, which may contain spaces, so the line is split at most seven times and the eighth field compared byte-for-byte against the socket path (as given and as `os.path.abspath`). Abstract sockets appear as `@name` and never match. A dead daemon's leftover file is not listed; a daemon between `bind()` and `listen()` is.

**Waiting for a starting daemon.** Poll `connect()` every 100ms until `startup_timeout` (5s). Success: `attached`. Timeout:
- path was listed: `failed`, `daemon at <path> is starting but did not accept connections within <N>s`;
- `/proc/net/unix` unreadable: `failed`, `daemon at <path> may be starting (could not read /proc/net/unix) and did not accept connections within <N>s; not removing it`.

`<N>` is `startup_timeout` formatted with `:g` (so `5`, not `5.0`).

---

## 4. The spawn step

Reached only when the detect step found no socket or a stale one.

1. `mkdir -p` the log directory with mode 0700. Failure is ignored here; it just means the log open in step 5 fails and the pipe fallback (§4.1) is used.
2. **Lock.** The lock file is `pare-spawn.lock` in `lock_dir` if given, else in `socket_path.parent` — beside the socket it guards. With the default socket path (`$XDG_RUNTIME_DIR/pare.sock`, from agent_core's config) that is `/run/user/<uid>/pare-spawn.lock`. The directory is created with mode 0700 if needed, the file opened `O_CREAT | O_WRONLY` with mode 0600, and a non-blocking exclusive `flock` taken.
   - File can't be opened: `failed`, `cannot open spawn lock <lock path>: <ExceptionType>: <message>`. Without the lock nothing may be unlinked, so this is a failure, not a reason to spawn unserialized.
   - `flock` fails with anything other than contention: `failed`, `cannot lock <lock path>: <ExceptionType>: <message>`.
   - Contended: another process or thread is spawning. Never reap, never spawn: poll `connect()` every 100ms until `startup_timeout`; success is `attached`, timeout is `failed`, `concurrent spawn timed out`.
   - The lock is released and its fd closed on every return path. The fd is non-inheritable and Popen closes fds, so the daemon never holds it.
3. **Under the lock, probe again.** A spawner that finished between our first probe and our lock is live, not stale: `connect()` success means `attached`. `FileNotFoundError` means proceed. `ConnectionRefusedError` repeats the §3 checks immediately before the unlink — not-a-socket is `failed`; listed or unreadable waits for a starting daemon — because the systemd unit never takes our lock and may have bound since our first probe. Only when the lock is held, a fresh connect was just refused, the path is a socket, and the kernel does not list it, is the path unlinked. An unlink error other than `FileNotFoundError` is `failed`, `cannot remove stale socket <path>: <ExceptionType>: <message>`. `PermissionError` and other `OSError`s fail as in §3.
4. **Temp log.** Open `<log_dir>/daemon-spawning-<uuid4 hex>.log` for writing. If a stale socket was just removed, its first line is `[pare-tui] reaped stale socket at <path> before spawn`.
5. **Popen** `spawn_cmd` with:
   - `stdin=DEVNULL`; `stdout` = the temp log (or `PIPE` if it couldn't be opened); `stderr=STDOUT`;
   - `start_new_session=True`, so the child leads its own process group and session and gets no hangup from the TUI's terminal;
   - `env` = a copy of `os.environ` with `PARE_SOCKET_PATH` set to `str(socket_path)`, so the path the TUI polls and the path the daemon binds are the same by construction;
   - `cwd=spawn_cwd` when it is not None (§4.3); otherwise the child inherits the TUI's cwd.

   A Popen `OSError` (for example `FileNotFoundError` for a missing command) removes the temp log and is `failed`, `<ExceptionType>: <message>`. On success the child's `Popen` object is recorded in the module's owned-children table (§5.2), and the temp log is renamed to `<log_dir>/daemon-<pid>.log` (if the rename fails, the temp name is reported).
6. **Poll** every 100ms until `startup_timeout`:
   - `connect()` succeeds: `spawned` with the PID and log path. In the pipe fallback a daemon thread keeps draining the child's stdout so a full pipe never blocks it.
   - The child exited: `failed`, `exited during startup, code <N> -- <where>`, with the log path in `log_path`.
   - Timeout with the child alive: signal its group SIGTERM, wait 2s, SIGKILL, wait 2s; `failed`, `did not accept a connection within <N>s -- <where>`.

   `<where>` is `see <log path>`, or `no log; captured tail below` followed by a newline and the captured output, or `no log; no output captured`. The separator in these module messages is two ASCII hyphens (`--`).

**Measured startup.** The real `pare-daemon` on agenthost takes about 1.3s from spawn to accepting connections (measured three times on 2026-09-26, and again from `/tmp` after the spawn-cwd fix). The default `startup_timeout` of 5s leaves room for that.

### 4.1 Log-open fallback

If the temp log can't be opened (log directory unwritable, disk full), the child's stdout and stderr go to one pipe. On a failure path, up to the **first** 4KB already written are read without blocking (a grandchild holding the write end must not hang us) and appended to `error` after `no log; captured tail below` and a newline. The app writes the text before that newline as the failure line and everything after it as its own `[...]` line. The success path still reports `spawned`, with `log_path=None`, and the ready line says `logs at none (log dir unwritable)`.

### 4.2 Concurrent-spawn safety

Two TUIs racing against the same socket path never both spawn: the `flock` serializes them, the loser polls, and it attaches when the winner's daemon accepts connections. A waiter never unlinks and never spawns. The loser does not retry the lock if the winner fails (§9).

### 4.3 Spawn working directory

`pare-daemon` loads `workers_yaml_path`, whose default is the relative `"workers.yaml"` (`pare/config.py`), against its cwd. The systemd unit pins `WorkingDirectory` to the repo root; a TUI-spawned daemon that inherited the operator's cwd would exit at startup anywhere else. So `resolve_spawn_cwd(env, repo_root=None) -> SpawnCwd(cwd: Path | None, error: str | None)` picks the child's cwd, with `repo_root` defaulting to `Path(pare.__file__).resolve().parents[1]` (the checkout under an editable install):

1. `PARE_WORKERS_YAML_PATH` set and non-empty: `SpawnCwd(cwd=None, error=None)` — the operator's choice; the child inherits the TUI's cwd and the variable through its environment.
2. `<repo root>/workers.yaml` is a file: `SpawnCwd(cwd=<repo root>, error=None)` — run where the systemd unit runs.
3. Neither: `SpawnCwd(cwd=None, error="workers.yaml not found in <repo root> — set PARE_WORKERS_YAML_PATH")`. The app does not spawn (§2.4 step 1). Under a non-editable install the root is `site-packages`, so this branch runs.

The daemon's cwd is only a fallback for messages that carry none; the TUI stamps its own launch cwd on every message.

### 4.4 Locating `pare-daemon`

`_daemon_command()` returns `[<dir of sys.executable>/pare-daemon]` if that file exists and is executable, else `["pare-daemon"]` for a `PATH` lookup. `.venv/bin/pare-tui` is routinely run without activating the venv, and then `pare-daemon` is not on `PATH` (checked on agenthost), so the bare name alone would fail every spawn.

---

## 5. Cleanup on TUI exit

### 5.1 `on_unmount`

1. If the background task is still running, cancel it and await it (errors swallowed).
2. `session.stop()`.
3. In a `finally`: `_reap_owned_daemon()`.
   - If a spawn future exists and is not done, await it through `asyncio.shield`. **Quitting mid-spawn therefore waits the spawn out**, so a daemon that finishes starting after the quit is still recorded and reaped rather than orphaned. Textual has already disabled input by then, so the screen is frozen for the wait (§9).
   - Call `_record_spawn(fut)` directly: asyncio runs done-callbacks one loop step after the result is set, so the callback may not have run yet. `_record_spawn` records only once per future, so a late callback cannot resurrect a PID that has already been reaped.
   - If a PID is owned, run `reap(pid)` on a thread with the default timeouts. `abandoned` is logged as a warning; any other outcome at info. An exception from `reap` is logged and never breaks the exit.
4. In an inner `finally`, last: remove the SIGHUP handler, so a second SIGHUP during the reap cannot kill the process.

Attach-lane and systemd-lane apps own nothing: `on_unmount` stops the session and reaps nothing.

### 5.2 `reap`

`reap` signals only a PID that `detect_or_spawn` spawned in this process and has not yet reaped. `daemon_lifecycle` keeps the `Popen` object of every child it spawns until it is reaped. Holding it keeps the child's zombie — and so its PID — ours until it is waited on, so the PID cannot have been reused by an unrelated process (it also stops `subprocess`'s own cleanup from reaping the child behind our back). Four return values:

- **`not_owned`** — the PID is not in the table (never ours, or already reaped). No signal is sent.
- **`exited_clean`** — the child had already exited, or exited within `sigterm_timeout` (5s) after SIGTERM. Because the daemon installs no SIGTERM handler, this is usually death by SIGTERM, not a clean shutdown.
- **`killed`** — it survived SIGTERM and exited within `sigkill_timeout` (2s) after SIGKILL.
- **`abandoned`** — it survived SIGKILL. It stays in the table.

Every group signal in the module (here and in the §4 timeout path) first checks that the target leads its own process group and that the group is not the TUI's own; otherwise only the PID itself is signalled. A mistake can never signal the TUI's process group.

`on_unmount` never unlinks the socket file (that would race the daemon's own cleanup). In practice the SIGTERM'd daemon leaves its socket behind every time, and the next auto-spawn launch removes it through §3-§4.

The daemon's MCP stdio workers run in their own sessions, so the group signal does not reach them; they exit on stdin EOF when the daemon dies.

### 5.3 SIGHUP

Closing the terminal or dropping SSH sends SIGHUP, whose default action kills the TUI without running `on_unmount` — and the spawned daemon, in its own session, gets no hangup, so it would be orphaned. In the auto-spawn lane only, `on_mount` installs a SIGHUP handler with `loop.add_signal_handler` that calls `self.exit()`, so the normal unmount-and-reap path runs. It is installed before the background task is created, so there is no window in which a spawn exists without the handler. It is skipped (not installed) when `signal.SIGHUP` doesn't exist, when not on the main thread, and when the current SIGHUP disposition is anything other than `SIG_DFL` — including `SIG_IGN` under `nohup` (where SIGHUP cannot kill us anyway) and a handler someone else installed. Removal restores `SIG_DFL`.

Only SIGHUP is handled. SIGTERM and SIGKILL to the TUI still orphan a spawned daemon (§9).

---

## 6. Visible feedback

### 6.1 Transcript lines

All lifecycle lines are bracketed. Exact text, with `<...>` substituted:

| When | Line |
|---|---|
| Auto-spawn lane, before `detect_or_spawn` runs (the lane is not known yet) | `[checking for pare-daemon at <socket path>; spawning one if it is not running...]` |
| `spawned` | `[pare-daemon ready — PID <pid>, logs at <log path>]`, or `logs at none (log dir unwritable)` |
| `session.start()` succeeded and we did not spawn (any lane, including an `attached` result) | `[attached to running pare-daemon at <socket path>]` |
| `failed` result | `[daemon spawn failed: <reason>]`, where `<reason>` is `error` up to its first newline; then, if there was more, `[<rest of error>]` as its own line |
| spawn directory unresolvable and nothing to attach to | `[daemon spawn failed: workers.yaml not found in <repo root> — set PARE_WORKERS_YAML_PATH]` |
| exception escaping the spawn task or `detect_or_spawn` | `[daemon spawn failed: <ExceptionType>: <message>]` |
| systemd lane, `session.start()` failed | `[systemd-managed pare-daemon is not accepting connections — systemctl --user status pare-daemon]` (`SYSTEMD_ATTACH_FAILED`) |

`<reason>` is one of the `detect_or_spawn` error strings in §3-§4: `permission denied: …`, `socket_path is not a socket: …`, `daemon at … is starting but did not accept connections within Ns`, `daemon at … may be starting (could not read /proc/net/unix) …`, `cannot open spawn lock …`, `cannot lock …`, `concurrent spawn timed out`, `cannot remove stale socket …`, `exited during startup, code N -- …`, `did not accept a connection within Ns -- …`, or `<ExceptionType>: <message>`. The app lines use an em dash (`—`); the module's messages use `--`.

No PID is shown when attaching: attachers do not know it.

Two paths write no line (§9): a spawned daemon whose `session.start()` then fails, and a plain-attach-lane `start()` failure.

### 6.2 Status bar

`StatusBar.daemon_state` is `"up"`, `"down"` or `"spawn-failed"`, rendered as `daemon:up`, `daemon:DOWN` and `daemon:spawn-failed`. Setting any other value raises `ValueError`. The rendered line is `<daemon label>  channel:<id or ->  <pane summaries>` (two spaces between fields).

The app computes the state in `_refresh_status_bar` (on every message and on a 1s timer): `up` if connected; else `spawn-failed` if `_daemon_spawn_failed`; else `down`. `_daemon_spawn_failed` is set by any `failed` result, the unresolvable-spawn-directory failure, or an exception in the spawn task, and cleared by a successful connect or any live message from the daemon. It is therefore also set by detect-only failures that attempted no spawn (§9).

**Compatibility.** `StatusBar` still accepts `daemon_connected: bool` as a constructor keyword and as a read/write property: reading is `daemon_state == "up"`; writing True/False sets `"up"`/`"down"`. If both `daemon_state=` and `daemon_connected=` are passed to the constructor, `daemon_connected` wins. The app no longer uses it; one test does.

---

## 7. Testing

Run `.venv/bin/pytest tests/test_daemon_lifecycle.py tests/test_tui_app_integration.py -q` for these tests and their current count; `.venv/bin/pytest -q` for the whole suite, which should be green. This document deliberately states no counts.

### 7.1 Safety properties every test keeps

- No test spawns the real `pare-daemon` or touches the real socket: every `detect_or_spawn` call passes a stand-in `spawn_cmd` or `_spawn_hook` and binds in a short-lived `/tmp/pdl-*` directory; every app test that enables auto-spawn replaces both `detect_or_spawn` and `reap` at `pare.tui.app`.
- Every path that can reach a group signal is driven by a real child started with `start_new_session=True`; fake PIDs never reach the real `reap`.
- No test writes the real `~/.local/state/pare/`, and none calls the real `systemctl`.

### 7.2 `tests/test_daemon_lifecycle.py` (the module, with real sockets and real child processes)

- A listening socket is attached without spawning.
- Permission denied, and a regular file at the socket path, fail without spawning, and the file is not removed.
- A stale socket is removed before the child is started, and the log records the removal.
- A spawn from an absent socket starts the child in its own session, renames the log to `daemon-<pid>.log`, leaves no temp log, and passes `PARE_SOCKET_PATH` equal to the polled path — through both the hook and the production Popen path.
- A child that exits during startup fails with its exit code and log path; a child that never listens fails after `startup_timeout` and has been SIGTERM'd.
- An unwritable log directory still spawns on a pipe, puts the lock beside the socket, and returns the child's output in `error`.
- An unopenable lock fails without removing a stale socket.
- A contended lock waits and attaches, or times out with `concurrent spawn timed out` without removing anything; the default lock lives beside the socket; a daemon that came up between probe and lock is attached, not duplicated; the lock is released after every outcome; two concurrent callers produce exactly one spawn.
- A bound-but-not-listening socket is never removed or competed with: it fails with the "is starting" message on timeout and attaches if it starts listening in time; the check is repeated under the lock (a lock-less starter binding after the first probe is caught); an unreadable `/proc/net/unix` fails closed; waiting on a starting daemon needs no lock.
- `/proc/net/unix` matching is exact: spaces in the path match; prefixes, suffixes, abstract names and closed leftovers do not.
- `reap` SIGTERMs an owned child (`exited_clean`), escalates to SIGKILL for one that ignores SIGTERM (`killed`), treats an already-exited owned child as `exited_clean`, returns `not_owned` for a second reap and for a PID it did not spawn (which is left running), and never group-signals a child that shares the caller's process group.
- `spawn_cwd` is the child's real working directory; without it the child inherits the caller's.

### 7.3 `tests/test_tui_app_integration.py` (the app, with `detect_or_spawn`/`reap` replaced)

- `spawned`: the ready line names the PID and log path, the bar is `up`, and the PID is reaped on exit and not before.
- `attached`: the attach line is written, the bar is `up`, nothing is reaped.
- `failed`: the reason is written, `session.start()` is still tried, the bar is `spawn-failed` and stays so on the timer refresh; a multi-line error puts its tail on its own line.
- Without `auto_spawn` nothing is detected or spawned and the bar is `down`; the systemd lane writes `SYSTEMD_ATTACH_FAILED` and never detects.
- Quitting during an in-flight spawn still reaps the PID it produces; a spawn future resolved before its callback ran is reaped exactly once; a failing `reap` does not break exit; an exception inside the spawn task is written and sets `spawn-failed`.
- `launch_policy`: an enabled unit disables auto-spawn and marks the systemd lane (even with the opt-out set); a non-zero `is-enabled` or a missing `systemctl` auto-spawns; `PARE_TUI_NO_AUTO_SPAWN=1` disables it; a hung `systemctl` fails closed. `main()` passes the policy through.
- `_daemon_command` prefers the interpreter's sibling and falls back to the bare name; the resolved command and the resolved spawn cwd reach `detect_or_spawn`.
- `resolve_spawn_cwd`'s three branches, including the default repo root; an unresolvable spawn directory fails without spawning but still attaches to a running daemon.
- SIGHUP: the handler is registered in the auto-spawn lane and calls `exit()`; it is never installed over an existing disposition; after exit SIGHUP is back to `SIG_DFL` on a real loop. These tests set the disposition they need, so they pass under an inherited `SIG_IGN`.
- `StatusBar`: `spawn-failed` renders differently from `up`/`down`; the `daemon_connected` compatibility property maps to `up`/`down`.

### 7.4 Checked live, not in the suite

- The real `pare-daemon`'s startup time (about 1.3s) and a spawn from `/tmp` with and without a resolvable `workers.yaml`.
- `main()` on agenthost taking the systemd lane and attaching (bar `daemon:up`), with the systemd daemon untouched.
- A real-pty hangup reaping the spawned daemon, and a mutation (handler disabled) orphaning it.

### 7.5 Not tested

- The real `systemctl` call and its 5s timeout.
- The SIGKILL escalation inside `detect_or_spawn`'s startup-timeout path (the `reap` escalation is tested).
- The quit-mid-spawn test's 0.3s release timer can, on a very slow runner, exercise the already-recorded path instead of the in-flight one.
- Textual rendering beyond transcript and state assertions. The transcript spy reads Textual's private `RichLog._size_known`.

---

## 8. Rollback

- **Per operator, no code change:** `PARE_TUI_NO_AUTO_SPAWN=1`. The TUI then never calls `detect_or_spawn` at all — no detect, no wait for a starting daemon, no spawn, no SIGHUP handler — and does a plain `session.start()` exactly as before this feature (plain-attach lane, §2.1). The `systemctl` check still runs first, so on a host with an enabled unit the systemd lane and its transcript line still apply.
- **Full:** revert the implementation. `pare-tui` returns to plain attach. Leftover `daemon-<PID>.log` files and `pare-spawn.lock` are harmless.

---

## 9. Known limitations and not in scope

### 9.1 Known limitations

Orphaned daemons:
- **SIGTERM, SIGKILL or a crash of the TUI orphans a spawned daemon.** Only SIGHUP is turned into a normal exit. `kill <tui pid>`, `timeout`, logind's `KillUserProcesses`, or an uncaught crash skips `on_unmount`; the daemon, in its own session, keeps running reparented to init. Later launches attach to it and never reap it. Its environment is frozen at whatever shell started it.
- **The "is starting" message is also shown for a socket still bound by a daemon that is shutting down, or by an orphan.** Any socket listed in `/proc/net/unix` counts as starting. A listed socket whose owner never listens again — for example an orphan whose path was taken over and later released by another daemon — makes every later auto-spawn launch fail with `is starting but did not accept connections` until the holder is killed. The failure is safe (nothing is removed), but the message does not say who holds the socket.

Exit:
- **Quitting mid-spawn can freeze the screen for up to about 12.5s.** `on_unmount` waits for the in-flight spawn and then the reap, with input already disabled. The two slow paths are exclusive: a spawn that becomes ready just inside `startup_timeout` (5s, plus up to ~0.35s of probe/poll overshoot) is then reaped (up to 5s SIGTERM + 2s SIGKILL wait) ≈ 12.4s; a spawn that times out is killed inside `detect_or_spawn` (2s + 2s after the 5s wait) ≈ 9s and leaves nothing to reap. The typical wait is the ~1.3s real startup.
- **SIGTERM kills the daemon abruptly and leaves a stale socket.** Neither `pare-daemon` nor agent_core handles SIGTERM, so `reap`'s SIGTERM ends the daemon without `ashutdown()` and the socket file stays behind. Every exit after a spawn does this; the next auto-spawn launch removes it via §3-§4, which makes the stale-socket path the normal one. (A systemd-lane or plain-attach launch does not remove it, but also doesn't need to: the next daemon start unlinks and rebinds.)

Attached TUIs:
- **Attached TUIs lose the daemon silently when the TUI that spawned it quits.** They flip to `daemon:DOWN` with no transcript line. This follows from D2.
- **Silent connect failures.** A spawned daemon whose `session.start()` then fails, and a plain-attach-lane `start()` failure, write no transcript line; the bar reads `daemon:DOWN`.

Systemd lane:
- **The systemd lane doesn't wait for or reconnect to a starting daemon.** It never calls `detect_or_spawn`, so a launch during a unit restart — the ~1.3s bound-not-listening window, or the `RestartSec=5` gap with a stale file — gets `SYSTEMD_ATTACH_FAILED` and `daemon:DOWN` for the whole session, although the unit is healthy seconds later.
- **All non-zero `is-enabled` exits count as "no unit".** Exit 1 from "Failed to connect to bus" (for example under `su` or `sudo -u` without a user session) is treated like "not-found", so auto-spawn can be enabled next to a supervised daemon. D8's `/proc/net/unix` check covers the bound-not-listening half of the restart window.
- **A hung `systemctl` writes no transcript line.** The launch silently takes the plain-attach lane.
- **A few-syscall race against the systemd daemon.** The systemd-managed daemon never takes our lock. Between the under-lock `/proc/net/unix` check and the `unlink`, a restarting unit can unlink and rebind the path, and we would remove its fresh socket. The window is a few syscalls wide. Closing it needs an agent_core change (the daemon taking the same lock, or equivalent).

Detect and spawn:
- **A contended waiter never retries the lock.** If the spawner fails fast, the other TUI still polls for the full `startup_timeout` and reports `concurrent spawn timed out`, although it could have spawned.
- **An unresolvable spawn directory skips the concurrent-spawn wait.** A second TUI launched while another is mid-spawn fails its attach at once and shows `spawn-failed`.
- **The pipe fallback returns the head, not the tail.** It returns the first 4KB but labels it `captured tail below`; a traceback after a chatty startup can fall outside it. This path runs only when the log directory is unwritable.
- **`spawn-failed` also labels detect-only failures** (`is starting …`, `concurrent spawn timed out`, `permission denied`, `not a socket`) where nothing was spawned, and the bar reads `daemon:DOWN`, not a "spawning" state, while the spawn is in flight.
- **An empty `PARE_WORKERS_YAML_PATH`** is treated as unset by `resolve_spawn_cwd` but as set (to `""`) by agent_core's config loader, so the daemon spawns and then fails to load `""`. The failure is loud.
- **A socket path containing a newline** is never matched in `/proc/net/unix`, so a starting daemon at such a path would be treated as stale.
- **Non-default socket paths in shared directories** (for example `/tmp`) let another user pre-create `pare-spawn.lock` and block spawning. The default directory, `$XDG_RUNTIME_DIR`, is private.
- **`reap` is not guarded against two concurrent calls for the same PID.** The app reaps once per spawn.

### 9.2 Not in scope

- **Long-lived daemon duties.** If the daemon gains responsibilities that should outlive any one TUI (ArcticBase publishing, keepalives with pare-hardware-mcp, scheduled workers), D2's "TUI owns, kills on exit" becomes wrong; switch to shared or PID-file ownership then. The change is isolated to the spawn and cleanup logic.
- **In-session crash recovery.** A daemon that dies mid-session is shown (`daemon:DOWN`) but not respawned.
- **A `pare-daemon stop` command or clean shutdown protocol.**
- **Log rotation.** `daemon-<PID>.log` files accumulate in the log directory.
- **Non-Linux platforms.**
- **Multi-machine spawning.** Spawn is same-host only.
- **A configurable spawn command.** The command is `pare-daemon` beside the interpreter, else on `PATH` (§4.4). Operators who want custom flags start the daemon themselves and use the attach lane.
- **Direction 1: interactive UART pane.** A separate spec.

---

## 10. Code in this document

None, deliberately. This is a lifecycle and concurrency design (Popen ordering, flock semantics, signal propagation, socket-state transitions) — the class where code in prose is unexecuted, remote from the real files, and authoritative-looking enough to be transcribed past defects. Signatures and exact user-visible strings are given because they are contracts; the behaviour lives in `pare/tui/daemon_lifecycle.py` and `pare/tui/app.py`.

---

## Revision history

- 2026-09-20: initial design.
- 2026-09-26: refused ≠ stale — agent_core binds before `listen()`; a path listed in `/proc/net/unix` is a starting daemon, never unlinked; re-checked under the lock (Task 1 review B1).
- 2026-09-26: lock moved to `socket_path.parent / "pare-spawn.lock"` — it belongs beside the socket it guards, and an unwritable log directory must still reach the pipe fallback (Task 1 review S1).
- 2026-09-26: `reap` signals owned PIDs only and returns `not_owned` otherwise — the process-group guard alone doesn't prevent PID reuse (Task 1 review S2).
- 2026-09-26: `auto_spawn` is off by default and only `main()` enables it — constructing the app (every test) must never spawn a real daemon (plan correction C1).
- 2026-09-26: the child gets `PARE_SOCKET_PATH` — the polled and bound paths must match by construction (plan correction C2).
- 2026-09-26: systemd lane via `launch_policy` — never spawn beside an enabled user unit, fail closed when `systemctl`'s answer is unknown (user ruling C5).
- 2026-09-26: spawn runs as a background task and quit waits out an in-flight spawn — awaiting it froze the app, and a cancelled await would drop a live daemon's PID (Task 3, and its review S1).
- 2026-09-26: `pare-daemon` resolved beside `sys.executable` — an unactivated venv has no `pare-daemon` on `PATH` (Task 3).
- 2026-09-26: first transcript line is the neutral "checking for …" — the lane isn't known until detect returns (Task 3).
- 2026-09-26: spawn cwd resolved via `resolve_spawn_cwd` — the daemon's `workers.yaml` default is relative, and every earlier live check had run from the repo root (final review B1).
- 2026-09-26: SIGHUP turned into a normal exit — closing the terminal is a routine exit and orphaned the daemon (final review S1).
- 2026-09-26: rewritten in place to describe the shipped code — the appended "Correction 2026-09-26" folded into §1, §3-§5; `PARE_TUI_NO_AUTO_SPAWN` corrected (it skips detect entirely); §5 no longer claims the daemon removes its socket; §7 restated as properties without counts; §9 split into known limitations and not in scope (final review S2).
