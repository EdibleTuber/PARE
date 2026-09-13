# pare-hardware-mcp — design

**Date:** 2026-09-12
**Status:** v1 — designed, not implemented
**Depends on:** `2026-09-05-networked-workers-design.md` (transport, liveness)
**Relates to:** `2026-09-12-artifact-wiring-design.md` — **phase 1 does not depend on it**

Step 5 of the ArcticBase/artifacts design. `/mnt/secondary/projects/pare-hardware-mcp`
is an empty directory; this is the spec §15 says it gets.

## 1. What is on the bench, read on 2026-09-12

| | |
|---|---|
| Host | Raspberry Pi 5 Model B Rev 1.1, Ubuntu 26.04.1, Python 3.14.4, `pare` uid 1000 |
| Adapter | **Tigard V1.1**, FTDI FT2232H (`0403:6010`), serial `TG1119e7` |
| Channels | `if00` → `/dev/ttyUSB0`, `if01` → `/dev/ttyUSB1`, both with stable `by-id` symlinks |
| Access | `pare` is in `dialout`, `spi`, `i2c`, `gpio`, `plugdev`; opened `ttyUSB0` at 115200 with no udev work |
| Present | `flashrom`, `pyserial` |
| Absent | `openocd`, `pyftdi`, **`mcp`** — the worker needs its own venv |
| Network | `tailscale0` = `100.97.133.126`; port **8770 free**. (Also listening, for completeness: 631 CUPS-via-snap on `0.0.0.0`, 8080 status page on loopback, systemd-resolved's stubs on `127.0.0.53`/`127.0.0.54`, and tailscale's own ports.) |
| Artifact drive | `/mnt/bench-store`, 116 GiB free, sentinel `0361c41f-e680-4d4e-b9c3-39af8a33d067` |
| Target | a board with a UART console, in hand |
| Power switching | **DSD TECH SH-UR04A**, 4-channel USB relay, arriving 2026-09-13 |

## 2. Scope, in phases

**Phase 1 — the console.** UART only. No artifacts, no flash, no JTAG.

**This phase depends on none of the artifact wiring.** A console returns *results*,
not artifacts, so it touches neither `open_artifact`, the descriptor contract, nor
the dispatch changes. It can ship while that work is still in flight. An earlier
reading of §15 assumed the flash dump would come first and therefore that the
hardware worker was blocked; choosing the console first removes the dependency
entirely.

**Phase 2 — target power.** The relay lands 2026-09-13. §7.

**Phase 3 — flash.** SPI dump via `flashrom`'s `ft2232_spi`. This is the phase that
consumes the artifact contract, and it reintroduces every long-call hazard phase 1
does not have. Not specified here beyond the constraints it inherits (§9).

**Out of scope entirely:** JTAG/SWD (needs `openocd`, not installed), glitching,
and any tool that interprets target state.

## 3. Decisions

**B1 — The port is addressed by `by-id`, and the serial number is asserted — but
the expected value does *not* live in the trust anchor, and that is a real
weakening.**

An earlier draft said the expected serial lives in "`workers.yaml`-adjacent worker
config". **There is no such place for a networked worker.** `WorkerSpec` sets
`model_config = ConfigDict(extra="forbid")` (`types.py:64`), and `workers.yaml:3-7`
records the consequence: one unknown key aborts the whole file, leaving no workers
*and* no `risk_overrides`. And `MCPClient.from_spec` (`client.py:100-113`) passes
`command`/`args`/`env`/`cwd` **only on the stdio branch** — the HTTP branch
forwards `endpoint` and the two timeouts and nothing else. No operator-declared
value reaches a networked worker today.

So the device path and expected serial live in `Environment=` in the systemd unit
**on the Pi**, next to the code that checks them, under the same uid. That is
weaker than `artifact_root`, which is deliberately in the file the worker cannot
touch (`types.py:112-116`), and the difference must be stated rather than implied:
this defends against a wrong cable, a replugged adapter and a second FTDI device.
It does **not** defend against a compromised worker, which asserts a serial
against a value it controls. Closing that gap means a new `WorkerSpec` field and
plumbing it through the HTTP branch — an `agent_core` change this phase does not
make.
`ttyUSBn` is assigned by enumeration order and is not stable across reboots or
across a second adapter appearing. `workers.yaml`-adjacent worker config names the
`by-id` path; the worker additionally asserts the resolved device's
`ID_SERIAL_SHORT` matches what it expects. A second FTDI adapter on the bench must
not be able to become "the console" by enumerating first.

