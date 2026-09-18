"""Approval modal: renders a ToolApprovalRequestMessage, collects the
operator's decision, and resolves to a ToolApprovalResponseMessage --
without blocking the app's other tasks.

Spec I2: while this modal is open and unanswered, pane pollers must keep
running -- the operator's usual reason to approve a hardware call is what the
console is showing right now. A `ModalScreen` is modal for INPUT only. This
widget never awaits the operator's decision itself: pushing it
(`App.push_screen(modal, callback=...)`) returns immediately, and the
decision is delivered later through Textual's own dismiss/callback path --
`Screen.dismiss()` invokes the registered `ResultCallback`, which schedules
the callback via `MessagePump.call_next` (textual/screen.py) rather than
resuming an awaited call here. Nothing on the event loop is suspended while
the modal sits open, so pane-poller tasks scheduled elsewhere keep making
progress. See `pare/tui/app.py`'s `handle_tool_approval_request` for the
mounting side and where the callback actually sends the response.
"""
from __future__ import annotations

from agent_core.protocol import ToolApprovalRequestMessage, ToolApprovalResponseMessage
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static

from pare.tui.sanitize import clip_args


class ApprovalModal(ModalScreen[ToolApprovalResponseMessage]):
    """Collects one approve/deny decision for a `ToolApprovalRequestMessage`.

    The decision set mirrors exactly what the gate accepts
    (`agent_core/workers/risk_pool.py:409-414`): approve once, approve for
    session, approve with justification, deny. A `critical` request never
    offers approve-for-session -- the gate's own check is
    `if effective != "critical" and self.is_session_approved(...)`, so a
    session grant is never even consulted for a critical tool, and approving
    a critical tool at all additionally requires a non-empty justification
    (mirrors `agent_core/adapters/cli.py:64-75`'s `bool(justification) if
    is_critical else True`).
    """

    DEFAULT_CSS = """
    ApprovalModal {
        align: center middle;
    }

    #approval-dialog {
        width: 60%;
        max-width: 80;
        height: auto;
        border: thick $error 80%;
        background: $surface;
        padding: 1 2;
    }

    #approval-buttons {
        height: auto;
        margin-top: 1;
    }

    #approval-buttons Button {
        margin-right: 1;
    }
    """

    BINDINGS = [
        Binding("escape", "deny_unanswered", "Deny", show=True),
    ]

    def __init__(self, request: ToolApprovalRequestMessage) -> None:
        super().__init__()
        self.request = request

    @property
    def is_critical(self) -> bool:
        return self.request.effective_tier == "critical"

    def compose(self) -> ComposeResult:
        request = self.request
        with Vertical(id="approval-dialog"):
            yield Static(
                f"Approval required: {request.worker}.{request.tool}",
                id="approval-title",
            )
            yield Static(
                f"declared={request.declared_tier}  effective={request.effective_tier}",
                id="approval-tiers",
            )
            # Tool arguments carry attacker-influenced bytes (brief
            # requirement 3) -- always render through clip_args, exactly as
            # the CLI does (agent_core/adapters/cli.py:31-46), never
            # str(request.arguments) directly.
            yield Static(clip_args(request.arguments), id="approval-args")
            yield Input(
                placeholder="justification (required for critical)",
                id="approval-justification",
            )
            with Horizontal(id="approval-buttons"):
                yield Button("Approve once", id="approve-once", variant="success")
                if not self.is_critical:
                    yield Button(
                        "Approve for session", id="approve-session", variant="success"
                    )
                yield Button(
                    "Approve with justification",
                    id="approve-justified",
                    variant="warning",
                )
                yield Button("Deny", id="deny", variant="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        button_id = event.button.id
        if button_id == "approve-once":
            self._resolve(approved=True, justification=None, scope="once")
        elif button_id == "approve-session":
            self._resolve(approved=True, justification=None, scope="session")
        elif button_id == "approve-justified":
            justification = self.query_one("#approval-justification", Input).value.strip()
            approved = bool(justification) if self.is_critical else True
            self._resolve(
                approved=approved, justification=justification or None, scope="once"
            )
        elif button_id == "deny":
            self._resolve(approved=False, justification=None, scope="once")

    def action_deny_unanswered(self) -> None:
        """Closing the modal without an explicit decision (Escape) must deny,
        never leave the daemon waiting on a decision that never comes (brief
        requirement 4)."""
        self._resolve(approved=False, justification=None, scope="once")

    def _resolve(self, *, approved: bool, justification: str | None, scope: str) -> None:
        self.dismiss(
            ToolApprovalResponseMessage(
                proposal_id=self.request.proposal_id,
                approved=approved,
                justification=justification,
                scope=scope,
            )
        )
