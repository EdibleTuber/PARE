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
import os
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
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
from agent_core.capture import CaptureLayer, CaptureStore, SearchCapture, ReadCapture
from pare.capture_store import CaptureStoreManager
from pare.handback import (
    COMMIT_TOOLS, NAME_SEARCH_TOOLS, POLL_TOOLS,
    candidate_classes, near_duplicate, normalize_class,
    disambig_question, spin_question,
)
from pare.repeat_guard import RepeatGuard
from pare.tools import ReadVaultDoc, StaticAnalyze
from pare.tools._http import ApkReAgentsClient

logger = logging.getLogger(__name__)

# Per-turn project store, set by _bind_store() at the top of each handler and read
# by the CaptureLayer's provider and by the retrieval tools (ctx.agent.capture_store).
# A ContextVar (not an instance attr) so concurrent channels on the one daemon never
# see each other's store.
_current_store: ContextVar[CaptureStore | None] = ContextVar("pare_capture_store", default=None)


class PareAgent(Agent):
    name = "pare"
    env_prefix = "PARE_"

    tools = [ReadVaultDoc, SearchCapture, ReadCapture]  # add Tool subclasses here
    # StaticAnalyze (apk_re_agents) is appended in register_tools() only when
    # config.enable_apk_re_agents is set (default off) — see setup().
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

    @property
    def capture_store(self) -> CaptureStore | None:
        return _current_store.get()

    @contextmanager
    def _bind_store(self, ctx):
        store = self._capture_stores.resolve(getattr(ctx, "cwd", None), ctx.channel_id)
        token = _current_store.set(store)
        try:
            yield
        finally:
            _current_store.reset(token)

    def setup(self) -> None:
        """Construct domain resources: apk_re_agents HTTP client (Phase 1), the
        MCP connection pool, and a RiskAwareToolPool that gates high/critical
        tool calls on operator approval and audits every dispatch.

        Framework managers (including tool_approval_registry) are already
        populated on self at this point by agent_core's runtime.
        """
        self.apk_re_agents_client = (
            ApkReAgentsClient(self.config.apk_re_agents_url)
            if self.config.enable_apk_re_agents else None
        )
        registry = WorkerRegistry.load(self.config.workers_yaml_path)
        specs = registry.all()
        self._worker_specs = specs
        self.mcp_pool = MCPClientPool(specs)
        self._launch_ts = time.time()   # process start; per-launch refinement deferred (spec §11)
        self._capture_stores = CaptureStoreManager(
            marker=self.config.project_marker,
            home=Path.home(),
            xdg_state=Path(os.environ.get("XDG_STATE_HOME",
                                          str(Path.home() / ".local" / "state"))) / "pare",
        )
        inline_budget = int(self.config.context_window_tokens / self.config.history_depth * 3.5)
        self._capture_layer = CaptureLayer(
            inline_budget=inline_budget, launch_ts=self._launch_ts,
            store_provider=lambda: self.capture_store,
        )
        self.tool_pool = RiskAwareToolPool(
            inner=self.mcp_pool,
            specs={s.name: s for s in specs},
            risk_gate=RiskGate(overrides=registry.risk_overrides()),
            approval_registry=self.tool_approval_registry,
            audit_log=AuditLog(self.config.audit_dir),
            send_message=None,  # approval requests delivered via per-request ctx.emit
            capture_layer=self._capture_layer,
        )
        # Per-channel sets of already-resolved disambiguation candidate groups
        # (frozenset of dotted class names), so a resumed turn that re-commits
        # to a class from a group the operator already picked from doesn't
        # hand back a second time. Created lazily like `self.last_usage`.
        self._disambig_resolved: dict[str, set[frozenset[str]]] = {}

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

        classes = asyncio.run(_discover())
        # apk_re_agents' static_analyze is advertised only when explicitly enabled
        # (default off). The coordinator isn't part of every deployment, and an
        # always-registered tool the model reaches for first only dead-ends on a
        # connection-refused. Gate keeps the Phase-1 integration one config flag away.
        if self.config.enable_apk_re_agents:
            classes = [*classes, StaticAnalyze]
        return classes

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
        with self._bind_store(ctx):
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
        with self._bind_store(ctx):
            from agent_core.inference import StreamEnd

            conv = ctx.conversation
            conv.add_user(msg.text)
            mode = self.decide_mode(conv)            # "on" | "off" (never "auto")
            messages = conv.get_messages_for_api(system_prompt=self.system_prompt(ctx))
            schemas = self.tool_executor.schemas()
            # Smart no-progress stop: a fresh guard per turn catches verbatim
            # tool-call repeats that return identical results (see repeat_guard).
            # MAX_TOOL_ROUNDS stays only as a coarse final backstop — with the
            # guard doing the real stopping, hitting it should be rare.
            guard = RepeatGuard()
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

                # Operator-handback checkpoints (see docs/superpowers/specs/
                # 2026-07-18-operator-handback-checkpoints-design.md):
                #   Trigger 1 (spin): RepeatGuard.tripped() fires once a call is
                #     confirmed stuck (hard-blocked, not yet handed back this turn).
                #   Trigger 2 (commit-time disambiguation): a class-scoped dig-in
                #     tool commits to a class that a prior grep this turn showed is
                #     one of several near-identical variants.
                # name_searches remembers each grep pattern's candidate classes for
                # the rest of the turn; resolved remembers which candidate groups
                # this channel has already been asked about (persists across turns
                # via self._disambig_resolved so an answered question isn't re-asked).
                name_searches: dict[str, set[str]] = {}
                resolved = self._disambig_resolved.setdefault(ctx.channel_id, set())

                def _settle_and_handback(question: str, done_ids: set[str]) -> ResponseMessage:
                    """Fill a synthetic tool result for every tool_call id in this
                    round that hasn't been answered yet, then append the handback
                    question as the assistant turn. A dangling tool_call id with no
                    matching tool result makes the NEXT turn's API request invalid,
                    so every id in the batch must be settled before we return."""
                    for tc in tool_calls:
                        if tc.id not in done_ids:
                            conv.add_tool_result(
                                tc.id, "[handed back to operator — call not executed]")
                    conv.add_assistant(question)
                    return ResponseMessage(text=question)

                for _round in range(MAX_TOOL_ROUNDS):
                    conv.add_assistant_tool_calls([
                        {"id": tc.id, "type": "function",
                         "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)}}
                        for tc in tool_calls
                    ])
                    done_ids: set[str] = set()
                    for tc in tool_calls:
                        if tc.name in COMMIT_TOOLS:
                            cls = normalize_class(str((tc.arguments or {}).get("cls", "")))
                            for pat, cands in name_searches.items():
                                if (cls in cands and near_duplicate(cands, pat)
                                        and frozenset(cands) not in resolved):
                                    q = disambig_question(cls, cands)
                                    resolved.add(frozenset(cands))
                                    yield _settle_and_handback(q, done_ids)
                                    return
                        yield ToolProgressMessage(tool=tc.name, arguments=tc.arguments)
                        if guard.should_run(tc.name, tc.arguments):
                            result = await self.tool_executor.run(tc.name, tc.arguments, ctx)
                            result = guard.record(tc.name, tc.arguments, result)
                            if tc.name in NAME_SEARCH_TOOLS:
                                pat = str((tc.arguments or {}).get("pattern", ""))
                                if pat:
                                    name_searches[pat] = candidate_classes(
                                        result, pat, capture_store=self.capture_store)
                        else:
                            # Identical call already returned the same result too many
                            # times this turn — short-circuit instead of re-running.
                            if tc.name not in POLL_TOOLS and guard.tripped(tc.name, tc.arguments):
                                pat = str((tc.arguments or {}).get("pattern", ""))
                                total, last_result = guard.entry(tc.name, tc.arguments) or (0, "")
                                q = spin_question(tc.name, tc.arguments, total, last_result,
                                                  name_searches.get(pat, set()))
                                yield _settle_and_handback(q, done_ids)
                                return
                            result = guard.blocked(tc.name, tc.arguments)
                        conv.add_tool_result(tc.id, result)
                        done_ids.add(tc.id)
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
