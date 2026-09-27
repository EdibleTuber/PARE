"""Fixes from the operator's first hands-on pare-tui session (2026-09-27):

1. the operator's own input is echoed into the transcript;
2. a streamed reply renders as one Markdown block, not one line per token;
3. a key binding cycles the chat / pane-dock split;
4. the chosen theme persists across launches.

Every app here is built WITHOUT auto_spawn (nothing is spawned) and with a
tmp `tui_config_path` (nothing touches the real ~/.config).
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import pytest
from agent_core.protocol import (
    ChatMessage,
    CommandMessage,
    ErrorMessage,
    ResponseMessage,
    StreamChunkMessage,
    ToolProgressMessage,
)
from textual.widgets import Input, RichLog


class _Session:
    """DaemonSession double: subscribe/start/stop/send plus the two
    parse_input entry points. `reply` (if set) is delivered to subscribers
    synchronously inside send_chat, i.e. as early as a reply can possibly
    arrive."""

    def __init__(self, *, fail_send: Exception | None = None) -> None:
        self.sent: list[object] = []
        self.subscribers: list = []
        self.fail_send = fail_send
        self.reply: list[object] = []

    def subscribe(self, handler) -> None:
        self.subscribers.append(handler)

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def send(self, msg) -> None:
        self.sent.append(msg)

    async def send_chat(self, text: str) -> None:
        if self.fail_send is not None:
            raise self.fail_send
        self.sent.append(ChatMessage(text=text))
        self.deliver(*self.reply)

    async def send_command(self, name: str, args: str) -> None:
        if self.fail_send is not None:
            raise self.fail_send
        self.sent.append(CommandMessage(name=name, args=args))

    def deliver(self, *msgs) -> None:
        for m in msgs:
            for handler in self.subscribers:
                handler(m)


def _make_app(tmp_path: Path, session: _Session | None = None, **kwargs):
    from pare.tui.app import PareTUI

    kwargs.setdefault("tui_config_path", tmp_path / "config" / "pare" / "tui.json")
    app = PareTUI(Path("/nonexistent/pare.sock"), "chan-1", "/tmp", **kwargs)
    app.session = session if session is not None else _Session()
    return app


def _lines(app) -> list[str]:
    """The committed transcript as the operator sees it, text only."""
    log = app.query_one("#transcript", RichLog)
    return [strip.text.rstrip() for strip in log.lines]


def _index(lines: list[str], needle: str) -> int:
    hits = [i for i, line in enumerate(lines) if needle in line]
    assert len(hits) == 1, f"{needle!r} found {len(hits)} times in {lines!r}"
    return hits[0]


async def _submit(pilot, app, text: str) -> None:
    box = app.query_one("#chat-input", Input)
    box.focus()
    box.value = text
    await pilot.press("enter")
    await pilot.pause()


# --- 1. echo ---------------------------------------------------------------


async def test_submitted_chat_is_echoed_before_the_reply(tmp_path):
    session = _Session()
    session.reply = [ResponseMessage(text="pong")]
    app = _make_app(tmp_path, session)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await _submit(pilot, app, "ping")
        lines = _lines(app)
        assert _index(lines, "you> ping") < _index(lines, "pong")
    assert session.sent == [ChatMessage(text="ping")]


async def test_slash_commands_are_echoed_too(tmp_path):
    session = _Session()
    app = _make_app(tmp_path, session)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await _submit(pilot, app, "/worker list")
        _index(_lines(app), "you> /worker list")
    assert session.sent == [CommandMessage(name="worker", args="list")]


@pytest.mark.parametrize("blank", ["", "   ", "\t "])
async def test_blank_input_is_not_echoed(tmp_path, blank):
    session = _Session()
    app = _make_app(tmp_path, session)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await _submit(pilot, app, blank)
        assert not any("you>" in line for line in _lines(app))
    assert session.sent == []


async def test_echo_survives_a_failed_send_and_precedes_the_error(tmp_path):
    session = _Session(fail_send=RuntimeError("connect() before send()"))
    app = _make_app(tmp_path, session)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await _submit(pilot, app, "hello?")
        lines = _lines(app)
        assert _index(lines, "you> hello?") < _index(lines, "[send failed: connect() before send()]")


async def test_echo_is_styled_distinctly_from_replies(tmp_path):
    """The prefix carries a style a daemon reply's plain text does not."""
    app = _make_app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await _submit(pilot, app, "styled?")
        log = app.query_one("#transcript", RichLog)
        strip = log.lines[_index(_lines(app), "you> styled?")]
        prefix_styles = [seg.style for seg in strip if seg.text.startswith("you>")]
        assert prefix_styles and prefix_styles[0] is not None and prefix_styles[0].bold


