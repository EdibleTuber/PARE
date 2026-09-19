"""The pane/source boundary: `ConsoleSource` and `ConsoleSlice`.

A `ConsoleSource` is whatever a `Pane` polls for device output -- in v1 that
is an MCP client wrapping `pare-hardware-mcp`'s `console_*` tools, but
nothing in this module or in `pare.tui.panes.base` names MCP or any
concrete source. That is deliberate: the pane/dock boundary is testable
against a stub alone (see `tests/test_tui_pane_dock.py`), and the real MCP
source (Task 8's fake, Task 10's concrete `mcp_console.py`) is built to this
Protocol rather than the other way around.

`ConsoleSlice` mirrors `console_read`'s reply field-for-field
(`pare-hardware-mcp/src/pare_hardware_mcp/tools.py:194-198`):

    return _ok(session=session,
               data_b64=base64.b64encode(data).decode("ascii"),
               next_cursor=next_cursor, dropped=dropped, remaining=remaining,
               limit_applied=capped,
               alive=sess.alive, capture_gaps=capture_gaps)

`session` is not carried on the slice -- `attach()` already returns the
session id once, and `read()` is called with a cursor the pane already
owns, so echoing it back on every slice would be a value the pane already
has. `data_b64` becomes `data: bytes` (decoded -- a source implementation
does the base64 decode before handing back a slice; nothing downstream of
this boundary should have to know the wire encoding). Every other field
survives under its own name: dropping one here would make Task 9's honesty
rendering (surfacing `dropped`/`capture_gaps`/`limit_applied` truthfully to
the operator) impossible to write against this type.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass
class ConsoleSlice:
    """One `console_read` reply, decoded and field-matched.

    `dropped`: bytes evicted by ring-buffer wraparound before this read
    could see them (tools.py's comment above `capture_gaps`: "`dropped`
    only ever counts bytes evicted by ring-buffer wraparound").

    `capture_gaps`: suspensions (e.g. a baud scan pausing the reader) whose
    recorded position falls inside this slice's byte range -- a hole in
    time that `dropped` cannot see, because no bytes were ever evicted.

    `remaining`: bytes still unread past `next_cursor` at the source, so a
    caller can tell a clamped read from a buffer that happened to end
    exactly at `limit_applied`.

    `limit_applied`: the ceiling actually used for this read -- may be
    lower than any limit the caller requested.

    `alive`: whether the source session was still alive at read time.
    """

    data: bytes
    next_cursor: int
    dropped: int
    remaining: int
    capture_gaps: list
    alive: bool
    limit_applied: int


@runtime_checkable
class ConsoleSource(Protocol):
    """What a `Pane` polls. No method here, or any implementation a `Pane`
    is handed, may assume MCP -- a `Pane` and the dock that manages it must
    work against any object satisfying this Protocol."""

    async def attach(self) -> str | None:
        """Attach to (or discover) a live session. Returns a session id, or
        `None` when there is no live session to attach to -- not an
        exception; "no session yet" is an expected, poll-again state."""
        ...

    async def read(self, cursor: int, limit: int | None = None) -> ConsoleSlice:
        """Read from `cursor` onward, at most `limit` bytes (source-defined
        default when `None`). Raises on a source-level failure -- the pane
        is what turns that into an in-place error render and backoff, not
        this method."""
        ...

    async def send(self, session: str, data: bytes) -> None:
        """Write `data` to the session identified by `session`."""
        ...

    async def status(self) -> dict:
        """Source/session state, as a plain dict -- shape is source-defined
        (mirrors `console_status`'s reply, which is not field-matched here
        because no consumer in this task needs it structured)."""
        ...
