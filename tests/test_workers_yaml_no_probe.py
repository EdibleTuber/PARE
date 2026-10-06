"""Tripwire against a declared `probe` worker in workers.yaml.

docs/superpowers/specs/2026-09-12-artifact-wiring-design.md §8: the probe
worker's workers.yaml entry needs a **tripwire, not a sentence** — "leaving it
declared is a finding" is enforced by nobody else. The format-comment test is
structurally blind to anything below the `workers:` key, and the risk-overrides
coverage test would at most emit a UserWarning. A test that fails on a declared
`probe` worker is the enforcement.

It passes today: the real workers.yaml declares no `probe` entry. The
acceptance branch of the parent spec deploys the probe worker, and turning
THIS test RED there is the intended outcome — the finding firing, not a
regression. The second test below builds a synthetic registry that does
declare `probe` and asserts this same helper fires on it, so the tripwire
logic itself can never rot into a no-op that passes on any file.
"""
from pathlib import Path

from agent_core.workers.registry import WorkerRegistry

_WORKERS_YAML = Path(__file__).resolve().parent.parent / "workers.yaml"


def _declared_probe_names(registry: WorkerRegistry) -> list[str]:
    """Sorted names of the entries in a parsed registry that are the probe
    worker — every spec whose name is exactly "probe". Empty means clean."""
    return sorted(spec.name for spec in registry.all() if spec.name == "probe")


def test_no_probe_worker_is_declared():
    """The real workers.yaml must not declare the probe worker. Per the
    parent spec §8 the probe is deployed for the acceptance run only; leaving
    it declared afterwards is a finding, and this is the thing that makes
    "nobody enforces that" false. Note the helper is only as strong as the
    tripwire-fires test below — the two are deliberately paired."""
    registry = WorkerRegistry.load(_WORKERS_YAML)
    found = _declared_probe_names(registry)
    assert not found, (
        f"workers.yaml declares the probe worker as {found}. Per the artifact-"
        f"wiring spec §8 a probe left declared after the acceptance run is a "
        f"finding: remove the entry (and any risk_overrides pins for it) once "
        f"the run is over.")


def test_the_tripwire_fires_on_a_declared_probe(tmp_path):
    """The other half: the check must actually be able to fire. Builds a
    minimal synthetic workers.yaml with exactly one entry named `probe`
    (inert stdio, dummy command, no artifact fields) plus a normal entry,
    loads it through the same daemon loader, and asserts the helper returns
    ["probe"] — so a green run of the first test can never mean a broken
    helper."""
    fake_yaml = tmp_path / "workers.yaml"
    fake_yaml.write_text(
        "workers:\n"
        "  probe:\n"
        "    command: /nonexistent/pare-probe-mcp\n"
        "    transport: stdio\n"
        "    risk_default: low\n"
        "    autoload: false\n"
        "  static:\n"
        "    command: /nonexistent/pare-static-mcp\n"
        "    transport: stdio\n"
        "    risk_default: low\n"
        "    autoload: false\n"
    )
    registry = WorkerRegistry.load(fake_yaml)
    assert _declared_probe_names(registry) == ["probe"]
