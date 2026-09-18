# `pare-tui` — an operator surface with live device panes

**Status:** design, 2026-09-18.
**Replaces:** `pare-cli` as the day-to-day operator surface. `pare-cli` stays as
the fallback and the reference implementation of the wire protocol.

The motivating problem: PARE can drive a UART console on the bench, but the
operator cannot *watch* it. `console_read` returns bytes to the model, and the
operator sees only the model's summary of them. The same gap exists for mitm
flows and Frida hook output — the worker sees the stream, the operator sees a
paraphrase.

This design adds a terminal UI that holds the conversation **and** renders live
device output beside it, starting with an interactive UART console for the
bench Pi.

---

## 1. Decisions

| | Decision | Why |
|---|---|---|
| **D1** | The TUI replaces `pare-cli`; it is the only client attached at a time. | `agent_core/daemon.py:105-107` builds `_emit` as a closure over *one connection's* writer, and `_run_handler` writes each yielded message to that same writer. There is no fan-out. A second client attached concurrently sees none of the first's stream, tool progress, or approval requests. Single-client is not a simplification here — it is the only shape the daemon supports without change. |
| **D2** | Panes read **directly from the source**, not through the daemon's tool pool. | See §3. |
| **D3** | Operator keystrokes at a pane are **not** risk-gated. | See §3.2. |
| **D4** | Pane activity is **logged through the daemon** via a PARE-local protocol message. | See §5. |
| **D5** | Built with Textual, in `pare/tui/`, not in `agent_core`. | The panes are RE-specific. `agent_core/adapters/discord_gateway.py:3` records the ecosystem's pattern for this — "Lifted from PAL's discord_adapter" — built in the consumer, generalized once a second agent wanted it. Nothing here forecloses that lift. |
| **D6** | v1 ships the UART pane only. | mitm and Frida panes reuse the same pane interface (§3.4) but are not in scope. |

### What this design does NOT change

- **No change to `agent_core`.** Not the daemon, not the protocol module, not
  the risk gate. §5 was chosen partly because it needs none: `register_message`
  is already exported (`agent_core/protocol/__init__.py:17`) and
  `Agent.handle_other` already exists as an override point
  (`agent_core/agent.py:147`). PAL registers its own custom message types this way
  in `pal/protocol.py`.
- **No change to the risk posture for the model.** `hardware: risk_default:
  high` in `workers.yaml` stays exactly as it is, and every model-dispatched
  hardware call keeps gating as it does today. §3.2 is about a different caller,
  not a relaxation.

---

## 2. Shape

```
┌──────────────────────────────┬──────────────────────────────┐
│ chat transcript              │ pane dock                    │
│                              │  ┌─────────────────────────┐ │
│                              │  │ UART — TG1119e7 @115200 │ │
│                              │  │ (scrollback)            │ │
│                              │  ├─────────────────────────┤ │
│                              │  │ send:                   │ │
│                              │  └─────────────────────────┘ │
├──────────────────────────────┴──────────────────────────────┤
│ > chat input                                                │
├─────────────────────────────────────────────────────────────┤
│ daemon ●  hardware ●  channel cli-20260918-…   cursor 41208 │
└─────────────────────────────────────────────────────────────┘
```

### 2.1 Concurrency is the architecture

Three task families run independently. **No one of them may block another** —
that property is the entire reason this is a TUI and not a REPL, and it is what
the tests in §7 exist to hold.

1. **The daemon reader** — exactly one task, owning `DaemonConnection.receive()`
   for the process lifetime, decoding messages and posting them to widgets.
2. **One poller per pane** — each owning its own MCP client session to its
   source.
3. **The Textual event loop** — input and rendering, blocking on neither.

**Invariant I1 — `receive()` has exactly one owner.** This is a requirement of
the client, not a preference. `agent_core/client.py:57-63`: `receive()` "Does
NOT hold `_read_lock`. Do not interleave with `chat()`/`command()`/
`command_stream()` on the same connection; `readline()` races on the shared
`StreamReader` and produces interleaved or out-of-order results." The TUI
therefore uses **only** `connect()`, `send()`, `receive()`, `close()`. The
high-level helpers are forbidden. `send()` is safe from any task — it writes the
other half of the socket.

