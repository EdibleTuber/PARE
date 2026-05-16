"""PARE configuration: subclasses agent_core.config.BaseConfig with PARE-specific fields.

All settings read from PARE_* env vars using load_config with the "pare" agent name.
Defaults assume a local lab setup.
"""
from __future__ import annotations

from dataclasses import dataclass

from agent_core.config import BaseConfig
from agent_core.config import load_config as _load_base_config


@dataclass
class PAREConfig(BaseConfig):
    """PARE-specific configuration. All BaseConfig fields are inherited; we only
    add fields PARE alone needs.

    The apk_re_agents_url points to the APK RE agents service endpoint (default
    assumes localhost on port 8000 in the local lab).
    """

    apk_re_agents_url: str = "http://127.0.0.1:8000"


def load_config() -> PAREConfig:
    """Load PARE config from PARE_* environment variables."""
    return _load_base_config(PAREConfig, agent_name="pare")
