"""read_vault_doc — fetch a full vault document body over the retrieval service.

Companion to the framework `search_vault` builtin: `search_vault` returns hits
with a `path` field (`"{id}.md"`); this tool takes that `path`, recovers the
`doc_id`, and returns the document's full content. RAG-only — no local filesystem
access, so PARE never needs PAL's vault mounted locally.
"""
from __future__ import annotations

import json
from typing import Any, ClassVar

from agent_core.tools.base import Tool

_MAX_CONTENT_CHARS = 20000


class ReadVaultDoc(Tool):
    name: ClassVar[str] = "read_vault_doc"
    description: ClassVar[str] = (
        "Fetch the full body of a vault document found via search_vault. "
        "Pass the `path` value from a search_vault result. Returns JSON: "
        "{status, name, summary, content}."
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "The `path` field from a search_vault result, e.g. 'AI/agents.md'.",
            },
        },
        "required": ["path"],
    }
    requires: ClassVar[tuple[str, ...]] = ("retrieval",)

    async def run(self, args: dict[str, Any], ctx: Any) -> str:
        path = (args.get("path") or "").strip()
        if not path:
            return json.dumps({"status": "error", "reason": "'path' parameter is required."})
        doc_id = path[:-3] if path.endswith(".md") else path
        if not doc_id:
            return json.dumps({"status": "error", "path": path,
                               "reason": "Invalid path: resolves to an empty doc_id."})
        try:
            doc = await ctx.agent.retrieval.get_document(doc_id)
        except FileNotFoundError:
            return json.dumps({"status": "error", "path": path,
                               "reason": f"Document not found: {path}"})
        except Exception as exc:
            return json.dumps({"status": "error", "path": path,
                               "reason": f"{type(exc).__name__}: {exc}"})
        content = doc.get("content", "") or ""
        if len(content) > _MAX_CONTENT_CHARS:
            content = content[:_MAX_CONTENT_CHARS] + "\n…[truncated]"
        return json.dumps({
            "status": "ok",
            "name": doc.get("name") or doc_id,
            "summary": doc.get("summary", ""),
            "content": content,
        })
