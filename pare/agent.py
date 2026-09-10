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
from contextlib import suppress
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
from agent_core.workers import MCPClientPool, WorkerManager
from agent_core.workers.registry import WorkerRegistry
from agent_core.workers.risk import RiskGate
from agent_core.workers.risk_pool import RiskAwareToolPool
from agent_core.workers.audit import AuditLog

from pare.commands.hello import Hello
from pare.commands.health import Health
from pare.commands.snapshot import Snapshot
from pare.commands.frida_views import Devices, Ps, Apps, Sessions
from pare.commands.frida_actions import Select, Attach, Detach
from pare.commands.mitm import Mitm
from pare.commands.worker import Worker
from agent_core.capture import CaptureLayer, CaptureStore, SearchCapture, ReadCapture
from pare.capture_store import CaptureStoreManager
from pare.handback import (
    COMMIT_TOOLS, NAME_SEARCH_TOOLS, POLL_TOOLS,
    POLL_FAILURE_LIMIT, is_worker_failure, poll_failure_question,
    candidate_classes, near_duplicate, normalize_class,
    disambig_question, spin_question,
)
from pare.repeat_guard import RepeatGuard
from pare.arcticbase import ArcticBaseClient
from pare.heartbeat import (BEAT_INTERVAL_SECONDS, Heartbeat)
from pare.tools import PublishFinding, ReadVaultDoc, StaticAnalyze
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
    # StaticAnalyze (apk_re_agents) and PublishFinding (ArcticBase) are appended
    # in register_tools() only when their backend is configured — see setup().
    commands = [
        Hello, Health, Snapshot,
        Devices, Ps, Apps, Sessions,   # operator fast-path views
        Select, Attach, Detach,        # operator fast-path actions
        Mitm,                          # HTTPS-traffic daemon control
        Worker,                        # runtime worker lifecycle
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
        self.worker_registry = registry
        # Empty, deliberately. The pool holds LOADED specs, not declared ones:
        # if it were seeded with every declaration, the first dispatch after an
        # unload would lazily reconnect the worker just unloaded, silently
        # undoing it. WorkerManager owns all population.
        self.mcp_pool = MCPClientPool([])
        # Sentinel so the /worker command's requires=("worker_manager",) check
        # passes in _attach_registries, which runs BEFORE astartup. Same pattern
        # the framework uses for command_registry (agent_core/runtime.py:55-58).
        # The real manager is built in astartup(), once tool_executor exists.
        #
        # INSTANCE attribute, deliberately: `requires` is a hasattr() check, so
        # a class-level `worker_manager = None` would satisfy it on every
        # instance and deleting this line would no longer fail boot — the check
        # spec 9.1 asked for would silently protect nothing. Unit tests that
        # build a bare PareAgent() without setup() stub it themselves.
        self.worker_manager = None
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
            specs={},
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
        """Declarative tools only. Worker tools are registered by astartup().

        Discovery used to run here inside a throwaway asyncio.run loop, which
        forced a close_all() afterwards because the connections were bound to
        that dead loop. astartup() runs in the serving loop instead, so that
        whole dance is gone.
        """
        extra = []
        if self.config.enable_apk_re_agents:
            extra.append(StaticAnalyze)
        if self.config.arcticbase_url:
            # Same reasoning config.py gives for apk_re_agents: a tool whose
            # backend is not configured is a dead end the model reaches for
            # first, so it is absent rather than present-and-failing.
            extra.append(PublishFinding)
        return extra

    async def astartup(self) -> None:
        """Build the worker manager and connect every autoload worker.

        Runs after _attach_registries (so tool_executor exists) and before the
        daemon accepts a connection.
        """
        self.worker_manager = WorkerManager(
            self.worker_registry, self.tool_pool, self.tool_executor)
        results = await self.worker_manager.load_autoload()
        ok = [r for r in results if r.ok]
        bad = [r for r in results if not r.ok]
        logger.info("workers loaded: %s%s",
                    ", ".join(f"{r.name}({r.tool_count})" for r in ok) or "none",
                    "".join(f" | {r.name} FAILED: {r.error}" for r in bad))
        # PARE runs its own sweep loop instead of WorkerManager.start_liveness().
        # §8.2 requires the heartbeat to be written by the task that does the
        # polling: a beat on a timer of its own keeps ticking through a wedged
        # daemon, which is the state it exists to reveal. agent_core's loop is
        # `sleep -> probe_all -> log and continue` and offers no hook, so the
        # loop lives here and calls the same public probe_all(). If that loop
        # ever grows behaviour, this has to track it.
        #
        # Started here rather than in a constructor: it creates a task, and a
        # task needs a running loop that will outlive this call. astartup runs
        # inside the serving loop, which is the one that will.
        self._heartbeat = (
            Heartbeat(ArcticBaseClient(self.config.arcticbase_url))
            if self.config.arcticbase_url else None)
        self._sweep_task = asyncio.create_task(
            self._sweep_loop(), name="pare-sweep-heartbeat")

    async def _sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(BEAT_INTERVAL_SECONDS)
            try:
                await self._sweep_and_beat()
            except asyncio.CancelledError:
                raise
            except Exception:
                # A sweep loop that dies takes worker liveness AND the operator's
                # only "the daemon is alive" signal with it, silently.
                logger.warning("sweep/heartbeat failed; continuing", exc_info=True)

    async def _sweep_and_beat(self) -> bool:
        """One sweep, then one beat. Returns whether the sweep succeeded.

        The beat is skipped when the sweep raises. That is the entire design:
        a heartbeat that survives a wedged poller is a false all-clear.
        """
        try:
            workers = await self.worker_manager.probe_all()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("worker sweep failed; skipping the beat", exc_info=True)
            return False
        if self._heartbeat is not None:
            try:
                await asyncio.to_thread(
                    self._heartbeat.beat,
                    active_slug=self._active_slug(), workers=workers)
            except Exception:
                # Heartbeat.beat swallows its own errors; this is belt and
                # braces so a bug there cannot stop worker probing.
                logger.warning("heartbeat write failed", exc_info=True)
        return True

    def _active_slug(self) -> str | None:
        """The project the daemon most recently served, or None outside one.

        Best-effort by design: a daemon serving no project is a different fact
        from a dead daemon, and must not be reported as one.
        """
        stores = getattr(self, "_capture_stores", None)
        db_path = getattr(stores, "last_db_path", None) if stores else None
        if db_path is None:
            return None
        try:
            from pare.project_slug import read_or_create_slug
            return read_or_create_slug(Path(db_path).parent)
        except Exception:
            return None

    async def ashutdown(self) -> None:
        """Close worker connections and project capture stores.

        The capture-store close runs in `finally` so it always happens, even
        if worker_manager.close_all() lets a CancelledError through (it
        suppresses plain Exception internally but must not swallow
        cancellation). If astartup() never got as far as building
        worker_manager (e.g. WorkerManager() rejected a misconfigured pool),
        fall back to closing mcp_pool directly so its worker subprocesses
        don't outlive the daemon.
        """
        try:
            # Before close_all: a probe firing against a worker being torn down
            # would log a spurious "unreachable" for a shutdown the operator
            # asked for. This is PARE's own loop (see astartup), so stopping it
            # is PARE's job -- an un-cancelled task would keep probing, and keep
            # beating, through the shutdown it is supposed to make visible.
            task = getattr(self, "_sweep_task", None)
            if task is not None and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            if self.worker_manager is not None:
                # Belt and braces: harmless if PARE never started it, and
                # correct if some other path did.
                await self.worker_manager.stop_liveness()
                await self.worker_manager.close_all()
            else:
                close_pool = getattr(self.mcp_pool, "close_all", None)
                if close_pool is not None:
                    await close_pool()
        finally:
            close = getattr(self._capture_stores, "close_all", None)
            if close is not None:
                close()

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
            # Consecutive FAILED polls per tool. The spin guard deliberately
            # exempts POLL_TOOLS -- polling is supposed to repeat -- which
            # left the one tool the prompt tells the model to hammer as the
            # one tool with no backstop at all when its link degrades.
            poll_failures: dict[str, int] = {}
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
                #   Trigger 3 (unloaded worker): a tool whose worker was
                #     unloaded mid-session. Needs its own trigger because
                #     neither of the above fires — distinct frida_* tools are
                #     distinct RepeatGuard signatures, and POLL_TOOLS exempts
                #     frida_read_hook_events from the spin handback by design
                #     (system.md tells the model to poll it repeatedly).
                name_searches: dict[str, set[str]] = {}
                resolved = self._disambig_resolved.setdefault(ctx.channel_id, set())
                unavailable_hits = 0
                UNAVAILABLE_HANDBACK_AFTER = 2

                def _settle(marker: str, done_ids: set[str]) -> None:
                    """Fill a synthetic tool result for every tool_call id in this
                    round that hasn't been answered yet. A dangling tool_call id
                    with no matching tool result makes the NEXT turn's API request
                    invalid, so every id in the batch must be settled before this
                    round can be abandoned by any route."""
                    for tc in tool_calls:
                        if tc.id not in done_ids:
                            conv.add_tool_result(tc.id, marker)

                def _settle_and_handback(question: str, done_ids: set[str]) -> ResponseMessage:
                    """Settle the round, then append the handback question as the
                    assistant turn."""
                    _settle("[handed back to operator — call not executed]", done_ids)
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
                        mgr = self.worker_manager
                        # Presence in the executor IS the provenance test here.
                        # A registered tool is dispatchable right now, so it is
                        # never the unloaded-worker case: it is either a
                        # declarative PARE tool (no `worker` provenance at all)
                        # or a synthesized tool whose worker is loaded — and
                        # unload/rollback both remove a worker's tools before
                        # anything else. Only an ABSENT name can belong to an
                        # unloaded worker, and worker_of()'s name-prefix match
                        # is the only signal left for it. Without this gate the
                        # prefix false-attributes the declarative
                        # `static_analyze` (pare/tools/static_analyze.py) to a
                        # worker named `static`, handing the turn back about a
                        # tool that just executed fine (spec 8.2's namespace
                        # overlap, made active).
                        if mgr is not None and tc.name not in self.tool_executor:
                            owner = mgr.worker_of(tc.name)
                            why = mgr.unavailable_reason(owner) if owner else None
                            if why:
                                unavailable_hits += 1
                                if unavailable_hits >= UNAVAILABLE_HANDBACK_AFTER:
                                    # `why` (agent_core's unavailable_reason) is
                                    # MODEL-facing wording -- it tells the model
                                    # to "ask the operator to run /worker load
                                    # ...", which belongs in a tool result the
                                    # model reads. This handback instead goes
                                    # straight to the human operator (the model
                                    # is cut out of this round), so it needs its
                                    # own phrasing addressed to that reader --
                                    # reusing `why` verbatim would have the
                                    # assistant tell the operator to ask the
                                    # operator.
                                    yield _settle_and_handback(
                                        f"the {owner!r} worker is not loaded, "
                                        f"so the assistant's last tool call "
                                        f"didn't run. Run /worker load {owner} "
                                        f"to bring it back, then continue — or "
                                        f"tell me how you'd like to proceed "
                                        f"without it.",
                                        done_ids)
                                    return
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
                            try:
                                result = await self.tool_executor.run(
                                    tc.name, tc.arguments, ctx)
                            except asyncio.CancelledError:
                                # The daemon cancels a handler task the moment its
                                # client disconnects, and this await is where a turn
                                # spends most of its time. ToolExecutor.run re-raises
                                # CancelledError — a BaseException — so the `except
                                # Exception` below never sees it, and without this the
                                # round's add_assistant_tool_calls entry would keep
                                # ids that no tool result ever answers, invalidating
                                # this channel's NEXT request. Settle them, then
                                # re-raise: cancellation must still propagate, and is
                                # never swallowed.
                                _settle("[interrupted — call not completed]", done_ids)
                                raise
                            # Judged and reported on the RAW result, before
                            # guard.record replaces a repeat with its own
                            # "[repeat-guard] ..." wrapper. Reading the wrapper
                            # instead handed the operator the guard's message
                            # where the ConnectError should have been -- the
                            # one line they need to tell a dead host from a
                            # quiet one.
                            raw_result = result
                            result = guard.record(tc.name, tc.arguments, result)
                            if tc.name in POLL_TOOLS:
                                if is_worker_failure(raw_result):
                                    poll_failures[tc.name] = poll_failures.get(tc.name, 0) + 1
                                    if poll_failures[tc.name] >= POLL_FAILURE_LIMIT:
                                        conv.add_tool_result(tc.id, result)
                                        done_ids.add(tc.id)
                                        yield _settle_and_handback(
                                            poll_failure_question(
                                                tc.name, poll_failures[tc.name],
                                                raw_result),
                                            done_ids)
                                        return
                                else:
                                    # Any success resets it: the trigger is
                                    # CONSECUTIVE failure, so a poll that comes
                                    # back empty-but-fine clears the count.
                                    poll_failures[tc.name] = 0
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