**Invariant I2 — an approval modal must not stall the pollers.** A
`ToolApprovalRequestMessage` arriving mid-turn raises a modal over the chat
pane. The UART pane keeps polling and stays readable underneath it. This is the
common case in practice, not an edge: an operator's usual reason to approve a
hardware call is *what they can see on the console right now*, so a modal that
freezes the console defeats its own purpose.

**Invariant I3 — a dead source degrades one pane, never the app.** A pane whose
poller errors renders its error in place and keeps retrying with backoff. The
chat stays usable.

### 2.2 What is ported from `adapters/cli.py`, and what is not

`run_repl` (`agent_core/adapters/cli.py:148-207`) cannot be reused. It is
strictly turn-synchronous: `await session.prompt_async("> ")`, send, then drain
`conn.receive()` until a `ResponseMessage` or `ErrorMessage` and stop. One task
does input and output in lockstep — which is exactly why you cannot type while
PARE is thinking today.

Two pieces of its logic **must be ported rather than inherited**, because they
encode wire behaviour rather than REPL behaviour:

- **End-of-turn.** A turn closes on `ResponseMessage` or `ErrorMessage`
  (`cli.py:202-203`).
- **Stream/response de-duplication.** A `reasoning=off` turn streams
  `StreamChunkMessage` tokens *and then* emits a `ResponseMessage` carrying the
  same joined text; a `reasoning=on` turn emits only the latter. `_TurnPrinter`
  (`cli.py:118-146`) resolves this by suppressing a `ResponseMessage` that
  merely repeats what was streamed. A TUI that skips this renders every
  non-reasoning answer twice.

Slash commands keep their current wire form: a leading `/` becomes a
`CommandMessage(name, args)` (`cli.py:177-183`), dispatched by
`PareAgent.handle_command` (`pare/agent.py:327`). The TUI adds completion over
the command registry; it does not reimplement any command.

Each launch mints a fresh `channel_id`, as `pare/cli.py:_new_channel_id` does
today, so a session starts clean instead of replaying `cli-default`.

---

## 3. Panes

### 3.1 Why direct, and not through the daemon

Routing pane reads through the daemon's tool pool was considered and rejected.

`workers.yaml` sets `hardware: risk_default: high`, and
`agent_core/workers/risk.py:71` resolves the effective tier as
`_max_tier(floor, advertised)`. The hardware worker advertises `console_read`
as `low` (`pare-hardware-mcp/src/pare_hardware_mcp/contract.py:78`), so its
**effective** tier is `high` — gated on operator approval on every call unless
session-approved. A pane polling through the daemon would either prompt per
poll, or require carving a hole in the gate.

Direct access also buys a property the daemon route does not.
`MANAGER = SessionManager(...)` at
`pare-hardware-mcp/src/pare_hardware_mcp/tools.py:59` is module-global:
**console sessions belong to the worker, not to a client.** So a second MCP
client converges on the same session and the same ring buffer. PARE opens the
console; the operator watches it fill and types into it; PARE's next
`console_read` — cursor-based off that shared buffer — sees the operator's
command and the device's answer as ordinary capture. Hands-on work becomes
evidence in the RE loop without anyone wiring it there.

The cost is a second unauthenticated client on the tailnet. That cost was
already priced in and accepted, deliberately, by
`docs/superpowers/specs/2026-09-05-networked-workers-design.md` — D2 (the
network is the trust boundary, no tokens, no TLS), D3 (bind a named interface,
never a wildcard), and `:411` (Tailscale ACLs as the control that fits this
deployment better than tokens). That spec also already names the consequence
this design inherits: "**The audit log stops being complete**" (`:376`). §5 is
the answer to that, and it is the only part of the accepted cost this design
declines to simply accept.

### 3.2 Why operator keystrokes are not gated

`workers.yaml:113-118` states why the hardware floor was raised to `high`: a
tool that failed to advertise a wire tier — "a half-wired dev build, or a
tampered one under-reporting" — would have "DISPATCHED WITH NO PROMPT AT ALL,
bypassing every approval surface," and "A floor of high means the worst case is
a prompt, not a silent flash write."

