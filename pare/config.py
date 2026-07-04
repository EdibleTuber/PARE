"""PARE configuration: subclasses agent_core.config.BaseConfig with PARE-specific fields.

All settings read from PARE_* env vars using load_config with the "pare" agent name.
Defaults assume a local lab setup.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from agent_core.config import BaseConfig
from agent_core.config import load_config as _load_base_config


@dataclass
class PAREConfig(BaseConfig):
    """PARE-specific configuration. All BaseConfig fields are inherited; we only
    add fields PARE alone needs.

    The apk_re_agents_url points to the APK RE agents service endpoint (default
    assumes localhost on port 8000 in the local lab).
    """

    # Override BaseConfig's Qwen default: gemma-4-26b handles tool-calling
    # far more reliably for PARE's agentic loop. Env-overridable via PARE_MODEL.
    model: str = "gemma-4-26b-a4b-it-q4_k_m"

    apk_re_agents_url: str = "http://127.0.0.1:8000"
    workers_yaml_path: str = "workers.yaml"
    audit_dir: Path = field(
        default_factory=lambda: Path.home() / ".local" / "share" / "pare" / "audit"
    )
    project_marker: str | None = ".pare"


def load_config() -> PAREConfig:
    """Load PARE config from PARE_* environment variables."""
    return _load_base_config(PAREConfig, agent_name="pare")
