"""Shared plumbing for operator fast-path commands.

These commands drive the frida worker DIRECTLY through the audited tool_pool -
the LLM is never in this path (commands bypass the model, exactly like
/snapshot). Every call is still risk-gated and audited by RiskAwareToolPool,
identically to an agent-initiated call.
"""
from __future__ import annotations

import json

WORKER = "frida"


def result_text(result) -> str:
    """Concatenate the text blocks of an MCP CallToolResult."""
    return "".join(getattr(b, "text", "") for b in (getattr(result, "content", None) or []))


def unavailable(ctx, worker: str = WORKER) -> str | None:
    """Operator-facing reason this worker cannot serve a call, or None.

    Advisory only — enforcement stays in RiskAwareToolPool.call_tool. Its value
    here is avoiding a pointless approval prompt: with the spec gone from the
    pool, resolve_declared_tier returns "high" for an unknown worker, so the
    operator would be asked to approve a call that then fails with KeyError.

    getattr-guarded because several test fakes (and any partially-constructed
    agent) have a tool_pool but no worker_manager.
    """
    mgr = getattr(ctx.agent, "worker_manager", None)
    return mgr.unavailable_reason(worker) if mgr is not None else None


async def call(ctx, tool: str, args: dict | None = None) -> dict:
    """Call a frida worker tool through the audited pool and parse its JSON
    envelope. Returns the parsed dict, or an error-shaped dict ({"error": True,
    "summary": ...}) on a transport error or non-JSON result so callers render
    failures uniformly.

    capture=False: the result is stored to the project capture store at the
    wire (risk-tier auditing still runs), but the pool must never substitute a
    stub in place of the real payload — the operator sees the actual response.
    """
    why = unavailable(ctx)
    if why:
        return {"error": True, "summary": why}
    result = await ctx.agent.tool_pool.call_tool(WORKER, tool, args or {}, ctx=ctx, capture=False)
    if getattr(result, "isError", False):
        return {"error": True, "summary": f"{tool} call failed"}
    try:
        return json.loads(result_text(result))
    except (json.JSONDecodeError, ValueError):
        return {"error": True, "summary": f"{tool} returned no/invalid JSON"}
