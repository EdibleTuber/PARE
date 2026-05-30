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


def main() -> None:
    config = load_config()
    asyncio.run(run_repl(config.socket_path, _PareRenderer()))


if __name__ == "__main__":
    main()
