"""The TUI must be importable and expose an App subclass, so a packaging
mistake fails here rather than at an operator's first launch."""
from textual.app import App


def test_app_is_a_textual_app():
    from pare.tui.app import PareTUI
    assert issubclass(PareTUI, App)


def test_main_is_callable():
    from pare.tui.app import main
    assert callable(main)
