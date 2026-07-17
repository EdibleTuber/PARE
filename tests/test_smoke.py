"""Smoke test: confirm the agent class can be imported and instantiated."""


def test_agent_class_importable():
    from pare.agent import PareAgent
    assert PareAgent.name == "pare"


def test_agent_can_be_instantiated():
    from pare.agent import PareAgent
    agent = PareAgent()
    assert agent.name == "pare"


def test_hello_command_registered():
    from pare.agent import PareAgent
    from pare.commands.hello import Hello
    assert Hello in PareAgent.commands


def test_static_analyze_not_registered_by_default():
    # apk_re_agents is opt-in (config.enable_apk_re_agents, default off): the tool
    # must not be advertised on the class-level toolset, so the model isn't handed
    # a dead tool it reaches for first. The enabled path is covered in
    # tests/test_register_tools.py.
    from pare.agent import PareAgent
    from pare.tools import StaticAnalyze
    assert StaticAnalyze not in PareAgent.tools
