"""Entry point: python -m pare"""
from agent_core.runtime import run_daemon
from pare.agent import PareAgent


def main() -> None:
    run_daemon(PareAgent())


if __name__ == "__main__":
    main()
