"""/snapshot — deterministic viewer over the frida worker's @snapshots store.

Calls the worker's page_capture tool through the audited tool_pool and renders
the rows itself; the LLM is never in this path (commands bypass the model).
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator

from agent_core.commands.base import Command
from agent_core.protocol.messages import ResponseMessage

from pare.commands._snapshot_render import render_table, render_catalog

_WORKER = "frida"
_HANDLE = "@snapshots"


def _result_text(result) -> str:
    return "".join(getattr(b, "text", "") for b in (getattr(result, "content", None) or []))


class Snapshot(Command):
    name = "snapshot"
    args = "[list | <key> [query]]"
    description = "View a captured snapshot from @snapshots (complete, deterministic)"

    async def _page(self, ctx, **args) -> dict | None:
        args.setdefault("session_id", _HANDLE)
        result = await ctx.agent.tool_pool.call_tool(_WORKER, "page_capture", args, ctx=ctx)
        if getattr(result, "isError", False):
            return None
        return json.loads(_result_text(result))

    async def run(self, raw_args: str, ctx) -> AsyncIterator:
        parts = raw_args.split(None, 1)
        sub = parts[0] if parts else ""
        rest = parts[1].strip() if len(parts) > 1 else ""

        if sub == "list":
            data = await self._page(ctx, list_sources=True)
            if data is None:
                yield ResponseMessage(text="snapshot read failed")
                return
            yield ResponseMessage(text=render_catalog(data.get("sources", [])))
            return

        if sub == "":
            data = await self._page(ctx)            # latest
            yield ResponseMessage(text=self._render(data))
            return

        # sub = key substring, rest = optional query. Resolve against the catalog.
        catalog = await self._page(ctx, list_sources=True)
        sources = [s["source"] for s in (catalog or {}).get("sources", [])]
        matches = [s for s in sources if sub in s]
        if not matches:
            yield ResponseMessage(text=f"no snapshot matches '{sub}' — try /snapshot list")
            return
        if len(matches) > 1:
            listing = "\n".join(f"  {m}" for m in matches)
            yield ResponseMessage(text=f"ambiguous key '{sub}' — matches:\n{listing}")
            return
        kwargs = {"source": matches[0]}
        if rest:
            kwargs.update(field="summary", contains=rest)
        data = await self._page(ctx, **kwargs)
        yield ResponseMessage(text=self._render(data, query=rest))

    def _render(self, data: dict | None, query: str = "") -> str:
        if data is None:
            return "snapshot read failed"
        if not data.get("source"):
            return "nothing captured yet — run an enumerate tool first"
        rows = data.get("rows", [])
        total, shown = data.get("total", len(rows)), data.get("shown", len(rows))
        if query and total == 0:
            return f"0 rows match '{query}' in {data['source']}"
        header = f"{data['source']} · {total} rows" + (
            f" (showing {shown})" if shown < total else "")
        return f"{header}\n{render_table(rows)}"
