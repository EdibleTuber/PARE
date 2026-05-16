<!-- BEFORE INIT -->
# Agent Template

This repository is a starter scaffold for building agents on top of
[agent_core](https://github.com/EdibleTuber/agent_core).

**Run the init script to scaffold a real agent:**

```bash
./scripts/init-agent.sh <agent-name>
```

The agent name must be a lowercase slug (letters, digits, hyphens; must start
with a letter). Examples: `re-lab`, `coding`, `my-agent`.

The script will:
- Ask for a one-line description of your agent
- Rename the package directory and service files
- Replace all placeholders in all files
- Remove this "Before Init" section from the README
- Remove itself

After init, install, configure `.env`, and run the smoke test to confirm
everything wires up.

<!-- END BEFORE INIT -->

---

# {{AGENT_NAME}}

{{AGENT_DESCRIPTION}}

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
- `{{AGENT_PREFIX}}_INFERENCE_URL` — Ollama base URL (default: `http://localhost:11434`)
- `{{AGENT_PREFIX}}_MODEL` — model name to use
- `{{AGENT_PREFIX}}_VAULT_PATH` — path to your vault directory
- `{{AGENT_PREFIX}}_COLLECTION_ID` — vector collection name for retrieval

See `.env.example` for the full list.

## Run

Start the daemon:

```bash
.venv/bin/python -m {{agent_pkg}}
```

Or use the installed script:

```bash
.venv/bin/{{AGENT_NAME}}-daemon
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
2. In `{{agent_pkg}}/__main__.py`, instantiate the gateway and pass it to
   `run_daemon` (see `agent_core.adapters.discord_gateway` for the API).

## Systemd Setup

Two service files are included in `systemd/`. Edit the `WorkingDirectory` and
`EnvironmentFile` paths to match your deployment, then:

```bash
cp systemd/{{AGENT_NAME}}-daemon.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now {{AGENT_NAME}}-daemon
```

For a CLI service that stays running, use `agent_core.adapters.cli` as the
ExecStart target (no template provided; the CLI is typically run interactively).

---

## Architecture

```
{{agent_pkg}}/
    agent.py         {{AGENT_CLASS}} — your Agent subclass; add commands + tools here
    __main__.py      python -m {{agent_pkg}} entry point (calls run_daemon)
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
| `{{AGENT_CLASS}}.commands` | Add `Command` subclasses; `/help` is automatic |
| `{{AGENT_CLASS}}.tools` | Add `Tool` subclasses; executor picks them up |
| `{{AGENT_CLASS}}.disabled_builtins` | Remove built-in commands/tools by name |
| `{{AGENT_CLASS}}.setup()` | Construct domain resources after framework managers are ready |
| `{{AGENT_CLASS}}.system_prompt(ctx)` | Build the per-turn system prompt |
| `{{AGENT_CLASS}}.decide_mode(conversation)` | Override reasoning-mode logic |

Full API docs: [agent_core](https://github.com/EdibleTuber/agent_core)
