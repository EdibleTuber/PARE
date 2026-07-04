"""/snapshot — deterministic viewer over the PARE-side capture store.

Reads captures straight from ctx.agent.capture_store and renders the rows
itself; the LLM is never in this path. The frida worker store is gone;
captures are written to the project store at the wire layer.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator

from agent_core.commands.base import Command
from agent_core.capture.shape import infer_rows
from agent_core.protocol.messages import ResponseMessage

from pare.commands._snapshot_render import render_table, render_catalog


class Snapshot(Command):
    name = "snapshot"
    args = "[list | <ref-or-key> [query]]"
    description = "View a captured tool result from the project store (complete, deterministic)"

    async def run(self, raw_args: str, ctx) -> AsyncIterator:
        store = getattr(ctx.agent, "capture_store", None)
        if store is None:
            yield ResponseMessage(text="no capture store for this session")
            return
        # Use split(" ", 1) so a leading space produces sub="" with the rest as a query.
        parts = raw_args.split(" ", 1)
        sub = parts[0] if parts else ""
        rest = parts[1].strip() if len(parts) > 1 else ""

        if sub == "list":
            cat = [{"count": r["rows"], "source": f'{r["tool"]} [{r["ref"]}]'}
                   for r in store.recent(limit=20)]
            yield ResponseMessage(text=render_catalog(cat))
            return

        if sub == "":
            recent = store.recent(limit=1)
            if not recent:
                yield ResponseMessage(text="nothing captured yet — run an enumerate tool first")
                return
            yield ResponseMessage(text=self._render(store.get(recent[0]["ref"]), rest))
            return

        row = store.get(sub)
        if row is None:
            hits = store.search(text=sub, limit=5)
            if not hits:
                yield ResponseMessage(text=f"no capture matches '{sub}' — try /snapshot list")
                return
            if len(hits) > 1:
                listing = "\n".join(f'  {h["tool"]} [{h["ref"]}]' for h in hits)
                yield ResponseMessage(text=f"ambiguous '{sub}' — matches:\n{listing}")
                return
            row = store.get(hits[0]["ref"])
        yield ResponseMessage(text=self._render(row, rest))

    def _render(self, row: dict | None, query: str = "") -> str:
        if row is None:
            return "capture read failed"
        rows = infer_rows(json.loads(row["body"]))
        if query:
            q = query.lower()
            rows = [r for r in rows if any(q in str(v).lower() for v in r.values())]
        header = f'{row["tool"]} [{row["ref"]}] · {len(rows)} rows'
        return f"{header}\n{render_table(rows)}"
