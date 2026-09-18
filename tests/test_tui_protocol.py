"""PaneActivityMessage must round-trip through agent_core's registry, or the
daemon decodes it as an unknown type and the log silently goes nowhere."""
from agent_core.protocol import decode_message, encode_message


def test_pane_activity_round_trips():
    from pare.protocol import PaneActivityMessage

    msg = PaneActivityMessage(
        source="hardware", session="s1", kind="sent", data_b64="aGk=",
        cursor_start=10, cursor_end=12, channel_id="tui-1", cwd="/tmp",
    )
    back = decode_message(encode_message(msg).rstrip(b"\n"))
    assert back == msg


def test_registered_under_its_type_string():
    from pare.protocol import PaneActivityMessage

    wire = encode_message(
        PaneActivityMessage(source="hardware", session="s1", kind="observed",
                            data_b64="", cursor_start=0, cursor_end=0)
    )
    assert b'"type": "pane_activity"' in wire
    assert isinstance(decode_message(wire.rstrip(b"\n")), PaneActivityMessage)
