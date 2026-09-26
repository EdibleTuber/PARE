"""End-to-end against the fake source and a stub daemon: the three task
families coexist without blocking one another."""
import asyncio

from agent_core.protocol import ChatMessage, CommandMessage, ResponseMessage


class StubSession:
    """Stands in for DaemonSession: records what was sent, and lets a test
    hold a turn open so concurrency is observable."""

    def __init__(self) -> None:
        self.sent: list[object] = []
        self.subscribers: list = []
        self.turn_open = asyncio.Event()

    def subscribe(self, handler) -> None:
        self.subscribers.append(handler)

    async def send(self, msg) -> None:
        self.sent.append(msg)

    async def send_chat(self, text: str) -> None:
        self.sent.append(ChatMessage(text=text))

    async def send_command(self, name: str, args: str) -> None:
        self.sent.append(CommandMessage(name=name, args=args))

    def finish_turn(self, text: str = "done") -> None:
        for handler in self.subscribers:
            handler(ResponseMessage(text=text))


async def test_chat_and_pane_run_concurrently():
    """The property the whole design exists for: while a turn is in flight,
    the pane's cursor still advances."""
    from pare.tui.panes.uart import UartPane
    from pare.tui.sources.fake_console import FakeConsoleSource

    session, src = StubSession(), FakeConsoleSource()
    pane = UartPane(source=src)
    await pane.attach()

    await session.send_chat("what is on the console?")   # turn in flight
    before = pane.cursor
    src.feed(b"U-Boot 2024.01\n")
    await pane.advance()
    after = pane.cursor

    assert after > before, "pane did not advance while a turn was open"
    session.finish_turn()


async def test_a_dead_source_leaves_chat_usable():
    """Invariant I3: one pane's source failing must not take the app down."""
    from pare.tui.panes.uart import UartPane
    from pare.tui.sources.fake_console import FakeConsoleSource

    session, src = StubSession(), FakeConsoleSource()
    pane = UartPane(source=src)
    await pane.attach()
    src.fail_next_read(RuntimeError("link down"))
    await pane.advance()          # must not raise out of the pane

    await session.send_chat("still here?")
    assert any(isinstance(m, ChatMessage) for m in session.sent)


async def test_status_bar_reflects_source_failure():
    from pare.tui.panes.uart import UartPane
    from pare.tui.sources.fake_console import FakeConsoleSource
    from pare.tui.widgets.statusbar import StatusBar

    src = FakeConsoleSource()
    pane = UartPane(source=src)
    await pane.attach()
    src.feed(b"ok")
    await pane.advance()
    healthy = StatusBar().render_for([pane])

    src.fail_next_read(RuntimeError("link down"))
    await pane.advance()
    failed = StatusBar().render_for([pane])

    assert failed != healthy


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


async def test_a_leading_slash_sends_a_command_not_a_chat():
    """Spec section 2.2: slash commands keep their wire form. Sending "/worker
    list" as chat would put it in front of the model instead of the registry."""
    from pare.tui.app import parse_input

    session = StubSession()
    await parse_input(session, "/worker list")
    await parse_input(session, "what workers are loaded?")

    kinds = [type(m).__name__ for m in session.sent]
    assert kinds == ["CommandMessage", "ChatMessage"]
    command = session.sent[0]
    assert command.name == "worker" and command.args == "list"


async def test_a_bare_slash_is_not_a_command():
    """A lone "/" has no command name; splitting it blindly raises IndexError
    in the REPL's parsing shape (agent_core/adapters/cli.py:177-183)."""
    from pare.tui.app import parse_input

    session = StubSession()
    await parse_input(session, "/")
    assert session.sent == [] or type(session.sent[0]).__name__ == "ChatMessage"


