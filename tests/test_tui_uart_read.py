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


async def test_attach_failure_renders_a_connect_failed_marker():
    """The real-world failure behind the TUI crash: `attach()` raising at
    mount (bad endpoint -> HTTP 404 -> SDK's 'Session terminated'). The
    pane must show a MARKER saying the connect failed and that it is
    retrying -- rendered in place, distinct from device text -- instead of
    the exception escaping `on_mount` to the app.

    Asserted on the structural `is_marker` flag (the real distinctness
    guarantee, per the module docstring), not just the snapshot framing.
    """
    from pare.tui.panes.base import PaneDock
    from pare.tui.panes.uart import UartPane
    from textual.app import App, ComposeResult

    class _DeadSource:
        async def attach(self):
            raise RuntimeError("Session terminated")

        async def read(self, cursor, limit=None):
            raise RuntimeError("no attached session")

        async def send(self, session, data):
            raise NotImplementedError

        async def status(self):
            return {}

    pane = UartPane(source=_DeadSource(), poll_interval=0.02, id="uart")

    class _App(App):
        def compose(self) -> ComposeResult:
            yield PaneDock([pane], id="dock")

    app = _App()
    async with app.run_test() as pilot:
        await pilot.pause(0.15)
        lines = [text for text, is_marker in pane._lines if is_marker]
        assert any("connect failed: Session terminated" in line for line in lines), (
            "the pane must render the connect failure in place as a marker, "
            f"got markers: {lines!r}"
        )
        assert pane.healthy is False
        assert pane.last_error == "Session terminated"


def _strip_styles(strip):
    """The Rich styles actually carried by a log line (one per segment).
    `_segments` is the line's stored content -- asserting on it means the
    test sees what the widget really wrote, not a reconstruction. A
    segment written without a style carries None; normalize to the empty
    style so callers can read `.bold` etc. uniformly."""
    from rich.style import Style

    return [segment.style or Style() for segment in strip._segments]


async def test_log_styles_markers_structurally_not_by_text_shape():
    """The anti-spoofing guarantee lives in what the pane WRITES to its
    display (`_append` -> the `#uart-log` child, the real widget output),
    keyed off the structural `is_marker` flag set when a slice's honesty
    field fires -- NOT in `snapshot_lines()`, which is only a readability
    seam (per the pane's own docstring). Every other unmounted test in
    this file asserts on `snapshot_lines()`, so none of them can tell the
    display's real style attribution from a version that fakes it by
    inspecting the text -- this is the same "asserted on the wrong layer"
    trap already caught once in this file for the honesty fields
    themselves, one layer up.

    (Migrated from the old `render() -> Text` seam to the log's stored
    lines when the display became a `RichLog` child; same property, same
    two halves.)

    Two halves, both against the real log contents:

      (a) a genuine marker (a real dropped-bytes event, no device text
          alongside it) is written to the log with a style the device
          line read just before it does not carry.
      (b) device BYTES that happen to be marker-shaped text -- what an
          operator would see if the board itself printed something that
          reads like a marker -- must be written with no such style. This
          is the assertion that actually pins the property: a pane that
          derived styling from the text instead of the flag would style
          this line too.
    """
    from pare.tui.sources.fake_console import FakeConsoleSource
    from textual.widgets import RichLog

    # (a) a genuine marker is styled; the device line before it is not.
    async with _mounted_pane(FakeConsoleSource()) as (app, pane, pilot):
        src = pane.source
        src.feed(b"boot ok\n")
        await pilot.pause(0.1)             # one device line: "boot ok"
        src.feed(b"x" * 20)
        src.drop(1000)                     # evicts everything just fed
        await pilot.pause(0.1)             # -> one dropped marker, no text

        log = pane.query_one("#uart-log", RichLog)
        lines = list(log.lines)
        device_line = next(s for s in lines if s.text == "boot ok")
        marker_line = next(
            s for s in lines if "byte(s) dropped" in s.text
        )
        assert not any(st.bold for st in _strip_styles(device_line)), (
            "device text must carry no marker style"
        )
        assert any(st.bold for st in _strip_styles(marker_line)), (
            "a genuine marker must be styled distinctly"
        )

    # (b) device bytes shaped exactly like a marker must NOT get that style.
    async with _mounted_pane(FakeConsoleSource()) as (app, pane, pilot):
        spoof_text = "-- session ended -- device is no longer connected --"
        pane.source.feed(spoof_text.encode())
        await pilot.pause(0.1)

        log = pane.query_one("#uart-log", RichLog)
        strip = next(s for s in log.lines if s.text == spoof_text)
        assert not any(st.bold for st in _strip_styles(strip)), (
            "device bytes shaped like a marker must render as ordinary "
            "output, not be misclassified as a marker"
        )


# --- the pane's live display and input ----------------------------------
#
# The operator-facing symptoms behind this section: device output that
# arrived for a whole session never became visible (the widget repainted
# only on unrelated invalidation, and even then only its OLDEST lines,
# since its renderable was the entire ever-growing list), and there was no
# way to type at the console from the pane at all (`submit` existed but
# nothing in the TUI ever called it).
#
# The tests above drive the pane with bare `attach()`/`advance()` -- never
# mounted -- so they cannot exercise the log/input widgets at all. These
# mount the pane in a bare app the way the production app lays it out.


