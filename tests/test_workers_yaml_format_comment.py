"""workers.yaml's format comment became load-bearing; keep it complete.

agent_core's WorkerSpec now sets `extra="forbid"`, so a key the comment fails to
mention is not merely undocumented -- it is a config-load error, and one unknown
key aborts the WHOLE file: no workers and no risk_overrides. That makes this
comment the operator's only map of what may be written, which is a job a
hand-maintained list is bad at.

So it is checked rather than trusted. This test is the thing that stops the
comment rotting the next time WorkerSpec gains a field -- which is exactly how
it came to document six of sixteen.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agent_core.workers.types import WorkerSpec

WORKERS_YAML = Path(__file__).resolve().parent.parent / "workers.yaml"


def _header_comment() -> str:
    """Everything before the `workers:` mapping -- the operator's map."""
    lines = []
    for line in WORKERS_YAML.read_text().splitlines():
        if line.startswith("workers:"):
            break
        lines.append(line)
    return "\n".join(lines)


def test_the_format_comment_mentions_every_field_the_daemon_accepts():
    header = _header_comment()
    # `name` comes from the mapping key, so it is never written as a field.
    expected = set(WorkerSpec.model_fields) - {"name"}
    missing = sorted(f for f in expected if f not in header)
    assert not missing, (
        f"workers.yaml's format comment does not mention {missing}. With "
        f"extra=\"forbid\" an operator who trusts this comment and omits a key "
        f"is fine, but one who needs a key it fails to mention has no way to "
        f"know it is permitted -- and a key NOT permitted aborts the whole file.")


def test_the_comment_does_not_advertise_fields_that_do_not_exist():
    """The other direction: a field removed from WorkerSpec but still documented
    sends an operator to write a key that now aborts the entire file."""
    header = _header_comment()
    # Only the two-column entries are offers. Requiring a run of whitespace
    # AFTER the token is what distinguishes `autoload   connect at boot` from a
    # wrapped prose line that happens to begin with a word -- an earlier version
    # matched only the leading indent and flagged half the prose as field names.
    import re
    offered = set(re.findall(r"^#\s{2,}([a-z_]{3,})\s{2,}\S", header, re.MULTILINE))
    bogus = sorted(offered - set(WorkerSpec.model_fields))
    assert not bogus, (
        f"workers.yaml's comment offers {bogus}, which WorkerSpec does not "
        f"accept; writing one would abort the whole file")
