"""Tests for the static_analyze Tool."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from pare.tools.static_analyze import StaticAnalyze
from pare.tools._http import JobResult


@pytest.mark.asyncio
async def test_static_analyze_returns_findings_summary():
    """Happy path: the tool returns a summary string with the job id and a
    list of agent_name → findings file path entries."""
    tool = StaticAnalyze()

    # Mock the agent context: the tool reads ctx.agent.apk_re_agents_client
    # (a long-lived client constructed in PareAgent.setup()).
    fake_client = MagicMock()
    fake_client.run_to_completion = AsyncMock(return_value=JobResult(
        job_id="abc-123",
        state="completed",
        results={
            "manifest_analyzer": "/work/findings/abc-123/manifest_analyzer.json",
            "string_extractor": "/work/findings/abc-123/string_extractor.json",
        },
    ))

    ctx = MagicMock()
    ctx.agent.apk_re_agents_client = fake_client

    result = await tool.run({"apk_path": "/work/input/sample.apk"}, ctx)

    assert "abc-123" in result
    assert "manifest_analyzer" in result
    assert "string_extractor" in result
    fake_client.run_to_completion.assert_awaited_once_with(
        apk_path="/work/input/sample.apk"
    )


@pytest.mark.asyncio
async def test_static_analyze_surfaces_failure_as_string():
    """When the job fails, the tool returns a descriptive error string
    (not raises) — Tool runs are expected to surface errors to the LLM
    as text per agent_core convention."""
    tool = StaticAnalyze()
    fake_client = MagicMock()
    fake_client.run_to_completion = AsyncMock(side_effect=RuntimeError(
        "apk_re_agents job j1 failed: {'state': 'failed'}"
    ))
    ctx = MagicMock()
    ctx.agent.apk_re_agents_client = fake_client

    result = await tool.run({"apk_path": "/work/input/bad.apk"}, ctx)
    assert "failed" in result.lower()


def test_static_analyze_tool_metadata():
    """Tool exposes name, description, parameters."""
    assert StaticAnalyze.name == "static_analyze"
    assert "apk" in StaticAnalyze.description.lower()
    schema = StaticAnalyze.parameters
    assert schema["type"] == "object"
    assert "apk_path" in schema["properties"]
    assert "apk_path" in schema.get("required", [])
