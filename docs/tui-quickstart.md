# PARE TUI Quick Start

The **PARE TUI** (`pare-tui`) replaces the CLI as the operator surface.
It puts the chat transcript and a live UART console side by side in a
single terminal, with the approval modal, status bar, and layout cycling
you need for a hands-on hardware session.

This builds on the base [`QUICKSTART.md`](../QUICKSTART.md) (inference
server, vault, daemon). Read that first; this guide adds the TUI layer.

## What the TUI provides

| Feature | What it does |
|---|---|
| **Chat pane** | Send messages and `/command`s to `pare-daemon`, see streamed replies rendered as Markdown |
| **UART pane** | Live device console output from `pare-hardware-mcp` on the bench Pi, with honesty markers for dropped bytes, capture gaps, and clamped reads |
| **Approval modal** | Tool-approval prompts (spec I2) pop up as a modal; pane polling keeps running underneath |
| **Status bar** | One-line summary: `daemon:up/DOWN`, channel id, per-pane health and cursor position |
| **Layout cycling** | `Ctrl+L` cycles chat-wide → even → dock-wide → dock-hidden |
| **Theme persistence** | Pick a Textual palette theme; it is saved to `$XDG_CONFIG_HOME/pare/tui.json` |
| **Daemon auto-spawn** | If no systemd unit manages `pare-daemon`, the TUI detects or spawns one and reaps it on exit |
| **Operator input** | Type at the UART pane's input to send lines directly to the device console (ungated — you are the operator) |

## Prerequisites

- A running inference server and vault (see `QUICKSTART.md`)
- `pare-tui` installed (it is an entry point in the `pare` package)

## Launch

### Chat only (no hardware)

```bash
pare-tui
```

With no extra configuration the TUI launches against a **fake console
source** — a scriptable in-memory double of the hardware worker. The
UART pane mounts and polls but shows no device output. This is useful
for chat-only sessions or development without a bench Pi.

### With a live bench Pi

Set `PARE_TUI_HARDWARE_ENDPOINT` to the hardware worker's MCP endpoint:

```bash
PARE_TUI_HARDWARE_ENDPOINT=http://<bench-pi-ip>:9102/mcp pare-tui
```

The UART pane will connect to the worker over StreamableHTTP, attach to
whatever console session is open, and begin polling at 50 ms (20 Hz).

You should see the device's serial output (boot logs, shell prompts,
kernel messages) streaming into the UART pane in real time.

## Environment variables

| Variable | Default | Effect |
|---|---|---|
| `PARE_TUI_HARDWARE_ENDPOINT` | *(unset — uses fake source)* | MCP endpoint URL for `pare-hardware-mcp`. Set this to see real UART output. |
| `PARE_TUI_NO_AUTO_SPAWN` | *(unset)* | Set to `1` to prevent the TUI from auto-spawning `pare-daemon` |
| `PARE_WORKERS_YAML_PATH` | *(unset — looks for `workers.yaml` in the repo root)* | Explicit path to `workers.yaml` for a spawned daemon |
| `PARE_SOCKET_PATH` | `$XDG_RUNTIME_DIR/pare.sock` | Unix socket path for `pare-daemon` |

## Keyboard shortcuts

| Key | Action |
|---|---|
| `Ctrl+L` | Cycle layout: chat-wide → even split → dock-wide → dock-hidden |
| `Ctrl+Q` | Quit (reaps any daemon the TUI spawned) |
| `F1` | Send `/help` to the daemon |
| `Enter` (chat input) | Send a chat message or `/command` |
| `Enter` (UART input) | Send a line to the device console |

## Layout

The screen is a horizontal split: the **chat area** (transcript + input)
on the left, the **pane dock** (UART pane) on the right. `Ctrl+L` cycles
through four ratios:

1. **Chat wide** (default) — chat 2:1 dock
2. **Even** — chat 1:1 dock
3. **Dock wide** — chat 1:2 dock
4. **Dock hidden** — dock is `display: none` (pane keeps polling)

## UART pane details

### Honesty markers

The pane does not silently hide what it could not capture. Visible
markers (styled distinctly from device text — the board cannot forge the
style) appear for:

- **Dropped bytes** — evicted by ring-buffer wraparound before the pane
  could read them
- **Capture gaps** — the capture was suspended (e.g. a baud scan),
  leaving a hole in time that `dropped` cannot see
- **Clamped reads** — the read was capped at `limit_applied` bytes with
  more waiting; the pane catches up on the next poll
- **Session ended** — the device disconnected; the input is disabled

### Observed-output logging

