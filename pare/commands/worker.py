"""/worker — operator control over which MCP workers are loaded.

Unloading is how you reclaim the context a worker's tool schemas cost in every
turn. It is blunt by design: the client disconnects and, for a stdio worker,
its process ends — so live Frida attachments and installed hooks go with it.
There is no confirmation prompt and no --force, so the command's own output is
the only warning the operator gets: it has to name what it just destroyed.

Any load or unload also changes the tool list, which changes the prompt
prefix, so the next turn reprocesses the conversation from scratch — one
noticeably slower reply. Both operations say so, because without a note an
operator would reasonably read that delay as a hang.

`reload` is unload-then-load under one lock, so a FAILED reload has already
destroyed everything an unload destroys — see `_render_reload`.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

from agent_core.commands.base import Command
from agent_core.protocol.messages import ResponseMessage

from pare.commands._snapshot_render import render_table

_SUBCOMMANDS = ("list", "tools", "load", "unload", "reload")

_REPROCESS_NOTE = (
    "note: the tool list changed, so the next turn reprocesses the "
    "conversation prefix — expect one slower reply.")

# What an unload destroys. Shared by /worker unload and by a FAILED /worker
# reload, because reload's unload half runs first and unconditionally — see
# Worker._render_reload.
_DESTRUCTION_BODY = (
    "any live attachments, sessions and installed hooks for this worker "
    "are gone; captures of earlier results remain searchable, though "
    "session ids in them are now stale.")


class Worker(Command):
    name = "worker"
    args = "[list | tools <name> | load <name> | unload <name> | reload <name>]"
    description = "List, load, unload or reload MCP workers without restarting."
    requires = ("worker_manager",)

    async def run(self, raw_args: str, ctx) -> AsyncIterator:
        parts = raw_args.split()
        sub = parts[0] if parts else "list"
        target = parts[1] if len(parts) > 1 else None
        mgr = ctx.agent.worker_manager

        if sub not in _SUBCOMMANDS:
            yield ResponseMessage(
                text=f"unknown /worker subcommand {sub!r} — usage: /worker {self.args}")
            return
        if sub != "list" and target is None:
            yield ResponseMessage(text=f"usage: /worker {sub} <name>")
            return

        if sub == "list":
            yield ResponseMessage(text=self._render_list(mgr))
        elif sub == "tools":
            names = mgr.tools_of(target)
            yield ResponseMessage(
                text="\n".join(names) if names
                else f"{target} is not loaded (or exposes no tools) — /worker list")
        elif sub == "load":
            yield ResponseMessage(text=self._render_load(await mgr.load(target)))
        elif sub == "unload":
            yield ResponseMessage(text=self._render_unload(await mgr.unload(target)))
        else:
            # Snapshot what is about to be destroyed BEFORE the call: reload's
            # unload half runs first and removes the tools unconditionally, but
            # a failed reload's WorkerOpResult carries only the load half's
            # (zero) counts, so the result alone cannot say what went away.
            was_loaded = bool(mgr.is_loaded(target))
            doomed = len(mgr.tools_of(target) or [])
            yield ResponseMessage(
                text=self._render_reload(await mgr.reload(target), was_loaded, doomed))

    @staticmethod
    def _render_list(mgr) -> str:
        statuses = mgr.status()
        rows = []
        for s in statuses:
            rows.append({
                "worker": s.name,
                "state": "loaded" if s.loaded else "unloaded",
                "tools": str(s.tool_count) if s.loaded else "-",
                "transport": s.transport,
                "floor": s.risk_default,
                "boot": "auto" if s.autoload else "manual",
                "tags": ", ".join(s.capability_tags),
                "last error": s.last_error or "",
            })
        table = render_table(rows)
        # Eight columns competing for render_table's 100-char budget clip
        # `last error` down to the error's class — a realistic spawn_failed
        # message (a stale `command:` path in workers.yaml, spec 8.4) is well
        # over 100 chars on its own, so the table cell alone never shows the
        # path an operator needs to fix it. This is the only place that path
        # surfaces, so append it in full below the table — only when at least
        # one worker actually has an error, so a healthy fleet stays a clean
        # table.
        errors = [(s.name, s.last_error) for s in statuses if s.last_error]
        if not errors:
            return table
        footer = "\n".join(f"{name}: {err}" for name, err in errors)
        return f"{table}\n\n{footer}"

    @staticmethod
    def _render_load(res) -> str:
        if not res.ok:
            return f"{res.op} {res.name} failed [{res.error_kind}]: {res.error}"
        return (f"{res.op}ed {res.name} — {res.tool_count} tools available.\n"
                f"{_REPROCESS_NOTE}")

    @staticmethod
    def _render_unload(res) -> str:
        # WorkerManager.unload() removes the worker from the executor and the
        # pool BEFORE attempting the (timeout-bounded) disconnect — see its
        # docstring: "everything before the disconnect is unconditional and
        # cannot hang". So the tools are already gone whether or not the
        # disconnect itself finishes in time; only the disconnect's own
        # outcome is conditional. Claiming "client disconnected" on a
        # disconnect_timeout would contradict the very warning printed right
        # after it, which says the process may still be running.
        disconnect_state = "client disconnected" if res.ok else "disconnect did not complete"
        head = f"unloaded {res.name} — {res.tool_count} tools removed, {disconnect_state}."
        # The tool-list mutation above is unconditional, so the reprocess note
        # applies regardless of whether the disconnect itself succeeded.
        tail = _REPROCESS_NOTE if res.ok else f"WARNING [{res.error_kind}]: {res.error}\n{_REPROCESS_NOTE}"
        return f"{head}\n{_DESTRUCTION_BODY}\n{tail}"

    @staticmethod
    def _render_reload(res, was_loaded: bool, doomed: int) -> str:
        """A failed reload has UNLOAD's consequences and LOAD's shape.

        WorkerManager.reload runs _unload_locked FIRST, and that removes the
        tools and the pool spec unconditionally before either half can fail.
        So rendering a failed reload through _render_load — a bare
        "reload frida failed [spawn_failed]: ..." — invites the operator to
        read "failed" as "unchanged", when in fact the tools are gone, every
        live attachment died with the process, and the next turn reprocesses.
        Say what actually happened, and distinguish the two failure
        directions: WorkerOpResult.error carries an "unload half failed:"
        prefix when the load half was never reached.
        """
        if res.ok:
            return (f"{res.op}ed {res.name} — {res.tool_count} tools available.\n"
                    f"{_REPROCESS_NOTE}")
        head = f"{res.op} {res.name} failed [{res.error_kind}]: {res.error}"
        if not was_loaded:
            # The unload half was a no-op, so this is a plain failed load: it
            # destroyed nothing and the tool list never changed. No warning,
            # and no reprocess note (see
            # test_failed_load_does_not_claim_a_reprocess_is_coming).
            return (f"{head}\n{res.name} was not loaded before this reload, "
                    f"so nothing was destroyed.")
        if str(res.error or "").startswith("unload half failed:"):
            state = (f"WARNING: the unload half removed {res.name}'s {doomed} tools "
                     f"before its disconnect was attempted, and the load half never "
                     f"ran — {res.name} is now unloaded, but its process may still "
                     f"be running.")
        else:
            state = (f"WARNING: the unload half had already completed, so {res.name} "
                     f"is now UNLOADED — its {doomed} tools are gone and the load "
                     f"half did not bring them back. Fix the cause above, then "
                     f"/worker load {res.name}.")
        return f"{head}\n{state}\n{_DESTRUCTION_BODY}\n{_REPROCESS_NOTE}"
