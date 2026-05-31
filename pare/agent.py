"""PareAgent — minimal Agent subclass.

Extension points:
    name (ClassVar)                — short slug, e.g. "pare"
    env_prefix (ClassVar)          — env-var prefix, derived from name if None
    tools (ClassVar list[type])    — Tool subclasses to register
    commands (ClassVar list[type]) — Command subclasses to register
    disabled_builtins              — names from BUILTIN_TOOLS / BUILTIN_COMMANDS to skip
    setup() override               — construct domain-specific resources
    system_prompt(ctx) override    — build the per-turn system prompt
"""
from __future__ import annotations

import asyncio

from agent_core.agent import Agent, HandlerContext
from agent_core.workers import MCPClientPool, discover_and_register
from agent_core.workers.registry import WorkerRegistry
from agent_core.workers.risk import RiskGate
from agent_core.workers.risk_pool import RiskAwareToolPool
from agent_core.workers.audit import AuditLog

from pare.commands.hello import Hello
from pare.commands.health import Health
from pare.tools import ReadVaultDoc, StaticAnalyze
from pare.tools._http import ApkReAgentsClient


class PareAgent(Agent):
    name = "pare"
    env_prefix = "PARE_"

    tools = [StaticAnalyze, ReadVaultDoc]  # add Tool subclasses here
    commands = [Hello, Health]  # framework builtins serve /help, /clear, etc.

    # vault_path is PARE's private state dir (RAG-only reads of PAL's vault),
    # so the framework shell builtins — scoped to vault_path — would only let
    # the model grep PARE's own state. Disable them; PAL research goes through
    # search_vault + read_vault_doc. Workspace-scoped reads return in PR2.
    disabled_builtins = frozenset({
        "cat", "head", "tail", "ls", "grep", "find", "read_lines",
    })

    def setup(self) -> None:
        """Construct domain resources: apk_re_agents HTTP client (Phase 1), the
        MCP connection pool, and a RiskAwareToolPool that gates high/critical
        tool calls on operator approval and audits every dispatch.

        Framework managers (including tool_approval_registry) are already
        populated on self at this point by agent_core's runtime.
        """
        self.apk_re_agents_client = ApkReAgentsClient(self.config.apk_re_agents_url)
        registry = WorkerRegistry.load(self.config.workers_yaml_path)
        specs = registry.all()
        self._worker_specs = specs
        self.mcp_pool = MCPClientPool(specs)
        self.tool_pool = RiskAwareToolPool(
            inner=self.mcp_pool,
            specs={s.name: s for s in specs},
            risk_gate=RiskGate(overrides=registry.risk_overrides()),
            approval_registry=self.tool_approval_registry,
            audit_log=AuditLog(self.config.audit_dir),
            send_message=None,  # approval requests delivered via per-request ctx.emit
        )

    def register_tools(self):
        """Discover MCP-direct workers and return their tools, wired to dispatch
        through the RiskAwareToolPool (so calls are risk-gated + audited).

        Called by agent_core's runtime after setup(). The returned list is
        unioned with the class-level `tools` ClassVar (StaticAnalyze). Bridges
        async discovery to the sync hook via asyncio.run.
        """
        return asyncio.run(discover_and_register(self._worker_specs, self.tool_pool))

    def system_prompt(self, ctx: HandlerContext) -> str:
        from pathlib import Path
        # Read the base prompt from prompts/system.md.
        prompt_path = Path(__file__).parent / "prompts" / "system.md"
        base = prompt_path.read_text() if prompt_path.exists() else "You are PareAgent."
        pb = self.prompt_builder
        return "\n\n".join(filter(None, [
            base,
            pb.render_profile(),
            pb.render_wisdom(),
            pb.render_scratchpad(ctx.channel_id),
            pb.render_commands_catalog(),
        ]))
