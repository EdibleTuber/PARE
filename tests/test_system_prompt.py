"""system_prompt embeds the base RE-methodology prompt (Orient -> Enumerate ->
Hypothesize -> Verify -> Re-orient), plus vault + live-session mechanics."""
from unittest.mock import MagicMock

from pare.agent import PareAgent


def _prompt() -> str:
    agent = PareAgent()
    pb = MagicMock()
    pb.render_profile.return_value = ""
    pb.render_wisdom.return_value = ""
    pb.render_scratchpad.return_value = ""
    pb.render_commands_catalog.return_value = ""
    agent.prompt_builder = pb
    ctx = MagicMock(); ctx.channel_id = "c"
    return agent.system_prompt(ctx)


def test_prompt_has_all_five_methodology_beats():
    p = _prompt()
    for beat in ("Orient", "Enumerate", "Hypothesize", "Verify", "Re-orient"):
        assert beat in p, f"missing beat: {beat}"


def test_enumerate_builds_candidate_set_before_committing():
    """The core fix: enumerate the candidate family before committing to one."""
    p = _prompt().lower()
    assert "candidate" in p
    assert "family" in p


def test_operator_description_is_a_lead_not_ground_truth():
    """Anti-overfitting: don't anchor on the operator/harness label as the target."""
    p = _prompt().lower()
    assert "lead" in p
    assert "corroborate" in p


def test_empty_is_not_a_contradiction():
    """Empty capture => action not triggered yet; do NOT change targets."""
    p = _prompt().lower()
    assert "triggered" in p
    assert "contradict" in p  # matches "contradict"/"contradicts"/"contradiction"


def test_hypothesis_before_action_is_explicit():
    p = _prompt().lower()
    assert "before you" in p  # "...before you act / attach / hook"


def test_preserves_dataflow_exit_point_lesson():
    """Trace data to where it appears, not the named method's argument."""
    p = _prompt()
    assert "doFinal" in p
    assert "not the named" in p.lower()


def test_reorient_keeps_bidirectional_forward_lead():
    """A runtime-only class / native call is a forward lead back to static,
    not a dead-end."""
    p = _prompt().lower()
    assert "native" in p


def test_no_repeat_discipline_has_requery_carveout():
    """The general no-repeat rule must not suppress the mandatory liveness check."""
    p = _prompt()
    assert "list_sessions" in p
    assert "cannot have changed" in p.lower()


def test_preserves_vault_discipline():
    p = _prompt()
    assert "search_vault" in p
    assert "read_vault_doc" in p


def test_preserves_dynamic_flow_steering():
    p = _prompt()
    assert "enumerate_processes" in p
    assert "read_hook_events" in p
    assert "instrument from there" in p


def test_preserves_approval_gate_line():
    p = _prompt().lower()
    assert "least-invasive" in p