The threat being contained there is **the model** dispatching through a worker
whose self-report cannot be trusted. The networked-workers spec states the same
principle from the other side (`:355`): PARE "never derived authority from the
worker."

An operator typing at a UART prompt is not that caller. Gating those keystrokes
means prompting the operator to approve the key they just pressed — a tautology,
and at a prompt per line, an unusable shell. It is also actively harmful: it
trains the operator to approve reflexively, which degrades the gate for the
calls that *are* the model's.

So: **`console_send` from a pane is not gated. It is logged (§5).** Nothing
about the model's path changes.

### 3.3 The UART pane

**Attach, do not own.** On open the pane calls `console_status()` and attaches
to the live session. If there is none it says so and offers `console_open` as an
explicit operator action — never on a timer, and never implicitly as a
side-effect of the pane being visible.

**Reading** polls `console_read(session, cursor, limit)` with an advancing
cursor (`tools.py:149`).

**Requirement R1 — the pane must render the honesty fields, not just `data_b64`.**
`console_read` returns `dropped`, `capture_gaps`, `remaining`, `alive` and
`limit_applied` alongside the bytes, and each exists because concatenating the
bytes alone would misrepresent the capture:

- `dropped` — bytes evicted by ring wraparound.
- `capture_gaps` — `tools.py:181-186` explains it precisely: `dropped` "cannot
  see a capture that was SUSPENDED (a baud scan pausing the reader), because
  that leaves no byte range in cursor space at all, only a hole in time," and
  `capture_gaps` exists "so this byte-contiguous stream does not read as an
  unbroken one." A pane that ignores it displays a lie. Gaps render as a visible
  break in scrollback.
- `limit_applied` / `remaining` — `tools.py:189-195`: how a caller learns a
  clamp happened rather than seeing a short read as an empty buffer.
- `alive` — a closed session must stop looking live.

**Requirement R2 — the poll interval is measured, not chosen.** It is set from a
real round-trip measurement against the deployed worker over the tailnet. The
networked-workers spec records tailnet latency samples for frida (`:522`); the
equivalent number for this worker does not exist yet because the worker is not
deployed (§6). The spec does not pick one.

**Writing** is `console_send(session, data_b64)`, **line-at-a-time in v1**:
local line editing in the pane's input, sent on Enter. Raw per-keystroke mode is
the better console, but it is a request per character to the Pi, and echo
already returns on the poll interval rather than on the keypress — so character
mode would feel *worse* than line mode until the read path is push-based. The
pane interface (§3.4) keeps the write path swappable so this can change without
touching the pane dock.

**Untrusted bytes.** UART output is arbitrary bytes from an untrusted target —
the worker's own contract says so (`contract.py:79-80`), which is why it returns
base64. The pane renders it as such: control characters and escape sequences are
neutralised before display. A target that emits terminal escapes must not be
able to repaint the operator's UI.

### 3.4 The pane interface

The UART pane is the first implementation of a small interface, so mitm and
Frida panes are later additions rather than a rewrite. Specify, do not
pre-build: a pane declares how it attaches to a source, how it produces the next
slice of output given its own cursor state, how it reports source health, and
whether it accepts input. The pane dock owns lifecycle, focus and layout and
knows nothing about MCP.

Only the UART pane is implemented in v1. The interface earns its place by being
what makes R1's honesty fields a pane-level concern rather than a UART-specific
hack — a flow list has its own equivalent of "you are not seeing everything."

---

## 4. The chat surface

Message handling is a dispatch over the registered protocol types. Beyond the
port in §2.2:

- `ToolProgressMessage` renders inline in the transcript. The sanitisation in
  `cli.py:31-46` (`_CONTROL_CHARS`, the 200-char per-arg clip, the 1000-char
  total clip) is ported — tool arguments can contain attacker-influenced bytes.
