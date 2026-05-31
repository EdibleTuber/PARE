# PARE Quick Start

Get PARE talking — backed by PAL's research vault and risk-gated RE workers — in
a few minutes. For the full picture see [`README.md`](README.md); for design
detail see [`docs/superpowers/`](docs/superpowers/).

## 1. Prerequisites

- **Python 3.12+**.
- **An inference manager** reachable on your LAN that exposes an OpenAI-compatible
  `/v1/chat/completions` **and** a retrieval API at `/collections/{id}/search`
  (the [`inference_server`](https://github.com/EdibleTuber/inference_server)
  project). It must have a model that emits **structured tool calls** loaded —
  `gemma-4-26b-a4b-it-q4_k_m` is the tested default.
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

`agent_core` and the in-house Frida server (`pare-frida-mcp`) install as
dependencies.

## 3. Configure

```bash
cp .env.example .env
$EDITOR .env
```

Minimum to set:

| Variable | What | Example |
|---|---|---|
| `PARE_INFERENCE_URL` | inference manager (completions **and** `/collections`) | `http://192.168.1.14:11434` |
| `PARE_MODEL` | a model that emits structured tool calls | `gemma-4-26b-a4b-it-q4_k_m` |
| `PARE_COLLECTION_ID` | the collection PAL's vault is indexed into | `vault` |

`PARE_VAULT_PATH` is PARE's own state directory (profile/wisdom/channels), **not**
where it reads PAL's research — that comes over RAG.

## 4. Run

> **Activate the venv** before starting the daemon. PARE launches the Frida
> worker by bare command name (`pare-frida-mcp`), so it must be on `PATH`.
> Skipping activation logs `worker frida discovery failed (No such file or
> directory)` and you get no Frida tools.

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

**A risk-gated action** (drives a Frida worker tool):

```
> List the available Frida devices.
```

Because the `frida` worker's `risk_default` is `high`, PARE pauses for approval
before dispatching:

```
--- approval required ---
  frida.list_devices  (declared=high effective=high)
  args:
  approve? [y/n/j/a]:
```

`y` approves once · `n` denies · `j` approves with a justification (required for
`critical`) · `a` approves this tool for the rest of the session. Every dispatch —
approved, denied, or auto — is appended to the JSONL audit log under
`PARE_AUDIT_DIR` (default `~/.local/share/pare/audit/`).

## 6. Verify the install (no live stack needed)

```bash
.venv/bin/pytest tests/ -q
```

Expected: the suite passes (currently 44 passed, 3 skipped; the skips are
env-gated worker smokes).

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `worker frida discovery failed (No such file or directory: 'pare-frida-mcp')` | Daemon started without the venv on `PATH`. Activate the venv, restart. |
| Model narrates calling tools but nothing happens (e.g. `<\|tool\|>` text, made-up note names) | The model isn't emitting structured tool calls. Use `gemma-4-26b-a4b-it-q4_k_m` (set `PARE_MODEL`). |
| `search_vault` returns nothing | The inference server's `vault` collection isn't populated/indexed, or `PARE_COLLECTION_ID` doesn't match. Re-check step 1. |
| Connecting `pare-cli` errors | Start the daemon first; confirm the socket path matches (`PARE_SOCKET_PATH`). |