async def test_send_approval_response_survives_a_dead_socket():
    """Fix round 1, Important finding: the daemon can die during the window
    an operator spends deciding on an approval modal. When it does,
    `_send_approval_response`'s `await self.session.send(response)` hits a
    dead socket -- and because this runs inside a Textual screen-dismiss
    callback, an uncaught exception there propagates into
    `App._handle_exception`, which EXITS THE WHOLE APP mid-conversation
    (worse than a stuck modal). This must not happen: the failed send is
    caught, logged, and surfaced in the transcript instead."""
    from pathlib import Path

    from agent_core.protocol import ToolApprovalResponseMessage

    from pare.tui.app import PareTUI

    class _DeadSession:
        """A session whose socket has already died: every send raises,
        exactly what `DaemonConnection.send` does once the connection is
        gone (agent_core/client.py:53's `assert self.writer is not None`)."""

        async def send(self, msg: object) -> None:
            raise RuntimeError("socket closed")

    app = PareTUI(Path("/nonexistent/pare.sock"), "chan-1", "/tmp")
    app.session = _DeadSession()
    async with app.run_test() as pilot:
        # If _send_approval_response does not catch the send failure, this
        # await raises out of the test -- there is no try/except here on
        # purpose, so a regression shows up as this test failing, not as a
        # silently-swallowed app crash.
        await app._send_approval_response(
            ToolApprovalResponseMessage(proposal_id="p1", approved=True)
        )
        await pilot.pause()
        # The app is still alive and respondable after the failed send.
        assert app.is_running


def test_build_uart_pane_defaults_to_fake_when_env_var_unset(monkeypatch):
    """Unset env var is the default: FakeConsoleSource, unchanged from Task 9.
    Regression guard against a future change that silently makes the pane
    require a live Pi endpoint."""
    from pathlib import Path

    from pare.tui.app import PareTUI
    from pare.tui.sources.fake_console import FakeConsoleSource

    monkeypatch.delenv("PARE_TUI_HARDWARE_ENDPOINT", raising=False)

    app = PareTUI(Path("/nonexistent/pare.sock"), "chan-1", "/tmp")
    pane = app._build_uart_pane()
    assert isinstance(pane.source, FakeConsoleSource)


def test_build_uart_pane_uses_mcp_source_when_env_var_set(monkeypatch):
    """Set env var -> McpConsoleSource(endpoint=<value>). Constructs the
    source; does NOT attach (no live transport in tests)."""
    from pathlib import Path

    from pare.tui.app import PareTUI
    from pare.tui.sources.mcp_console import McpConsoleSource

    monkeypatch.setenv("PARE_TUI_HARDWARE_ENDPOINT", "http://100.97.133.126:9102/mcp")

    app = PareTUI(Path("/nonexistent/pare.sock"), "chan-1", "/tmp")
    pane = app._build_uart_pane()
    assert isinstance(pane.source, McpConsoleSource)
    assert pane.source.endpoint == "http://100.97.133.126:9102/mcp"


# --- Daemon auto-spawn wiring (plan 2026-09-20-daemon-auto-spawn, Task 3) ---
#
# Every test here monkeypatches BOTH `detect_or_spawn` and `reap` at the
# `pare.tui.app` import site (plan C4): a faked pid=12345 must never reach
# the real `reap`, and nothing may spawn a real daemon or write into the
# real ~/.local/state/pare/.


class _LifecycleSession:
    """A session double with the subscribe/start/stop surface on_mount
    needs; `start` either connects or raises like a missing socket."""

    def __init__(self, *, start_ok: bool) -> None:
        self.start_ok = start_ok
        self.started = False
        self.start_calls = 0
        self.subscribers: list = []

    def subscribe(self, handler) -> None:
        self.subscribers.append(handler)

    async def start(self) -> None:
        self.start_calls += 1
        if not self.start_ok:
            raise FileNotFoundError(2, "No such file or directory")
        self.started = True

    async def stop(self) -> None:
        pass

    async def send(self, msg) -> None:
        pass


def _patch_lifecycle(monkeypatch, result=None, *, detect=None, reap_raises=False):
    """Install fakes; return (detect_calls, reap_calls, transcript_writes)."""
    from pare.tui import app as app_module

    detect_calls: list = []
    reap_calls: list = []

    def fake_detect(socket_path, log_dir, **kwargs):
        detect_calls.append((socket_path, log_dir))
        if detect is not None:
            return detect()
        return result

    def fake_reap(pid, **kwargs):
        reap_calls.append(pid)
        if reap_raises:
            raise RuntimeError("reap blew up")
        return "exited_clean"

    monkeypatch.setattr(app_module, "detect_or_spawn", fake_detect)
    monkeypatch.setattr(app_module, "reap", fake_reap)
    # Independent of the checkout's workers.yaml and the caller's env; tests
    # of the resolution itself override this.
    monkeypatch.setattr(
        app_module, "resolve_spawn_cwd",
        lambda env: app_module.SpawnCwd(cwd=None, error=None),
    )
    transcript = _record_transcript(monkeypatch)
    return detect_calls, reap_calls, transcript


