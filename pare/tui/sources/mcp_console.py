"""McpConsoleSource: the real `ConsoleSource`, over MCP `streamable_http`.

**The invariant this whole module exists to satisfy** (see
`agent_core/workers/client_pool.py`'s module docstring, which this mirrors):
`MCPClient.connect()` enters two nested anyio cancel scopes -- the transport
client's task group, and `ClientSession.__aenter__` -- and anyio binds a
cancel scope to the TASK that entered it. `close()` therefore MUST run in
the same task that ran `connect()`. Closing from a different task raises

    RuntimeError: Attempted to exit cancel scope in a different task than
                  it was entered in

and leaves the connection alive (the transport's task group and the httpx
client are never released). Dispatch is unaffected: calling `call_tool`
from a foreign task is safe. Only teardown carries the affinity requirement.

The natural Textual shape -- `connect()` in `on_mount`, `close()` in
`on_unmount` -- runs those two calls in different tasks (Textual schedules
each lifecycle callback as its own task) and hits this exactly. This class
avoids it the way `MCPClientPool._own` does: one OWNER TASK per source,
spawned by `start()`, which connects, parks on a stop event, and closes in
its own `finally` -- the same task throughout. `stop()` never calls
`close()` itself; it only signals the stop event and waits (bounded) for
the owner task to reach its own `finally` and do it.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
from typing import Any, Callable

from agent_core.workers.client import MCPClient

from pare.tui.sources.base import ConsoleSlice

logger = logging.getLogger(__name__)

#: Bound on how long `stop()` waits for the owner task to unwind after the
#: stop event is set. Without this, an owner wedged inside `client.close()`
#: (or one that simply never checks the stop event) would hang the app's
#: shutdown forever -- see `stop()`.
DEFAULT_STOP_TIMEOUT = 5.0


class McpConsoleSourceError(RuntimeError):
    """A worker reply carried `{"error": ...}`, or the wire call itself failed.

    Raised instead of returning an empty/successful-looking `ConsoleSlice` or
    dict: an error reply is a FAILED call, not "nothing new" -- collapsing
    the two would render a real failure as a reassuring empty read.
    """


class McpConsoleSource:
    """A `ConsoleSource` that talks directly to `pare-hardware-mcp` over
    `streamable_http`.

    Constructed with the worker's endpoint and an EXPLICIT tool-name prefix.
    Through PARE, `agent_core`'s `tool_factory` builds
    `f"{worker.name}_{tool_name}"`, so the bare contract name `console_read`
    dispatches as e.g. `hardware_console_read`. This class connects DIRECTLY
    to the worker, where the bare name is correct -- `worker_prefix`
    defaults to `""` for exactly that reason, and is made explicit here
    rather than hard-coded so a caller connecting through some future
    prefixing proxy is not silently wrong.

    Tracks the attached session id itself: `ConsoleSource.read()` takes no
    session argument (mirroring `FakeConsoleSource`), so the session `attach()`
    discovered is cached on `self` and used for every subsequent `read()`.
    `send()` takes its session explicitly (a `Pane` already owns the id from
    its own `attach()` call) and does not touch the cached one.
    """

    def __init__(
        self,
        endpoint: str,
        worker_prefix: str = "",
        *,
        stop_timeout: float = DEFAULT_STOP_TIMEOUT,
        client_factory: Callable[[str], Any] | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.worker_prefix = worker_prefix
        self.stop_timeout = stop_timeout
        # Test seam only: production always uses the default, a real
        # MCPClient. Tests substitute a task-recording wrapper around a real
        # MCPClient to make the connect/close task affinity OBSERVABLE
        # (see tests/test_tui_mcp_console.py) without weakening anything a
        # real run does.
        self._client_factory = client_factory or (
            lambda endpoint: MCPClient(endpoint=endpoint)
        )
        self._client: MCPClient | None = None
        self._session: str | None = None
        self._owner: asyncio.Task | None = None
        self._ready = asyncio.Event()
        self._stop = asyncio.Event()
        self._connect_error: BaseException | None = None

    def _tool_name(self, bare_name: str) -> str:
        return f"{self.worker_prefix}{bare_name}" if self.worker_prefix else bare_name

    # --- lifecycle: the owner task -----------------------------------
    async def start(self) -> None:
        """Spawn the owner task and wait until it has connected (or failed).

        Idempotent while a previous owner is still alive; safe to call again
        after a failed `start()` or a completed `stop()`.
        """
        if self._owner is not None and not self._owner.done():
            return
        self._ready = asyncio.Event()
        self._stop = asyncio.Event()
        self._connect_error = None
        self._owner = asyncio.create_task(
            self._own(), name=f"mcp-console-owner:{self.endpoint}"
        )
        await self._ready.wait()
        if self._connect_error is not None:
            err, self._connect_error = self._connect_error, None
            raise err

    async def _own(self) -> None:
        """Own this source's connection for its entire lifetime.

        Connect, publish, park, close -- all in this one task, which is
        what makes teardown legal. Mirrors `MCPClientPool._own`.
        """
        client = self._client_factory(self.endpoint)
        try:
            await client.connect()
            await client.initialize()
        except BaseException as exc:  # noqa: BLE001 - surfaced to start()
            self._connect_error = exc
            # MUST close here, in this same task: connect() may have
            # partially entered the transport and/or session scopes even
            # though it raised.
            with contextlib.suppress(BaseException):
                await client.close()
            self._ready.set()
            return
        self._client = client
        self._ready.set()
        try:
            await self._stop.wait()
        finally:
            self._client = None
            with contextlib.suppress(BaseException):
                await client.close()

    async def stop(self) -> None:
        """Signal the owner to stop and wait for it, BOUNDED.

        An owner that will not stop (wedged in `close()`, or one that never
        checks the stop event at all) must not hang the caller forever.
        `asyncio.shield` keeps a timeout here from cancelling the owner task
        itself -- only the wrapper future -- so on a timeout this reaches
        for `Task.cancel()` explicitly, the sanctioned way to abort a
        partially-entered anyio scope from outside its own task.
        """
        owner = self._owner
        if owner is None:
            return
        self._stop.set()
        try:
            await asyncio.wait_for(asyncio.shield(owner), timeout=self.stop_timeout)
        except asyncio.TimeoutError:
            logger.warning(
                "mcp console owner for %r did not stop within %ss; "
                "cancelling it and abandoning the connection",
                self.endpoint, self.stop_timeout,
            )
            owner.cancel()
        except asyncio.CancelledError:
            # stop() itself was cancelled by ITS caller. `shield` means the
            # owner keeps running unaffected; leave it be rather than
            # guessing at its state.
            raise
        finally:
            self._client = None
            if owner.done():
                self._owner = None

    # --- ConsoleSource --------------------------------------------------
    async def attach(self) -> str | None:
        # `Pane.on_mount` calls attach() as its first source interaction,
        # and the source is constructed synchronously in `PareTUI.compose`
        # (`pare/tui/app.py:_build_uart_pane`) -- there is no async hook
        # between construction and this call for the caller to await
        # start() in, so attach() self-starts. Idempotent: start() returns
        # immediately if a live owner is already running.
        if self._client is None:
            await self.start()
        payload = await self._call("console_status", {})
        session = payload.get("session") if payload.get("open") else None
        self._session = session
        return session

    async def read(self, cursor: int, limit: int | None = None) -> ConsoleSlice:
        if self._session is None:
            raise McpConsoleSourceError(
                "no attached session -- call attach() first"
            )
        args: dict[str, Any] = {"session": self._session, "cursor": cursor}
        if limit is not None:
            args["limit"] = limit
        payload = await self._call("console_read", args)
        # `limit_applied` is the worker's clamp, authoritative over whatever
        # `limit` this call requested -- carried through unchanged below.
        return ConsoleSlice(
            data=base64.b64decode(payload["data_b64"]),
            next_cursor=payload["next_cursor"],
            dropped=payload["dropped"],
            remaining=payload["remaining"],
            capture_gaps=payload.get("capture_gaps", []),
            alive=payload["alive"],
            limit_applied=payload["limit_applied"],
        )

    async def send(self, session: str, data: bytes) -> None:
        await self._call(
            "console_send",
            {"session": session, "data_b64": base64.b64encode(data).decode("ascii")},
        )

    async def status(self) -> dict:
        return await self._call("console_status", {})

    # --- dispatch ---------------------------------------------------------
    async def _call(self, bare_tool_name: str, arguments: dict[str, Any]) -> dict:
        """Call a worker tool and return its decoded JSON reply.

        Safe from any task -- only `start`/`stop` carry the task-affinity
        requirement. Raises `McpConsoleSourceError` for a protocol-level
        error result AND for an `{"error": ...}` reply: both are failures,
        never an empty/successful read.
        """
        if self._client is None:
            raise McpConsoleSourceError("McpConsoleSource is not started")
        tool = self._tool_name(bare_tool_name)
        result = await self._client.call_tool(tool, arguments)
        text = _first_text(result)
        if getattr(result, "isError", False):
            raise McpConsoleSourceError(text or f"{tool} failed")
        if text is None:
            raise McpConsoleSourceError(f"{tool} returned no text content")
        payload = json.loads(text)
        if isinstance(payload, dict) and "error" in payload:
            raise McpConsoleSourceError(str(payload["error"]))
        return payload


def _first_text(result: Any) -> str | None:
    """Pull the first text block's `.text` out of a `CallToolResult`."""
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text is not None:
            return text
    return None
