"""system_prompt embeds the base prompt incl. vault-usage guidance."""
from unittest.mock import MagicMock

from pare.agent import PareAgent


def test_system_prompt_includes_vault_guidance():
    agent = PareAgent()
    pb = MagicMock()
    pb.render_profile.return_value = ""
    pb.render_wisdom.return_value = ""
    pb.render_scratchpad.return_value = ""
    pb.render_commands_catalog.return_value = ""
    agent.prompt_builder = pb
    ctx = MagicMock(); ctx.channel_id = "c"

    prompt = agent.system_prompt(ctx)

    assert "search_vault" in prompt
    assert "read_vault_doc" in prompt
