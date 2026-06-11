"""Operator fast-path action commands: device selection and session lifecycle.
No LLM in the loop - see pare.commands._frida.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

from agent_core.commands.base import Command
from agent_core.protocol.messages import ResponseMessage

from pare.commands import _frida


class Select(Command):
    name = "select"
    args = "<device_id>"
    description = "Select a Frida device by id (operator fast path)."

    async def run(self, raw_args: str, ctx) -> AsyncIterator:
        device_id = raw_args.strip()
        if not device_id:
            yield ResponseMessage(text="usage: /select <device_id> — run /devices to list ids")
            return
        data = await _frida.call(ctx, "select_device", {"device_id": device_id})
        if data.get("error"):
            yield ResponseMessage(text=data.get("summary", "select_device failed"))
            return
        yield ResponseMessage(
            text=f"selected {data.get('name')} ({data.get('id')}, {data.get('type')})")


class Attach(Command):
    name = "attach"
    args = "<target> [<device_id>]"
    description = "Attach to a process by pid or name (operator fast path)."

    async def run(self, raw_args: str, ctx) -> AsyncIterator:
        parts = raw_args.split()
        if not parts:
            yield ResponseMessage(text="usage: /attach <pid|name> [<device_id>]")
            return
        args = {"target": parts[0]}
        if len(parts) > 1:
            args["device_id"] = parts[1]
        data = await _frida.call(ctx, "attach", args)
        if data.get("error"):
            yield ResponseMessage(text=data.get("summary", "attach failed"))
            return
        yield ResponseMessage(
            text=f"attached {data.get('name')} pid {data.get('pid')} → session {data.get('session_id')}")


class Detach(Command):
    name = "detach"
    args = "<session_id>"
    description = "Detach a session and tear down its state (operator fast path)."

    async def run(self, raw_args: str, ctx) -> AsyncIterator:
        session_id = raw_args.strip()
        if not session_id:
            yield ResponseMessage(text="usage: /detach <session_id> — run /sessions to list them")
            return
        data = await _frida.call(ctx, "detach", {"session_id": session_id})
        if data.get("error"):
            yield ResponseMessage(text=data.get("summary", "detach failed"))
            return
        yield ResponseMessage(text=f"detached {data.get('session_id')}")
