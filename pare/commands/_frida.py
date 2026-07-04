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


async def call(ctx, tool: str, args: dict | None = None) -> dict:
    """Call a frida worker tool through the audited pool and parse its JSON
    envelope. Returns the parsed dict, or an error-shaped dict ({"error": True,
    "summary": ...}) on a transport error or non-JSON result so callers render
    failures uniformly.

    capture=False: the result is stored to the project capture store at the
    wire (risk-tier auditing still runs), but the pool must never substitute a
    stub in place of the real payload — the operator sees the actual response.
    """
    result = await ctx.agent.tool_pool.call_tool(WORKER, tool, args or {}, ctx=ctx, capture=False)
    if getattr(result, "isError", False):
        return {"error": True, "summary": f"{tool} call failed"}
    try:
        return json.loads(result_text(result))
    except (json.JSONDecodeError, ValueError):
        return {"error": True, "summary": f"{tool} returned no/invalid JSON"}
