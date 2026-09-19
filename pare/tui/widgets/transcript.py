"""Turn accumulation for the chat transcript.

Ported from agent_core/adapters/cli.py:118-146 rather than imported: the CLI's
_TurnPrinter returns (text, end) tuples shaped for print(), and it is private.
The wire behaviour it encodes is what matters here, not its signature.
"""
from __future__ import annotations

from agent_core.protocol import (
    ErrorMessage,
    LearningCandidateProposalMessage,
    ResponseMessage,
    StreamChunkMessage,
    ToolProgressMessage,
)

from pare.tui.sanitize import clip_args


def is_turn_end(msg: object) -> bool:
    """A turn closes on ResponseMessage or ErrorMessage — the same rule the
    REPL's drain loop uses (agent_core/adapters/cli.py:202-203)."""
    return isinstance(msg, (ResponseMessage, ErrorMessage))


class TurnAccumulator:
    """Decides what text each message contributes, so a streamed turn's answer
    is not rendered twice. One instance per session; reset() between turns."""

    def __init__(self) -> None:
        self._streamed: list[str] = []

    def reset(self) -> None:
        self._streamed.clear()

    def feed(self, msg: object) -> str | None:
        if isinstance(msg, StreamChunkMessage):
            self._streamed.append(msg.token)
            return msg.token
        if isinstance(msg, ResponseMessage):
            if self._streamed and msg.text == "".join(self._streamed):
                return None      # duplicate of the stream: already rendered
            return msg.text
        if isinstance(msg, ErrorMessage):
            return f"Error: {msg.error}"
        if isinstance(msg, ToolProgressMessage):
            return f"  [{msg.tool}({clip_args(msg.arguments)})]"
        if isinstance(msg, LearningCandidateProposalMessage):
            return f"\n[Learning candidate: {msg.title}]\n{msg.body}\n"
        # Never drop an unrecognised type: a message we cannot render is still
        # evidence something happened. Mirrors _default_format's fallback
        # (agent_core/adapters/cli.py:115).
        return f"[unrendered {type(msg).__name__}]"