- `ToolApprovalRequestMessage` raises the modal (I2). The decision is returned as
  `ToolApprovalResponseMessage`; the daemon routes it by `proposal_id` through
  `_route_approval_response` (`daemon.py:176`). The modal must offer the same
  decisions the CLI does, including that `critical` cannot be session-approved
  (`risk_pool.py:414`) and requires a typed justification.
- `LearningCandidateProposalMessage` and unknown types render with a visible
  fallback rather than being dropped, mirroring `_default_format`'s
  `[unrendered …]` (`cli.py:115`).

---

## 5. Logging pane activity

**The TUI must not write the capture store directly.** `pare/capture_store.py:59-69`
takes `fcntl.flock(LOCK_EX | LOCK_NB)` on `.pare/daemon.lock`, held for process
lifetime, and refuses a second holder outright: "another PARE daemon holds …;
refusing to share a capture store." The lock exists so a second writer "fails
loudly instead of racing FTS writes" (`capture_store.py:5-8`). The daemon holds
it.

So pane activity is sent to the daemon, which writes it through the store it
already owns. This restores the completeness the networked-workers spec gave up
(`:376`) — not by gating, but by making the operator's own console traffic part
of the same record as the model's.

**Mechanism.** A PARE-local message type in a new `pare/protocol.py`, registered
with `@register_message` (`agent_core/protocol/__init__.py:17`), carrying what
the operator sent and what the pane observed, with its session and cursor range.
`PareAgent` gains a `handle_other` override — it has none today — which writes
the record through its bound capture store. This is the pattern PAL already
uses: `pal/protocol.py` registers its custom types the same way (`grep -c register_message pal/protocol.py` for how many).

**Requirement R3 — the handler must not block the read loop.** `daemon.py:136`
awaits `handle_other` **inline in the connection's read loop**, not as a task —
unlike `ChatMessage` and `CommandMessage`, which are dispatched with
`asyncio.create_task` (`daemon.py:119-126`). A slow log write therefore stalls
every subsequent read on that connection, *including*
`ToolApprovalResponseMessage` routing — so a slow logger could deadlock an
approval. The handler hands off to a task or an in-process queue and returns
immediately.

**Requirement R4 — logging failure must not silently drop the record.** A write
that fails is surfaced in the status bar. A complete-looking trail with silent
holes is worse than a visibly broken one.

---

## 6. Prerequisite: deploy the worker to the Pi

**Verified on the device, 2026-09-18** (`ssh pare@100.97.133.126`):

| | |
|---|---|
| Tigard | attached — `/dev/serial/by-id/usb-SecuringHardware.com_Tigard_V1.1_TG1119e7-if00-port0` → `ttyUSB0`, `…-if01-port0` → `ttyUSB1`; FT2232H on bus 002 |
| `pare` user | in `dialout` (gid 20); `/dev/ttyUSB*` are `crw-rw---- root dialout` |
| `pare_hardware_mcp` | **not installed** — `ModuleNotFoundError`, no venv under `/home/pare` or `/opt` |
| services | `pare-bench-kiosk`, `pare-bench-status` only. No worker unit |
| listening | `127.0.0.1:8080` (status page) and tailscale's own ports. Nothing serving MCP |

Re-read with: `ssh pare@100.97.133.126 'systemctl list-units --type=service --all | grep pare; ls /dev/serial/by-id/; ss -lntp'`

The UART pane cannot work until the worker serves over HTTP from the Pi. That
move is already the declared intent — `workers.yaml:105-109` says the worker
"is designed to run on pare-bench (the Pi) as `transport: streamable_http` with
an endpoint," and the worker's own `config.py:3-10` is written for that
transport. It is wiring, not a build:

1. Install the package on the Pi with a venv.
2. A systemd unit running `run_worker` bound to **`tailscale0`** — the named
   interface, never a wildcard (networked-workers D3; `pare_worker_kit/serve.py:168-173`
   warns on a bind that widens the surface). `Environment=` is the **only**
   channel for `PARE_HW_DEVICE` and `PARE_HW_EXPECT_SERIAL`: `config.py:3-10`
   states that for `streamable_http`, agent_core forwards only `endpoint`,
   `connect_timeout` and `read_timeout` from `workers.yaml`, and "`env` is a
   stdio-only channel and is never plumbed through." The real values are known —
   the by-id path above, and serial `TG1119e7`.
