"""/mitm — operator control for the HTTPS-traffic daemon + worker."""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from agent_core.commands.base import Command
from agent_core.protocol.messages import ResponseMessage


_NOT_FOUND = (
    "pare-mitm-daemon not found — install the worker into this venv: "
    "pip install -e ~/Projects/pare-mitm-mcp")


class DaemonNotFound(Exception):
    """Raised when the pare-mitm-daemon executable isn't on PATH."""


async def _launch_daemon(subcommand: str) -> tuple[int, str]:
    """Run `pare-mitm-daemon <subcommand>` and return (rc, full output).

    The full output is relayed, not just the last line: `up` prints the
    mitmweb UI URL (which carries the auth token) on its own line, and the
    operator needs that to open the side-by-side view.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "pare-mitm-daemon", subcommand,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    except FileNotFoundError:
        raise DaemonNotFound(_NOT_FOUND)
    out, _ = await proc.communicate()
    return proc.returncode or 0, (out or b"").decode().strip()


def _result_text(result) -> str:
    for block in getattr(result, "content", []) or []:
        if getattr(block, "type", None) == "text":
            return block.text
    return "{}"


_SUBCOMMANDS = ("up", "down", "status")


class Mitm(Command):
    name = "mitm"
    args = "[up|down|status]"
    description = "Start / stop / check the HTTPS-traffic (mitmproxy) daemon"

    async def run(self, raw_args: str, ctx) -> AsyncIterator:
        sub = raw_args.strip().split()[0] if raw_args.strip() else "status"

        if sub not in _SUBCOMMANDS:
            yield ResponseMessage(
                text=f"unknown /mitm subcommand {sub!r} — usage: /mitm {self.args}")
            return

        if sub in ("up", "down"):
            try:
                rc, output = await _launch_daemon(sub)
            except (DaemonNotFound, FileNotFoundError):
                yield ResponseMessage(text=_NOT_FOUND)
                return
            prefix = "" if rc == 0 else f"(exit {rc}) "
            yield ResponseMessage(text=prefix + (output or f"started pare-mitm-daemon {sub}"))
            return

        # status (default)
        result = await ctx.agent.tool_pool.call_tool("mitm", "capture_health", {}, ctx=ctx)
        payload = json.loads(_result_text(result))
        yield ResponseMessage(text=payload.get("summary", "no status"))
