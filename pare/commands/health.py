"""/health — daemon status command."""
import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

from agent_core.commands.base import Command
from agent_core.protocol.messages import ResponseMessage

from pare.arcticbase import ArcticBaseClient, ArcticBaseError, NotConfigured
from pare.project_slug import ProjectSlugError, resolve_project_slug

# Short on purpose: /health is typed when something is already wrong, and the
# most likely wrongness is that the workbench host is not answering. Waiting the
# client's default on a dead host makes the diagnosis tool feel like the fault.
_PROBE_TIMEOUT = 2.0


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

        lines.extend(await _arcticbase_lines(ctx))
        lines.extend(_artifact_root_lines(ctx))

        yield ResponseMessage(text="\n".join(lines))


async def _arcticbase_lines(ctx) -> list[str]:
    """§10.1. Eliminate the ArcticBase candidate, or point at it.

    Reports three separable facts, because they fail separately: is the host
    answering, which workbench would a finding go to, and is the daemon still
    beating.
    """
    cfg = ctx.agent.config
    url = getattr(cfg, "arcticbase_url", "") or ""
    if not url:
        return ["arcticbase: not configured (set PARE_ARCTICBASE_URL) — "
                "publishing is off; nothing was probed"]

    client = ArcticBaseClient(url, timeout=_PROBE_TIMEOUT)
    try:
        health = await asyncio.to_thread(client.health)
        state = f"ok (v{health.get('version', '?')})"
    except NotConfigured as exc:            # cannot happen given the guard above
        state = f"not configured: {exc}"
    except ArcticBaseError as exc:
        state = f"UNREACHABLE — {exc}"
    except Exception as exc:
        # Deliberately broad. /health is what an operator types when something
        # is ALREADY wrong, so it must degrade to a bad line rather than a
        # traceback. A malformed PARE_ARCTICBASE_URL raises ValueError out of
        # urllib's own url parsing, which is not an ArcticBaseError and killed
        # the whole command before this clause existed.
        state = f"BAD URL or probe failed — {type(exc).__name__}: {exc}"
    lines = [f"arcticbase: {url} · {state}"]
    lines.append("  " + _project_line(ctx, url))
    lines.append("  " + _heartbeat_line(ctx))
    return lines


def _project_line(ctx, base_url: str) -> str:
    """Which workbench a finding published right now would land in.

    §10 asks for this so the operator knows which page to open before walking
    away from the bench.
    """
    cwd = getattr(ctx, "cwd", None)
    if not cwd:
        return "project: no project (this request carried no cwd)"
    cfg = ctx.agent.config
    try:
        slug = resolve_project_slug(
            cwd, marker=getattr(cfg, "project_marker", None) or ".pare",
            home=Path.home())
    except ProjectSlugError:
        # Not an error to report loudly here: a daemon outside a project is a
        # different fact from a broken one. It only becomes an error when
        # something actually tries to publish.
        return f"project: no project at {cwd}"
    return f"project: {slug} · {base_url.rstrip('/')}/wb/{slug}"


def _heartbeat_line(ctx) -> str:
    hb = getattr(ctx.agent, "_heartbeat", None)
    if hb is None:
        return "heartbeat: off"
    last = getattr(hb, "last_beat_at", None)
    boot = getattr(hb, "boot_id", "?")
    if last is None:
        # Distinct from stale. Never-written and stopped are different problems
        # with different next steps.
        line = f"heartbeat: boot {boot} · never written"
    else:
        line = f"heartbeat: boot {boot} · last write {last}{_age(last)}"
    err = getattr(hb, "last_error", None)
    if err:
        line += f"\n    last beat error: {err}"
    return line


def _age(iso: str) -> str:
    """Best-effort age. Never guesses: an unparseable stamp says nothing."""
    try:
        stamp = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        seconds = (datetime.now(UTC) - stamp).total_seconds()
    except Exception:
        return ""
    return f" ({int(seconds)}s ago)"


def _artifact_root_lines(ctx) -> list[str]:
    """The DECLARED artifact root, and nothing more.

    §10.1 also wants each root's live state. That cannot come from here: the
    artifact lives on the worker's machine, so resolving or stat-ing the path
    daemon-side resolves against the wrong namespace — §5.4 corrects exactly
    that mistake and calls it worse than not checking. §8.4 puts the live answer
    behind a low-tier `bench_status` tool on the worker, which does not exist
    yet. Until it does, this reports the declaration and says that is all it is.
    """
    registry = getattr(ctx.agent, "worker_registry", None)
    if registry is None:
        return []
    out = []
    for spec in registry.all():
        root = getattr(spec, "artifact_root", None)
        if not root:
            continue
        out.append(f"  {spec.name} artifact_root: {root} "
                   f"(declared in workers.yaml; not checked here — "
                   f"needs the worker's own bench_status tool)")
    return out


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
