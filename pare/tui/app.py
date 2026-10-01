"""The PARE TUI application: chat beside live device panes.

Replaces pare-cli as the operator surface. See
docs/superpowers/specs/2026-09-18-pare-tui-design.md.
"""
from __future__ import annotations

import asyncio
import functools
import logging
import os
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Mapping

from agent_core.protocol import ToolApprovalRequestMessage, ToolApprovalResponseMessage
from rich.markdown import Markdown as RichMarkdown
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Footer, Header, Input, Markdown, RichLog, Static

import pare
from pare.config import load_config
from pare.tui.daemon_lifecycle import detect_or_spawn, reap
from pare.tui.panes.base import PaneDock
from pare.tui.panes.uart import UartPane
from pare.tui.prefs import default_tui_config_path, load_theme, save_theme
from pare.tui.session import DaemonDisconnected, DaemonSession
from pare.tui.sources.fake_console import FakeConsoleSource
from pare.tui.widgets.approval import ApprovalModal
from pare.tui.widgets.statusbar import StatusBar
from pare.tui.widgets.transcript import Committed, TranscriptLog, TurnRenderer

logger = logging.getLogger(__name__)


def _new_channel_id() -> str:
    """A fresh channel per launch, so a session starts clean instead of
    replaying cli-default. Mirrors pare/cli.py:_new_channel_id."""
    return datetime.now().strftime("tui-%Y%m%d-%H%M%S")


#: Cycles the chat / pane-dock split. Unbound in Textual 8.2.8's App, Screen
#: and Input bindings (Input binds ctrl+a/c/d/e/k/u/v/w/x; the App binds
#: ctrl+p for the palette, ctrl+q and ctrl+c), so it works with the chat
#: box focused -- which is nearly always.
LAYOUT_KEY = "ctrl+l"

#: The split states LAYOUT_KEY steps through, as #main-area classes. The
#: first is the default and needs no class.
LAYOUT_CLASSES = ("", "-layout-even", "-layout-dock-wide", "-layout-dock-hidden")

ECHO_PREFIX = "you> "


SYSTEMD_ATTACH_FAILED = (
    "[systemd-managed pare-daemon is not accepting connections — "
    "systemctl --user status pare-daemon]"
)


def _daemon_command() -> list[str]:
    """The `pare-daemon` installed beside this interpreter, else the bare
    name for a PATH lookup. `.venv/bin/pare-tui` is routinely run without
    activating the venv, and then `pare-daemon` is not on PATH (checked on
    agenthost: a plain PATH does not resolve it), so the bare name alone
    would fail every spawn with ENOENT."""
    sibling = Path(sys.executable).parent / "pare-daemon"
    if sibling.is_file() and os.access(sibling, os.X_OK):
        return [str(sibling)]
    return ["pare-daemon"]


@dataclass(frozen=True)
class SpawnCwd:
    """Where a TUI-spawned daemon runs. `error` set means: do not spawn."""

    cwd: Path | None
    error: str | None


def resolve_spawn_cwd(
    env: Mapping[str, str], repo_root: Path | None = None
) -> SpawnCwd:
    """The spawned daemon's working directory.

    pare-daemon loads `workers_yaml_path`, default the RELATIVE
    "workers.yaml" (pare/config.py), resolved against its cwd. The systemd
    unit pins WorkingDirectory to the repo root; a TUI-spawned daemon would
    otherwise inherit wherever the operator launched pare-tui and exit at
    startup (final review B1).

    - `PARE_WORKERS_YAML_PATH` set: the operator's choice; cwd unchanged.
    - `<repo root>/workers.yaml` exists: run there, as the systemd unit does.
    - Neither: an error -- the caller must not spawn.

    The daemon's cwd is only a fallback for messages that carry none
    (pare/capture_store.py, pare/tools/publish_finding.py); the TUI stamps
    its own cwd on every message (pare/tui/session.py, panes/uart.py).
    """
    if env.get("PARE_WORKERS_YAML_PATH"):
        return SpawnCwd(cwd=None, error=None)
    root = repo_root if repo_root is not None else Path(pare.__file__).resolve().parents[1]
    if (root / "workers.yaml").is_file():
        return SpawnCwd(cwd=root, error=None)
    return SpawnCwd(
        cwd=None,
        error=f"workers.yaml not found in {root} — set PARE_WORKERS_YAML_PATH",
    )


