"""Pins on the hardware worker's artifact wiring in workers.yaml.

docs/superpowers/specs/2026-09-12-artifact-wiring-design.md and the P4 plan
docs/superpowers/plans/2026-10-06-artifact-wiring-p4.md (T7) landed the
artifact fields on the hardware entry. These pins guard exactly what landed:
a green run means the entry carries what the acceptance gates assume —
artifact_root and artifact_drive_id for drive identity, the read_timeout
artifact floor, and NO explicit artifact_host (its absence is deliberate;
see below).

artifact_host, empirically established from the installed agent_core under
.venv (agent_core/workers/types.py, WorkerSpec.validate_artifact_declaration):
when artifact_root is set, the transport is streamable_http, and
artifact_host is absent from the entry, the loader defaults spec.artifact_host
to urlsplit(endpoint).hostname. So after load spec.artifact_host ==
"100.97.133.126" for this entry, and that is pinned below — an entry that
starts declaring the host by hand, or a loader that stops defaulting it, is
a design change the acceptance gates should hear about as a RED test.

Provenance of artifact_drive_id: re-read from
/mnt/bench-store/.bench-store-id on pare-bench over ssh on 2026-10-06. If
the drive is ever re-imaged, the workers.yaml entry and this pin are updated
together.
"""
from pathlib import Path

import yaml

from agent_core.workers.registry import WorkerRegistry

_WORKERS_YAML = Path(__file__).resolve().parent.parent / "workers.yaml"


def _hardware_spec():
    """The hardware WorkerSpec from the real workers.yaml, through the same
    loader the daemon uses (WorkerRegistry.load)."""
    return WorkerRegistry.load(_WORKERS_YAML).get("hardware")


def test_hardware_worker_is_declared():
    """The entry every artifact gate hangs off must exist in the file the
    daemon actually loads."""
    registry = WorkerRegistry.load(_WORKERS_YAML)
    names = {spec.name for spec in registry.all()}
    assert "hardware" in names, (
        f"workers.yaml declares no `hardware` worker (found: {sorted(names)}) "
        f"— the P4 T7 artifact wiring has nothing to pin.")


def test_hardware_artifact_root():
    """The T7 root. Must be absolute and normalised — WorkerSpec's
    validator runs at load, so a malformed root fails the load itself; this
    pin is against drift to some other *valid* root."""
    spec = _hardware_spec()
    assert spec.artifact_root == "/mnt/bench-store", (
        f"hardware artifact_root is {spec.artifact_root!r}, not "
        f"'/mnt/bench-store' as landed by P4 T7.")


def test_hardware_artifact_drive_id():
    """The drive sentinel. Provenance: re-read from
    /mnt/bench-store/.bench-store-id on pare-bench over ssh on 2026-10-06;
    if the drive is ever re-imaged, the workers.yaml entry and this pin are
    updated together. WorkerSpec requires root and drive-id to be declared
    together, so both must move in one edit."""
    spec = _hardware_spec()
    assert spec.artifact_drive_id == "b2657680-9f16-4918-9195-ef3ba17da924", (
        f"hardware artifact_drive_id is {spec.artifact_drive_id!r}, not the "
        f"sentinel re-read from /mnt/bench-store/.bench-store-id on "
        f"2026-10-06.")


def test_hardware_read_timeout():
    """The artifact floor: 60 s, derived from the measured write rate in the
    comment workers.yaml now carries next to it (P4 T7)."""
    spec = _hardware_spec()
    assert spec.read_timeout == 60, (
        f"hardware read_timeout is {spec.read_timeout!r}, not the 60 s "
        f"artifact floor landed by P4 T7.")


def test_hardware_artifact_host_undeclared_but_defaulted():
    """The absence is the contract. The header comment at workers.yaml:39-41
    and the P4 plan (design summary §5) say a networked worker must NOT
    declare artifact_host by hand — the loader defaults it to the endpoint
    hostname. So: raw parse shows no artifact_host key on the entry, and the
    loaded spec carries the defaulted hostname (see the empirical note in
    the module docstring; the default is pinned, not guessed)."""
    raw = yaml.safe_load(_WORKERS_YAML.read_text())
    assert "artifact_host" not in raw["workers"]["hardware"], (
        "workers.yaml's hardware entry declares artifact_host by hand; the "
        "networked default is the design — remove the key.")
    spec = _hardware_spec()
    assert spec.artifact_host == "100.97.133.126", (
        f"loaded hardware artifact_host is {spec.artifact_host!r}, not the "
        f"endpoint-hostname default '100.97.133.126' the acceptance gates "
        f"assume.")
