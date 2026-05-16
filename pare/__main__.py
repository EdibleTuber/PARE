"""Entry point: python -m pare"""
from agent_core.runtime import run_daemon

from pare.agent import PareAgent
from pare.config import PAREConfig


def main() -> None:
    run_daemon(PareAgent(), config_cls=PAREConfig)


if __name__ == "__main__":
    main()