# --- 2. streamed replies ---------------------------------------------------


async def test_streamed_reply_commits_as_whole_lines_around_a_tool_line(tmp_path):
    """The key sequence: StreamChunks -> ToolProgress -> StreamChunks ->
    ResponseMessage (a duplicate of the stream), through the real app."""
    session = _Session()
    app = _make_app(tmp_path, session)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        session.deliver(*[StreamChunkMessage(token=t) for t in
                          ["The", " quick", " brown", " fox", " jumps."]])
        await pilot.pause()
        # Still in progress: nothing committed yet, the live block holds it.
        assert not any("quick" in line for line in _lines(app))
        assert app.query_one("#live-scroll").display is True
        assert app.query_one("#live-reply").source == "The quick brown fox jumps."

        session.deliver(ToolProgressMessage(tool="static_strings", arguments={"apk": "x.apk"}))
        session.deliver(*[StreamChunkMessage(token=t) for t in ["Then", " it", " sleeps."]])
        session.deliver(ResponseMessage(text="The quick brown fox jumps.Then it sleeps."))
        await pilot.pause()

        lines = _lines(app)
        first = _index(lines, "The quick brown fox jumps.")
        tool = _index(lines, "static_strings")
        second = _index(lines, "Then it sleeps.")
        assert first < tool < second
        # No line holds a lone token (the narrow-column symptom).
        for token in ["The", "quick", "brown", "fox", "Then", "it"]:
            assert token not in [line.strip() for line in lines]
        assert app.query_one("#live-scroll").display is False


async def test_unstreamed_reply_renders_markdown(tmp_path):
    session = _Session()
    app = _make_app(tmp_path, session)
    text = "Two items:\n\n- alpha\n- beta\n\n```python\nprint('hi')\n```\n"
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        session.deliver(ResponseMessage(text=text))
        await pilot.pause()
        lines = _lines(app)
        assert any("•" in line and "alpha" in line for line in lines)
        _index(lines, "print('hi')")
        assert not any("```" in line for line in lines)
        assert not any(line.lstrip().startswith("- alpha") for line in lines)


async def test_markdown_reply_spans_the_full_transcript_width(tmp_path):
    session = _Session()
    app = _make_app(tmp_path, session)
    words = " ".join(f"word{i}" for i in range(60))
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        session.deliver(ResponseMessage(text=words))
        await pilot.pause()
        log = app.query_one("#transcript", RichLog)
        widest = max(len(line) for line in _lines(app) if "word" in line)
        # Wrapped to the log's width, not a narrow column.
        assert widest > log.scrollable_content_region.width - 12


async def test_error_still_renders_as_error_line(tmp_path):
    session = _Session()
    app = _make_app(tmp_path, session)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        session.deliver(StreamChunkMessage(token="partial answer"))
        session.deliver(ErrorMessage(error="model fell over"))
        await pilot.pause()
        lines = _lines(app)
        assert _index(lines, "partial answer") < _index(lines, "Error: model fell over")
        assert app.query_one("#live-scroll").display is False


async def test_a_status_line_mid_stream_does_not_jump_ahead_of_streamed_text(tmp_path):
    """Any transcript write flushes the live reply first, so the transcript
    stays in arrival order."""
    session = _Session()
    app = _make_app(tmp_path, session)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        session.deliver(StreamChunkMessage(token="streamed first"))
        app._write_transcript("[status second]")
        session.deliver(StreamChunkMessage(token=" and third"))
        session.deliver(ResponseMessage(text="streamed first and third"))
        await pilot.pause()
        lines = _lines(app)
        assert (_index(lines, "streamed first") < _index(lines, "[status second]")
                < _index(lines, "and third"))


# --- 3. layout cycling -----------------------------------------------------