def _record_transcript(monkeypatch) -> list[str]:
    """Record every write that reaches the #transcript RichLog widget. Its
    rendered `lines` wrap at 80 columns mid-path, so assert on the writes."""
    from textual.widgets import RichLog

    writes: list[str] = []
    original = RichLog.write

    def spy(self, content, *args, **kwargs):
        # RichLog defers writes made before its size is known and replays
        # them through write() later; count each write once, on replay.
        if self.id == "transcript" and getattr(self, "_size_known", True):
            writes.append(str(content))
        return original(self, content, *args, **kwargs)

    monkeypatch.setattr(RichLog, "write", spy)
    return writes


async def _mount_auto_spawn(app, pilot) -> None:
    """Wait for the background spawn+connect task on_mount started."""
    assert app._daemon_connect_task is not None
    await app._daemon_connect_task
    await pilot.pause()


def _make_auto_spawn_app(tmp_path, *, start_ok: bool, **kwargs):
    from pathlib import Path

    from pare.tui.app import PareTUI

    app = PareTUI(
        Path("/nonexistent/pare.sock"), "chan-1", "/tmp",
        auto_spawn=True, daemon_log_dir=tmp_path / "logs", **kwargs,
    )
    app.session = _LifecycleSession(start_ok=start_ok)
    return app


async def test_spawned_daemon_is_announced_and_reaped_on_exit(monkeypatch, tmp_path):
    from pare.tui.daemon_lifecycle import DaemonSpawnResult

    log_path = tmp_path / "logs" / "daemon-12345.log"
    detect_calls, reap_calls, transcript = _patch_lifecycle(
        monkeypatch,
        DaemonSpawnResult(mode="spawned", pid=12345, log_path=log_path, error=None),
    )
    app = _make_auto_spawn_app(tmp_path, start_ok=True)
    async with app.run_test(size=(240, 40)) as pilot:
        await _mount_auto_spawn(app, pilot)
        text = "\n".join(transcript)
        assert "PID 12345" in text
        assert str(log_path) in text
        assert "attached to running" not in text
        assert app.query_one("#status-bar").daemon_state == "up"
        assert reap_calls == []  # not before exit
    assert detect_calls == [(app.socket_path, tmp_path / "logs")]
    assert reap_calls == [12345]


async def test_attached_daemon_is_announced_and_not_reaped(monkeypatch, tmp_path):
    from pare.tui.daemon_lifecycle import DaemonSpawnResult

    detect_calls, reap_calls, transcript = _patch_lifecycle(
        monkeypatch,
        DaemonSpawnResult(mode="attached", pid=None, log_path=None, error=None),
    )
    app = _make_auto_spawn_app(tmp_path, start_ok=True)
    async with app.run_test(size=(240, 40)) as pilot:
        await _mount_auto_spawn(app, pilot)
        assert "[attached to running pare-daemon at /nonexistent/pare.sock]" in transcript
        assert app.query_one("#status-bar").daemon_state == "up"
    assert len(detect_calls) == 1
    assert reap_calls == []


async def test_spawn_failure_shows_reason_and_spawn_failed_state(monkeypatch, tmp_path):
    from pare.tui.daemon_lifecycle import DaemonSpawnResult

    _, reap_calls, transcript = _patch_lifecycle(
        monkeypatch,
        DaemonSpawnResult(
            mode="failed", pid=None, log_path=tmp_path / "logs" / "daemon-9.log",
            error="did not accept a connection within 5s -- see /x/daemon-9.log",
        ),
    )
    app = _make_auto_spawn_app(tmp_path, start_ok=False)
    async with app.run_test(size=(240, 40)) as pilot:
        await _mount_auto_spawn(app, pilot)
        text = "\n".join(transcript)
        assert "daemon spawn failed: did not accept a connection within 5s" in text
        # The existing start/except path ran, and the bar says spawn-failed,
        # not the ambiguous DOWN.
        assert app.session.start_calls == 1
        assert app.session.started is False
        bar = app.query_one("#status-bar")
        assert bar.daemon_state == "spawn-failed"
        app._refresh_status_bar()  # the 1s timer's path keeps it
        assert bar.daemon_state == "spawn-failed"
    assert reap_calls == []


