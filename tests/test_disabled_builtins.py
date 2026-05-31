"""PARE disables the vault-scoped shell builtins (vault_path is PARE's private
state dir under the RAG-only read design, not a research corpus)."""
from pare.agent import PareAgent


def test_shell_builtins_disabled():
    expected = {"cat", "head", "tail", "ls", "grep", "find", "read_lines"}
    assert expected.issubset(PareAgent.disabled_builtins)


def test_search_vault_not_disabled():
    # PAL research access must stay enabled.
    assert "search_vault" not in PareAgent.disabled_builtins
