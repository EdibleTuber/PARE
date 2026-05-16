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


def test_static_analyze_registered():
    from pare.agent import PareAgent
    from pare.tools import StaticAnalyze
    assert StaticAnalyze in PareAgent.tools
