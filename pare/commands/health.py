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
            lines.extend(_worker_lines(mgr.status()))

        yield ResponseMessage(text="\n".join(lines))


def _worker_lines(statuses) -> list[str]:
    """Worker health, one line per worker plus a summary.

    /health is the command an operator reflexively types first, and it used to
    be strictly LESS informative than /worker list: no last_error, no
    transport, no endpoint. That was tolerable when every worker was a local
    subprocess. With three machines in play, "which host" and "is it still
    answering" are the first two questions, and sending the operator to a
    second command to get them is friction at exactly the wrong moment.
    """
    loaded = [s for s in statuses if s.loaded]
    idle = [s for s in statuses if not s.loaded]
    lines = ["workers: " + (", ".join(f"{s.name}({s.tool_count})" for s in loaded)
                            or "none")
             + (" · unloaded: " + ", ".join(s.name for s in idle) if idle else "")]

    for s in statuses:
        where = getattr(s, "endpoint", None) or s.transport
        reachable = getattr(s, "reachable", None)
        if not s.loaded:
            state = "unloaded"
        elif reachable is False:
            # The case this exists for: `loaded` alone would read as healthy.
            state = "LOADED but UNREACHABLE"
        elif reachable is True:
            state = "loaded, reachable"
        else:
            # stdio, or networked-but-not-yet-probed. Do not claim a liveness
            # observation that was never made.
            state = "loaded"
        line = f"  {s.name}: {state} · {where}"
        if s.last_error:
            line += f"\n      last error: {s.last_error}"
        lines.append(line)
    return lines
