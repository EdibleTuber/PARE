"""The PARE TUI application: chat beside live device panes.

Replaces pare-cli as the operator surface. See
docs/superpowers/specs/2026-09-18-pare-tui-design.md.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime

from agent_core.protocol import ToolApprovalRequestMessage, ToolApprovalResponseMessage
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header, Input, RichLog

from pare.config import load_config
from pare.tui.panes.base import PaneDock
from pare.tui.panes.uart import UartPane
from pare.tui.session import DaemonDisconnected, DaemonSession
from pare.tui.sources.fake_console import FakeConsoleSource
from pare.tui.widgets.approval import ApprovalModal
from pare.tui.widgets.statusbar import StatusBar
from pare.tui.widgets.transcript import TurnAccumulator, is_turn_end

logger = logging.getLogger(__name__)


def _new_channel_id() -> str:
    """A fresh channel per launch, so a session starts clean instead of
    replaying cli-default. Mirrors pare/cli.py:_new_channel_id."""
    return datetime.now().strftime("tui-%Y%m%d-%H%M%S")


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

    #pane-dock {
        width: 1fr;
        height: 1fr;
        border-left: solid $primary;
    }

    StatusBar {
        dock: bottom;
        height: 1;
        background: $panel;
    }
    """

    def __init__(self, socket_path, channel_id: str, cwd: str) -> None:
        super().__init__()
        self.socket_path = socket_path
        self.channel_id = channel_id
        self.cwd = cwd
        # Construction only -- no connection is opened here. `on_mount`
        # starts it and subscribes to its message stream once the app is
        # actually running.
        self.session = DaemonSession(socket_path, channel_id, cwd)
        self._turn = TurnAccumulator()
        self._daemon_connected = False

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="main-area"):
            with Vertical(id="chat-area"):
                yield RichLog(id="transcript", wrap=True, markup=False)
                yield Input(
                    placeholder="Type a message, or /command ...", id="chat-input"
                )
            yield PaneDock([self._build_uart_pane()], id="pane-dock")
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
        """
        return UartPane(
            source=FakeConsoleSource(),
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
            try:
                await start()
            except Exception:
                # No running daemon is an expected state to launch into
                # (spec's "done when" gate: don't block on one existing) --
                # log it and leave the app usable rather than crashing the
                # whole UI before it can even render.
                logger.exception(
                    "failed to connect to daemon at %s", self.socket_path
                )
            else:
                self._daemon_connected = True
                self._set_pane_daemon_session(self.session)
        self._refresh_status_bar()

    async def on_unmount(self) -> None:
        stop = getattr(self.session, "stop", None)
        if stop is not None:
            await stop()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "chat-input":
            return
        line = event.value
        event.input.value = ""
        try:
            await parse_input(self.session, line)
        except Exception as exc:
            # Typing into the chat box while there is no live daemon
            # connection (verified: DaemonConnection.send asserts
            # "connect() before send()" -- agent_core/client.py:53) must
            # not crash the whole app out from under an event handler; the
            # UART pane's own I3 resilience is the model here.
            logger.exception("failed to send chat/command input")
            try:
                self.query_one("#transcript", RichLog).write(
                    f"[send failed: {exc}]"
                )
            except Exception:
                pass

    def _on_daemon_message(self, msg: object) -> None:
        """The single dispatch point for everything `DaemonSession` fans
        out: an approval request mounts the modal (Task 6's path, left
        undisturbed), a disconnect flips the status bar, and everything
        else feeds the transcript accumulator."""
        if isinstance(msg, ToolApprovalRequestMessage):
            self.handle_tool_approval_request(msg)
            return
        if isinstance(msg, DaemonDisconnected):
            self._daemon_connected = False
            self._set_pane_daemon_session(None)
            self._refresh_status_bar()
            return
        self._append_transcript(msg)
        if is_turn_end(msg):
            self._turn.reset()
        self._refresh_status_bar()

    def _append_transcript(self, msg: object) -> None:
        text = self._turn.feed(msg)
        if not text:
            return
        try:
            log = self.query_one("#transcript", RichLog)
        except Exception:
            return
        log.write(text)

    def _refresh_status_bar(self) -> None:
        try:
            status_bar = self.query_one("#status-bar", StatusBar)
            pane_dock = self.query_one("#pane-dock", PaneDock)
        except Exception:
            return
        status_bar.daemon_connected = self._daemon_connected
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
            try:
                self.query_one("#transcript", RichLog).write(
                    f"[approval response not sent: {exc}]"
                )
            except Exception:
                pass


def main() -> None:
    config = load_config()
    app = PareTUI(config.socket_path, _new_channel_id(), os.getcwd())
    app.run()