This stops being a nicety in phase 2: if the relay is CH340-based it enumerates as
another `/dev/ttyUSB*`, giving the Pi three serial nodes whose numbering depends on
which device the kernel saw first.

**B2 — Capture starts at `open`, not at `read`.**
On an unknown board the boot log is the most valuable output and it arrives before
anyone can ask for it. A worker that begins capturing when the model calls a read
tool has already missed the thing worth having. The ring buffer is the worker's
core state.

**B3 — `read` never blocks. (But `detect_baud` does, so "phase 1 holds no request
open" is false as an unqualified claim.)**
`hardware_console_read` returns whatever is buffered *now*, including nothing. This
is deliberate and it is what keeps every `read_timeout` hazard in the artifact spec
out of phase 1 **for `read`**: a timed-out call that keeps running worker-side, no
cancellation on the wire, a retry racing the first attempt.

**`console_detect_baud` is the exception and is phase 1's longest call.** It
samples the line at each candidate rate (B7, B8), so it holds a request open for
roughly `sample_duration × candidate_count`, and B9's quiet-line case has no early
exit — it is slowest exactly when it finds nothing. The spec must therefore bound
it: a stated candidate list, a stated per-rate sample duration, and a total budget
that is **derived from and checked against** `read_timeout`, not asserted to fit
it. Two implementers given no budget will choose differently and one will exceed
the timeout.

Phase 2 has the same shape: `power_cycle` holds a request open across the
off-wait-on interval. Phase 3 reintroduces the rest (§9).

**B4 — The port is exclusive; a second `open` is refused, not queued.**
Serial ports do not multiplex. The refusal names the holder and when it opened. A
queue would mean one tool call blocking on another operator's session.

**B5 — `open` is tier `medium`, because opening a port is not a read-only act.**
Opening asserts DTR and RTS, and many boards are wired to reset on DTR. "Just
looking" can reboot the target. The worker sets DTR/RTS explicitly on open rather
than inheriting the driver default, and the tool result states which it did.

**B6 — `console_send` is tier `high`, pinned as `hardware_console_send`, and sits
outside the flash pin patterns.**
The existing pins were written for flash: *"anything that writes to a target you
cannot un-write"*. A console keystroke is not persistent — except when it is, at a
bootloader prompt. `high` prompts but is session-approvable (`risk_pool.py:414`),
which is exactly the branch that exists for interactive work; `critical` would mean
a typed justification per keystroke, and the realistic outcome of that is the
operator abandoning the tool for `picocom`. The tier encodes **persistence**, not
direction.

**The accepted risk is larger than an earlier draft stated, and the correct
statement comes from the key.** `record_session_approval` stores
`(worker, tool, generation)` and `is_session_approved` looks up the same triple
(`risk_pool.py:259-267`) — **not the arguments, not the device, not the console
session.** So one `a` at the prompt means:

- every subsequent `console_send` runs unprompted **with any payload**, not the
  one the operator saw;
- the model may `console_close` (low) and `console_open` (medium) a **different**
  `by-id` device and send to *that* target under the approval granted for the
  first;
- it persists for the life of the daemon generation, across close/open cycles —
  not for the console session, which is how the draft framed it.

What does re-prompt: a generation bump, from `add_spec`/`remove_spec`/`close_all`,
or `_bump_on_link_loss` (`risk_pool.py:182-206`) on a transport error — which for
a networked worker fires on any bench-link blip. That is a real mitigation and it
cuts both ways: a flaky tailnet produces repeated re-approval prompts, which is
the same habituation pressure §7.1 is trying to measure.

Mitigations available and **not** adopted in phase 1, recorded so the choice is
visible rather than accidental: expiring session approval at `console_close`,
bounding `send` payload length, or making `send` `critical` while phase 1 has no
bulk-write path anyway.

**B7 — Baud is detected, and detection returns evidence rather than a verdict.**
An earlier draft of this spec deferred autobaud as "a named follow-up" on the
grounds that a quiet line cannot be sampled. That let the uncommon case veto the
common one, and it was wrong: the case you most want detection for — an unknown
board being brought up — is the case with abundant traffic.
`hardware_console_detect_baud` returns a ranked list of
`(rate, printable_ratio, framing_errors, sample)`. A wrong baud produces
*plausible garbage*, not an error, which is precisely why the scores must travel
with the answer: the caller can see an ambiguous result instead of being handed a
confident number.

**B8 — Detection changes speed on the held fd; it does not close and reopen.**
Reopening re-asserts DTR (B5), so a scan implemented as reopen-per-rate reboots a
DTR-reset board once per candidate — and the target never stays up long enough to
produce a clean sample. Change the speed via termios on the open descriptor.

**B9 — A quiet line is reported as "no data at any rate", never as a best guess.**
The honest answer names the remedy: power-cycle the target and detect during boot,
which is phase 2's first payoff.

**B10 — The worker depends on `mcp`, `pare-worker-kit` and `pyserial`. Never
`agent_core`.** That is the kit's whole reason to exist. Measured previously: a
bare `agent_core.workers.risk` import loads 21 modules and would pull
`trafilatura`, `markitdown[pdf,docx,pptx,xlsx]`, `rich` and `prompt-toolkit` onto
a Pi for a constant.

## 4. The tool surface

**Contract names are bare. The daemon adds the prefix.** `tool_factory.py:52`
builds `prefixed = f"{worker.name}_{tool_name}"` from the worker's key in
`workers.yaml`, and `risk.py:126` builds the pin-match target the same way.
`pare-frida-mcp/contract.py:37` therefore declares `ToolSpec("list_devices", ...)`
and the operator writes the pin as `frida_execute_script`.

An earlier draft of this table named the tools `hardware_console_send`,
`hardware_list_devices` and so on. **That would have produced
`hardware_hardware_console_send` on the wire, and the one pin protecting the one
dangerous tool in phase 1 would have matched nothing — with no error anywhere**,
because `RiskGate` validates the tier string and never checks that a pattern
matches something.

| Contract name | Dispatched as | Wire tier | Returns |
|---|---|---|---|
| `list_devices` | `hardware_list_devices` | low | adapters present, by `by-id` path and serial |
| `bench_status` | `hardware_bench_status` | low | artifact root present/writable, drive id, device presence — see below |
| `console_detect_baud` | `hardware_console_detect_baud` | low | ranked `(rate, printable_ratio, framing_errors, sample)` |
| `console_open` | `hardware_console_open` | medium | session id, resolved device, baud, DTR/RTS state set |
| `console_read` | `hardware_console_read` | low | `(bytes, next_cursor, dropped, remaining)` — takes `(cursor, limit)`; see §7a |
| `console_send` | `hardware_console_send` | high | bytes accepted; **pinned** in `workers.yaml` |
| `console_status` | `hardware_console_status` | low | open?, holder, buffer head, cursor lag, session age |
| `console_close` | `hardware_console_close` | low | releases the port |

**`bench_status` is not new work invented here — a consumer is already waiting.**
`pare/commands/health.py:130-151` says in situ: *"§8.4 puts the live answer behind
a low-tier `bench_status` tool on the worker, which does not exist yet."* Phase 1
is the first worker that could provide it, so it does.

Every tool advertises its tier over the wire, and the conformance suite rejects a
tool that fails to (`_assert_valid_risk_tier_meta`). Every tool declares
`produces: result`; nothing in phase 1 produces an artifact.

## 5. State, lifecycle and failure modes

**The ring buffer.** Capture begins at `open`. `read(cursor)` returns a slice and a
next cursor. `dropped` is the count of bytes lost because the ring wrapped past the
caller's cursor, and it is **never silently zero** — a boot log with a hole that
looks complete is worse than an error, because the model will reason confidently
across the gap. Buffer size is worker configuration, not a per-call argument, and
the default must hold a full boot log for the class of target in use.

**Device disappearance.** The adapter can be unplugged and this bench has a
documented history of USB instability. A read against a vanished device returns a
distinct error naming the `by-id` path that no longer resolves. It does **not**
hang, and it does **not** return an empty slice — which would be indistinguishable
from "the target is quiet". The session is marked dead and must be reopened; the
buffer stays readable, because what was captured before the unplug is still
evidence.

**Worker restart with a port open.** The OS releases the descriptor, so the port
frees itself, but the buffer is gone. `open` after a restart is a new session with
a fresh cursor, and `status` must make the loss of history obvious rather than
presenting an empty buffer as a quiet target.

**The bench clock.** The Pi 5's RTC has no battery. Measured on 2026-09-12: the
kernel started with the clock reading 2026-07-27, the RTC then set it to
1970-01-01, and chronyd stepped it by 4 038 561 s about 21 seconds into boot. So
**every boot has a ~21 s window in which wall-clock time is wrong by ~47 days.**
Phase 1 timestamps are session metadata and can tolerate it; phase 3's `hashed_at`
is a custody record and cannot. Fitting the RTC battery fixes it at source. The
bench status page already compares Pi time against the workbench host with a 60 s
tolerance, so the condition is detectable where it matters.

## 6. Transport and deployment

**`streamable_http`, bound by interface name.** The unit sets
`AGENT_WORKER_HOST=tailscale0`, not a literal address. `resolve_bind_address`
refuses every spelling of a wildcard — the bind address *is* the access control on
a deployment with no application auth — and it resolves an interface name itself,
so the value validated is the value bound. Using the interface name survives the
tailnet address changing and **fails closed when tailscaled has not come up**,
which on 2026-09-12 was not hypothetical.

Port **8770**.

**Its own venv**, `/opt/pare-hardware/venv`: `mcp` is not installed system-wide on
the Pi and should not be.

**The systemd unit lives in PARE**, at `bench/systemd/pare-hardware-mcp.service`,
because `scripts/bench_deploy.sh` is what places units on the Pi.

**But that script cannot deploy this worker as it stands, and an earlier draft
claimed otherwise.** Three separate gaps, all verified in the script:

- **`FILES` is a hardcoded five-entry array** (`bench_deploy.sh:26-32`). The new
  unit must be added to it, or `--check` reports *"Everything deployed matches"*
  while the unit is absent. Cheap — one line — but not automatic.
- **There is no venv or pip logic anywhere in the repo.** A grep across `scripts/`
  and `bench/` finds only comments explaining that the status page deliberately
  has none. The script's whole model is
  `sha256(repo file) == sha256(deployed file)` → `install -D`. It cannot express
  "create `/opt/pare-hardware/venv` if absent and install a pinned requirement
  set". That is new functionality, not a parameter.
- **It has no concept of a second repo.** `REPO` is a single checkout and every
  `FILES` source is `$REPO/<path>`. The worker package lives in
  `pare-hardware-mcp`, so the script cannot hash it, diff it, or stamp it.

**The provenance claim therefore needs narrowing.** `/opt/pare/DEPLOYED_FROM`
records PARE's HEAD (`bench_deploy.sh:100-104`) — observed on the Pi:
`a9a4ef7  deployed 2026-09-11T20:49:20Z`. That answers *"which commit of PARE
shipped the unit file"* and says nothing about the commit inside
`/opt/pare-hardware/venv`, which is the code that actually drives the target. The
deliverable is therefore an extended stamp recording the hardware-worker version
and the `pare-worker-kit` version installed in that venv — not just PARE's SHA.
Until that exists, the bench's provenance trail has a hole exactly where the
hardware is.

**Unit directives, which an earlier draft omitted entirely and which are not
optional here.** `run_worker` raises `SystemExit(2)` on a `WorkerServeError`
(`serve.py:275`) with no retry, and systemd's default is `Restart=no` — so a unit
without explicit directives fails **once** and stays dead until a human
intervenes. That is worse than a crash loop, and it is guaranteed to happen: on
this Pi `tailscale0` does not get its IPv4 until after chronyd steps the clock
(§5), so a worker started early in boot finds no address and exits.

The unit needs `Restart=on-failure`, an explicit `RestartSec`, and
`StartLimitIntervalSec=0` under `[Unit]` — the pattern
`bench/systemd/pare-bench-status.service` already uses, with a comment explaining
that a crash loop must not be the reason the screen is blank.

For ordering, **`After=` and `Wants=tailscale-online.target`**, not
`After=tailscaled.service`: the latter waits only for the daemon process to exist,
not for it to have an address, which is precisely the gap that produced today's
outage. `tailscale-wait-online.service` (`ExecStart=/usr/bin/tailscale wait`,
`WantedBy=tailscale-online.target`) is **already installed on this Pi and
currently disabled** — enabling it is the intended mechanism, not a workaround.

**`workers.yaml` changes on four axes together:**

```yaml
  hardware:
    endpoint: http://100.97.133.126:8770/mcp   # was: command: <local path>
    transport: streamable_http                 # was: stdio
    risk_default: high                         # UNCHANGED — §7.1
    autoload: false                            # unchanged, and deliberate
    connect_timeout: 20
    read_timeout: 30
    capability_tags: [hardware, uart]          # not jtag/glitch — §2 puts both
                                               # out of scope; tags are operator-
                                               # facing (commands/worker.py:164)
                                               # and should not advertise
                                               # capabilities that do not exist
```

`autoload: false` stays: hardware work is occasional and tool schemas cost context
in every turn that is not doing it.

**`read_timeout: 30` is correct for phase 1 and wrong for phase 3.** Nothing in the
console surface holds a request open (B3). A flash dump holds one for ~70 s at the
bench's measured ~28 MB/s, and the artifact spec establishes what a client-side
timeout does: `McpError` raised locally, no cancellation sent
(`mcp/shared/session.py:290-303`), the worker finishing and renaming underneath the
daemon, and a retry that can destroy the good file. Phase 3 revisits this value and
must not inherit it silently.

## 7. Reconciling the risk model

**An earlier draft of this section made two changes together and was wrong about
both.** It dropped the floor from `high` to `low` and removed all four
forward-declared pins. The corrected position keeps the floor and keeps most of
the pins, and the reasoning matters more than the conclusion.

### 7.1 The floor stays `high`, and the transport change ships alone

The draft argued that once the worker exists, the build-time conformance
assertion replaces the floor. **It does not, for a reason that is checkable:**
`assert_stdio_conformance` and `assert_streamable_http_conformance`
(`conformance.py:173`, `:277`) have **no production caller** — a search across all
five repos outside tests returns only their definitions. They run in CI, in a
different repo, against whatever that repo built. Nothing runs them against the
process actually answering on port 8770.

The frida comparison was not like-for-like either. `_is_local`
(`risk_pool.py:167-180`) states the difference directly: for stdio *"the daemon
spawned the child and holds its pipe, so the process behind a connection cannot
change without a reload"*; for anything networked *"a Pi can reboot, a systemd
unit can restart, a container can be redeployed, and none of it reaches the
daemon."* Frida runs at `low` **because it is stdio**. `hardware` would be the
first worker at `low` where the kernel guarantees nothing.

Worse, the draft made both changes in one edit. **The transport change is what
removes the identity guarantee; the floor change is what removes the compensating
control for not having it.** They are independent decisions and they ship apart:

1. **Now:** `transport: streamable_http`, floor **stays `high`**.
2. **Later, separately:** measure how bad the prompt wall on `console_read`
   actually is in practice, with the pin mechanism in §7.2 genuinely enforced,
   and lower the floor then if the evidence supports it.

The cost is real and is accepted: at floor `high`, **every** phase-1 tool prompts,
including `console_read`. Polling a boot log is a wall of approvals, and
habituation to approving is itself a security failure. That is the thing to
measure before trading it away — not to trade away first and measure after.

One consequence of keeping the floor: B5's argument that `console_open` should
prompt actually holds. `medium` never gates on its own (`risk_pool.py:413` tests
only `high`/`critical`), so at floor `low` the tool the spec says can reboot a
target would dispatch silently. At floor `high` it prompts.

**Testing note:** `_floor_highwater` is a per-worker ratchet that is never evicted
(`risk_pool.py:231-244`), so a floor change takes effect on a fresh daemon, not on
a `/worker reload`. Test it in a fresh daemon or you will misread the result.

### 7.2 Pins are live runtime policy, not forward declarations

The draft removed all four `hardware_*` pins on the grounds that they "have been
warning, not checking". **That conflates the coverage test with the control, and
the control was never the test.**

`RiskGate.evaluate` (`risk.py:126-141`) fnmatches every pin against
`f"{worker}_{tool}"` **at every dispatch**, consulting no registry of known tools.
`hardware_flash_*: critical` therefore binds a `hardware_flash_dump` the instant
one appears on the wire, from any source, with nobody having done anything. That
is the strongest control in the system for a tool PARE has never seen — and the
daemon registers a Tool class for every entry `tools/list` returns
(`discovery.py:96-105`), with no allowlist of expected names anywhere.

`tests/test_risk_overrides_coverage.py` is a *spelling* check on pins. It being
broken is a reason to fix it, not a reason to delete what it failed to check.

**It is also a reversal of two decisions this spec failed to cite:** the
networked-workers design's **D5** (*"Destructive tools get operator pins, from day
one… That must be fixed before the hardware worker ships, not after"*) and the
ArcticBase design's **§15 step 0** (*"Prerequisite — the pins… This lands first or
nothing else matters"*). Reversing those needs an argument stronger than a broken
test, and there isn't one.

**What actually changes, and why it is two pins rather than four.** Phase 1 adopts
a subsystem-first naming scheme (`console_send`, and later `flash_dump`,
`flash_write`, `glitch_inject`). Under that scheme:

| Pin | Fate | Why |
|---|---|---|
| `hardware_flash_*` | **kept** | matches every future flash tool, including `hardware_flash_write` |
| `hardware_glitch_*` | **kept** | matches every future glitch tool |
| `hardware_write_*` | **removed** | can never match: a flash write is `hardware_flash_write` |
| `hardware_erase_*` | **removed** | can never match: an erase is `hardware_flash_erase` |

Two pins are deleted because the naming scheme makes them **unmatchable**, not
because they were unchecked. The two that remain keep binding tools that do not
exist yet, which is exactly their job.

Added: `hardware_console_send → high`.

### 7.3 Making the coverage test honest — which needs more than a dict entry

The draft said phase 1 "adds `hardware` to `_CONTRACT_MODULES`". **That alone does
nothing.** `_tool_targets()` (`tests/test_risk_overrides_coverage.py:44-51`) does
`importlib.import_module(module)` inside `except ImportError: continue`. The
hardware worker's package lives in a venv **on the Pi** (§6); it is not importable
in PARE's environment, so `hardware` never enters `installed`, every
`hardware_*` pin falls back to `unchecked`, and the run stays green with a
`UserWarning` — identical to today.

For the entry to mean anything, **PARE's own environment must be able to import
`pare_hardware_mcp.contract`**. PARE's CI already installs the three worker
contract packages explicitly from git; this needs the same treatment, which
implies:

- a published `EdibleTuber/pare-hardware-mcp` repo,
- a `contract.py` exposing `TOOL_SPECS`, importable **without** `pyserial` or any
  hardware present, so it installs on a CI runner,
- an added line in PARE's CI install step.

**Sequencing hazard, stated so it is not walked into:** if the pin edits land in
PARE before that repo exists, the "enforced invariant" is silently absent — the
precise failure §7 exists to fix. The pin edits and the CI install land together
or not at all.

## 7a. Obligations inherited from the parent designs

These are not new requirements. Each was placed on *this* worker by a design this
spec depends on, and an earlier draft omitted every one of them.

**Worker-side request logging — the networked-workers design calls it Required.**
Over stdio, PARE's audit log is a total record of what the worker did, because the
daemon is the only thing that can reach it. Over HTTP it is not: anything on the
tailnet can call the worker directly and PARE never sees it. That design's §6 puts
the remedy in `run_worker` — peer, tool, timestamp, argument hash — and **it is not
implemented**: there is no logging of any kind in `pare-worker-kit/src`. This is
the worker where *"after a bricked target, the operator cannot establish whether
PARE did it"* stops being rhetorical, so phase 1 either implements it in the kit
or states plainly that the audit trail is incomplete. It does not get to omit it.

**A payload bound on `console_read`, with an existing precedent to copy.** §5
requires the ring buffer to hold a full boot log, and §4's `console_read` takes
only a cursor — so one call returns the entire boot log in a single
`CallToolResult`, into model context and the capture store. `frida_read_hook_events`
already solved this shape: `since_seq` plus a limit, and a count of what remains
buffered (`pare-frida-mcp/contract.py:150-153`). `console_read` takes the same
form: `(cursor, limit)` returning `(bytes, next_cursor, dropped, remaining)`.
`remaining` is what lets the model decide whether to keep draining without
guessing.

**`console_send` is undispatchable without an operator attached.** `_await_operator`
fails closed when no send channel is resolvable (`risk_pool.py:452-458`), and the
parent's **D8** puts the approval surface at `pare-cli` running **at the bench**.
So §10's Level 3 "from a PARE conversation" is not precise enough: the conversation
must be one with a live CLI the operator is sitting in front of, at the bench, or
every `high` tool blocks. Any unattended use of phase 1 is limited to the
low-tier read path — and at floor `high` (§7.1) even that prompts, so **phase 1 is
an attended-only worker by construction**. Better to say that than to discover it.

**`RequiresMountsFor` must not be used on the worker unit.** The parent's §8.4 is
explicit, and it matters now rather than later because phase 1 writes the unit
phase 3 inherits: a mount dependency turns an absent artifact drive into a unit
that never starts, instead of a worker that starts and reports the drive missing.

**`serverInfo` is the only provenance a networked worker has, and it needs naming
care.** `stamp_version` (`pare-worker-kit/src/pare_worker_kit/serve.py`) resolves
the version from installed package metadata **keyed by the FastMCP instance's
name**, so the server must be named after its distribution and installed with
metadata or the version silently resolves to nothing. Note the limit honestly:
`client_pool` records `serverInfo` per connection and **never compares it to an
expected value**, so a redeploy at an unchanged version is invisible and a swapped
worker is not detectable from the daemon side today. That is the gap §7.1's floor
exists to bound.

**`console_read` will trip PARE's spin guard, and that is a PARE change this phase
owns.** `POLL_TOOLS` (`pare/handback.py:19`) is a hardcoded frozenset of exactly
two frida tools, and `RepeatGuard` hard-blocks after three identical (call,
result) pairs. Waiting for a board to boot — the primary phase-1 workflow, and the
remedy B9 names for a quiet line — issues identical `console_read(cursor=N)` calls
returning identical empty results, and gets blocked. `hardware_console_read` joins
`POLL_TOOLS`. One line, and without it the worker's main loop does not work.

## 8. Phase 2 — target power

The relay arrives 2026-09-13. Three properties must be **confirmed on arrival**
rather than designed around, because the listing states none of them:

1. **Control protocol.** DSD TECH boards are commonly CH340-based with a simple
   serial command set. If so it enumerates as another `/dev/ttyUSB*` — see B1.
2. **Power-on default state.** If the Pi reboots or USB re-enumerates, does the
   relay come up open or closed, and does it glitch during enumeration? An
   unplanned cycle during a flash write is how targets are bricked.
3. **Contact ratings** against the target's supply and inrush.

**Wiring is a decision, not a detail.** Normally-open means the target
de-energises if the Pi dies — safe for leaving a board unattended. Normally-closed
means a Pi reboot does not interrupt a long operation. For phase 3's unattended
dumps these pull in opposite directions and the choice must be recorded with its
reason.

**The relay is open-loop: the worker knows what it commanded, not what happened.**
Tigard's VTGT pin is a voltage reference from the target, so reading it can close
the loop and let `hardware_power_cycle` *verify* rather than assert. Whether V1.1
exposes that readably is an open question for the schematic — Tigard is open
hardware, so it is checkable rather than a matter of opinion.

**Tigard can supply the target but probably cannot switch it.** The voltage
selector is a mechanical slide switch; no software control is known. If the target
is powered from Tigard, cycling it means cycling Tigard's own USB, which is what
the relay is for.

Tools: `hardware_power_on`, `hardware_power_off`, `hardware_power_cycle`. Tier
`high` at minimum — cutting power mid-write can brick a target — and each gets a
pin under the rule in §7.

## 9. What phase 3 inherits, stated now so it is not rediscovered

- **The artifact contract**, which phase 1 does not touch: `artifact_root`,
  `artifact_drive_id`, `artifact_host`, `open_artifact`, the descriptor shape, and
  the daemon-side validation. All specified in the artifact-wiring design.
- **`read_timeout` (§6)**, and the `RENAME_NOREPLACE` backstop that makes a wrong
  bound survivable.
- **`hashed_at` versus the bench clock (§5).**
- **`artifact_drive_id` = `0361c41f-e680-4d4e-b9c3-39af8a33d067`**, read off the
  drive on 2026-09-12 — the one value here that must never be typed from memory.
- **A flash dump is one encapsulated `critical` tool, never exposed primitives**,
  with an operator pin from day one.

## 10. Testing and acceptance

**Level 1 — no hardware.** A pty pair stands in for the serial device. Covers
cursor arithmetic, ring wrap and the `dropped` count, exclusivity refusal, device
disappearance (close the far end), and baud scoring against recorded samples at
right and wrong rates.

**Level 2 — Tigard, no target.** Loop TX to RX on the Tigard header: the worker
sees its own bytes at the configured rate, `by-id` resolution picks the intended
channel, and the serial-number assertion refuses a different FTDI adapter. This is
what catches "we opened channel A and the UART is on channel B".

**Level 3 — the real board.** `/worker load hardware`, open the console, capture a
boot log, send a command, read the response — **from a PARE conversation**. Not
complete until it has been read there, not merely seen in a test.

**Test discipline.** Every regression test verified failing against the pre-change
code. **No assertion on a literal where a relationship will do**: assert that the
cursor advanced by the number of bytes read, not that it equals 4096.

## 11. Risks

- **B6's accepted risk**: a session-approved console at a bootloader prompt can
  write flash without a fresh prompt.
- **Keeping the floor at `high` means every phase-1 tool prompts, including
  `console_read`.** Polling a boot log is a wall of approvals, and habituation to
  approving is a security failure in its own right. This is the accepted cost of
  §7.1, and it is the thing to measure before trading away rather than after.
- **Two pins are removed because the naming scheme makes them unmatchable**
  (§7.2). If a future tool is ever named `hardware_write_something` rather than
  `hardware_flash_write`, it ships unpinned. The scheme is the mitigation, and a
  scheme is only as good as the discipline of applying it.
- **§7.3's coverage fix depends on a repo that does not exist yet.** Until
  `pare_hardware_mcp.contract` is importable in PARE's environment, the pins go on
  warning. The sequencing hazard is named there; it is still a hazard.
- **Anything that answers on 8770 is trusted.** There is no authentication in the
  path — `MCPClient.connect` builds a plain HTTP transport with no headers, token
  or TLS — and `serverInfo` is recorded per connection but never compared against
  an expected value. The floor and the pins are what bound the damage, which is
  why §7 keeps both.
- **`by-id` assertion depends on the operator configuring the right serial.** A
  wrong serial fails closed (no device), which is the correct direction.
- **The relay's three unknowns (§8)** are unknown at spec time by choice; designing
  around guesses would be worse.

## 12. Provenance

Every fact in §1 was read from `pare-bench` over SSH on 2026-09-12, not recalled.
Line references to `agent_core`, `pare-worker-kit` and PARE were read from the
files the same day. The DSD TECH model number is from the listing the operator
linked; its protocol, default state and ratings are explicitly **not** established
and are marked so in §8.
