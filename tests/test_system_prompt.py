"""system_prompt embeds the base RE-methodology prompt (Orient -> Enumerate ->
Hypothesize -> Verify -> Re-orient), plus vault + live-session mechanics.

Phrase assertions run against a whitespace-collapsed, lowercased copy of the
prompt (`_flat`) so prose can be re-wrapped without breaking tests; beat names
are checked on the raw text (they are distinct capitalized tokens)."""
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


def _flat() -> str:
    """Prompt with runs of whitespace collapsed to single spaces, lowercased."""
    return " ".join(_prompt().split()).lower()


def test_prompt_has_all_five_methodology_beats():
    p = _prompt()
    for beat in ("Orient", "Enumerate", "Hypothesize", "Verify", "Re-orient"):
        assert beat in p, f"missing beat: {beat}"


def test_enumerate_builds_candidate_set_before_committing():
    """The core fix: enumerate the candidate family before committing to one."""
    f = _flat()
    assert "candidate" in f
    assert "family" in f


def test_enumerate_candidates_are_externalized():
    """gemma won't retain an implicit set across turns; it must WRITE the list."""
    f = _flat()
    assert "list the candidates explicitly" in f
    assert "fallback" in f


def test_operator_description_is_a_lead_not_ground_truth():
    """Anti-overfitting: don't anchor on the operator/harness label as the target."""
    f = _flat()
    assert "lead" in f
    assert "corroborate" in f
    assert "menu label" in f or "harness" in f  # the anchor is named to be forbidden


def test_empty_is_not_a_contradiction():
    """Empty capture => action not triggered yet; do NOT change targets."""
    f = _flat()
    assert "triggered" in f
    assert "contradict" in f  # matches "contradict"/"contradicts"/"contradiction"


def test_hypothesis_before_action_is_a_hard_brake():
    """No stated hypothesis, no tool call — the brake against premature hooking."""
    f = _flat()
    assert "until you have written down" in f
    assert "no tool call" in f


def test_preserves_dataflow_exit_point_lesson():
    """Trace data to where it appears, not the named method's argument."""
    assert "doFinal" in _prompt()
    assert "not the named" in _flat()


def test_compute_path_avoids_java_bridge():
    """Preserve main's compute HOW: pure-JS execute_script, no Java, no atob."""
    p = _prompt()
    assert "execute_script" in p
    assert "atob" in p.lower()


def test_reorient_keeps_bidirectional_forward_lead():
    """A runtime-only class / native call is a forward lead back to static,
    not a dead-end."""
    assert "native" in _flat()


def test_no_repeat_discipline_has_requery_carveout():
    """The general no-repeat rule must not suppress the mandatory liveness check."""
    f = _flat()
    assert "list_sessions" in f
    assert "cannot have changed" in f


def test_preserves_vault_discipline():
    p = _prompt()
    assert "search_vault" in p
    assert "read_vault_doc" in p


def test_preserves_dynamic_flow_steering():
    f = _flat()
    assert "enumerate_processes" in f
    assert "read_hook_events" in f
    assert "instrument from there" in f


def test_preserves_approval_gate_line():
    assert "least-invasive" in _flat()
