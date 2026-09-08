"""publish_finding — publish a model-authored finding to the project's workbench.

Spec: docs/superpowers/specs/2026-09-06-arcticbase-artifacts-design.md D4, D5, §5.2.

D4 splits the workbench in two. Facts publish automatically because a tool
declared them; the *interpretation* is the model's editorial act, and this is
that act. The capture store already holds every result and is FTS-searchable, so
publishing everything would bury the one finding that matters in a surface whose
whole value is that it is curated.

Two constraints are structural rather than advisory:

* **There is no `kind` parameter.** D5 decides model-authored content is `md`,
  never `html`, and a parameter would hand that decision back to the model. The
  client refuses executable kinds too; this is the second lock on the same door.
* **No project, no publish.** §5.2. Outside a `.pare/` project the capture store
  falls back to a per-launch path, and a workbench derived from that would be
  new on every CLI launch. This fails and names the cwd instead.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

from agent_core.tools.base import Tool

from pare.arcticbase import ArcticBaseClient, ArcticBaseError
from pare.project_slug import ProjectSlugError, resolve_project_slug


class PublishFinding(Tool):
    name: ClassVar[str] = "publish_finding"
    description: ClassVar[str] = (
        "Publish a finding to this project's workbench, where the operator at "
        "the bench can read it on screen. Use it for an interpretation worth "
        "keeping - what a result MEANS - not for raw tool output, which is "
        "already captured and searchable. Markdown body. Returns JSON: "
        "{status, slug, object_id, url}."
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Short title for the finding, e.g. 'Static keystore passphrase'.",
            },
            "markdown": {
                "type": "string",
                "description": "The finding itself, as markdown.",
            },
        },
        "required": ["title", "markdown"],
    }

    async def run(self, args: dict[str, Any], ctx: Any) -> str:
        title = (args.get("title") or "").strip()
        markdown = (args.get("markdown") or "").strip()
        if not title:
            return _err("'title' parameter is required.")
        if not markdown:
            return _err("'markdown' parameter is required and must not be blank.")

        config = ctx.agent.config
        cwd = getattr(ctx, "cwd", None) or str(Path.cwd())

        try:
            slug = resolve_project_slug(
                cwd, marker=config.project_marker or ".pare", home=Path.home())
        except ProjectSlugError as exc:
            return _err(str(exc))

        client = ArcticBaseClient(config.arcticbase_url)
        try:
            client.ensure_workbench(slug, slug)
            oid = client.publish_report(slug, title, markdown)
        except ArcticBaseError as exc:
            # Reported, never raised: §10 says the operator has five candidates
            # to choose between when something does not arrive, so the message
            # has to say which one this was.
            return _err(f"{type(exc).__name__}: {exc}")

        base = config.arcticbase_url.rstrip("/")
        return json.dumps({
            "status": "ok",
            "slug": slug,
            "object_id": oid,
            "url": f"{base}/wb/{slug}",
        })


def _err(reason: str) -> str:
    return json.dumps({"status": "error", "reason": reason})
