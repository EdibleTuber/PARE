"""Suite-wide isolation."""
import pytest


@pytest.fixture(autouse=True)
def _isolated_xdg_config_home(tmp_path_factory, monkeypatch):
    """PareTUI reads (and, on a theme change, writes) $XDG_CONFIG_HOME/pare/
    tui.json by default. Most tests construct it with that default, so point
    XDG_CONFIG_HOME at a tmp dir: no test may read or write the operator's
    real ~/.config."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path_factory.mktemp("xdg-config")))
