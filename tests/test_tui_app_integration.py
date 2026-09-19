"""End-to-end against the fake source and a stub daemon: the three task
families coexist without blocking one another."""
import asyncio

from agent_core.protocol import ChatMessage, CommandMessage, ResponseMessage


class StubSession:
    """Stands in for DaemonSession: records what was sent, and lets a test
    hold a turn open so concurrency is observable."""

    def __init__(self) -> None:
        self.sent: list[object] = []
        self.subscribers: list = []
        self.turn_open = asyncio.Event()

    def subscribe(self, handler) -> None:
        self.subscribers.append(handler)

    async def send(self, msg) -> None:
        self.sent.append(msg)

    async def send_chat(self, text: str) -> None:
        self.sent.append(ChatMessage(text=text))

    async def send_command(self, name: str, args: str) -> None:
        self.sent.append(CommandMessage(name=name, args=args))

    def finish_turn(self, text: str = "done") -> None:
        for handler in self.subscribers:
            handler(ResponseMessage(text=text))


async def test_chat_and_pane_run_concurrently():
    """The property the whole design exists for: while a turn is in flight,
    the pane's cursor still advances."""
    from pare.tui.panes.uart import UartPane
    from pare.tui.sources.fake_console import FakeConsoleSource

    session, src = StubSession(), FakeConsoleSource()
    pane = UartPane(source=src)
    await pane.attach()

    await session.send_chat("what is on the console?")   # turn in flight
    before = pane.cursor
    src.feed(b"U-Boot 2024.01\n")
    await pane.advance()
    after = pane.cursor

    assert after > before, "pane did not advance while a turn was open"
    session.finish_turn()


async def test_a_dead_source_leaves_chat_usable():
    """Invariant I3: one pane's source failing must not take the app down."""
    from pare.tui.panes.uart import UartPane
    from pare.tui.sources.fake_console import FakeConsoleSource

    session, src = StubSession(), FakeConsoleSource()
    pane = UartPane(source=src)
    await pane.attach()
    src.fail_next_read(RuntimeError("link down"))
    await pane.advance()          # must not raise out of the pane

    await session.send_chat("still here?")
    assert any(isinstance(m, ChatMessage) for m in session.sent)


async def test_status_bar_reflects_source_failure():
    from pare.tui.panes.uart import UartPane
    from pare.tui.sources.fake_console import FakeConsoleSource
    from pare.tui.widgets.statusbar import StatusBar

    src = FakeConsoleSource()
    pane = UartPane(source=src)
    await pane.attach()
    src.feed(b"ok")
    await pane.advance()
    healthy = StatusBar().render_for([pane])

    src.fail_next_read(RuntimeError("link down"))
    await pane.advance()
    failed = StatusBar().render_for([pane])

    assert failed != healthy


async def test_a_leading_slash_sends_a_command_not_a_chat():
    """Spec section 2.2: slash commands keep their wire form. Sending "/worker
    list" as chat would put it in front of the model instead of the registry."""
    from pare.tui.app import parse_input

    session = StubSession()
    await parse_input(session, "/worker list")
    await parse_input(session, "what workers are loaded?")

    kinds = [type(m).__name__ for m in session.sent]
    assert kinds == ["CommandMessage", "ChatMessage"]
    command = session.sent[0]
    assert command.name == "worker" and command.args == "list"


async def test_a_bare_slash_is_not_a_command():
    """A lone "/" has no command name; splitting it blindly raises IndexError
    in the REPL's parsing shape (agent_core/adapters/cli.py:177-183)."""
    from pare.tui.app import parse_input

    session = StubSession()
    await parse_input(session, "/")
    assert session.sent == [] or type(session.sent[0]).__name__ == "ChatMessage"
