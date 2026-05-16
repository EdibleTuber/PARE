"""Entry point: python -m {{agent_pkg}}"""
from agent_core.runtime import run_daemon
from {{agent_pkg}}.agent import {{AGENT_CLASS}}


def main() -> None:
    run_daemon({{AGENT_CLASS}}())


if __name__ == "__main__":
    main()
