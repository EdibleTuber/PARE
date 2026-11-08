"""Suite-wide isolation."""
import pytest


@pytest.fixture(autouse=True)
def _isolated_xdg_config_home(tmp_path_factory, monkeypatch):
    """PareTUI reads (and, on a theme change, writes) $XDG_CONFIG_HOME/pare/
    tui.json by default. Most tests construct it with that default, so point
    XDG_CONFIG_HOME at a tmp dir: no test may read or write the operator's
    real ~/.config."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path_factory.mktemp("xdg-config")))


@pytest.fixture(autouse=True)
def _no_live_bench_endpoint(tmp_path_factory, monkeypatch):
    """No mounted PareTUI may connect to the real bench Pi.

    `PareTUI._build_uart_pane` resolves its console source with
    `hardware_endpoint(os.environ)`, whose default registry is the
    checkout's own `workers.yaml` -- and that file names the bench Pi's
    real endpoint. Every test that mounts the app would then attach an
    `McpConsoleSource` to it; when the Pi is unreachable the connect never
    returns, and Textual's `run_test` times out after 60s per test.

    Only the DEFAULT root changes, to an empty directory. A test that
    passes `repo_root=` itself, sets `PARE_WORKERS_YAML_PATH`, or sets
    `PARE_TUI_HARDWARE_ENDPOINT` (after this fixture clears any value
    inherited from the operator's shell) still gets exactly what it asks
    for. `PARE_WORKERS_YAML_PATH` itself is left alone: the daemon's
    config and `resolve_spawn_cwd` read it too."""
    import pare.tui.app as tui_app

    real = tui_app.hardware_endpoint
    empty_root = tmp_path_factory.mktemp("no-workers-yaml")

    def hardware_endpoint(env, repo_root=None):
        return real(env, repo_root=empty_root if repo_root is None else repo_root)

    monkeypatch.delenv("PARE_TUI_HARDWARE_ENDPOINT", raising=False)
    monkeypatch.setattr(tui_app, "hardware_endpoint", hardware_endpoint)