3. Flip the `workers.yaml` entry from `command`/`args` to `endpoint` +
   `connect_timeout` + `read_timeout` + `artifact_root`, keeping `autoload:
   false` (required for a networked worker) and `risk_default: high` unchanged.
4. Update `config.py`'s module docstring, which currently says the worker "is
   still declared `transport: stdio` in workers.yaml" (`:38-39`).

**Open, for the operator at the board:** which of `if00`/`if01` is the UART
channel. `tests/unit/test_tools_status.py:14-15` uses `if01`, but a test fixture
is not authority on the physical wiring, and `PARE_HW_EXPECT_SERIAL` guards
against the wrong *board*, not the wrong *channel*.

This prerequisite may be landed before or alongside the TUI. The TUI's chat
surface, pane dock, and logging path are all testable without it; only the UART
pane's live path is blocked.

---

## 7. What the tests must discriminate

The risk here is concurrency, so the tests are specified by what they must
*distinguish*, not by their assertions. Implementation writes them against the
real modules.

1. **I1 — single reader.** A test must fail if any code path invokes
   `DaemonConnection.chat()`/`command()`/`command_stream()`, or opens a second
   consumer of `receive()`. Interleaved reads corrupt silently rather than
   raising, so this cannot be left to an integration test noticing.
2. **I2 — the modal does not stall pollers.** With an approval modal open and
   unanswered, a pane poller must still complete reads. The discriminating
   observation is poll *progress* while the modal is up, not that the app
   remains responsive.
3. **R3 — `handle_other` does not block.** A deliberately slow log write must not
   delay a `ToolApprovalResponseMessage` sent immediately after it on the same
   connection. This test must be verified **failing** against a naïve
   synchronous handler; a version that passes both ways is testing nothing.
4. **R1 — the honesty fields reach the screen.** For each of `dropped`,
   `capture_gaps`, `alive: false` and a clamped `limit_applied`, a
   `console_read` reply carrying it must produce a visible difference in
   rendered output. Assert the *difference*, not a literal string — a snapshot
   of exact pane text rots on the next styling change.
5. **§2.2 de-duplication.** A `reasoning=off` turn (stream chunks then a
   `ResponseMessage` repeating them) must render its answer **once**; a
   `reasoning=on` turn (a `ResponseMessage` with no preceding stream) must
   render it too. One test catching only one of these leaves the other silently
   broken.
6. **§3.3 untrusted bytes.** A `console_read` payload containing terminal escape
   sequences must not alter anything outside the pane's own region.

Two anti-patterns to avoid, from prior rounds in this project: an assertion on a
magic count (tool counts, pane counts, byte totals) breaks on the next
legitimate change — assert relationships instead; and an assertion whose setup
empties the thing it compares against passes forever while testing nothing.

---

## 8. Not in scope

- **mitm and Frida panes.** The pane interface (§3.4) accommodates them; v1
  implements neither. Note for whoever picks them up: mitm's control API binds
  loopback by default (`pare-mitm-mcp/src/pare_mitm_mcp/config.py:18`), so a mitm
  pane either runs on the mitmweb host or that bind has to change — which is a
  trust-boundary decision, not a config tweak.
- **Raw per-keystroke console mode** (§3.3).
- **Multi-client fan-out in the daemon** (D1). If a second attached client is
  ever wanted, that is a change to `agent_core/daemon.py` and its own design.
- **Retiring `pare-cli`.** It stays as fallback and as the reference client.

---

## 9. Code in this document

There is none, deliberately. This design is concurrency and a trust boundary:
three task families that must not block one another, a handler that runs inline
in a socket read loop, and a deliberate decision about which caller a security
control applies to. Code written into prose here would be unexecuted, remote
from the real files, and authoritative-looking enough to be transcribed
faithfully. §2.1, §3.3 and §5 give the invariants, the failure modes and the
exact source lines that constrain them; the implementer writes the code against
the actual codebase, and §7 says what the tests must tell apart.
