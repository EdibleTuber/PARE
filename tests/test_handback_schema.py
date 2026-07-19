"""Schema-drift guard for the operator-handback triggers.

pare/handback.py hardcodes worker-prefixed tool names (COMMIT_TOOLS,
NAME_SEARCH_TOOLS, POLL_TOOLS) that handle_chat matches against the model's
`tc.name`. Those names are never derived from the real worker contracts —
they're hand-typed strings that must stay in sync with:

  - the worker's own tool name (e.g. `list_methods` on the static worker),
  - the `f"{worker.name}_{tool_name}"` prefix agent_core's tool_factory
    applies to every MCP-discovered tool (agent_core/workers/tool_factory.py),
  - the `cls` parameter name COMMIT_TOOLS' disambiguation check reads off
    `tc.arguments`.

A worker-side rename, or forgetting the worker prefix when adding a new
constant, silently disarms a trigger with no error — the tool call just never
matches the set. These tests build the exact registered name for every tool
declared by the static and frida contracts (no running worker needed — see
tests/test_risk_overrides_coverage.py for the same pattern against the frida
contract) and assert every handback constant resolves against a *real* tool.
"""
import pytest

from pare.handback import COMMIT_TOOLS, NAME_SEARCH_TOOLS, POLL_TOOLS


def _registered_tool_specs() -> dict[str, object]:
    """{registered_name: ToolSpec} for every tool the static + frida workers
    declare, prefixed exactly as agent_core.workers.tool_factory prefixes
    real MCP-discovered tools (f"{worker.name}_{tool_name}")."""
    specs: dict[str, object] = {}

    static = pytest.importorskip(
        "pare_static_mcp.contract",
        reason="pare-static-mcp not installed in this venv — install it "
               "(pip install -e ~/Projects/pare-static-mcp) to run the "
               "schema-drift guard against the real static contract.",
    )
    for spec in static.TOOL_SPECS:
        specs[f"static_{spec.name}"] = spec

    frida = pytest.importorskip(
        "pare_frida_mcp.contract",
        reason="pare-frida-mcp not installed in this venv.",
    )
    for spec in frida.TOOL_SPECS:
        specs[f"frida_{spec.name}"] = spec

    return specs


def test_commit_tools_exist_and_declare_cls():
    """Every COMMIT_TOOLS name must be a real registered tool, and its
    input_schema must still declare a `cls` parameter — that's the argument
    handle_chat reads to run the near-duplicate disambiguation check. If a
    worker ever renames `cls` (e.g. to `class_name`), the check in agent.py
    silently stops firing (str(...get("cls", "")) just returns "") — this
    assertion is what would catch that."""
    specs = _registered_tool_specs()
    for name in COMMIT_TOOLS:
        assert name in specs, (
            f"COMMIT_TOOLS entry {name!r} does not match any real registered "
            f"tool name. Known tools: {sorted(specs)}"
        )
        props = specs[name].input_schema.get("properties", {})
        assert "cls" in props, (
            f"{name!r} no longer declares a `cls` parameter (has: "
            f"{sorted(props)}) — the commit-time disambiguation check in "
            f"handle_chat reads tc.arguments['cls'] and would silently no-op."
        )


def test_name_search_tools_exist():
    """NAME_SEARCH_TOOLS entries must be real registered tools so grep results
    actually populate name_searches / candidate_classes for the turn."""
    specs = _registered_tool_specs()
    for name in NAME_SEARCH_TOOLS:
        assert name in specs, (
            f"NAME_SEARCH_TOOLS entry {name!r} does not match any real "
            f"registered tool name. Known tools: {sorted(specs)}"
        )


def test_poll_tools_exist():
    """POLL_TOOLS entries must be real registered tools, or a legitimate
    re-poll (e.g. frida_list_sessions after the operator triggers an attach)
    would fail to be exempted from the spin handback and get flagged as
    'stuck' instead of being allowed to keep polling."""
    specs = _registered_tool_specs()
    for name in POLL_TOOLS:
        assert name in specs, (
            f"POLL_TOOLS entry {name!r} does not match any real registered "
            f"tool name (check the worker prefix). Known tools: {sorted(specs)}"
        )