@dataclass(frozen=True)
class LaunchPolicy:
    """How `main()` constructs `PareTUI` with respect to the daemon."""

    auto_spawn: bool
    systemd_managed: bool


def _systemd_unit_enabled(run: Callable[..., object]) -> bool | None:
    """`systemctl --user is-enabled pare-daemon` exited 0?

    True/False for a definite answer; a missing `systemctl` binary is a
    definite False ("no unit", plan C5). None when the answer is unknown
    (systemctl hung or could not be run for another reason)."""
    try:
        proc = run(
            ["systemctl", "--user", "is-enabled", "pare-daemon"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except FileNotFoundError:
        return False
    except (OSError, subprocess.SubprocessError):
        logger.exception("could not ask systemctl whether pare-daemon is enabled")
        return None
    return proc.returncode == 0


def launch_policy(
    env: Mapping[str, str], run: Callable[..., object] = subprocess.run
) -> LaunchPolicy:
    """Decide whether this launch may spawn a daemon (plan C5, spec §8).

    - A user systemd unit manages pare-daemon (`is-enabled` exits 0): never
      spawn. agent_core unlinks and rebinds the socket on every start, so a
      systemd restart would orphan a TUI-spawned daemon.
    - `PARE_TUI_NO_AUTO_SPAWN=1`: never spawn (the spec §8 escape hatch).
    - systemctl's answer unknown (it hung or failed to run): fail closed --
      do not spawn next to a daemon that may be supervised.
    - Otherwise spawn when nothing is running.

    `run` is `subprocess.run`-shaped so tests pass a fake instead of
    shelling out to the real systemctl.
    """
    enabled = _systemd_unit_enabled(run)
    if enabled:
        return LaunchPolicy(auto_spawn=False, systemd_managed=True)
    if env.get("PARE_TUI_NO_AUTO_SPAWN") == "1" or enabled is None:
        return LaunchPolicy(auto_spawn=False, systemd_managed=False)
    return LaunchPolicy(auto_spawn=True, systemd_managed=False)


def is_sendable(line: str) -> bool:
    """Whether `parse_input` would send anything for `line`: not blank, and
    not a bare "/" (which has no command name)."""
    line = line.strip()
    return bool(line) and bool(line[1:].split(None, 1) if line.startswith("/") else line)


async def parse_input(session, line: str) -> None:
    """Route one line of chat-input text to the daemon.

    Mirrors `agent_core/adapters/cli.py:177-183`'s REPL split: a leading
    "/" is a slash command (`session.send_command`), anything else is chat
    (`session.send_chat`) -- so `/worker list` reaches the command registry
    on the wire instead of being handed to the model as text.

    A bare "/" has no command name: `line[1:]` is empty, and the CLI's own
    `"".split(None, 1)[0]` would raise IndexError on that shape. Chosen
    behaviour here: swallow it silently -- send nothing. It is neither a
    command (there is no name to send) nor plausibly intended as chat (an
    operator who typed a lone "/" almost certainly meant to start a command
    and stopped, not to say the literal character to the model).
    """
    line = line.strip()
    if not line:
        return
    if line.startswith("/"):
        parts = line[1:].split(None, 1)
        if not parts:
            return
        name = parts[0]
        args = parts[1] if len(parts) > 1 else ""
        await session.send_command(name, args)
        return
    await session.send_chat(line)


class PareTUI(App):
    """The application shell: header, transcript, chat input, a pane dock
    holding the UART pane, a status bar, and the footer."""

    TITLE = "PARE"

    BINDINGS = [
        Binding(LAYOUT_KEY, "cycle_layout", "Layout"),
        # Declared here, not inherited: the Textual 8.2.8 Footer renders
        # only the app subclass's own BINDINGS (verified: the base App's
        # ^q -> quit does not appear in the footer), so "at a glance"
        # requires the explicit entries.
        Binding("ctrl+q", "quit", "Quit"),
        Binding("f1", "show_help", "Help"),
    ]

    CSS = """
    #main-area {
        height: 1fr;
    }

    #chat-area {
        width: 2fr;
        height: 1fr;
    }

    #transcript {
        height: 1fr;
    }

    /* The reply being streamed: grows below the committed transcript,
       capped so the transcript stays visible; scrolled to its tail. */
    #live-scroll {
        height: auto;
        max-height: 50%;
        display: none;
    }

    #live-scroll.-streaming {
        display: block;
    }

    #live-reply {
        padding: 0 1;
    }

    #pane-dock {
        width: 1fr;
        height: 1fr;
        border-left: solid $primary;
    }

    #main-area.-layout-even #chat-area {
        width: 1fr;
    }

    #main-area.-layout-dock-wide #chat-area {
        width: 1fr;
    }

    #main-area.-layout-dock-wide #pane-dock {
        width: 2fr;
    }

    /* display:none only drops it from layout: the pane stays mounted, so
       its own poll timer keeps running and the status bar still reads it. */
    #main-area.-layout-dock-hidden #pane-dock {
        display: none;
    }

    /* In normal flow after #main-area, not docked: the Footer docks bottom,
       and two bottom docks share one row (the Footer painted over this). */
    StatusBar {
        height: 1;
        background: $panel;
    }

    /* One-line dim title strip above a pane's content. Static by design:
       live pane state belongs in the StatusBar row above the footer. */
    .pane-label {
        height: 1;
        text-style: dim;
        padding: 0 1;
        background: $panel;
    }
    """

    def __init__(
        self,
        socket_path,
        channel_id: str,
        cwd: str,
        *,
        auto_spawn: bool = False,
        daemon_log_dir: Path | None = None,
        systemd_managed: bool = False,
        tui_config_path: Path | None = None,
    ) -> None:
        """`auto_spawn` is opt-in (plan C1): only `main()` turns it on, so
        constructing the app -- as every test does -- never launches a
        daemon. `systemd_managed` only changes the transcript line shown
        when the attach fails (plan C5). `tui_config_path` is the
        preferences file (default `$XDG_CONFIG_HOME/pare/tui.json`); tests
        pass a tmp path."""
        super().__init__()
        self.socket_path = socket_path
        self.channel_id = channel_id
        self.cwd = cwd
        self.auto_spawn = auto_spawn
        self.systemd_managed = systemd_managed
        self._daemon_log_dir = (
            daemon_log_dir
            if daemon_log_dir is not None
            else Path.home() / ".local" / "state" / "pare"
        )
        # Set only when detect_or_spawn launched the daemon this app owns;
        # on_unmount reaps it. Attach-lane apps never touch the daemon.
        self._daemon_owned_pid: int | None = None
        self._daemon_spawn_failed = False
        self._daemon_connect_task: asyncio.Task | None = None
        self._daemon_spawn_future: asyncio.Future | None = None
        self._sighup_installed = False
        # _record_spawn runs at most once per spawn future (done-callback, or
        # the unmount path when the callback has not run yet).
        self._spawn_recorded = False
        # Construction only -- no connection is opened here. `on_mount`
        # starts it and subscribes to its message stream once the app is
        # actually running.
        self.session = DaemonSession(socket_path, channel_id, cwd)
        self._turn = TurnRenderer()
        self._daemon_connected = False
        self.tui_config_path = (
            tui_config_path
            if tui_config_path is not None
            else default_tui_config_path(os.environ)
        )
        # The theme name the file already holds; watch_theme saves only a
        # change from it, so applying the loaded theme never rewrites it.
        self._saved_theme: str | None = None
        self._layout_index = 0
        self._live_render_task: asyncio.Task | None = None
        self._live_dirty = False
        # Bumped whenever the live text is committed to the transcript: a
        # render that started before the bump shows a reply that is already
        # in the transcript, so it must not reveal the live block.
        self._live_generation = 0

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="main-area"):
            with Vertical(id="chat-area"):
                yield Static("Chat", classes="pane-label")
                yield TranscriptLog(id="transcript", wrap=True, markup=False)
                with VerticalScroll(id="live-scroll"):
                    yield Markdown(id="live-reply")
                yield Input(
                    placeholder="Type a message, or /command ...", id="chat-input"
                )
            yield PaneDock([self._build_uart_pane()], id="pane-dock", title="UART")
        yield StatusBar(id="status-bar")
        yield Footer()

    def _build_uart_pane(self) -> UartPane:
        """v1 default source: `FakeConsoleSource`, not `McpConsoleSource`.

        There is no deployed Pi in this plan's test bed (spec: "the whole
        plan is developed against the fake because no Pi deployment
        exists"), and `pare-tui` must still launch and show a working pane
        without one. The source is injected via `source=`, never hardcoded
        inside `UartPane` itself, so pointing this at a real board later
        (`McpConsoleSource(endpoint, worker_prefix=...)`, `pare/tui/sources/
        mcp_console.py`) is a one-line change here, not a rewrite.

        `daemon_session` starts as `None` -- deliberately NOT `self.session`
        -- and is attached only once `on_mount` confirms the connection is
        actually up (`_set_pane_daemon_session`, below). `UartPane` polls on
        its own timer from the moment it mounts, independent of the daemon
        connection, and `_log_activity` (`pare/tui/panes/uart.py:290-312`)
        calls `self._daemon_session.send(msg)` with no guard for "session
        exists but never connected" -- that reaches `DaemonConnection.send`'s
        `assert self.writer is not None, "connect() before send()"`
        (agent_core/client.py:53) uncaught, inside a Textual poll-timer
        callback, which crashes the app. Verified live: launching this app
        against a nonexistent socket with `daemon_session=self.session`
        wired in eagerly raises exactly that AssertionError out of
        `_poll_tick` the first time the pane's observed-output batch
        flushes. Gating on an actually-established connection avoids it
        without touching Task 4's or Task 9's files.

        Setting `PARE_TUI_HARDWARE_ENDPOINT` at launch swaps
        `FakeConsoleSource` for `McpConsoleSource(endpoint=...)`; unset
        leaves the fake as the default so existing tests keep passing
        without modification. Spec:
        `docs/superpowers/specs/2026-09-20-bench-integration-design.md` §5.1.
        """
        endpoint = os.environ.get("PARE_TUI_HARDWARE_ENDPOINT")
        if endpoint:
            from pare.tui.sources.mcp_console import McpConsoleSource

            source = McpConsoleSource(endpoint=endpoint)
        else:
            source = FakeConsoleSource()
        return UartPane(
            source=source,
            channel_id=self.channel_id,
            cwd=self.cwd,
            daemon_session=None,
            id="uart-pane",
        )

    def _set_pane_daemon_session(self, session: object | None) -> None:
        """Attach/detach the live session on every docked pane that logs
        through one. Reaches `Pane._daemon_session` directly (private to
        `UartPane`, Task 9's file) rather than adding a public setter there,
        since that file is out of scope for this task; confined to this one
        call site."""
        try:
            dock = self.query_one("#pane-dock", PaneDock)
        except Exception:
            return
        for pane in dock.panes:
            if hasattr(pane, "_daemon_session"):
                pane._daemon_session = session

    async def on_mount(self) -> None:
        self._load_theme()
        # Refresh the status line on a timer, independent of message
        # traffic, so the UART cursor visibly ticks even during a quiet
        # stretch (spec: "the UART cursor" is one of the things StatusBar
        # must show).
        self.set_interval(1.0, self._refresh_status_bar)

        # Guarded on hasattr rather than `isinstance(self.session,
        # DaemonSession)`: tests/test_tui_approval.py's `_make_app` swaps
        # `self.session` for a `_FakeSession` double that implements only
        # `send()` -- exactly the one method the approval modal's callback
        # needs -- before ever mounting the app. A real `DaemonSession`
        # always has both `subscribe` and `start` together, so this only
        # ever skips the connect step for that kind of test double, never
        # for the real app.
        subscribe = getattr(self.session, "subscribe", None)
        start = getattr(self.session, "start", None)
        if subscribe is not None and start is not None:
            subscribe(self._on_daemon_message)
            if self.auto_spawn:
                self._install_sighup_handler()
                # detect_or_spawn blocks for up to its startup_timeout plus
                # the timeout path's kill waits. Awaited here, it would hold
                # the app unready -- nothing painted, no key handled, not
                # even quit -- for that long (measured: run_test() did not
                # return until on_mount did). So the spawn lane runs as a
                # background task and on_mount returns at once.
                self._daemon_connect_task = asyncio.create_task(
                    self._spawn_then_connect(start)
                )
            else:
                await self._connect(start, spawned=False)
        self._refresh_status_bar()

    def _install_sighup_handler(self) -> None:
        """Closing the terminal or dropping SSH sends SIGHUP, whose default
        action kills the process without running on_unmount -- orphaning a
        daemon we spawned (it is in its own session, so it gets no hangup).
        Turn it into a normal exit so the unmount/reap path runs (final
        review S1). Main thread only; skipped where unsupported, and never
        over a handler someone else installed (including SIG_IGN, e.g.
        under nohup, where SIGHUP cannot kill us anyway)."""
        if not hasattr(signal, "SIGHUP"):
            return
        if threading.current_thread() is not threading.main_thread():
            return
        if signal.getsignal(signal.SIGHUP) != signal.SIG_DFL:
            return
        try:
            asyncio.get_running_loop().add_signal_handler(
                signal.SIGHUP, self._on_sighup
            )
        except (NotImplementedError, RuntimeError, ValueError, OSError):
            logger.exception("could not install a SIGHUP handler")
            return
        self._sighup_installed = True

    def _on_sighup(self) -> None:
        logger.info("SIGHUP: exiting so the owned daemon is reaped")
        self.exit()

    def _remove_sighup_handler(self) -> None:
        if not self._sighup_installed:
            return
        self._sighup_installed = False
        try:
            asyncio.get_running_loop().remove_signal_handler(signal.SIGHUP)
        except Exception:
            logger.exception("could not remove the SIGHUP handler")

    async def _connect(
        self, start, *, spawned: bool, failure_line: str | None = None
    ) -> None:
        try:
            await start()
        except Exception:
            # No running daemon is an expected state to launch into
            # (spec's "done when" gate: don't block on one existing) --
            # log it and leave the app usable rather than crashing the
            # whole UI before it can even render.
            logger.exception("failed to connect to daemon at %s", self.socket_path)
            if failure_line is not None:
                self._daemon_spawn_failed = True
                self._write_transcript(failure_line)
            if self.systemd_managed:
                self._write_transcript(SYSTEMD_ATTACH_FAILED)
        else:
            self._daemon_connected = True
            self._daemon_spawn_failed = False
            self._set_pane_daemon_session(self.session)
            if not spawned:
                self._write_transcript(
                    f"[attached to running pare-daemon at {self.socket_path}]"
                )

    async def _spawn_then_connect(self, start) -> None:
        # Nothing awaits this task on the happy path, so an exception escaping
        # it would never be retrieved and the UI would sit on "checking..."
        # and daemon:DOWN forever. Surface it instead. (CancelledError is not
        # an Exception and still propagates.)
        try:
            where = resolve_spawn_cwd(os.environ)
            if where.error is not None:
                # Spawning would only produce a daemon that exits at startup.
                # Don't; still attach if a daemon is already running.
                await self._connect(
                    start, spawned=False,
                    failure_line=f"[daemon spawn failed: {where.error}]",
                )
            else:
                spawned = await self._detect_or_spawn_daemon(where.cwd)
                await self._connect(start, spawned=spawned)
        except Exception as exc:
            logger.exception("daemon spawn/connect task failed")
            self._daemon_spawn_failed = True
            self._write_transcript(f"[daemon spawn failed: {type(exc).__name__}: {exc}]")
        self._refresh_status_bar()

    def _record_spawn(self, fut: asyncio.Future) -> None:
        """Done-callback on the detect_or_spawn future: record an owned PID
        the moment the thread returns, whoever (if anyone) is awaiting it --
        so a spawn that completes after the connect task was cancelled is
        still reaped by on_unmount.

        Idempotent: asyncio runs done-callbacks one loop step after the
        result is set, so `_reap_owned_daemon` also calls this for a future
        that is done but whose callback may not have run. Only the first
        call records; a late callback cannot resurrect an already-reaped PID.
        """
        if self._spawn_recorded or not fut.done():
            return
        self._spawn_recorded = True
        if fut.cancelled() or fut.exception() is not None:
            return
        result = fut.result()
        if result.mode == "spawned":
            self._daemon_owned_pid = result.pid

    async def _detect_or_spawn_daemon(self, spawn_cwd: Path | None = None) -> bool:
        """Run `detect_or_spawn` on a thread and report the outcome in the
        transcript. Returns True iff this app now owns a daemon.

        The "checking" line is written before the thread starts: which lane
        we are in is not known until it returns."""
        self._write_transcript(
            f"[checking for pare-daemon at {self.socket_path}; "
            "spawning one if it is not running...]"
        )
        loop = asyncio.get_running_loop()
        # A bare executor future, never cancelled (shielded below): cancelling
        # an await on to_thread() drops the thread's result, which here would
        # be the PID of a daemon we just started.
        fut = loop.run_in_executor(
            None,
            functools.partial(
                detect_or_spawn,
                Path(self.socket_path),
                self._daemon_log_dir,
                spawn_cmd=_daemon_command(),
                spawn_cwd=spawn_cwd,
            ),
        )
        self._spawn_recorded = False
        fut.add_done_callback(self._record_spawn)
        self._daemon_spawn_future = fut
        try:
            result = await asyncio.shield(fut)
        except Exception as exc:
            logger.exception("detect_or_spawn raised")
            self._daemon_spawn_failed = True
            self._write_transcript(f"[daemon spawn failed: {type(exc).__name__}: {exc}]")
            return False
        if result.mode == "spawned":
            logs = result.log_path if result.log_path is not None else "none (log dir unwritable)"
            self._write_transcript(
                f"[pare-daemon ready — PID {result.pid}, logs at {logs}]"
            )
            return True
        if result.mode == "failed":
            self._daemon_spawn_failed = True
            reason, _, tail = (result.error or "unknown error").partition("\n")
            self._write_transcript(f"[daemon spawn failed: {reason}]")
            if tail:
                self._write_transcript(f"[{tail}]")
        # "attached": the attached line is written once session.start()
        # actually connects, the same as for a non-spawning launch.
        return False

    async def on_unmount(self) -> None:
        try:
            task = self._daemon_connect_task
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            stop = getattr(self.session, "stop", None)
            if stop is not None:
                await stop()
        finally:
            try:
                await self._reap_owned_daemon()
            finally:
                # Last: a second SIGHUP during the reap must not kill us.
                self._remove_sighup_handler()

    async def _reap_owned_daemon(self) -> None:
        """Stop the daemon this app spawned, if any.

        A spawn still in flight is waited out first (bounded by
        detect_or_spawn's own timeouts) so its PID, if any, is recorded and
        reaped rather than orphaned. `reap` is blocking (bounded by its
        SIGTERM/SIGKILL timeouts), so it too runs on a thread. Any failure is
        logged and never crashes the exit."""
        fut = self._daemon_spawn_future
        if fut is not None:
            if not fut.done():
                try:
                    # Shielded, like the await in _detect_or_spawn_daemon: a
                    # cancel reaching the executor future would mark it
                    # cancelled while the thread runs on, dropping the PID.
                    await asyncio.shield(fut)
                except Exception:
                    pass  # nothing is owned
            # The done-callback may not have run yet (it is scheduled one
            # loop step after set_result): read the PID from the future.
            self._record_spawn(fut)
        pid, self._daemon_owned_pid = self._daemon_owned_pid, None
        if pid is None:
            return
        try:
            outcome = await asyncio.to_thread(reap, pid)
        except Exception:
            logger.exception("failed to reap owned pare-daemon PID %s", pid)
            return
        if outcome == "abandoned":
            logger.warning("pare-daemon PID %s survived SIGKILL; abandoned", pid)
        else:
            logger.info("reaped pare-daemon PID %s: %s", pid, outcome)

    def _write_transcript(self, text: str | Text) -> None:
        """Commit one plain line. Any reply still streaming is committed
        first, so the transcript stays in arrival order."""
        self._commit(self._turn.flush())
        self._write_entry(text)

    def _write_entry(self, content: str | Text | RichMarkdown) -> None:
        try:
            log = self.query_one("#transcript", RichLog)
        except Exception:
            return
        if isinstance(content, RichMarkdown):
            log.write(content, expand=True)
        else:
            log.write(content)

    def _commit(self, entries: list[Committed]) -> None:
        for entry in entries:
            if entry.kind == "markdown":
                self._write_entry(RichMarkdown(entry.text))
            else:
                self._write_entry(entry.text)
        if entries:
            self._live_generation += 1
            self._sync_live_reply()

    def _sync_live_reply(self) -> None:
        """Show `self._turn.live` in the live widget. The Markdown re-render
        is coalesced onto one task, since `Markdown.update` is async and
        tokens arrive faster than it runs. Hiding is immediate; showing
        waits for that render (see `_render_live_reply`), so the block is
        never revealed still holding the previous, committed reply."""
        try:
            scroll = self.query_one("#live-scroll", VerticalScroll)
        except Exception:
            return
        if not self._turn.live:
            scroll.remove_class("-streaming")
        self._live_dirty = True
        if self._live_render_task is None or self._live_render_task.done():
            self._live_render_task = asyncio.create_task(self._render_live_reply())

    async def _render_live_reply(self) -> None:
        try:
            live = self.query_one("#live-reply", Markdown)
            scroll = self.query_one("#live-scroll", VerticalScroll)
            while self._live_dirty:
                self._live_dirty = False
                text, generation = self._turn.live, self._live_generation
                # update(), not append(): append() computes its offsets
                # before taking the widget lock, so overlapping un-awaited
                # appends corrupt the document. A whole-text update is
                # always correct, and coalescing keeps it cheap.
                await live.update(text)
                if text and generation == self._live_generation:
                    # What was rendered belongs to the reply still live.
                    scroll.add_class("-streaming")
                    scroll.scroll_end(animate=False)
        except Exception:
            logger.exception("failed to render the in-progress reply")

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "chat-input":
            return
        line = event.value
        event.input.value = ""
        await self._submit_line(line)

    async def _submit_line(self, line: str) -> None:
        """One line into the daemon, exactly as typing it would send it:
        the `you>` echo first (a reply cannot precede it, and it stays if
        the send fails), then the send with its failure line. `F1`
        (action_show_help) submits "/help" through this same path."""
        if is_sendable(line):
            # Before the send: a reply cannot precede it, and it stays if
            # the send fails.
            self._write_transcript(
                Text.assemble((ECHO_PREFIX, "bold cyan"), line.strip())
            )
        try:
            await parse_input(self.session, line)
        except Exception as exc:
            # Typing into the chat box while there is no live daemon
            # connection (verified: DaemonConnection.send asserts
            # "connect() before send()" -- agent_core/client.py:53) must
            # not crash the whole app out from under an event handler; the
            # UART pane's own I3 resilience is the model here.
            logger.exception("failed to send chat/command input")
            self._write_transcript(f"[send failed: {exc}]")

    def _on_daemon_message(self, msg: object) -> None:
        """The single dispatch point for everything `DaemonSession` fans
        out: an approval request mounts the modal (Task 6's path, left
        undisturbed), a disconnect flips the status bar, and everything
        else feeds the transcript accumulator."""
        if isinstance(msg, ToolApprovalRequestMessage):
            self.handle_tool_approval_request(msg)
            return
        if isinstance(msg, DaemonDisconnected):
            # The turn cannot finish: keep what streamed, and start the
            # next turn with clean dedup state.
            self._commit(self._turn.end_turn())
            self._daemon_connected = False
            self._set_pane_daemon_session(None)
            self._refresh_status_bar()
            return
        # A live message means the daemon is talking to us; a spawn failure
        # from launch is stale.
        self._daemon_spawn_failed = False
        self._append_transcript(msg)
        self._refresh_status_bar()

    def _append_transcript(self, msg: object) -> None:
        committed = self._turn.feed(msg)
        if committed:
            self._commit(committed)
        else:
            self._sync_live_reply()

    def action_cycle_layout(self) -> None:
        """chat 2:1 -> 1:1 -> 1:2 -> dock hidden -> 2:1."""
        try:
            area = self.query_one("#main-area", Horizontal)
        except Exception:
            return
        self._layout_index = (self._layout_index + 1) % len(LAYOUT_CLASSES)
        for cls in LAYOUT_CLASSES:
            if cls:
                area.set_class(cls == LAYOUT_CLASSES[self._layout_index], cls)

    async def action_show_help(self) -> None:
        await self._submit_line("/help")

    def _load_theme(self) -> None:
        name = load_theme(self.tui_config_path)
        if name is None:
            return
        if name not in self.available_themes:
            logger.warning(
                "ignoring unknown theme %r in %s; using the default",
                name, self.tui_config_path,
            )
            return
        self._saved_theme = name
        self.theme = name

    def watch_theme(self, theme: str) -> None:
        """Persist a theme the operator picked (the palette's theme picker
        sets `App.theme`, textual/theme.py ThemeProvider)."""
        if theme == getattr(self, "_saved_theme", theme):
            return
        if save_theme(self.tui_config_path, theme):
            self._saved_theme = theme

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

    def handle_tool_approval_request(self, message: ToolApprovalRequestMessage) -> None:
        """Mount the approval modal for an incoming request (spec I2).

        `push_screen` schedules the mount and returns immediately -- it does
        not await the operator's decision, so nothing here suspends pane
        pollers or any other task running on the event loop. The decision
        arrives later through `_send_approval_response`, which Textual
        invokes via `call_next` once the modal calls `dismiss()`.
        """
        self.push_screen(ApprovalModal(message), callback=self._send_approval_response)

    async def _send_approval_response(self, response: ToolApprovalResponseMessage) -> None:
        """Route the modal's decision back through the one DaemonSession
        send path (Task 4) -- the modal itself knows nothing about the
        session.

        Guarded the same way as `on_input_submitted`'s chat send: the daemon
        can die during the (possibly long) window the operator spends
        deciding, so `self.session.send` can hit the same dead-socket
        failure a chat send can. There it only means the message never went
        out; here an uncaught exception would propagate out of a Textual
        screen-dismiss callback and through `App._handle_exception`, which
        EXITS the whole app -- mid-conversation, right as the operator
        finishes a decision. Catch it, log it, and surface it in the
        transcript instead of crashing.
        """
        try:
            await self.session.send(response)
        except Exception as exc:
            logger.exception("failed to send approval response")
            self._write_transcript(f"[approval response not sent: {exc}]")


def main() -> None:
    config = load_config()
    policy = launch_policy(os.environ)
    app = PareTUI(
        config.socket_path,
        _new_channel_id(),
        os.getcwd(),
        auto_spawn=policy.auto_spawn,
        systemd_managed=policy.systemd_managed,
    )
    app.run()