async def test_captured_tail_goes_on_its_own_transcript_line(monkeypatch, tmp_path):
    from pare.tui.daemon_lifecycle import DaemonSpawnResult

    _, _, transcript = _patch_lifecycle(
        monkeypatch,
        DaemonSpawnResult(
            mode="failed", pid=None, log_path=None,
            error="exited during startup, code 1 -- no log; captured tail below\n"
                  "ImportError: boom",
        ),
    )
    app = _make_auto_spawn_app(tmp_path, start_ok=False)
    async with app.run_test(size=(240, 40)) as pilot:
        await _mount_auto_spawn(app, pilot)
        lines = transcript
        assert any(l.startswith("[daemon spawn failed: exited during startup, code 1")
                   for l in lines)
        assert "[ImportError: boom]" in lines


async def test_without_auto_spawn_nothing_is_detected_or_spawned(monkeypatch, tmp_path):
    """C1: the default constructor -- what every other test uses -- must
    never reach detect_or_spawn."""
    from pathlib import Path

    from pare.tui.app import PareTUI

    detect_calls, reap_calls, transcript = _patch_lifecycle(monkeypatch, None)
    app = PareTUI(Path("/nonexistent/pare.sock"), "chan-1", "/tmp")
    app.session = _LifecycleSession(start_ok=False)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.query_one("#status-bar").daemon_state == "down"
    assert detect_calls == []
    assert reap_calls == []


async def test_systemd_managed_attach_failure_names_the_unit(monkeypatch, tmp_path):
    from pathlib import Path

    from pare.tui.app import SYSTEMD_ATTACH_FAILED, PareTUI

    detect_calls, _, transcript = _patch_lifecycle(monkeypatch, None)
    app = PareTUI(
        Path("/nonexistent/pare.sock"), "chan-1", "/tmp",
        systemd_managed=True, daemon_log_dir=tmp_path,
    )
    app.session = _LifecycleSession(start_ok=False)
    async with app.run_test(size=(240, 40)) as pilot:
        await pilot.pause()
        assert SYSTEMD_ATTACH_FAILED in transcript
        assert app.query_one("#status-bar").daemon_state == "down"
    assert detect_calls == []


async def test_exit_during_an_in_flight_spawn_still_reaps_it(monkeypatch, tmp_path):
    """The spawn lane runs in the background so the UI is live meanwhile.
    Quitting before detect_or_spawn returns must not orphan the daemon it
    goes on to start: on_unmount waits the spawn out and reaps its PID."""
    import threading

    from pare.tui.daemon_lifecycle import DaemonSpawnResult

    entered = threading.Event()
    release = threading.Event()

    def slow_detect():
        entered.set()
        release.wait(10)
        return DaemonSpawnResult(
            mode="spawned", pid=12345, log_path=tmp_path / "l.log", error=None,
        )

    _, reap_calls, transcript = _patch_lifecycle(monkeypatch, detect=slow_detect)
    app = _make_auto_spawn_app(tmp_path, start_ok=True)
    async with app.run_test() as pilot:
        await asyncio.to_thread(entered.wait, 5)
        assert app._daemon_owned_pid is None  # still in flight
        # Release the spawn only after the exit has begun.
        threading.Timer(0.3, release.set).start()
    assert reap_calls == [12345]


async def test_a_reap_failure_does_not_break_exit(monkeypatch, tmp_path):
    from pare.tui.daemon_lifecycle import DaemonSpawnResult

    _, reap_calls, transcript = _patch_lifecycle(
        monkeypatch,
        DaemonSpawnResult(mode="spawned", pid=12345, log_path=None, error=None),
        reap_raises=True,
    )
    app = _make_auto_spawn_app(tmp_path, start_ok=True)
    async with app.run_test(size=(240, 40)) as pilot:
        await _mount_auto_spawn(app, pilot)
        assert any("logs at none" in w for w in transcript)
    assert reap_calls == [12345]


# --- launch_policy: main()'s spawn decision (plan C5, spec §8) ---


class _FakeRun:
    def __init__(self, *, returncode=None, raises=None):
        self.returncode = returncode
        self.raises = raises
        self.calls: list = []

    def __call__(self, argv, **kwargs):
        import subprocess

        self.calls.append(argv)
        if self.raises is not None:
            raise self.raises
        return subprocess.CompletedProcess(argv, self.returncode)


