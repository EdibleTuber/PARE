"""Both turn shapes must render their answer exactly once.

A test covering only one shape leaves the other silently broken: reasoning=off
double-renders, reasoning=on renders nothing. These are the two halves of the
same bug and are tested together deliberately.
"""
from agent_core.protocol import (
    ErrorMessage, ResponseMessage, StreamChunkMessage, ToolProgressMessage,
)


def _render(acc, messages):
    out = []
    for m in messages:
        piece = acc.feed(m)
        if piece is not None:
            out.append(piece)
    return "".join(out)


def test_streamed_turn_renders_answer_once():
    from pare.tui.widgets.transcript import TurnAccumulator

    acc = TurnAccumulator()
    rendered = _render(acc, [
        StreamChunkMessage(token="Hello "),
        StreamChunkMessage(token="world"),
        ResponseMessage(text="Hello world"),
    ])
    assert rendered.count("Hello world") == 1


def test_unstreamed_turn_still_renders_answer():
    from pare.tui.widgets.transcript import TurnAccumulator

    acc = TurnAccumulator()
    rendered = _render(acc, [ResponseMessage(text="Reasoned answer")])
    assert rendered.count("Reasoned answer") == 1


def test_response_diverging_from_stream_is_not_suppressed():
    """A ResponseMessage whose text differs from what was streamed carries
    information the stream did not. Suppressing it loses output."""
    from pare.tui.widgets.transcript import TurnAccumulator

    acc = TurnAccumulator()
    rendered = _render(acc, [
        StreamChunkMessage(token="partial"),
        ResponseMessage(text="partial plus more"),
    ])
    assert "partial plus more" in rendered


def test_turn_end_recognises_both_terminators():
    from pare.tui.widgets.transcript import is_turn_end

    assert is_turn_end(ResponseMessage(text="x"))
    assert is_turn_end(ErrorMessage(error="boom"))
    assert not is_turn_end(StreamChunkMessage(token="x"))
    assert not is_turn_end(ToolProgressMessage(tool="t", arguments={}))


def test_tool_progress_is_rendered_not_dropped():
    """Spec section 4: tool progress renders inline. A TurnAccumulator that
    returns None for it makes the model's work invisible mid-turn."""
    from pare.tui.widgets.transcript import TurnAccumulator

    acc = TurnAccumulator()
    out = acc.feed(ToolProgressMessage(tool="static_strings", arguments={"apk": "x"}))
    assert out is not None and "static_strings" in out


def test_unknown_message_types_render_a_visible_fallback():
    """Dropping an unrecognised type loses output silently. The CLI renders
    "[unrendered ...]" (agent_core/adapters/cli.py:115); so must this."""
    from pare.tui.widgets.transcript import TurnAccumulator

    class Surprise:
        type = "surprise"

    out = TurnAccumulator().feed(Surprise())
    assert out is not None and "Surprise" in out


def test_reset_clears_stream_state_between_turns():
    """Without a reset, turn two's ResponseMessage is compared against turn
    one's stream and can be wrongly suppressed."""
    from pare.tui.widgets.transcript import TurnAccumulator

    acc = TurnAccumulator()
    _render(acc, [StreamChunkMessage(token="same"), ResponseMessage(text="same")])
    acc.reset()
    rendered = _render(acc, [ResponseMessage(text="same")])
    assert rendered.count("same") == 1


# --- TurnRenderer: a streamed reply is one growing block, not one line per
# token (operator session 2026-09-27, the "narrow column" bug). ---


def _feed_all(renderer, messages):
    committed = []
    for m in messages:
        committed.extend(renderer.feed(m))
    return committed


def test_stream_chunks_accumulate_into_one_live_block_and_commit_nothing():
    from pare.tui.widgets.transcript import TurnRenderer

    r = TurnRenderer()
    committed = _feed_all(r, [
        StreamChunkMessage(token="The"),
        StreamChunkMessage(token=" quick"),
        StreamChunkMessage(token=" fox."),
    ])
    assert committed == []
    assert r.live == "The quick fox."


def test_tool_progress_commits_the_streamed_text_before_the_tool_line():
    from pare.tui.widgets.transcript import TurnRenderer

    r = TurnRenderer()
    committed = _feed_all(r, [
        StreamChunkMessage(token="Before"),
        StreamChunkMessage(token=" tool."),
        ToolProgressMessage(tool="static_strings", arguments={"apk": "x"}),
        StreamChunkMessage(token="After"),
        StreamChunkMessage(token=" tool."),
        ResponseMessage(text="Before tool.After tool."),
    ])
    kinds_texts = [(c.kind, c.text) for c in committed]
    assert kinds_texts[0] == ("markdown", "Before tool.")
    assert kinds_texts[1][0] == "plain" and "static_strings" in kinds_texts[1][1]
    assert kinds_texts[2] == ("markdown", "After tool.")
    # The ResponseMessage duplicates the stream: nothing more.
    assert len(committed) == 3
    assert r.live == ""


def test_renderer_unstreamed_response_commits_as_markdown_once():
    from pare.tui.widgets.transcript import TurnRenderer

    r = TurnRenderer()
    committed = _feed_all(r, [ResponseMessage(text="- a\n- b")])
    assert [(c.kind, c.text) for c in committed] == [("markdown", "- a\n- b")]


def test_renderer_diverging_response_commits_both():
    from pare.tui.widgets.transcript import TurnRenderer

    r = TurnRenderer()
    committed = _feed_all(r, [
        StreamChunkMessage(token="partial"),
        ResponseMessage(text="partial plus more"),
    ])
    assert [c.text for c in committed] == ["partial", "partial plus more"]


def test_renderer_error_is_plain_and_flushes_the_stream_first():
    from pare.tui.widgets.transcript import TurnRenderer

    r = TurnRenderer()
    committed = _feed_all(r, [
        StreamChunkMessage(token="half an answer"),
        ErrorMessage(error="boom"),
    ])
    assert [(c.kind, c.text) for c in committed] == [
        ("markdown", "half an answer"), ("plain", "Error: boom"),
    ]
    assert r.live == ""


def test_renderer_resets_between_turns():
    """Turn two's identical ResponseMessage must not be suppressed as a
    duplicate of turn one's stream."""
    from pare.tui.widgets.transcript import TurnRenderer

    r = TurnRenderer()
    _feed_all(r, [StreamChunkMessage(token="same"), ResponseMessage(text="same")])
    committed = _feed_all(r, [ResponseMessage(text="same")])
    assert [c.text for c in committed] == ["same"]


def test_renderer_unknown_type_keeps_the_fallback():
    from pare.tui.widgets.transcript import TurnRenderer

    class Surprise:
        pass

    committed = TurnRenderer().feed(Surprise())
    assert [(c.kind, c.text) for c in committed] == [("plain", "[unrendered Surprise]")]


def test_renderer_flush_commits_the_live_text_without_ending_the_turn():
    """flush() (used when another line must be written mid-stream) moves the
    live text out but keeps the turn's dedup state: the closing
    ResponseMessage is still recognised as a duplicate."""
    from pare.tui.widgets.transcript import TurnRenderer

    r = TurnRenderer()
    _feed_all(r, [StreamChunkMessage(token="one ")])
    assert [c.text for c in r.flush()] == ["one "]
    assert r.flush() == []
    committed = _feed_all(r, [
        StreamChunkMessage(token="two"), ResponseMessage(text="one two"),
    ])
    assert [c.text for c in committed] == ["two"]
