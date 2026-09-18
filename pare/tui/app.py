"""The PARE TUI application: chat beside live device panes.

Replaces pare-cli as the operator surface. See
docs/superpowers/specs/2026-09-18-pare-tui-design.md.
"""
from __future__ import annotations

import os
from datetime import datetime

from textual.app import App, ComposeResult
from textual.widgets import Footer, Header

from pare.config import load_config


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

    def compose(self) -> ComposeResult:
        yield Header()
        yield Footer()


def main() -> None:
    config = load_config()
    app = PareTUI(config.socket_path, _new_channel_id(), os.getcwd())
    app.run()
