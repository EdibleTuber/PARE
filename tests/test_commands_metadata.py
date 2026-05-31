"""Every PARE-declared command must define the required Command ClassVars.

CommandRegistry.metadata() (agent_core/commands/registry.py) reads
`type(c).args` (and .name/.description) for every command when building the
command catalog that system_prompt() embeds. A command missing `args` raises
`AttributeError: type object '<Cmd>' has no attribute 'args'` on EVERY chat
turn (handle_chat calls system_prompt early). Regression: `Health` shipped
without `args`, which surfaced once PR1 made handle_chat actually run.
"""
import pytest

from pare.agent import PareAgent

REQUIRED_CLASSVARS = ("name", "args", "description")


@pytest.mark.parametrize("cmd_cls", PareAgent.commands, ids=lambda c: c.__name__)
@pytest.mark.parametrize("attr", REQUIRED_CLASSVARS)
def test_command_defines_required_classvar(cmd_cls, attr):
    value = getattr(cmd_cls, attr, None)
    assert isinstance(value, str), (
        f"{cmd_cls.__name__} is missing required Command ClassVar "
        f"{attr!r} (str). CommandRegistry.metadata() reads type(c).{attr}; "
        f"a missing value crashes every chat turn via the command catalog."
    )