def test_launch_policy_enabled_systemd_unit_disables_auto_spawn():
    from pare.tui.app import LaunchPolicy, launch_policy

    run = _FakeRun(returncode=0)
    assert launch_policy({}, run) == LaunchPolicy(auto_spawn=False, systemd_managed=True)
    assert run.calls == [["systemctl", "--user", "is-enabled", "pare-daemon"]]


def test_launch_policy_no_unit_auto_spawns():
    from pare.tui.app import LaunchPolicy, launch_policy

    assert launch_policy({}, _FakeRun(returncode=1)) == LaunchPolicy(
        auto_spawn=True, systemd_managed=False
    )


def test_launch_policy_missing_systemctl_counts_as_no_unit():
    from pare.tui.app import LaunchPolicy, launch_policy

    run = _FakeRun(raises=FileNotFoundError(2, "No such file", "systemctl"))
    assert launch_policy({}, run) == LaunchPolicy(auto_spawn=True, systemd_managed=False)


def test_launch_policy_env_opt_out():
    from pare.tui.app import LaunchPolicy, launch_policy

    env = {"PARE_TUI_NO_AUTO_SPAWN": "1"}
    assert launch_policy(env, _FakeRun(returncode=1)) == LaunchPolicy(
        auto_spawn=False, systemd_managed=False
    )
    # The unit check still wins its transcript hint under the opt-out.
    assert launch_policy(env, _FakeRun(returncode=0)) == LaunchPolicy(
        auto_spawn=False, systemd_managed=True
    )


def test_launch_policy_hung_systemctl_fails_closed():
    import subprocess

    from pare.tui.app import LaunchPolicy, launch_policy

    run = _FakeRun(raises=subprocess.TimeoutExpired(["systemctl"], 5))
    assert launch_policy({}, run) == LaunchPolicy(auto_spawn=False, systemd_managed=False)


def test_main_builds_the_app_from_launch_policy(monkeypatch):
    """main() passes the policy through; the app is not actually run."""
    from pare.tui import app as app_module

    built: list = []

    class _RecordingApp:
        def __init__(self, *args, **kwargs):
            built.append(kwargs)

        def run(self):
            pass

    monkeypatch.setattr(app_module, "PareTUI", _RecordingApp)
    monkeypatch.setattr(
        app_module, "launch_policy",
        lambda env: app_module.LaunchPolicy(auto_spawn=False, systemd_managed=True),
    )
    app_module.main()
    assert built == [{"auto_spawn": False, "systemd_managed": True}]


def test_daemon_command_prefers_the_sibling_of_this_interpreter(monkeypatch, tmp_path):
    """A venv's pare-tui run without activation has no pare-daemon on PATH;
    the spawn must use the one installed beside the running interpreter."""
    from pare.tui import app as app_module

    exe = tmp_path / "python3"
    exe.write_text("")
    daemon = tmp_path / "pare-daemon"
    daemon.write_text("#!/bin/sh\n")
    daemon.chmod(0o755)
    monkeypatch.setattr(app_module.sys, "executable", str(exe))
    assert app_module._daemon_command() == [str(daemon)]

    daemon.unlink()
    assert app_module._daemon_command() == ["pare-daemon"]


async def test_detect_or_spawn_gets_the_resolved_daemon_command(monkeypatch, tmp_path):
    from pare.tui import app as app_module
    from pare.tui.daemon_lifecycle import DaemonSpawnResult

    seen: list = []

    def fake_detect(socket_path, log_dir, **kwargs):
        seen.append(kwargs.get("spawn_cmd"))
        return DaemonSpawnResult(mode="attached", pid=None, log_path=None, error=None)

    monkeypatch.setattr(app_module, "detect_or_spawn", fake_detect)
    monkeypatch.setattr(app_module, "reap", lambda pid, **kw: "not_owned")
    monkeypatch.setattr(app_module, "_daemon_command", lambda: ["/x/pare-daemon"])
    app = _make_auto_spawn_app(tmp_path, start_ok=True)
    async with app.run_test() as pilot:
        await _mount_auto_spawn(app, pilot)
    assert seen == [["/x/pare-daemon"]]


