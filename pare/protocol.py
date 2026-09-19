"""PARE-local protocol messages, registered with agent_core's registry.

Generic primitives (Chat, Command, StreamChunk, Response, Error, ToolProgress,
approval messages) live in agent_core.protocol. Registering here means
encode_message/decode_message round-trip these types too — PAL does the same
in pal/protocol.py.
"""
from dataclasses import dataclass

from agent_core.protocol import register_message


@register_message
@dataclass
class PaneActivityMessage:
    """One slice of operator-visible device activity, logged through the
    daemon because the TUI cannot open the capture store itself — the daemon
    holds an exclusive flock on it (pare/capture_store.py:59-69).

    kind: "sent" (the operator wrote these bytes) or "observed" (they came
    back from the device). cursor_start/cursor_end locate the slice in the
    worker's cursor space; for "sent" they are equal, since a write occupies
    no range in the read cursor.
    """
    source: str
    session: str
    kind: str
    data_b64: str
    cursor_start: int
    cursor_end: int
    channel_id: str | None = None
    cwd: str | None = None
    type: str = "pane_activity"