def _widths(app) -> tuple[int, int | None]:
    chat = app.query_one("#chat-area").region.width
    dock = app.query_one("#pane-dock")
    return chat, (dock.region.width if dock.display else None)


async def test_layout_key_is_in_the_footer_bindings(tmp_path):
    from pare.tui.app import LAYOUT_KEY

    app = _make_app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        active = app.active_bindings
        assert LAYOUT_KEY in active
        binding = active[LAYOUT_KEY].binding
        assert binding.description == "Layout" and binding.show
        # Not stolen from Textual: not the palette, not quit.
        assert LAYOUT_KEY not in {"ctrl+p", "ctrl+q", "ctrl+c"}


async def test_layout_key_cycles_the_split_while_typing(tmp_path):
    from pare.tui.app import LAYOUT_KEY

    app = _make_app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.query_one("#chat-input", Input).focus()
        seen = [_widths(app)]
        for _ in range(4):
            await pilot.press(LAYOUT_KEY)
            await pilot.pause()
            seen.append(_widths(app))
        (c0, d0), (c1, d1), (c2, d2), (c3, d3), (c4, d4) = seen
        assert c0 == 2 * d0          # 2:1
        assert c1 == d1              # 1:1
        assert 2 * c2 == d2          # 1:2
        assert d3 is None and c3 == 120   # dock hidden, chat takes it all
        assert (c4, d4) == (c0, d0)  # back to 2:1
        # Typing into the box never lost the key to the input.
        assert app.query_one("#chat-input", Input).value == ""


async def test_hidden_dock_keeps_polling_and_the_status_summary(tmp_path, monkeypatch):
    from pare.tui.app import LAYOUT_KEY

    monkeypatch.delenv("PARE_TUI_HARDWARE_ENDPOINT", raising=False)
    app = _make_app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        pane = app.query_one("#uart-pane")
        bar = app.query_one("#status-bar")
        for _ in range(3):
            await pilot.press(LAYOUT_KEY)
        await pilot.pause()
        assert app.query_one("#pane-dock").display is False
        before = pane.cursor
        pane.source.feed(b"U-Boot 2024.01\n")
        await pilot.pause(0.3)
        assert pane.cursor > before, "hidden pane stopped polling"
        app._refresh_status_bar()  # what the app's 1s timer calls
        shown = str(bar.content)
        assert f"UartPane[ok cursor={pane.cursor}]" in shown, shown


async def test_changing_the_layout_rewraps_the_existing_transcript(tmp_path):
    """RichLog wraps at write time. Without a re-wrap, narrowing the chat
    leaves every earlier line wider than the log (clipped behind a
    horizontal scrollbar), and widening it leaves them in a narrow column."""
    from pare.tui.app import LAYOUT_KEY

    session = _Session()
    app = _make_app(tmp_path, session)
    words = " ".join(f"word{i}" for i in range(80))
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        session.deliver(ResponseMessage(text=words))
        await pilot.pause()
        log = app.query_one("#transcript", RichLog)
        settle = log.REWRAP_DEBOUNCE + 0.2       # the re-wrap is debounced
        for _ in range(2):                      # -> 1:2, the narrowest chat
            await pilot.press(LAYOUT_KEY)
        await pilot.pause(settle)
        narrow = log.scrollable_content_region.width
        assert max(len(l) for l in _lines(app)) <= narrow
        await pilot.press(LAYOUT_KEY)           # -> dock hidden, widest
        await pilot.pause(settle)
        wide = log.scrollable_content_region.width
        assert wide > narrow
        assert max(len(l) for l in _lines(app)) > narrow
        # Same content, once: word79 appears exactly once after two re-wraps.
        _index(_lines(app), "word79")


async def test_filling_the_transcript_does_not_make_earlier_lines_overflow(tmp_path):
    """Markdown lines are padded to the log's width. If the vertical
    scrollbar only appeared once the log filled, the content width would
    shrink under them and a horizontal scrollbar would appear."""
    session = _Session()
    app = _make_app(tmp_path, session)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        session.deliver(ResponseMessage(text="first reply"))
        await pilot.pause()
        for i in range(60):
            session.deliver(ResponseMessage(text=f"filler {i}"))
        await pilot.pause()
        log = app.query_one("#transcript", RichLog)
        assert log.max_scroll_y > 0  # it really overflowed vertically
        assert max(s.cell_length for s in log.lines) <= log.scrollable_content_region.width
        assert log.max_scroll_x == 0


