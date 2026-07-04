"""Operator fast-path read commands: enumerate and view worker state as tables.
No LLM in the loop - see pare.commands._frida.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

from agent_core.commands.base import Command
from agent_core.protocol.messages import ResponseMessage

from pare.commands import _frida
from pare.commands._snapshot_render import render_table

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
    now returns the full list as JSON) and render the complete table. The same
    call is captured to the project store at the wire, so /snapshot can re-view
    it later; this command renders the payload it already has.
    """

    _tool: str = ""
    _rows_key: str = ""

    async def run(self, raw_args: str, ctx) -> AsyncIterator:
        device_id = raw_args.strip()
        data = await _frida.call(ctx, self._tool, {"device_id": device_id} if device_id else {})
        if data.get("error"):
            yield ResponseMessage(text=data.get("summary", f"{self._tool} failed"))
            return
        rows = data.get(self._rows_key, [])
        if not rows:
            yield ResponseMessage(text=data.get("summary", "nothing captured"))
            return
        header = f"{self._tool} · {len(rows)} rows"
        yield ResponseMessage(text=f"{header}\n{render_table(rows)}")


class Ps(_EnumView):
    name = "ps"
    args = "[<device_id>]"
    description = "Enumerate processes and show them (operator fast path)."
    _tool = "enumerate_processes"
    _rows_key = "processes"


class Apps(_EnumView):
    name = "apps"
    args = "[<device_id>]"
    description = "Enumerate installed apps and show them (operator fast path)."
    _tool = "enumerate_applications"
    _rows_key = "applications"
