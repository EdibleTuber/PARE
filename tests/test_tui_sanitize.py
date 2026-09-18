"""Escapes from an untrusted board must not survive into rendered output."""
import pytest


@pytest.mark.parametrize("payload,unsafe_bytes", [
    (b"\x1b[2J", ["\x1b"]),            # clear screen
    (b"\x1b[1;1H", ["\x1b"]),          # cursor home
    (b"\x1b]0;pwned\x07", ["\x1b", "\x07"]),   # set window title (OSC)
    (b"\x07", ["\x07"]),               # bell
    (b"\x08" * 40, ["\x08"]),          # backspace run
    (b"\r" * 10, ["\r"]),              # carriage-return overwrite
])
def test_control_sequences_do_not_survive(payload, unsafe_bytes):
    from pare.tui.sanitize import for_display

    out = for_display(b"before" + payload + b"after")
    for unsafe_byte in unsafe_bytes:
        assert unsafe_byte not in out
    assert "before" in out and "after" in out


def test_newlines_and_tabs_are_preserved():
    """Over-sanitising makes a console log unreadable; these are legitimate."""
    from pare.tui.sanitize import for_display

    assert for_display(b"a\nb\tc") == "a\nb\tc"


def test_invalid_utf8_does_not_raise():
    from pare.tui.sanitize import for_display

    assert for_display(b"\xff\xfe ok")


def test_clip_args_bounds_a_huge_argument():
    from pare.tui.sanitize import clip_args

    out = clip_args({"script": "A" * 100_000})
    assert len(out) < 10_000


def test_clip_args_strips_control_chars():
    from pare.tui.sanitize import clip_args

    assert "\x1b" not in clip_args({"payload": "x\x1b[2Jy"})
