# PARE Quick Start

Get PARE talking — backed by PAL's research vault and risk-gated RE workers — in
a few minutes. For what PARE is see [`README.md`](README.md); for the worker
registry and risk model [`WORKERS.md`](WORKERS.md); for how it fits together
[`ARCHITECTURE.md`](ARCHITECTURE.md); for design detail
[`docs/superpowers/`](docs/superpowers/).

## 1. Prerequisites

- **Python 3.12+**.
- **An inference manager** reachable on your LAN that exposes an OpenAI-compatible
  `/v1/chat/completions` **and** a retrieval API at `/collections/{id}/search`
  (the [`inference_server`](https://github.com/EdibleTuber/inference_server)
  project). It must have a model that emits **structured tool calls** loaded,
  and `PARE_MODEL` must name exactly that model, or the manager answers `409`.
  PARE's default is `model` in [`pare/config.py`](pare/config.py), and the
  comment beside it records why it changed.
- **PAL's vault indexed** into a collection on that server (default name: `vault`).
  Verify it returns hits:
  ```bash
  curl -s -X POST http://192.168.1.14:11434/collections/vault/search \
    -H 'Content-Type: application/json' -d '{"query":"frida","limit":3}'
  ```
  If this returns an empty `results` list, (re)index the collection before
  expecting PARE to find anything — semantic search is how PARE reaches the vault.

## 2. Install

```bash
git clone https://github.com/EdibleTuber/PARE && cd PARE
python -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e ".[dev]"
```

This installs PARE and `agent_core`. **The workers are not dependencies.** Each
lives in its own repo and is installed separately:

- [`pare-static-mcp`](https://github.com/EdibleTuber/pare-static-mcp) and
  [`pare-mitm-mcp`](https://github.com/EdibleTuber/pare-mitm-mcp) install into
  this venv (`pip install -e <checkout>`; mitm also needs `--no-deps`, see
  [`docs/mitm-quickstart.md`](docs/mitm-quickstart.md)).
- [`pare-frida-mcp`](https://github.com/EdibleTuber/pare-frida-mcp) runs on
  the machine the Android device is attached to, and PARE reaches it over the
  network. See [`docs/frida-quickstart.md`](docs/frida-quickstart.md).

`workers.yaml` launches the stdio workers by **absolute path**
(`command: /mnt/secondary/projects/PARE/.venv/bin/...`). On any other checkout,
edit those `command:` lines to point at your venv, or the workers fail to
start. PARE runs without any worker. You get the chat and the vault, and
`/worker list` shows which workers failed to load and why.

## 3. Configure

```bash
cp .env.example .env
$EDITOR .env
```

Minimum to set:

| Variable | What | Example |
|---|---|---|
| `PARE_INFERENCE_URL` | inference manager (completions **and** `/collections`) | `http://192.168.1.14:11434` |
| `PARE_MODEL` | the model loaded on the server's slot; must match exactly | see `pare/config.py` |
| `PARE_COLLECTION_ID` | the collection PAL's vault is indexed into | `vault` |

`PARE_VAULT_PATH` is PARE's own state directory (profile/wisdom/channels), **not**
where it reads PAL's research — that comes over RAG.

## 4. Run

> **Activate the venv** before starting the daemon. `/mitm up` runs the
> `pare-mitm-daemon` console script by name, so it must be on `PATH`.

Terminal 1 — the daemon:

```bash
source .venv/bin/activate
python -m pare
```

You should see `agent pare listening on /run/user/<uid>/pare.sock`.

Terminal 2 — the client:

```bash
source .venv/bin/activate
pare-cli
```

## 5. Try it

**A vault-backed question** (drives `search_vault` → `read_vault_doc`):

```
> Search your research vault for notes on installing frida-server and summarize the most relevant one.
```

PARE searches the vault, reads the best hit, and answers from the real note —
grounded in PAL's curated knowledge, not just the model's training data.

**A risk-gated action** (needs the mitm worker; see
[`docs/mitm-quickstart.md`](docs/mitm-quickstart.md)). Start the proxy with
`/mitm up`, then:

```
> Add a rule blocking requests to example.com.
```

Reads auto-execute, but `add_blocking_rule` changes traffic and is tier `high`,
so PARE stops for approval before dispatching:

```
--- approval required ---
  mitm.add_blocking_rule  (declared=high effective=high)
  args: ...
  approve? [y/n/j/a]:
```

`y` approves once · `n` denies · `j` approves with a justification (required for
`critical`) · `a` approves this tool for the rest of the session. Every dispatch —
approved, denied, or auto — is appended to the JSONL audit log under
`PARE_AUDIT_DIR` (default `~/.local/share/pare/audit/`).

For the full Frida dynamic-analysis workflow (device + `frida-server` setup,
attaching, Java hooks, the operator commands), see
[`docs/frida-quickstart.md`](docs/frida-quickstart.md).

For the TUI operator surface (chat + live UART pane, layout cycling, daemon
auto-spawn), see [`docs/tui-quickstart.md`](docs/tui-quickstart.md).

## 6. Verify the install (no live stack needed)

The suite checks the operator pins in `workers.yaml` against the real worker
contracts, so it needs the worker packages installed. Do that in a **separate
venv**, the way CI does (`.github/workflows/`). Installing `pare-mitm-mcp` with
its dependencies pulls in mitmproxy, which the daemon's venv deliberately does
not have (see the `--no-deps` note in
[`docs/mitm-quickstart.md`](docs/mitm-quickstart.md)).

```bash
python -m venv .venv-test
.venv-test/bin/pip install -e ".[dev]" \
  "pare-frida-mcp @ git+https://github.com/EdibleTuber/pare-frida-mcp.git" \
  "pare-static-mcp @ git+https://github.com/EdibleTuber/pare-static-mcp.git" \
  "pare-mitm-mcp @ git+https://github.com/EdibleTuber/pare-mitm-mcp.git"
.venv-test/bin/pytest tests/ -q
```

Expected: no failures. Some skips are expected; `pytest -rs` names each one's
reason (live smokes gated on an env var or a reachable service).

Two things look like a broken install but are not:

- **`test_risk_overrides_coverage` fails without the worker packages, by
  design.** An unchecked pin run must not look like a verified one. Run the
  suite from the venv above, not from the daemon's.
- **Some tests run against a live ArcticBase when one is reachable** at
  `PARE_ARCTICBASE_URL` or `PARE_ARCTIC_BASE_URL` (default `127.0.0.1:2929`),
  and skip otherwise. On a host that can see one, a failure there concerns
  that ArcticBase or the live test, not the PARE install.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `/worker list` shows a stdio worker as `spawn_failed` | Its `command:` path in `workers.yaml` doesn't exist on this host, or the worker isn't installed into the venv. See [Install](#2-install). |
| No Frida tools | The `frida` worker is networked and declared `autoload: false`. Run `/worker load frida`; setup is in [`docs/frida-quickstart.md`](docs/frida-quickstart.md). |
| Model narrates calling tools but nothing happens (e.g. `<\|tool\|>` text, made-up note names) | The model isn't emitting structured tool calls. Load one that does on the server, and set `PARE_MODEL` to exactly its name. |
| `search_vault` returns nothing | The inference server's `vault` collection isn't populated/indexed, or `PARE_COLLECTION_ID` doesn't match. Re-check step 1. |
| Connecting `pare-cli` errors | Start the daemon first; confirm the socket path matches (`PARE_SOCKET_PATH`). |
