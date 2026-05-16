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

from agent_core.agent import Agent, HandlerContext

from pare.commands.hello import Hello


class PareAgent(Agent):
    name = "pare"
    env_prefix = "PARE_"

    tools = []          # add Tool subclasses here
    commands = [Hello]  # framework builtins serve /help, /clear, etc.

    def setup(self) -> None:
        """Construct domain-specific resources here. Framework managers
        (profile, wisdom, channels, inference, retrieval, websearch,
        allowlist, approval_registry, learning, fetcher, config) are
        already populated on self at this point."""
        pass

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
