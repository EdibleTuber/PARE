# Workers & risk gating

How PARE reaches analysis tools, and what stops a dangerous one running
unattended. For the daemon's internals see [`ARCHITECTURE.md`](ARCHITECTURE.md).


PARE reaches analysis tools through MCP workers declared in `workers.yaml`. Each entry maps to an `agent_core` `WorkerSpec`:

- **Transport** — `streamable_http` (a worker reached over HTTP) or `stdio` (a worker PARE launches as a subprocess and talks to over stdin/stdout). The in-house workers ship as stdio console scripts, one repo each: [`pare-static-mcp`](https://github.com/EdibleTuber/pare-static-mcp), [`pare-frida-mcp`](https://github.com/EdibleTuber/pare-frida-mcp) and [`pare-mitm-mcp`](https://github.com/EdibleTuber/pare-mitm-mcp). A fourth, `pare-hardware-mcp`, is **forward-declared** in `workers.yaml` with `autoload: false` — it does not exist yet, so loading it reports `spawn_failed`, which is the honest answer and visible in `/worker list`. (The legacy `apk_re_agents` Streamable-HTTP backend is superseded by `pare-static-mcp` and disabled in `workers.yaml`.)

  `workers.yaml` is the list; `/worker list` is what is actually loaded. Neither is restated here, because a count in prose is wrong the first time a worker is added.

> For the end-to-end Frida dynamic-analysis workflow (device + `frida-server`
> setup, attach, Java hooks, scripts, capture store), see
> [`docs/frida-quickstart.md`](docs/frida-quickstart.md).
- **`risk_default`** — the tier applied to every tool the worker exposes. `low`/`medium` auto-execute (still audited); `high` requires operator approval before dispatch; `critical` requires approval **and** a justification.

At dispatch, calls flow through a `RiskAwareToolPool`. For `high`/`critical` tools you get an inline prompt in the CLI:

```
--- approval required ---
  frida.execute_script  (declared=critical effective=critical)
  args: source=Interceptor.attach(...
  approve? [y/n/j/a]:
```

`y` approves once, `n` denies, `j` approves with a justification (forced for `critical`), `a` approves every call to that tool for the rest of the session. Every dispatch — approved, denied, or auto — is appended to a JSONL audit log under `PARE_AUDIT_DIR` (default `~/.local/share/pare/audit`), which lives outside your vault.

The `mitm` HTTPS-traffic worker is driven by the `/mitm` command; `/worker tools mitm` lists what it currently exposes. Most are read-only (tier `low`), but the worker can also modify traffic: `add_blocking_rule`, `add_modification_rule` and `replay_flow` advertise tier `high`, and `inject_request` advertises `critical`. Note `delete_rule` and `clear_rules` mutate interception state at tier `low`, so they auto-execute. See [`docs/mitm-quickstart.md`](docs/mitm-quickstart.md).

### Runtime worker control: `/worker`

Workers load and unload without a daemon restart. `/worker` controls the running
pool:

| Subcommand | Effect |
|---|---|
| `/worker` / `/worker list` | Table of every declared worker: state, tool count, transport, risk floor, boot mode, tags, last error |
| `/worker tools <name>` | List the tools a loaded worker currently exposes |
| `/worker load <name>` | Connect a declared-but-unloaded worker and register its tools |
| `/worker unload <name>` | Disconnect a loaded worker and deregister its tools |
| `/worker reload <name>` | Unload then load — e.g. to pick up a restarted worker process |

Two consequences to know before you use it:

- **Unloading drops live state.** `/worker unload frida` disconnects the client
  and, for a stdio worker, ends its process — any live Frida attachments and
  installed hooks for that worker are gone with it. There is no confirmation
  prompt and no `--force`; the command's own output names what it just
  destroyed.
- **Any load or unload changes the tool list**, which changes the prompt
  prefix the model sees — so the *next* turn reprocesses the whole
  conversation from scratch, one noticeably slower reply. Without this note,
  that delay reads as a hang.

**What unload does *not* cost:** captured findings — flows, hook events, prior
tool output — stay searchable through `search_capture` / `read_capture` after
the worker that produced them is unloaded. Only *live* state (session ids, the
ability to issue new calls against them) goes stale. This is what makes
unloading a worker to reclaim context budget mid-investigation safe: you lose
the ability to act through it, not the record of what you already found.

### Adding a worker

`workers.yaml` remains the trust anchor: nothing is loadable, at any point in
the daemon's life, that an operator has not declared there ahead of time with a
`risk_default` — the floor tier every tool from that worker is gated at.
Declaring a worker in `workers.yaml` is still the first step. Streamable HTTP
worker:

```yaml
workers:
  my_http_worker:
    endpoint: http://127.0.0.1:9100/mcp
    transport: streamable_http
    risk_default: low
    capability_tags: [static, apk]
    autoload: true
```

stdio worker (PARE launches the process). The in-house Frida server ships as the
`pare-frida-mcp` console script, so the daemon must run with `.venv/bin` on
`PATH` (see [Run](#run)):

```yaml
workers:
  frida:
    command: pare-frida-mcp
    transport: stdio
    risk_default: high          # FLOOR; per-tool wire tiers + operator pins can escalate
    capability_tags: [mobile, dynamic, android, frida]
    autoload: true
```

What changed is the second step. `autoload` (default `true`) decides *when*
that declared worker actually connects: `autoload: true` connects it during
daemon startup like today; `autoload: false` declares it without connecting,
and an operator brings it up later with `/worker load my_http_worker` — no
restart required. Either way, the worker only ever exposes what `workers.yaml`
declared, at no lower than its `risk_default`.

Operator pins in `workers.yaml` can force a tool's tier up regardless of what the
worker advertises (e.g. `frida_execute_script → critical`,
`frida_write_memory → high`).

## Discord (optional)

Discord is not wired by default. To opt in:

1. Add `agent_core[discord]` to `dependencies` in `pyproject.toml`.
2. In `pare/__main__.py`, instantiate the gateway and pass it to
   `run_daemon` (see `agent_core.adapters.discord_gateway` for the API).

