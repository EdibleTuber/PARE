"""/health — daemon status command."""
from collections.abc import AsyncIterator

from agent_core.commands.base import Command
from agent_core.protocol.messages import ResponseMessage


class Health(Command):
    """Report PARE daemon status: agent name, model, configured endpoints."""

    name = "health"
    args = ""  # takes no arguments; required by Command base + CommandRegistry.metadata()
    description = "Show PARE daemon status and configured endpoints."

    async def run(self, raw_args: str, ctx) -> AsyncIterator:
        cfg = ctx.agent.config
        lines = [
            f"agent: {ctx.agent.name}",
            f"model: {cfg.model}",
            f"inference: {cfg.inference_url}",
            f"vault: {cfg.vault_path}",
            f"apk_re_agents: {cfg.apk_re_agents_url}",
        ]

        mgr = getattr(ctx.agent, "worker_manager", None)
        if mgr is not None:
            statuses = mgr.status()
            loaded = ", ".join(f"{s.name}({s.tool_count})" for s in statuses if s.loaded)
            idle = ", ".join(s.name for s in statuses if not s.loaded)
            lines.append(f"workers: {loaded or 'none'}"
                         + (f" · unloaded: {idle}" if idle else ""))

        yield ResponseMessage(text="\n".join(lines))