# --- 4. theme persistence --------------------------------------------------


async def test_saved_theme_is_loaded_at_startup(tmp_path):
    cfg = tmp_path / "tui.json"
    cfg.write_text(json.dumps({"theme": "nord"}))
    app = _make_app(tmp_path, tui_config_path=cfg)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert app.theme == "nord"


async def test_changing_the_theme_saves_it_and_a_new_launch_loads_it(tmp_path):
    cfg = tmp_path / "nested" / "pare" / "tui.json"
    app = _make_app(tmp_path, tui_config_path=cfg)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        # What the palette's theme picker does (textual/theme.py ThemeProvider).
        app.theme = "gruvbox"
        await pilot.pause()
    assert json.loads(cfg.read_text())["theme"] == "gruvbox"

    again = _make_app(tmp_path, tui_config_path=cfg)
    async with again.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert again.theme == "gruvbox"


async def test_saving_keeps_other_keys_in_the_file(tmp_path):
    cfg = tmp_path / "tui.json"
    cfg.write_text(json.dumps({"theme": "nord", "future_key": 7}))
    app = _make_app(tmp_path, tui_config_path=cfg)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.theme = "dracula"
        await pilot.pause()
    assert json.loads(cfg.read_text()) == {"theme": "dracula", "future_key": 7}


async def test_loading_does_not_rewrite_the_file(tmp_path):
    cfg = tmp_path / "tui.json"
    original = '{ "theme":   "nord" }\n'
    cfg.write_text(original)
    app = _make_app(tmp_path, tui_config_path=cfg)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
    assert cfg.read_text() == original


@pytest.mark.parametrize(
    "content",
    [
        json.dumps({"theme": "no-such-theme-anywhere"}),
        "{not json",
        json.dumps(["nord"]),
        json.dumps({"theme": 42}),
        b"\xff\xfe\x00garbage",
    ],
    ids=["unknown-theme", "corrupt-json", "not-an-object", "theme-not-a-string", "not-utf8"],
)
async def test_bad_config_falls_back_to_default_with_a_warning(tmp_path, caplog, content):
    from pare.tui.app import PareTUI

    cfg = tmp_path / "tui.json"
    if isinstance(content, bytes):
        cfg.write_bytes(content)
    else:
        cfg.write_text(content)
    default = PareTUI(Path("/x.sock"), "c", "/tmp", tui_config_path=tmp_path / "absent.json").theme
    app = _make_app(tmp_path, tui_config_path=cfg)
    with caplog.at_level(logging.WARNING):
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert app.theme == default
    assert any(r.levelno == logging.WARNING and str(cfg) in r.getMessage()
               for r in caplog.records)


async def test_unreadable_config_falls_back_with_a_warning(tmp_path, caplog):
    cfg = tmp_path / "tui.json"
    cfg.mkdir()  # a directory where the file should be: open() raises
    app = _make_app(tmp_path, tui_config_path=cfg)
    with caplog.at_level(logging.WARNING):
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert app.theme == "textual-dark"
            app.theme = "nord"  # the save fails too, and must not crash
            await pilot.pause()
            assert app.is_running
    assert sum(r.levelno == logging.WARNING for r in caplog.records) >= 2


async def test_missing_config_is_silent(tmp_path, caplog):
    app = _make_app(tmp_path, tui_config_path=tmp_path / "absent" / "tui.json")
    with caplog.at_level(logging.WARNING, logger="pare"):
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
    assert not [r for r in caplog.records if r.name.startswith("pare")]
    assert not (tmp_path / "absent").exists()


def test_default_config_path_follows_xdg(tmp_path):
    from pare.tui.prefs import default_tui_config_path

    assert default_tui_config_path({"XDG_CONFIG_HOME": str(tmp_path)}) == (
        tmp_path / "pare" / "tui.json"
    )
    home_default = Path.home() / ".config" / "pare" / "tui.json"
    assert default_tui_config_path({}) == home_default
    # The XDG spec: an empty or relative value is ignored.
    assert default_tui_config_path({"XDG_CONFIG_HOME": ""}) == home_default
    assert default_tui_config_path({"XDG_CONFIG_HOME": "rel/dir"}) == home_default


