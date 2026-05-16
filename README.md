---

# pare

Personal Agentic Reverse Engineer — conversational mobile RE operator.

## Design & Plans

- Design spec: [`docs/superpowers/specs/2026-05-12-pare-v1-design.md`](docs/superpowers/specs/2026-05-12-pare-v1-design.md)
- Phase 0 (agent_core extraction): [`docs/superpowers/plans/2026-05-13-phase0-agent-core-extraction.md`](docs/superpowers/plans/2026-05-13-phase0-agent-core-extraction.md) — landed in `agent_core@v1.2.0`
- Phase 1 (this scaffold + apk_re_agents wrapper): [`docs/superpowers/plans/2026-05-14-phase1-pare-scaffold-and-static-wrapper.md`](docs/superpowers/plans/2026-05-14-phase1-pare-scaffold-and-static-wrapper.md)

## Install

```bash
python -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e ".[dev]"
```

Requires Python 3.12+. `agent_core` is pulled from GitHub at install time.

## Configure

Copy `.env.example` to `.env` and fill in the values:

```bash
cp .env.example .env
$EDITOR .env
```

Key variables:
- `PARE_INFERENCE_URL` — Ollama base URL (default: `http://localhost:11434`)
- `PARE_MODEL` — model name to use
- `PARE_VAULT_PATH` — path to your vault directory
- `PARE_COLLECTION_ID` — vector collection name for retrieval

See `.env.example` for the full list.

## Run

Start the daemon:

```bash
.venv/bin/python -m pare
```

Or use the installed script:

```bash
.venv/bin/pare-daemon
```

Connect via the CLI adapter (separate terminal):

```bash
.venv/bin/python -m agent_core.adapters.cli
```

## Smoke Test

```bash
.venv/bin/pytest tests/ -v
```

Expected: 3 tests pass (import, instantiation, command registration).

## Discord (optional)

Discord is not wired by default. To opt in:

1. Add `agent_core[discord]` to `dependencies` in `pyproject.toml`.
2. In `pare/__main__.py`, instantiate the gateway and pass it to
   `run_daemon` (see `agent_core.adapters.discord_gateway` for the API).

## Systemd Setup

Two service files are included in `systemd/`. Edit the `WorkingDirectory` and
`EnvironmentFile` paths to match your deployment, then:

```bash
cp systemd/pare-daemon.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now pare-daemon
```

For a CLI service that stays running, use `agent_core.adapters.cli` as the
ExecStart target (no template provided; the CLI is typically run interactively).

---

## Architecture

```
pare/
    agent.py         PareAgent — your Agent subclass; add commands + tools here
    __main__.py      python -m pare entry point (calls run_daemon)
    prompts/
        system.md    system prompt — edit to define your agent's identity
    commands/
        hello.py     example Command; copy to add your own
    tools/
        __init__.py  placeholder; add Tool subclasses and register on the class
```

### Extension points

| Point | How to use |
|---|---|
| `PareAgent.commands` | Add `Command` subclasses; `/help` is automatic |
| `PareAgent.tools` | Add `Tool` subclasses; executor picks them up |
| `PareAgent.disabled_builtins` | Remove built-in commands/tools by name |
| `PareAgent.setup()` | Construct domain resources after framework managers are ready |
| `PareAgent.system_prompt(ctx)` | Build the per-turn system prompt |
| `PareAgent.decide_mode(conversation)` | Override reasoning-mode logic |

Full API docs: [agent_core](https://github.com/EdibleTuber/agent_core)
