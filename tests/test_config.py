"""Tests for PAREConfig — env-driven configuration."""
import os

import pytest

from pare.config import PAREConfig, load_config


def test_config_defaults():
    """Test that PAREConfig has sensible defaults."""
    cfg = PAREConfig()
    # Inherited from BaseConfig
    assert cfg.inference_url.startswith("http")
    assert cfg.model
    assert cfg.vault_path
    assert cfg.collection_id == "vault"
    # PARE-specific
    assert cfg.apk_re_agents_url == "http://127.0.0.1:8000"


def test_config_reads_env(monkeypatch):
    """Test that load_config reads PARE_* env vars and overrides defaults."""
    monkeypatch.setenv("PARE_INFERENCE_URL", "http://example.invalid:11434")
    monkeypatch.setenv("PARE_APK_RE_AGENTS_URL", "http://example.invalid:8000")
    monkeypatch.setenv("PARE_VAULT_PATH", "/tmp/example-vault")
    monkeypatch.setenv("PARE_MODEL", "test-model")
    monkeypatch.setenv("PARE_COLLECTION_ID", "test-collection")

    cfg = load_config()
    assert cfg.inference_url == "http://example.invalid:11434"
    assert cfg.apk_re_agents_url == "http://example.invalid:8000"
    assert str(cfg.vault_path) == "/tmp/example-vault"
    assert cfg.model == "test-model"
    assert cfg.collection_id == "test-collection"


def test_inherited_baseconfig_fields():
    """Test that PAREConfig properly inherits BaseConfig fields."""
    cfg = PAREConfig()
    # Spot-check some BaseConfig fields
    assert hasattr(cfg, "history_depth")
    assert hasattr(cfg, "username")
    assert hasattr(cfg, "searxng_url")
    assert hasattr(cfg, "fetch_max_bytes")
    assert hasattr(cfg, "socket_path")