def test_app_default_config_path_comes_from_the_environment(tmp_path, monkeypatch):
    from pare.tui.app import PareTUI

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    app = PareTUI(Path("/x.sock"), "c", "/tmp")
    assert app.tui_config_path == tmp_path / "pare" / "tui.json"


async def test_status_bar_has_its_own_row_above_the_footer(tmp_path):
    """StatusBar and Footer both used to `dock: bottom`, so they shared the
    last row and the Footer painted over the status bar -- the operator never
    saw daemon:up/DOWN/spawn-failed or the pane summary."""
    from pathlib import Path

    from textual.widgets import Footer

    from pare.tui.app import PareTUI

    app = PareTUI(Path("/nonexistent/pare.sock"), "chan-1", "/tmp",
                  tui_config_path=tmp_path / "tui.json")

    class _NoDaemon:
        def subscribe(self, handler): pass
        async def start(self): raise ConnectionRefusedError()
        async def stop(self): pass

    app.session = _NoDaemon()
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        bar = app.query_one("#status-bar").region
        footer = app.query_one(Footer).region
        assert bar.height == 1
        assert not bar.overlaps(footer), (bar, footer)
        assert bar.y == footer.y - 1


# --- Fix round 1 -----------------------------------------------------------


def _painted(live) -> str:
    """What the live Markdown widget would paint: its mounted blocks. Not
    `source`, which update() sets the moment it is called."""
    from textual.widgets._markdown import MarkdownBlock

    return " ".join(str(b._content) for b in live.query(MarkdownBlock))


async def test_a_terminal_drag_replays_the_transcript_once(tmp_path):
    """S1(a): every width change used to replay the whole history on the
    event loop; a 10-step drag replayed 10 times. Debounced: once, at the
    final width. The debounce is widened here so a slow runner's gaps
    between steps cannot split the drag in two (the mechanism is what is
    under test, not the constant)."""
    from pare.tui.widgets.transcript import TranscriptLog

    session = _Session()
    app = _make_app(tmp_path, session)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        log = app.query_one("#transcript", TranscriptLog)
        log.REWRAP_DEBOUNCE = 1.0
        session.deliver(ResponseMessage(text=" ".join(f"word{i}" for i in range(80))))
        await pilot.pause()
        replays: list[int] = []
        original = log._rewrap
        log._rewrap = lambda: (replays.append(1), original())[1]
        for step in range(10):
            await pilot.resize_terminal(140 - 4 * (step + 1), 40)
        # A write inside the debounce window is recorded and survives the replay.
        session.deliver(ResponseMessage(text="written mid-drag"))
        await pilot.pause(1.5)
        assert len(replays) == 1
        lines = _lines(app)
        assert max(len(l) for l in lines) <= log.scrollable_content_region.width
        _index(lines, "written mid-drag")
        _index(lines, "word79")


async def test_transcript_history_is_bounded_and_drops_the_oldest(tmp_path):
    """S1(b): history and the rendered lines share one cap."""
    from pare.tui.widgets.transcript import TranscriptLog

    session = _Session()
    app = _make_app(tmp_path, session)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        log = app.query_one("#transcript", TranscriptLog)
        cap = log.HISTORY_LIMIT
        assert log.max_lines == cap
        for i in range(cap + 50):
            session.deliver(ResponseMessage(text=f"entry-{i}"))
        await pilot.pause()
        assert len(log._history) == cap
        assert len(log.lines) <= cap
        await pilot.resize_terminal(100, 40)
        await pilot.pause(log.REWRAP_DEBOUNCE + 0.3)
        lines = _lines(app)
        assert len(lines) <= cap
        stripped = [l.strip() for l in lines]
        assert "entry-0" not in stripped and "entry-49" not in stripped
        assert stripped.count("entry-50") == 1
        assert stripped.count(f"entry-{cap + 49}") == 1


