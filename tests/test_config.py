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


def test_arcticbase_url_defaults_to_unset_rather_than_localhost():
    """A default that is right for one deployment and wrong for every other one
    publishes findings into a void that looks like success. Unset is honest."""
    assert PAREConfig().arcticbase_url == ""


def test_arcticbase_url_is_read_from_the_env(monkeypatch):
    monkeypatch.setenv("PARE_ARCTICBASE_URL", "http://bench.example.invalid:2929")
    assert load_config().arcticbase_url == "http://bench.example.invalid:2929"


def test_the_client_names_the_env_var_the_config_actually_reads(monkeypatch):
    """Pins the two together. A renamed field silently makes the client's
    'set PARE_ARCTICBASE_URL' message point at a variable nothing reads."""
    import dataclasses

    from pare.arcticbase import _URL_ENV

    names = {f.name for f in dataclasses.fields(PAREConfig)}
    assert _URL_ENV == "PARE_" + "arcticbase_url".upper()
    assert "arcticbase_url" in names