async def test_a_spawn_resolved_before_its_callback_ran_is_still_reaped(monkeypatch, tmp_path):
    """Review S1: asyncio runs done-callbacks one loop step after
    set_result. If unmount reaches the reap in that step, fut.done() is True
    but _record_spawn has not run -- the PID must come from the future."""
    from pare.tui.daemon_lifecycle import DaemonSpawnResult

    _, reap_calls, _ = _patch_lifecycle(monkeypatch, None)
    app = _make_auto_spawn_app(tmp_path, start_ok=True)
    fut = asyncio.get_running_loop().create_future()
    fut.add_done_callback(app._record_spawn)
    app._daemon_spawn_future = fut
    fut.set_result(
        DaemonSpawnResult(mode="spawned", pid=4242, log_path=None, error=None)
    )
    # No yield between set_result and the reap: the callback is still queued.
    await app._reap_owned_daemon()
    assert reap_calls == [4242]
    await asyncio.sleep(0)  # let the queued callback run
    assert app._daemon_owned_pid is None  # and it does not resurrect the PID


async def test_an_exception_in_the_spawn_task_is_surfaced(monkeypatch, tmp_path):
    """Review N1: an exception escaping the background spawn task must not
    leave the UI on "checking..." and daemon:DOWN forever."""
    from pare.tui import app as app_module

    detect_calls, _, transcript = _patch_lifecycle(monkeypatch, None)

    def broken_command():
        raise TypeError("expected str, bytes or os.PathLike object, not NoneType")

    monkeypatch.setattr(app_module, "_daemon_command", broken_command)
    app = _make_auto_spawn_app(tmp_path, start_ok=True)
    async with app.run_test() as pilot:
        await _mount_auto_spawn(app, pilot)
        assert any(
            w.startswith("[daemon spawn failed: TypeError: expected str") for w in transcript
        )
        assert app.query_one("#status-bar").daemon_state == "spawn-failed"
    assert detect_calls == []


# --- spawn_cwd resolution (final review B1) ---


def test_resolve_spawn_cwd_respects_an_explicit_workers_yaml_path(tmp_path):
    from pare.tui.app import SpawnCwd, resolve_spawn_cwd

    (tmp_path / "workers.yaml").write_text("")
    env = {"PARE_WORKERS_YAML_PATH": "/abs/workers.yaml"}
    assert resolve_spawn_cwd(env, repo_root=tmp_path) == SpawnCwd(cwd=None, error=None)


def test_resolve_spawn_cwd_uses_the_repo_root_holding_workers_yaml(tmp_path):
    from pare.tui.app import SpawnCwd, resolve_spawn_cwd

    (tmp_path / "workers.yaml").write_text("")
    assert resolve_spawn_cwd({}, repo_root=tmp_path) == SpawnCwd(cwd=tmp_path, error=None)


def test_resolve_spawn_cwd_without_workers_yaml_is_an_error(tmp_path):
    from pare.tui.app import resolve_spawn_cwd

    r = resolve_spawn_cwd({}, repo_root=tmp_path)
    assert r.cwd is None
    assert "workers.yaml not found" in r.error
    assert "PARE_WORKERS_YAML_PATH" in r.error


def test_resolve_spawn_cwd_default_root_is_the_installed_repo():
    """Default repo_root mirrors the systemd unit's WorkingDirectory."""
    from pathlib import Path

    import pare
    from pare.tui.app import resolve_spawn_cwd

    root = Path(pare.__file__).resolve().parents[1]
    r = resolve_spawn_cwd({})
    if (root / "workers.yaml").exists():
        assert r.cwd == root
    else:
        assert r.error is not None


async def test_resolved_spawn_cwd_reaches_detect_or_spawn(monkeypatch, tmp_path):
    from pare.tui import app as app_module
    from pare.tui.daemon_lifecycle import DaemonSpawnResult

    seen: list = []

    def fake_detect(socket_path, log_dir, **kwargs):
        seen.append(kwargs.get("spawn_cwd", "<missing>"))
        return DaemonSpawnResult(mode="attached", pid=None, log_path=None, error=None)

    _patch_lifecycle(monkeypatch, None)
    monkeypatch.setattr(app_module, "detect_or_spawn", fake_detect)
    monkeypatch.setattr(
        app_module, "resolve_spawn_cwd",
        lambda env: app_module.SpawnCwd(cwd=tmp_path, error=None),
    )
    app = _make_auto_spawn_app(tmp_path, start_ok=True)
    async with app.run_test() as pilot:
        await _mount_auto_spawn(app, pilot)
    assert seen == [tmp_path]


