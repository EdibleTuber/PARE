"""Tests for PARE's CLI launcher (pare.cli).

Covers per-launch channel-id minting: each pare-cli invocation must start a fresh
conversation (its own channel + transcript file) instead of sharing the daemon's
cli-default channel, which leaked context across separate sessions.
"""
from __future__ import annotations

import re
from pathlib import Path

from agent_core.channels import validate_channel_id

from pare import cli


def test_new_channel_id_format():
    """Minted ids are cli-<YYYYMMDD>-<HHMMSS> so each launch is unique and the
    channel dir is human-readable on disk."""
    cid = cli._new_channel_id()
    assert re.fullmatch(r"cli-\d{8}-\d{6}", cid)


def test_new_channel_id_is_a_valid_channel_id():
    """The minted id must satisfy agent_core's channel_id validation, or
    get_or_create raises."""
    assert validate_channel_id(cli._new_channel_id())


def test_main_passes_a_per_launch_channel_id_to_repl(monkeypatch):
    """main() mints a fresh cli- channel id and hands it to run_repl, so the
    daemon routes to a per-launch channel instead of cli-default."""
    captured: dict = {}

    async def fake_run_repl(socket_path, renderer, channel_id=None, cwd=None):
        captured["socket_path"] = socket_path
        captured["channel_id"] = channel_id
        captured["cwd"] = cwd

    class _FakeConfig:
        socket_path = Path("/ignored.sock")

    monkeypatch.setattr(cli, "load_config", lambda: _FakeConfig())
    monkeypatch.setattr(cli, "run_repl", fake_run_repl)

    cli.main()

    assert captured["channel_id"] is not None
    assert captured["channel_id"].startswith("cli-")
    assert validate_channel_id(captured["channel_id"])
