"""The PARE TUI application: chat beside live device panes.

Replaces pare-cli as the operator surface. See
docs/superpowers/specs/2026-09-18-pare-tui-design.md.
"""
from __future__ import annotations

import os
from datetime import datetime

from agent_core.protocol import ToolApprovalRequestMessage, ToolApprovalResponseMessage
from textual.app import App, ComposeResult
from textual.widgets import Footer, Header

from pare.config import load_config
from pare.tui.session import DaemonSession
from pare.tui.widgets.approval import ApprovalModal


def _new_channel_id() -> str:
    """A fresh channel per launch, so a session starts clean instead of
    replaying cli-default. Mirrors pare/cli.py:_new_channel_id."""
    return datetime.now().strftime("tui-%Y%m%d-%H%M%S")


class PareTUI(App):
    """The application shell. Layout and wiring arrive in later tasks."""

    TITLE = "PARE"

    def __init__(self, socket_path, channel_id: str, cwd: str) -> None:
        super().__init__()
        self.socket_path = socket_path
        self.channel_id = channel_id
        self.cwd = cwd
        # Construction only -- no connection is opened here. Starting it
        # (session.start()) and subscribing it to inbound wire messages is
        # later-task wiring; this task only needs the one send() path the
        # approval modal's decision travels back through.
        self.session = DaemonSession(socket_path, channel_id, cwd)

    def compose(self) -> ComposeResult:
        yield Header()
        yield Footer()

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
        session."""
        await self.session.send(response)


def main() -> None:
    config = load_config()
    app = PareTUI(config.socket_path, _new_channel_id(), os.getcwd())
    app.run()
