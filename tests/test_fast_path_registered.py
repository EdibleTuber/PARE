from pare.agent import PareAgent


def test_fast_path_commands_registered():
    names = {c.name for c in PareAgent.commands}
    assert {"devices", "ps", "apps", "sessions", "select", "attach", "detach"} <= names


def test_fast_path_commands_have_metadata():
    fast = {"devices", "ps", "apps", "sessions", "select", "attach", "detach"}
    for c in PareAgent.commands:
        if c.name in fast:
            assert isinstance(c.args, str)            # args ClassVar required by CommandRegistry.metadata()
            assert c.description                       # non-empty description
