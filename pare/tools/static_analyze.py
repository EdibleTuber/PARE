"""static_analyze: agent_core Tool wrapping apk_re_agents /jobs.

Risk tier: low. The wrapper submits an APK to the apk_re_agents
coordinator, polls until completion, and returns a summary of the
findings paths. Findings remain on the apk_re_agents shared volume;
the LLM gets a reference, not the raw content. Reading the actual
findings is a separate operation (Phase 1+ may add `read_findings`
shortcuts; for v1 the operator inspects findings out-of-band).
"""
from __future__ import annotations

from typing import Any, ClassVar

from agent_core.tools.base import Tool


class StaticAnalyze(Tool):
    """Submit an APK to apk_re_agents and return findings refs on completion."""

    name: ClassVar[str] = "static_analyze"
    description: ClassVar[str] = (
        "Submit an APK file to the apk_re_agents static-analysis pipeline "
        "and wait for the job to complete. Returns a summary listing the "
        "findings file paths per analyser (manifest_analyzer, "
        "string_extractor, network_mapper, code_analyzer, api_extractor, "
        "report_synthesizer). Use this as the first step on a new APK "
        "before dynamic analysis. Path must be reachable by the "
        "apk_re_agents coordinator's shared volume (typically "
        "/work/input/<apk>)."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "apk_path": {
                "type": "string",
                "description": "Absolute path to the APK on the apk_re_agents shared volume.",
            },
        },
        "required": ["apk_path"],
    }
    requires: ClassVar[tuple[str, ...]] = ("apk_re_agents_client",)

    async def run(self, args: dict[str, Any], ctx: Any) -> str:
        apk_path = args["apk_path"]
        client = ctx.agent.apk_re_agents_client
        try:
            result = await client.run_to_completion(apk_path=apk_path)
        except RuntimeError as exc:
            return f"static_analyze failed: {exc}"
        if not result.results:
            return (
                f"static_analyze completed (job {result.job_id}) but reported "
                "no findings. Check apk_re_agents logs."
            )
        lines = [f"static_analyze completed (job {result.job_id}). Findings:"]
        for analyser, path in sorted(result.results.items()):
            lines.append(f"  {analyser}: {path}")
        return "\n".join(lines)
