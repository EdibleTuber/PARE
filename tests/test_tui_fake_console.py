"""The fake must be able to produce every condition the real console_read
reports, or Task 9's honesty rendering cannot be tested at all."""
import pytest


async def test_feed_then_read_advances_cursor():
    from pare.tui.sources.fake_console import FakeConsoleSource

    src = FakeConsoleSource()
    await src.attach()
    src.feed(b"hello")
    slice_ = await src.read(cursor=0)
    assert slice_.data == b"hello"
    assert slice_.next_cursor > 0


async def test_reports_dropped_bytes():
    from pare.tui.sources.fake_console import FakeConsoleSource

    src = FakeConsoleSource()
    await src.attach()
    src.feed(b"abc")
    src.drop(7)
    assert (await src.read(cursor=0)).dropped == 7


async def test_reports_a_capture_gap():
    from pare.tui.sources.fake_console import FakeConsoleSource

    src = FakeConsoleSource()
    await src.attach()
    src.feed(b"before")
    src.gap(at=6)
    src.feed(b"after")
    assert (await src.read(cursor=0)).capture_gaps


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


async def test_fail_next_raises_once_then_recovers():
    from pare.tui.sources.fake_console import FakeConsoleSource

    src = FakeConsoleSource()
    await src.attach()
    src.feed(b"x")
    src.fail_next(RuntimeError("link down"))
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
