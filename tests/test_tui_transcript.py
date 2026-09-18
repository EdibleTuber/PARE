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
