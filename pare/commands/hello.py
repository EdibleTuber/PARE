"""Hello — minimal example command."""
from collections.abc import AsyncIterator

from agent_core.commands.base import Command
from agent_core.protocol.messages import ResponseMessage


class Hello(Command):
    name = "hello"
    args = "[<name>]"
    description = "Greet someone (or the world)"

    async def run(self, raw_args: str, ctx) -> AsyncIterator:
        target = raw_args.strip() or "world"
        yield ResponseMessage(text=f"Hello, {target}!")
