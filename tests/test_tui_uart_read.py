"""Each honesty field must make a visible difference.

Assertions are on the DIFFERENCE a condition makes, not on exact pane text:
a snapshot of rendered output rots on the next styling change, while "the
render differs when bytes were dropped" stays true.

These tests are written against `FakeConsoleSource` as it behaves today
(`pare/tui/sources/fake_console.py`, fixed under Task 8's review to model
`console_read`'s cursor-relative `dropped` and windowed `capture_gaps`
faithfully) -- not against an older, looser fake API. All of the scripted
scenarios below (`drop(512)` on an 8-byte buffer, `gap(at=...)` as a void
call, `die()`, no `fail_next_*` usage at all) already match that real,
current API, so nothing here needed to be rewritten to match it; that was
checked line by line against `fake_console.py` rather than assumed.
"""
from __future__ import annotations


async def _pane_after(script):
    from pare.tui.panes.uart import UartPane
    from pare.tui.sources.fake_console import FakeConsoleSource

    src = FakeConsoleSource()
    pane = UartPane(source=src)
    await pane.attach()
    script(src)
    await pane.advance()
    return pane, src


async def test_plain_output_is_rendered():
    pane, _ = await _pane_after(lambda s: s.feed(b"boot ok\n"))
    assert "boot ok" in "\n".join(pane.snapshot_lines())


async def test_dropped_bytes_change_the_render():
    """Isolate the dropped-marker rendering from the data it ALSO affects.

    The brief's original scenario here (`feed(b"boot ok\\n")` then
    `drop(512)`) evicts the entire 8-byte buffer, so the resulting `data`
    is empty -- the render differs because the text vanished, not because
    a marker was added. Deleting the pane's dropped-marker code entirely
    still passes that scenario (verified by mutation: `if slice_.dropped:`
    -> `if False and slice_.dropped:` left this test green). That is the
    "test exercises a path that was accidentally already correct" failure
    mode, not a real discrimination of R1's dropped-bytes requirement.

    Feeding a junk prefix that gets evicted, leaving the SAME trailing
    bytes the clean read sees, isolates the marker: `data`, `remaining`,
    `capture_gaps`, and `alive` all match between the two reads, so only
    `dropped` differs -- the render can only differ because of the marker.
    Verified failing against the pre-fix (marker-less) code and passing
    after.
    """
    clean, _ = await _pane_after(lambda s: s.feed(b"boot ok\n"))
    lossy, _ = await _pane_after(
        lambda s: (s.feed(b"XXXXXboot ok\n"), s.drop(5)))
    assert lossy.snapshot_lines() != clean.snapshot_lines()


async def test_a_capture_gap_changes_the_render():
    clean, _ = await _pane_after(lambda s: s.feed(b"abcdef"))
    gapped, _ = await _pane_after(
        lambda s: (s.feed(b"abc"), s.gap(at=3), s.feed(b"def")))
    assert gapped.snapshot_lines() != clean.snapshot_lines()


async def test_a_dead_session_changes_the_render():
    live, _ = await _pane_after(lambda s: s.feed(b"x"))
    dead, _ = await _pane_after(lambda s: (s.feed(b"x"), s.die()))
    assert dead.snapshot_lines() != live.snapshot_lines()


async def test_a_clamped_read_changes_the_render():
    from pare.tui.panes.uart import UartPane
    from pare.tui.sources.fake_console import FakeConsoleSource

    src = FakeConsoleSource()
    pane = UartPane(source=src, read_limit=4)
    await pane.attach()
    src.feed(b"0123456789")
    await pane.advance()
    unclamped = UartPane(source=FakeConsoleSource())
    await unclamped.attach()
    unclamped.source.feed(b"0123")
    await unclamped.advance()
    assert pane.snapshot_lines() != unclamped.snapshot_lines()


async def test_cursor_advances_across_polls_without_repeating_bytes():
    """The whole point of a cursor. A pane that re-reads from 0 renders the
    boot log again on every poll."""
    from pare.tui.panes.uart import UartPane
    from pare.tui.sources.fake_console import FakeConsoleSource

    src = FakeConsoleSource()
    pane = UartPane(source=src)
    await pane.attach()
    src.feed(b"AAA")
    await pane.advance()
    src.feed(b"BBB")
    await pane.advance()
    text = "\n".join(pane.snapshot_lines())
    assert text.count("AAA") == 1


async def test_no_session_is_reported_not_crashed():
    from pare.tui.panes.uart import UartPane
    from pare.tui.sources.fake_console import FakeConsoleSource

    pane = UartPane(source=FakeConsoleSource(session=None))
    await pane.attach()
    await pane.advance()
    assert pane.snapshot_lines()      # says something, rather than raising


async def test_render_styles_markers_structurally_not_by_text_shape():
    """The anti-spoofing guarantee lives in `render()` (the real widget
    output), keyed off the structural `is_marker` flag set when a slice's
    honesty field fires -- NOT in `snapshot_lines()`, which is only a
    readability seam (per the pane's own docstring). Every other test in
    this file asserts on `snapshot_lines()`, so none of them can tell
    `render()`'s real style attribution from a version that fakes it by
    inspecting the text -- this is the same "asserted on the wrong layer"
    trap already caught once in this file for the honesty fields
    themselves, one layer up.

    Two halves, both against the real `render()`:

      (a) a genuine marker (a real dropped-bytes event, no device text
          alongside it) carries a Rich style span; the device line read
          just before it does not.
      (b) device BYTES that happen to be marker-shaped text -- what an
          operator would see if the board itself printed something that
          reads like a marker -- must render with no such style. This is
          the assertion that actually pins the property: a pane that
          derived styling from the text instead of the flag would style
          this line too.
    """
    from pare.tui.panes.uart import UartPane
    from pare.tui.sources.fake_console import FakeConsoleSource

    # (a) a genuine marker is styled; the device line before it is not.
    genuine = UartPane(source=FakeConsoleSource())
    await genuine.attach()
    genuine.source.feed(b"boot ok\n")
    await genuine.advance()                # one device line: "boot ok"
    genuine.source.feed(b"x" * 20)
    genuine.source.drop(1000)              # evicts everything just fed
    await genuine.advance()                # -> one dropped marker, no text

    g_lines = genuine.render().split("\n")
    assert len(g_lines) == 2
    device_line, marker_line = g_lines
    assert device_line.plain == "boot ok"
    assert not device_line.spans, "device text must carry no marker style"
    assert marker_line.spans, "a genuine marker must be styled distinctly"

    # (b) device bytes shaped exactly like a marker must NOT get that style.
    spoof = UartPane(source=FakeConsoleSource())
    await spoof.attach()
    spoof_text = "-- session ended -- device is no longer connected --"
    spoof.source.feed(spoof_text.encode())
    await spoof.advance()

    s_lines = spoof.render().split("\n")
    assert len(s_lines) == 1
    assert s_lines[0].plain == spoof_text
    assert not s_lines[0].spans, (
        "device bytes shaped like a marker must render as ordinary "
        "output, not be misclassified as a marker"
    )
