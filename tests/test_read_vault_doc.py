"""Tests for the read_vault_doc Tool."""
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from pare.tools.read_vault_doc import ReadVaultDoc


@pytest.mark.asyncio
async def test_read_vault_doc_returns_content():
    tool = ReadVaultDoc()
    ctx = MagicMock()
    ctx.agent.retrieval.get_document = AsyncMock(return_value={
        "id": "AI/agents",
        "name": "Agents",
        "summary": "about agents",
        "content": "FULL BODY TEXT",
    })

    result = await tool.run({"path": "AI/agents.md"}, ctx)

    payload = json.loads(result)
    assert payload["status"] == "ok"
    assert payload["content"] == "FULL BODY TEXT"
    assert payload["name"] == "Agents"
    ctx.agent.retrieval.get_document.assert_awaited_once_with("AI/agents")


@pytest.mark.asyncio
async def test_read_vault_doc_not_found_returns_error_string():
    tool = ReadVaultDoc()
    ctx = MagicMock()
    ctx.agent.retrieval.get_document = AsyncMock(side_effect=FileNotFoundError("nope"))
    result = await tool.run({"path": "missing.md"}, ctx)
    payload = json.loads(result)
    assert payload["status"] == "error"
    assert "not found" in payload["reason"].lower()


@pytest.mark.asyncio
async def test_read_vault_doc_traversal_returns_error_string():
    tool = ReadVaultDoc()
    ctx = MagicMock()
    ctx.agent.retrieval.get_document = AsyncMock(side_effect=ValueError("Invalid doc_id"))
    result = await tool.run({"path": "../etc/passwd"}, ctx)
    payload = json.loads(result)
    assert payload["status"] == "error"


@pytest.mark.asyncio
async def test_read_vault_doc_missing_path_returns_error_string():
    tool = ReadVaultDoc()
    ctx = MagicMock()
    result = await tool.run({}, ctx)
    payload = json.loads(result)
    assert payload["status"] == "error"


def test_read_vault_doc_metadata():
    assert ReadVaultDoc.name == "read_vault_doc"
    assert ReadVaultDoc.requires == ("retrieval",)
    assert "path" in ReadVaultDoc.parameters["properties"]
    assert "path" in ReadVaultDoc.parameters.get("required", [])


def test_read_vault_doc_registered_on_agent():
    from pare.agent import PareAgent
    assert ReadVaultDoc in PareAgent.tools