async def test_disconnect_mid_stream_commits_the_partial_reply(tmp_path):
    """N2: a dropped connection used to leave the partial answer stranded
    in the live block, and its tokens in the dedup state."""
    from pare.tui.session import DaemonDisconnected

    session = _Session()
    app = _make_app(tmp_path, session)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        session.deliver(StreamChunkMessage(token="half an"), StreamChunkMessage(token=" answer"))
        session.deliver(DaemonDisconnected(reason="EOF"))
        await pilot.pause()
        _index(_lines(app), "half an answer")
        assert not app.query_one("#live-scroll").has_class("-streaming")
        # The next turn starts clean: an identical, unstreamed response is
        # not suppressed as a duplicate of the dead turn's stream.
        session.deliver(ResponseMessage(text="half an answer"))
        await pilot.pause()
        assert sum("half an answer" in l for l in _lines(app)) == 2


async def test_a_new_stream_never_shows_the_previous_reply(tmp_path):
    """N1: the show class used to go on before the live widget's update()
    ran, so a new stream's first frame showed the last reply's text."""
    from textual.widgets import Markdown

    session = _Session()
    app = _make_app(tmp_path, session)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        scroll = app.query_one("#live-scroll")
        live = app.query_one("#live-reply", Markdown)
        session.deliver(StreamChunkMessage(token="OLD REPLY TEXT"))
        await pilot.pause()
        assert live.source == "OLD REPLY TEXT"          # rendered and shown
        session.deliver(ResponseMessage(text="OLD REPLY TEXT"))
        session.deliver(StreamChunkMessage(token="new"))
        # Check every state the screen can paint until the new text lands.
        for _ in range(20):
            if scroll.has_class("-streaming"):
                assert "OLD" not in live.source, live.source
                assert "OLD" not in _painted(live), _painted(live)
            await asyncio.sleep(0)
        await pilot.pause()
        assert scroll.has_class("-streaming") and live.source == "new"


async def test_a_render_in_flight_at_commit_does_not_reveal_the_old_reply(tmp_path):
    """N1, the in-flight variant: the reply is committed while the live
    widget is still rendering it. When that render lands it must not show
    the block, because its text is already in the transcript."""
    from textual.widgets import Markdown

    session = _Session()
    app = _make_app(tmp_path, session)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        scroll = app.query_one("#live-scroll")
        live = app.query_one("#live-reply", Markdown)
        session.deliver(StreamChunkMessage(token="OLD REPLY TEXT"))
        await asyncio.sleep(0)            # the render task starts its update()
        assert app._live_render_task is not None and not app._live_render_task.done()
        session.deliver(ResponseMessage(text="OLD REPLY TEXT"))
        session.deliver(StreamChunkMessage(token="new"))
        for _ in range(200):
            if scroll.has_class("-streaming"):
                assert "OLD" not in _painted(live), _painted(live)
                if "new" in _painted(live):
                    break
            await asyncio.sleep(0.001)
        await pilot.pause()
        assert scroll.has_class("-streaming") and live.source == "new"


async def test_history_keeps_only_entries_still_on_screen(tmp_path):
    """S1(b) with long replies: an entry cap alone still replays up to
    HISTORY_LIMIT multi-line entries (measured: 4.6 s for 1,500 long
    replies). Entries whose lines have all scrolled out of the log are
    dropped, so a replay renders about one log's worth of lines."""
    from pare.tui.widgets.transcript import TranscriptLog

    session = _Session()
    app = _make_app(tmp_path, session)
    ten_lines = "\n\n".join(f"para {k}" for k in range(5))   # 5 paras + 4 gaps
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        log = app.query_one("#transcript", TranscriptLog)
        cap = log.HISTORY_LIMIT
        start = len(log.lines)
        session.deliver(ResponseMessage(text=f"reply first\n\n{ten_lines}"))
        per_entry = len(log.lines) - start
        assert per_entry >= 9
        for i in range(cap // 3):
            session.deliver(ResponseMessage(text=f"reply {i}\n\n{ten_lines}"))
        await pilot.pause()
        assert len(log.lines) == cap
        assert len(log._history) <= cap // per_entry + 1
        # And a replay shows the same tail it had before.
        before = _lines(app)[-50:]
        await pilot.resize_terminal(118, 40)
        await pilot.pause(log.REWRAP_DEBOUNCE + 0.3)
        assert _lines(app)[-50:] == before