async def test_unresolvable_workers_yaml_fails_fast_without_spawning(monkeypatch, tmp_path):
    from pare.tui import app as app_module

    detect_calls, reap_calls, transcript = _patch_lifecycle(monkeypatch, None)
    monkeypatch.setattr(
        app_module, "resolve_spawn_cwd",
        lambda env: app_module.SpawnCwd(
            cwd=None, error="workers.yaml not found — set PARE_WORKERS_YAML_PATH"),
    )
    app = _make_auto_spawn_app(tmp_path, start_ok=False)
    async with app.run_test() as pilot:
        await _mount_auto_spawn(app, pilot)
        assert (
            "[daemon spawn failed: workers.yaml not found — set PARE_WORKERS_YAML_PATH]"
            in transcript
        )
        assert app.query_one("#status-bar").daemon_state == "spawn-failed"
        assert app.session.start_calls == 1  # a running daemon may still be attached
    assert detect_calls == []
    assert reap_calls == []


async def test_unresolvable_workers_yaml_still_attaches_to_a_running_daemon(
    monkeypatch, tmp_path
):
    from pare.tui import app as app_module

    detect_calls, _, transcript = _patch_lifecycle(monkeypatch, None)
    monkeypatch.setattr(
        app_module, "resolve_spawn_cwd",
        lambda env: app_module.SpawnCwd(cwd=None, error="workers.yaml not found"),
    )
    app = _make_auto_spawn_app(tmp_path, start_ok=True)
    async with app.run_test() as pilot:
        await _mount_auto_spawn(app, pilot)
        assert any(w.startswith("[attached to running") for w in transcript)
        assert not any("spawn failed" in w for w in transcript)
        assert app.query_one("#status-bar").daemon_state == "up"
    assert detect_calls == []


# --- SIGHUP (final review S1) ---


async def test_sighup_handler_is_registered_and_requests_exit(monkeypatch, tmp_path):
    import signal

    from pare.tui.daemon_lifecycle import DaemonSpawnResult

    _patch_lifecycle(
        monkeypatch,
        DaemonSpawnResult(mode="attached", pid=None, log_path=None, error=None),
    )
    loop = asyncio.get_running_loop()
    registered: dict = {}
    removed: list = []
    monkeypatch.setattr(
        loop, "add_signal_handler",
        lambda sig, cb, *a: registered.__setitem__(sig, cb),
    )
    monkeypatch.setattr(loop, "remove_signal_handler", lambda sig: removed.append(sig))
    app = _make_auto_spawn_app(tmp_path, start_ok=True)
    exits: list = []
    async with app.run_test() as pilot:
        await _mount_auto_spawn(app, pilot)
        assert signal.SIGHUP in registered
        monkeypatch.setattr(app, "exit", lambda *a, **k: exits.append(a))
        registered[signal.SIGHUP]()
        assert exits == [()]
    assert removed == [signal.SIGHUP]


async def test_sighup_handler_does_not_clobber_an_existing_one(monkeypatch, tmp_path):
    import signal

    from pare.tui.daemon_lifecycle import DaemonSpawnResult

    _patch_lifecycle(
        monkeypatch,
        DaemonSpawnResult(mode="attached", pid=None, log_path=None, error=None),
    )
    monkeypatch.setattr(signal, "getsignal", lambda sig: (lambda *a: None))
    loop = asyncio.get_running_loop()
    registered: dict = {}
    monkeypatch.setattr(
        loop, "add_signal_handler",
        lambda sig, cb, *a: registered.__setitem__(sig, cb),
    )
    app = _make_auto_spawn_app(tmp_path, start_ok=True)
    async with app.run_test() as pilot:
        await _mount_auto_spawn(app, pilot)
    assert registered == {}


async def test_sighup_handler_really_restores_sig_dfl_after_exit(monkeypatch, tmp_path):
    """No fakes on the loop: after the app exits, SIGHUP is back to default
    in this (the test) process."""
    import signal

    from pare.tui.daemon_lifecycle import DaemonSpawnResult

    _patch_lifecycle(
        monkeypatch,
        DaemonSpawnResult(mode="attached", pid=None, log_path=None, error=None),
    )
    assert signal.getsignal(signal.SIGHUP) == signal.SIG_DFL
    app = _make_auto_spawn_app(tmp_path, start_ok=True)
    async with app.run_test() as pilot:
        await _mount_auto_spawn(app, pilot)
        assert signal.getsignal(signal.SIGHUP) != signal.SIG_DFL
    assert signal.getsignal(signal.SIGHUP) == signal.SIG_DFL
