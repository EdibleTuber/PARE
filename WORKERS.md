# Workers & risk gating

How PARE reaches analysis tools, and what stops a dangerous one running
unattended. For the daemon's internals see [`ARCHITECTURE.md`](ARCHITECTURE.md).


PARE reaches analysis tools through MCP workers declared in `workers.yaml`. Each entry maps to an `agent_core` `WorkerSpec`:

- **Transport** — `streamable_http` (a worker reached over HTTP) or `stdio` (a worker PARE launches as a subprocess and talks to over stdin/stdout). The in-house workers are one repo each, and none is a PARE dependency. [`pare-static-mcp`](https://github.com/EdibleTuber/pare-static-mcp) and [`pare-mitm-mcp`](https://github.com/EdibleTuber/pare-mitm-mcp) run as stdio subprocesses on the PARE host. [`pare-frida-mcp`](https://github.com/EdibleTuber/pare-frida-mcp) runs on the machine the Android device is attached to, and [`pare-hardware-mcp`](https://github.com/EdibleTuber/pare-hardware-mcp) runs on the bench Pi. PARE reaches both of those over the tailnet, and both are declared `autoload: false`, so they connect on `/worker load`. (The legacy `apk_re_agents` Streamable-HTTP backend is superseded by `pare-static-mcp` and disabled in `workers.yaml`.)

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

- **Unloading drops live state.** `/worker unload <name>` disconnects the
  client. For a stdio worker it also ends the process, so that worker's live
  state (attachments, installed hooks) goes with it. A networked worker's
  process runs on its own host, and PARE does not end it. There is no
  confirmation prompt and no `--force`; the command's own output names what it
  just destroyed.
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
Declaring a worker in `workers.yaml` is still the first step. A stdio worker
(PARE launches the process) is the `static` entry:

```yaml
workers:
  static:
    command: /mnt/secondary/projects/PARE/.venv/bin/pare-static-mcp   # absolute: this host's venv
    transport: stdio
    risk_default: low          # FLOOR; per-tool wire tiers + operator pins can escalate
    autoload: true
    capability_tags: [static, apk, android]
```

A networked worker (streamable HTTP on another machine) is the `frida` entry:

```yaml
workers:
  frida:
    endpoint: http://100.68.47.23:9101/mcp     # the laptop's tailnet address
    transport: streamable_http
    connect_timeout: 20
    read_timeout: 60
    risk_default: low
    autoload: false        # REQUIRED for a networked worker
    capability_tags: [mobile, dynamic, android, frida]
```

`autoload: false` is required for a networked worker: a sleeping host drops
the connection rather than refusing it, so autoloading would cost the full
connect timeout at every daemon boot. The comment block at the top of
`workers.yaml` lists every key a worker may carry. An unknown key aborts the
**whole** file, so check it there.

What changed is the second step. `autoload` (default `true`) decides *when*
that declared worker actually connects: `autoload: true` connects it during
daemon startup; `autoload: false` declares it without connecting, and an
operator brings it up later with `/worker load <name>`, with no restart. Either
way, the worker only ever exposes what `workers.yaml` declared, at no lower than
its `risk_default`.

Operator pins in `workers.yaml` can force a tool's tier up regardless of what the
worker advertises (e.g. `frida_execute_script → critical`,
`frida_write_memory → high`).

## Discord (optional)

Discord is not wired by default. To opt in:

1. Add `agent_core[discord]` to `dependencies` in `pyproject.toml`.
2. In `pare/__main__.py`, instantiate the gateway and pass it to
   `run_daemon` (see `agent_core.adapters.discord_gateway` for the API).

