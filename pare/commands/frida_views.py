"""Operator fast-path read commands: enumerate and view worker state as tables.
No LLM in the loop - see pare.commands._frida.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

from agent_core.commands.base import Command
from agent_core.protocol.messages import ResponseMessage

from pare.commands import _frida
from pare.commands._snapshot_render import render_table

_HANDLE = "@snapshots"


class Devices(Command):
    name = "devices"
    args = ""
    description = "List Frida devices (operator fast path, no LLM)."

    async def run(self, raw_args: str, ctx) -> AsyncIterator:
        data = await _frida.call(ctx, "list_devices")
        if data.get("error"):
            yield ResponseMessage(text=data.get("summary", "list_devices failed"))
            return
        yield ResponseMessage(text=render_table(data.get("devices", [])))


class Sessions(Command):
    name = "sessions"
    args = ""
    description = "List live attach sessions with liveness (operator fast path)."

    async def run(self, raw_args: str, ctx) -> AsyncIterator:
        data = await _frida.call(ctx, "list_sessions")
        if data.get("error"):
            yield ResponseMessage(text=data.get("summary", "list_sessions failed"))
            return
        rows = data.get("sessions", [])
        if not rows:
            yield ResponseMessage(text="no live sessions — /attach <target> to start one")
            return
        yield ResponseMessage(text=render_table(rows))


class _EnumView(Command):
    """Base for device-scoped enumerate commands: run the enumerate tool (which
    persists rows to @snapshots — the agent's persisted view), then page the
    captured rows and render the complete table for the operator. This is the
    'dual output shape' (human render + persisted record) for free.
    """

    _tool: str = ""

    async def run(self, raw_args: str, ctx) -> AsyncIterator:
        device_id = raw_args.strip()
        cap = await _frida.call(ctx, self._tool, {"device_id": device_id} if device_id else {})
        if cap.get("error"):
            yield ResponseMessage(text=cap.get("summary", f"{self._tool} failed"))
            return
        source = cap.get("source")
        if not source:
            yield ResponseMessage(text=cap.get("summary", "nothing captured"))
            return
        page = await _frida.call(ctx, "page_capture", {"session_id": _HANDLE, "source": source})
        if page.get("error"):
            yield ResponseMessage(text=page.get("summary", "page_capture failed"))
            return
        rows = page.get("rows", [])
        header = f"{source} · {page.get('total', len(rows))} rows"
        yield ResponseMessage(text=f"{header}\n{render_table(rows)}")


class Ps(_EnumView):
    name = "ps"
    args = "[<device_id>]"
    description = "Enumerate processes into @snapshots and show them (operator fast path)."
    _tool = "enumerate_processes"


class Apps(_EnumView):
    name = "apps"
    args = "[<device_id>]"
    description = "Enumerate installed apps into @snapshots and show them (operator fast path)."
    _tool = "enumerate_applications"