def _mounted_pane(source, *, size=(60, 10), poll_interval=0.02):
    """Mount a `UartPane` on `source` in a bare Textual app and yield
    (app, pane, pilot).

    `#uart { height: 1fr; }` gives the pane the dock-filling layout the
    production app uses, so its children get a real, non-zero size.
    """
    from contextlib import asynccontextmanager

    from pare.tui.panes.base import PaneDock
    from pare.tui.panes.uart import UartPane
    from textual.app import App, ComposeResult

    pane = UartPane(source=source, poll_interval=poll_interval, id="uart")

    class _App(App):
        CSS = "#uart { height: 1fr; }"

        def compose(self) -> ComposeResult:
            yield PaneDock([pane], id="dock")

    @asynccontextmanager
    async def _ctx():
        app = _App()
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            yield app, pane, pilot

    return _ctx()


async def test_new_output_is_written_to_the_pane_log_as_it_arrives():
    """Device output must reach the pane's visible display as it arrives:
    the display is a log child that each slice writes into, not a
    renderable that only ever repaints on unrelated invalidation."""
    from pare.tui.sources.fake_console import FakeConsoleSource
    from textual.widgets import RichLog

    async with _mounted_pane(FakeConsoleSource()) as (app, pane, pilot):
        pane.source.feed(b"boot ok\n")
        await pilot.pause(0.1)
        log = pane.query_one("#uart-log", RichLog)
        assert "boot ok" in "\n".join(strip.text for strip in log.lines)


async def test_pane_log_follows_the_tail_when_output_overflows():
    """A pane shorter than its transcript must stay pinned to the newest
    line: a renderable of the whole list can only ever show the OLDEST
    lines, which is what made the live console look dead."""
    from pare.tui.sources.fake_console import FakeConsoleSource
    from textual.widgets import RichLog

    async with _mounted_pane(FakeConsoleSource(), size=(40, 8)) as (
        app, pane, pilot):
        pane.source.feed(b"".join(f"line {i}\n".encode() for i in range(30)))
        await pilot.pause(0.1)
        log = pane.query_one("#uart-log", RichLog)
        assert log.lines[-1].text == "line 29"
        assert log.is_vertical_scroll_end, "the log must sit at the newest line"


async def test_following_pauses_when_the_operator_scrolls_up():
    """Following must yield to the operator: while they are scrolled up
    reading old output, new lines must not yank the view back to the tail;
    once they are back at the tail, following resumes."""
    from pare.tui.sources.fake_console import FakeConsoleSource
    from textual.widgets import RichLog

    async with _mounted_pane(FakeConsoleSource(), size=(40, 8)) as (
        app, pane, pilot):
        src = pane.source
        src.feed(b"".join(f"line {i}\n".encode() for i in range(30)))
        await pilot.pause(0.1)
        log = pane.query_one("#uart-log", RichLog)
        assert log.is_vertical_scroll_end

        log.scroll_home(animate=False, immediate=True)
        await pilot.pause()
        src.feed(b"line 30\n")
        await pilot.pause(0.1)
        assert log.lines[-1].text == "line 30"  # the line still arrives
        assert not log.is_vertical_scroll_end, (
            "new output must not yank a scrolled-up view back to the tail"
        )

        log.scroll_end(animate=False, immediate=True)
        await pilot.pause()
        src.feed(b"line 31\n")
        await pilot.pause(0.1)
        assert log.is_vertical_scroll_end, (
            "following must resume once the operator is back at the tail"
        )


async def test_enter_in_the_pane_input_sends_the_line_to_the_target():
    """The operator asked where the input field was: the pane has one, and
    Enter on it sends the line through the pane's write path to the
    source, then clears the field."""
    from pare.tui.sources.fake_console import FakeConsoleSource
    from textual.widgets import Input

    async with _mounted_pane(FakeConsoleSource()) as (app, pane, pilot):
        pane.query_one("#uart-input", Input).focus()
        await pilot.pause()
        for ch in "boot":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert pane.source.sent == [b"boot\n"]
        assert pane.query_one("#uart-input", Input).value == ""


async def test_focusing_the_pane_moves_the_caret_into_its_input():
    """The dock's focus cycle calls `pane.focus()` on the pane container --
    for the input to be usable from that cycle, focusing the pane must land
    the caret in the pane's input, not on the container itself."""
    from pare.tui.sources.fake_console import FakeConsoleSource
    from textual.widgets import Input

    async with _mounted_pane(FakeConsoleSource()) as (app, pane, pilot):
        pane.focus()
        await pilot.pause()
        assert app.focused is pane.query_one("#uart-input", Input)


async def test_pane_input_is_disabled_while_there_is_no_live_session():
    """With no live session there is nowhere to send: the input says so
    (disabled, with a hint) instead of accepting lines that can only fail,
    and the no-session marker is visible in the log."""
    from pare.tui.sources.fake_console import FakeConsoleSource
    from textual.widgets import Input, RichLog

    async with _mounted_pane(FakeConsoleSource(session=None)) as (
        app, pane, pilot):
        assert pane.query_one("#uart-input", Input).disabled
        log = pane.query_one("#uart-log", RichLog)
        assert any("no live console session" in s.text for s in log.lines)


async def test_pane_input_is_disabled_when_the_session_dies():
    """A session that dies mid-stream (alive=False on a read) leaves
    nowhere to send either: the input disables with it."""
    from pare.tui.sources.fake_console import FakeConsoleSource
    from textual.widgets import Input

    async with _mounted_pane(FakeConsoleSource()) as (app, pane, pilot):
        src = pane.source
        src.feed(b"x")
        src.die()
        await pilot.pause(0.1)
        assert pane.query_one("#uart-input", Input).disabled
