"""PARE terminal CLI entry point.

Wires agent_core's generic REPL (`run_repl`) to PARE's config — same
`socket_path` the daemon binds — and a renderer. Run the daemon first
(`python -m pare`), then this in a second terminal:

    pare-cli              # console script, or
    python -m pare.cli

agent_core.adapters.cli is a library (no __main__); each agent provides its
own launcher so it can supply its config + renderer.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime

from agent_core.adapters.cli import run_repl
from pare.config import load_config


class _PareRenderer:
    """Minimal renderer implementing the agent_core REPL `Renderer` protocol
    (splash + format_message). format_message returns None for every message,
    delegating to the REPL's built-in default formatting. Can be enriched later
    to pretty-print PARE-specific message types."""

    def splash(self) -> str:
        return "PARE — Personal Agentic Reverse Engineer. Type a message, or /help."

    def format_message(self, msg) -> str | None:
        return None


def _new_channel_id() -> str:
    """Mint a fresh per-launch channel id (cli-<YYYYMMDD>-<HHMMSS>).

    Each pare-cli invocation gets its own channel, so the conversation starts
    clean and is persisted to its own transcript file under
    <vault>/_channels/pare/<id>/history.jsonl — instead of every launch sharing
    (and replaying) the daemon's cli-default channel, which leaked context
    across unrelated sessions."""
    return datetime.now().strftime("cli-%Y%m%d-%H%M%S")


def main() -> None:
    config = load_config()
    asyncio.run(run_repl(config.socket_path, _PareRenderer(),
                         channel_id=_new_channel_id(), cwd=os.getcwd()))


if __name__ == "__main__":
    main()
