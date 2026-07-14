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


def test_system_prompt_includes_session_liveness_guidance():
    agent = PareAgent()
    pb = MagicMock()
    pb.render_profile.return_value = ""
    pb.render_wisdom.return_value = ""
    pb.render_scratchpad.return_value = ""
    pb.render_commands_catalog.return_value = ""
    agent.prompt_builder = pb
    ctx = MagicMock(); ctx.channel_id = "c"

    prompt = agent.system_prompt(ctx)

    assert "list_sessions" in prompt


def test_system_prompt_includes_dynamic_flow_steering():
    """Steers the model past the observed enumerate_processes loop: once
    attached, instrument off the session_id — don't re-enumerate / re-attach."""
    agent = PareAgent()
    pb = MagicMock()
    pb.render_profile.return_value = ""
    pb.render_wisdom.return_value = ""
    pb.render_scratchpad.return_value = ""
    pb.render_commands_catalog.return_value = ""
    agent.prompt_builder = pb
    ctx = MagicMock(); ctx.channel_id = "c"

    prompt = agent.system_prompt(ctx)

    assert "enumerate_processes" in prompt
    assert "read_hook_events" in prompt
    assert "instrument from there" in prompt


def test_system_prompt_includes_re_workflow():
    """The static->hypothesis->dynamic method: form a hypothesis in static, verify
    in dynamic, cross-check the result, and treat runtime surprises as leads back
    to static (the bidirectional loop)."""
    agent = PareAgent()
    pb = MagicMock()
    pb.render_profile.return_value = ""
    pb.render_wisdom.return_value = ""
    pb.render_scratchpad.return_value = ""
    pb.render_commands_catalog.return_value = ""
    agent.prompt_builder = pb
    ctx = MagicMock(); ctx.channel_id = "c"

    prompt = agent.system_prompt(ctx)

    assert "static forms the hypothesis" in prompt   # the method is stated
    assert "Cross-check" in prompt                    # verify result vs hypothesis
    assert "loop runs both ways" in prompt            # dynamic surprise -> static
