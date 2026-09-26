"""StatusBar: a one-line operator summary of daemon connection state,
per-pane source health, the active channel id, and the UART cursor.

`render_for` is the plain-text seam tests assert against -- mirroring
`UartPane.snapshot_lines()`'s pattern in `pare/tui/panes/uart.py` -- and it is
deliberately callable with no live App and no mounted widget: see
`tests/test_tui_app_integration.py::test_status_bar_reflects_source_failure`,
which constructs a bare `StatusBar()` and calls `render_for` directly against
a list of panes, asserting only that the healthy and failed renders DIFFER
(never an exact string, per the plan's "assert the difference" rule).
"""
from __future__ import annotations

from typing import Literal

from textual.widgets import Static

from pare.tui.panes.base import Pane

DaemonState = Literal["up", "down", "spawn-failed"]
_DAEMON_LABELS: dict[str, str] = {
    "up": "daemon:up",
    "down": "daemon:DOWN",
    "spawn-failed": "daemon:spawn-failed",
}


class StatusBar(Static):
    """Renders one status line from ambient connection state plus whatever
    panes it is handed.

    Connection state (`daemon_connected`, `channel_id`) is set by the app as
    the daemon connection and active channel change; per-pane health and
    cursor come from the `panes` argument to `render_for` on each refresh --
    `Pane` already tracks `healthy`/`last_error`/`cursor` (`pare/tui/panes/
    base.py`), so this widget does not duplicate that state, only reads it.
    """

    def __init__(
        self,
        *,
        daemon_state: DaemonState = "down",
        channel_id: str = "",
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
        # Backwards-compat: accept daemon_connected as a keyword; True -> "up".
        daemon_connected: bool | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        if daemon_connected is not None:
            daemon_state = "up" if daemon_connected else "down"
        self.daemon_state = daemon_state
        self.channel_id = channel_id

    @property
    def daemon_state(self) -> DaemonState:
        return self._daemon_state

    @daemon_state.setter
    def daemon_state(self, value: DaemonState) -> None:
        if value not in _DAEMON_LABELS:
            raise ValueError(
                f"invalid daemon_state {value!r}; expected one of "
                f"{sorted(_DAEMON_LABELS)}"
            )
        self._daemon_state = value

    @property
    def daemon_connected(self) -> bool:
        """Backwards-compat: True iff daemon_state == 'up'."""
        return self.daemon_state == "up"

    @daemon_connected.setter
    def daemon_connected(self, value: bool) -> None:
        self.daemon_state = "up" if value else "down"

    def render_for(self, panes: list[Pane]) -> str:
        """Pure text: no widget lookups, no DOM -- safe to call on an
        unmounted, even un-composed, StatusBar instance."""
        daemon = _DAEMON_LABELS[self.daemon_state]
        channel = f"channel:{self.channel_id or '-'}"
        if not panes:
            panes_text = "panes:none"
        else:
            panes_text = " ".join(_pane_summary(pane) for pane in panes)
        return f"{daemon}  {channel}  {panes_text}"

    def refresh_for(self, panes: list[Pane]) -> None:
        """Re-render and push the text to screen in one call -- what the
        app's periodic refresh and message handlers call. `render_for`
        itself stays side-effect-free so a test needs no mounted widget."""
        self.update(self.render_for(panes))


def _pane_summary(pane: Pane) -> str:
    health = "ok" if pane.healthy else f"FAIL:{pane.last_error}"
    return f"{type(pane).__name__}[{health} cursor={pane.cursor}]"
