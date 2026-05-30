---

# pare

Personal Agentic Reverse Engineer — conversational mobile RE operator.

## Design & Plans

- Design spec: [`docs/superpowers/specs/2026-05-12-pare-v1-design.md`](docs/superpowers/specs/2026-05-12-pare-v1-design.md)
- Phase 0 (agent_core extraction): [`docs/superpowers/plans/2026-05-13-phase0-agent-core-extraction.md`](docs/superpowers/plans/2026-05-13-phase0-agent-core-extraction.md) — landed in `agent_core@v1.2.0`
- Phase 1 (this scaffold + apk_re_agents wrapper): [`docs/superpowers/plans/2026-05-14-phase1-pare-scaffold-and-static-wrapper.md`](docs/superpowers/plans/2026-05-14-phase1-pare-scaffold-and-static-wrapper.md)
- Phase 2 (agent_core MCP execution layer): [`docs/superpowers/plans/2026-05-16-phase2-agent-core-mcp-client.md`](docs/superpowers/plans/2026-05-16-phase2-agent-core-mcp-client.md) — landed in `agent_core@v1.3.0`
- Phase 3 (apk_re_agents Streamable HTTP migration + PARE MCP-direct workers): [`docs/superpowers/plans/2026-05-17-phase3-apk-re-agents-streamable-http-and-pare-wiring.md`](docs/superpowers/plans/2026-05-17-phase3-apk-re-agents-streamable-http-and-pare-wiring.md) — landed apk_re_agents v0.2.0 + PARE workers.yaml
- Risk enforcement (`agent_core@v1.5.0`/`v1.5.1`): [`docs/superpowers/plans/2026-05-27-risk-enforcement-mcp-dispatch.md`](docs/superpowers/plans/2026-05-27-risk-enforcement-mcp-dispatch.md) — `RiskAwareToolPool`: dispatch-time risk gating + HITL approval prompt + audit log
- Phase 4 (in progress): adopt MCP workers via `workers.yaml` — first up, an in-house Python Frida MCP server

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
- `PARE_INFERENCE_URL` — inference server base URL, OpenAI-compatible (default: `http://192.168.1.14:11434`)
- `PARE_MODEL` — model name to use
- `PARE_VAULT_PATH` — path to your vault directory
- `PARE_COLLECTION_ID` — vector collection name for retrieval
- `PARE_AUDIT_DIR` — where the risk-gating audit log is written (default: `~/.local/share/pare/audit`, outside the vault)

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

Connect via the CLI (separate terminal):

```bash
.venv/bin/pare-cli
```

(`pare-cli` wires `agent_core`'s generic REPL to PARE's config/socket. The bare
`python -m agent_core.adapters.cli` does **not** work — that module is a library
with no `__main__`.)

## Smoke Test

```bash
.venv/bin/pytest tests/ -v
```

Expected: `17 passed, 3 skipped`. The 3 skips are env-gated phase 1 / phase 3 smokes that need a running worker stack (set `PARE_PHASE1_SMOKE` / `PARE_PHASE3_SMOKE` to enable them).

## Workers & risk gating

PARE reaches analysis tools through MCP workers declared in `workers.yaml`. Each entry maps to an `agent_core` `WorkerSpec`:

- **Transport** — `streamable_http` (a worker reached over HTTP, e.g. the apk_re_agents agents) or `stdio` (a worker PARE launches as a subprocess and talks to over stdin/stdout, e.g. the forthcoming in-house Frida MCP server).
- **`risk_default`** — the tier applied to every tool the worker exposes. `low`/`medium` auto-execute (still audited); `high` requires operator approval before dispatch; `critical` requires approval **and** a justification.

At dispatch, calls flow through a `RiskAwareToolPool`. For `high`/`critical` tools you get an inline prompt in the CLI:

```
--- approval required ---
  frida.execute_in_session  (declared=high effective=high)
  args: javascript_code=Interceptor.attach(...
  approve? [y/n/j/a]:
```

`y` approves once, `n` denies, `j` approves with a justification (forced for `critical`), `a` approves every call to that tool for the rest of the session. Every dispatch — approved, denied, or auto — is appended to a JSONL audit log under `PARE_AUDIT_DIR` (default `~/.local/share/pare/audit`), which lives outside your vault.

### Adding a worker

Edit `workers.yaml` and restart the daemon. Streamable HTTP worker:

```yaml
workers:
  my_http_worker:
    endpoint: http://127.0.0.1:9100/mcp
    transport: streamable_http
    risk_default: low
    capability_tags: [static, apk]
```

stdio worker (PARE launches the process):

```yaml
workers:
  frida:
    command: /path/to/.venv/bin/python
    args: [-m, your_frida_mcp_module]
    transport: stdio
    risk_default: high
    capability_tags: [dynamic, frida]
```

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

The CLI (`pare-cli`) is typically run interactively in a terminal, not as a
service.

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
        __init__.py  StaticAnalyze (apk_re_agents /jobs wrapper) + Tool exports
workers.yaml         MCP worker registry — streamable_http + stdio (see "Workers & risk gating")
```

MCP-discovered tools are registered at startup by `PareAgent.register_tools()` and dispatched through a `RiskAwareToolPool`, so every worker tool call is risk-evaluated and audited.

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
