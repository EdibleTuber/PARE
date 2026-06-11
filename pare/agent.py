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
import json
import logging
from typing import AsyncIterator

from agent_core.protocol import (
    ChatMessage,
    CommandMessage,
    ErrorMessage,
    ResponseMessage,
    StreamChunkMessage,
    ToolProgressMessage,
)

from agent_core.agent import Agent, HandlerContext
from agent_core.workers import MCPClientPool, discover_and_register
from agent_core.workers.registry import WorkerRegistry
from agent_core.workers.risk import RiskGate
from agent_core.workers.risk_pool import RiskAwareToolPool
from agent_core.workers.audit import AuditLog

from pare.commands.hello import Hello
from pare.commands.health import Health
from pare.commands.snapshot import Snapshot
from pare.commands.frida_views import Devices, Ps, Apps, Sessions
from pare.commands.frida_actions import Select, Attach, Detach
from pare.tools import ReadVaultDoc, StaticAnalyze
from pare.tools._http import ApkReAgentsClient

logger = logging.getLogger(__name__)


class PareAgent(Agent):
    name = "pare"
    env_prefix = "PARE_"

    tools = [StaticAnalyze, ReadVaultDoc]  # add Tool subclasses here
    commands = [
        Hello, Health, Snapshot,
        Devices, Ps, Apps, Sessions,   # operator fast-path views
        Select, Attach, Detach,        # operator fast-path actions
    ]  # framework builtins serve /help, /clear, etc.

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

        Discovery lazy-connects each worker in THIS throwaway asyncio.run loop
        (MCPClientPool caches the client on first list_tools). We close the pool
        before returning so those connections don't leak into the daemon's
        separate serving loop — a stdio worker's streams are bound to the loop
        that opened them, so a reused-across-loops client dies on the first
        dispatched call with ClosedResourceError. The serving loop reconnects
        lazily on first call_tool, in its own loop.
        """
        async def _discover():
            classes = await discover_and_register(self._worker_specs, self.tool_pool)
            await self.tool_pool.close_all()
            return classes

        return asyncio.run(_discover())

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

    async def handle_command(
        self, msg: CommandMessage, ctx: HandlerContext,
    ) -> AsyncIterator[object]:
        """Delegate to the framework command registry (serves /hello, /health,
        and the builtins /help, /clear, /context)."""
        async for out in self.command_registry.dispatch(msg.name, msg.args, ctx):
            yield out

    async def handle_chat(
        self, msg: ChatMessage, ctx: HandlerContext,
    ) -> AsyncIterator[object]:
        """Stream a reply, running tools when the model calls them.

        Pure-yield: the daemon emits every yielded message. Risk gating and
        operator approval happen transparently inside tool_executor.run ->
        tool_pool.call_tool (the daemon's read loop resolves the approval
        future while we're parked on the await). Ported from pal/agent.py.
        """
        from agent_core.inference import StreamEnd

        conv = ctx.conversation
        conv.add_user(msg.text)
        mode = self.decide_mode(conv)            # "on" | "off" (never "auto")
        messages = conv.get_messages_for_api(system_prompt=self.system_prompt(ctx))
        schemas = self.tool_executor.schemas()
        MAX_TOOL_ROUNDS = 50
        MAX_TOKENS = 4096                        # runaway-loop stopgap (matches PAL)

        try:
            tool_calls = None
            if mode == "on":
                completion = await self.inference.complete(
                    messages, tools=schemas, reasoning=mode, max_tokens=MAX_TOKENS)
                self.record_usage(ctx.channel_id, completion.usage)
                if completion.type == "text":
                    conv.add_assistant(completion.content or "")
                    yield ResponseMessage(text=completion.content or "",
                                          reasoning=completion.reasoning or "")
                    return
                tool_calls = completion.tool_calls
            else:
                full: list[str] = []
                async for item in self.inference.stream(
                    messages, tools=schemas, reasoning=mode, max_tokens=MAX_TOKENS):
                    if isinstance(item, list):
                        tool_calls = item
                        break  # NOTE: usage for this streamed segment is not recorded
                               # (stream() omits StreamEnd on the tool-call path) — the
                               # follow-up complete() repopulates last_usage. Matches PAL.
                    if isinstance(item, StreamEnd):
                        self.record_usage(ctx.channel_id, item.usage)
                        break
                    yield StreamChunkMessage(token=item)
                    full.append(item)
                if tool_calls is None:
                    conv.add_assistant("".join(full))
                    yield ResponseMessage(text="".join(full))
                    return

            for _round in range(MAX_TOOL_ROUNDS):
                conv.add_assistant_tool_calls([
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)}}
                    for tc in tool_calls
                ])
                for tc in tool_calls:
                    yield ToolProgressMessage(tool=tc.name, arguments=tc.arguments)
                    result = await self.tool_executor.run(tc.name, tc.arguments, ctx)
                    conv.add_tool_result(tc.id, result)
                messages = conv.get_messages_for_api(system_prompt=self.system_prompt(ctx))
                completion = await self.inference.complete(
                    messages, tools=schemas, reasoning=mode, max_tokens=MAX_TOKENS)
                self.record_usage(ctx.channel_id, completion.usage)
                if completion.type == "text":
                    conv.add_assistant(completion.content or "")
                    yield ResponseMessage(text=completion.content or "",
                                          reasoning=completion.reasoning or "")
                    return
                tool_calls = completion.tool_calls

            cap = "Reached the tool-call limit for this turn. Here's what I have so far."
            conv.add_assistant(cap)
            yield ResponseMessage(text=cap)
        except Exception as exc:
            logger.exception("Chat error: %s", exc)
            yield ErrorMessage(error=f"Chat error: {exc}")