When a `daemon_session` is attached, the pane batches received bytes into
`PaneActivityMessage` records sent to the daemon. Batching is
controlled by a poll-count cap (default 4 non-empty polls) and a
quiet-boundary flush (an empty poll after data triggers an immediate
flush). This keeps the daemon's connection from being flooded by
per-poll messages during continuous output.

### Operator input

The UART input at the bottom of the pane sends lines directly to the
device via `console_send`. This is **ungated** — no approval prompt —
because you are the operator at a console you are already looking at.
A bare Enter sends a lone newline (what Enter does at a real console).

## Daemon connection

### Auto-spawn

On launch, if `systemctl --user is-enabled pare-daemon` exits non-zero
(no systemd unit manages the daemon), the TUI will:

1. Probe the socket. If a daemon is already listening, attach.
2. Otherwise, take an flock on `pare-spawn.lock`, spawn `pare-daemon`
   in its own session, and wait for it to accept connections.
3. On exit (`Ctrl+Q`, SIGHUP, or app close), reap the spawned daemon
   (SIGTERM → SIGKILL escalation).

Logs from a spawned daemon go to `~/.local/state/pare/daemon-<pid>.log`.

### Systemd-managed

When a systemd user unit is enabled, the TUI never spawns. If the attach
fails, it shows `[systemd-managed pare-daemon is not accepting
connections — systemctl --user status pare-daemon]`.

### No daemon

The TUI is usable without a daemon: the UART pane polls its source
independently. Chat input will show `[send failed: ...]` but the pane
keeps running.

## Troubleshooting

### UART pane shows no output

1. **Check the env var.** If `PARE_TUI_HARDWARE_ENDPOINT` is not set,
   the pane runs against the fake source. Set it to
   `http://<bench-pi-ip>:9102/mcp`.

2. **Check the worker.** From your workstation:
   ```bash
   curl -s -X POST http://<bench-pi-ip>:9102/mcp \
     -H 'Content-Type: application/json' \
     -H 'Accept: application/json, text/event-stream' \
     -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"probe","version":"0.1"}}}'
   ```
   A 200 with a `Mcp-Session-Id` header means the worker is alive.

3. **Check `console_status`.** After initializing a session (above), call
   `console_status` — look for `"open": true` and `buffer_head > 0`.
   If `buffer_head` is nonzero, the worker has data; the problem is
   between the worker and the pane.

4. **Check the pane's status bar.** The status bar shows
   `UartPane[ok cursor=N]` or `UartPane[FAIL:...]`. A `FAIL` means
   the source raised — the pane is backing off and retrying.

### Status bar shows `daemon:DOWN`

The TUI could not connect to `pare-daemon`. Check that the daemon is
running (`systemctl --user status pare-daemon` or `ss -lx | grep pare`),
or set `PARE_TUI_NO_AUTO_SPAWN` to troubleshoot spawn failures.

### Approval modal blocks the screen

The modal is designed to overlay without stopping pane polling. The UART
pane continues receiving data underneath. Approve or deny the tool call
to dismiss it.

## Architecture (for contributors)

```
pare/tui/
├── app.py                 PareTUI (the Textual App)
├── daemon_lifecycle.py    detect_or_spawn / reap (pure stdlib, no asyncio)
├── session.py             DaemonSession — single owner of the daemon socket
├── sanitize.py            Untrusted bytes → display-safe text
├── prefs.py               Theme persistence ($XDG_CONFIG_HOME/pare/tui.json)
├── panes/
│   ├── base.py            Pane (widget base, cursor, backoff) + PaneDock
│   └── uart.py            UartPane (renders ConsoleSlice + honesty markers)
├── sources/
│   ├── base.py            ConsoleSource Protocol + ConsoleSlice dataclass
│   ├── mcp_console.py     McpConsoleSource — real StreamableHTTP client
│   └── fake_console.py    FakeConsoleSource — scriptable test double
└── widgets/
    ├── approval.py        ApprovalModal
    ├── statusbar.py       StatusBar
    └── transcript.py      TurnRenderer + TranscriptLog
```

**Key invariant:** `McpConsoleSource` owns one "owner task" that
connects, parks, and closes the MCP client in the same asyncio task —
because `anyio` binds cancel scopes to the task that entered them, and
closing from a different task raises `RuntimeError`. The pane's poll
timer calls `read()` from a different task, which is safe; only
`start()`/`stop()` carry the affinity requirement.

**Source injection:** `_build_uart_pane` in `app.py` picks the source
from `PARE_TUI_HARDWARE_ENDPOINT`. Swapping from fake to real is a
one-line change. Every pane and test is built against the
`ConsoleSource` Protocol, never a concrete source.
