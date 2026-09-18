"""The fake must be able to produce every condition the real console_read
reports, or Task 9's honesty rendering cannot be tested at all.

`dropped` and `capture_gaps` are checked against the real formulas in
`pare_hardware_mcp/ringbuffer.py:173-182` (dropped/eviction) and
`pare_hardware_mcp/session.py:209-254` (gap dicts + `gaps_overlapping`), not
against whatever the fake happens to emit -- see the by-hand computations in
the docstrings below.
"""
import pytest


async def test_feed_then_read_advances_cursor():
    from pare.tui.sources.fake_console import FakeConsoleSource

    src = FakeConsoleSource()
    await src.attach()
    src.feed(b"hello")
    slice_ = await src.read(cursor=0)
    assert slice_.data == b"hello"
    assert slice_.next_cursor > 0


async def test_dropped_bytes_are_excluded_from_data():
    """feed 10 bytes, evict the first 4 (drop(4) -> oldest_retained=4), then
    read from cursor=2 -- a cursor that fell behind the evicted point.

    By hand, against ringbuffer.py's formula with head=10,
    oldest_retained=4, cursor=2:
        dropped = max(0, oldest_retained - cursor) = max(0, 4 - 2) = 2
        start   = max(cursor, oldest_retained)     = max(2, 4)     = 4
        data    = buf[start:head]                  = buf[4:10]    = b"456789"
        next_cursor = start + len(data)             = 4 + 6        = 10
        remaining   = head - next_cursor            = 10 - 10      = 0
    """
    from pare.tui.sources.fake_console import FakeConsoleSource

    src = FakeConsoleSource()
    await src.attach()
    src.feed(b"0123456789")
    src.drop(4)

    slice_ = await src.read(cursor=2)

    assert slice_.dropped == 2
    assert slice_.data == b"456789"
    assert b"0123" not in slice_.data  # the evicted bytes never come back
    assert slice_.next_cursor == 10
    assert slice_.remaining == 0


async def test_dropped_is_recomputed_not_drained():
    """Re-reading the SAME cursor with no new writes/evictions must report
    the SAME dropped count -- it is a function of (cursor, oldest_retained),
    not a one-shot counter that zeroes after being read."""
    from pare.tui.sources.fake_console import FakeConsoleSource

    src = FakeConsoleSource()
    await src.attach()
    src.feed(b"0123456789")
    src.drop(4)

    first = await src.read(cursor=2)
    second = await src.read(cursor=2)

    assert first.dropped == second.dropped == 2
    assert first.data == second.data


async def test_dropped_is_zero_once_cursor_clears_the_eviction():
    """A cursor at or past oldest_retained has nothing evicted ahead of it,
    so dropped is 0 -- dropped is relative to the reading cursor, not a
    running total of everything ever evicted."""
    from pare.tui.sources.fake_console import FakeConsoleSource

    src = FakeConsoleSource()
    await src.attach()
    src.feed(b"0123456789")
    src.drop(4)

    slice_ = await src.read(cursor=4)

    assert slice_.dropped == 0
    assert slice_.data == b"456789"


async def test_reports_a_capture_gap():
    """feed "before" (6 bytes) + gap at cursor 6 + feed "after" (5 bytes);
    reading the whole 11-byte buffer returns a window [0, 11] that contains
    at_cursor=6, so the gap dict is reported."""
    from pare.tui.sources.fake_console import FakeConsoleSource

    src = FakeConsoleSource()
    await src.attach()
    src.feed(b"before")
    src.gap(at=6, duration_s=1.25, reason="baud-scan")
    src.feed(b"after")

    slice_ = await src.read(cursor=0)

    assert slice_.capture_gaps == [
        {"at_cursor": 6, "duration_s": 1.25, "reason": "baud-scan", "in_progress": False}
    ]


async def test_capture_gap_outside_the_returned_window_is_not_reported():
    """A read that stops before a gap's at_cursor must not report that gap
    -- gaps_overlapping only returns gaps inside [start, next_cursor]."""
    from pare.tui.sources.fake_console import FakeConsoleSource

    src = FakeConsoleSource()
    await src.attach()
    src.feed(b"before")
    src.gap(at=6)
    src.feed(b"after")

    slice_ = await src.read(cursor=0, limit=3)  # window is [0, 3], gap is at 6

    assert slice_.capture_gaps == []


async def test_reports_a_dead_session():
    from pare.tui.sources.fake_console import FakeConsoleSource

    src = FakeConsoleSource()
    await src.attach()
    src.die()
    assert (await src.read(cursor=0)).alive is False


async def test_attach_returns_none_when_no_session():
    from pare.tui.sources.fake_console import FakeConsoleSource

    src = FakeConsoleSource(session=None)
    assert await src.attach() is None


async def test_fail_next_read_raises_once_then_recovers():
    from pare.tui.sources.fake_console import FakeConsoleSource

    src = FakeConsoleSource()
    await src.attach()
    src.feed(b"x")
    src.fail_next_read(RuntimeError("link down"))
    with pytest.raises(RuntimeError):
        await src.read(cursor=0)
    assert (await src.read(cursor=0)).data == b"x"


async def test_fail_next_send_raises_once_then_recovers():
    from pare.tui.sources.fake_console import FakeConsoleSource

    src = FakeConsoleSource()
    session = await src.attach()
    src.fail_next_send(RuntimeError("link down"))
    with pytest.raises(RuntimeError):
        await src.send(session, b"reboot\n")
    await src.send(session, b"reboot\n")
    assert src.sent == [b"reboot\n"]


async def test_read_and_send_failures_are_independent():
    """Scripting a read failure must not be silently consumed by an
    intervening send, and vice versa -- the two slots are independent."""
    from pare.tui.sources.fake_console import FakeConsoleSource

    src = FakeConsoleSource()
    session = await src.attach()
    src.feed(b"x")
    src.fail_next_read(RuntimeError("read broke"))

    await src.send(session, b"ok\n")  # must not consume the read failure

    with pytest.raises(RuntimeError):
        await src.read(cursor=0)
    assert (await src.read(cursor=0)).data == b"x"


async def test_limit_clamps_and_reports_remaining():
    from pare.tui.sources.fake_console import FakeConsoleSource

    src = FakeConsoleSource()
    await src.attach()
    src.feed(b"0123456789")
    slice_ = await src.read(cursor=0, limit=4)
    assert len(slice_.data) == 4
    assert slice_.limit_applied == 4
    assert slice_.remaining > 0


async def test_send_is_recorded():
    from pare.tui.sources.fake_console import FakeConsoleSource

    src = FakeConsoleSource()
    session = await src.attach()
    await src.send(session, b"reboot\n")
    assert src.sent == [b"reboot\n"]
