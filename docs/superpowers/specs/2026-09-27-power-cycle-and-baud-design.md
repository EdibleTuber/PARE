# Relay power control + cycle-assisted baud detection for `pare-hardware-mcp`

**Status:** design, 2026-09-27. Awaiting spec review before writing-plans.
**Repos touched:** `pare-hardware-mcp` (new tools, relay device, config), `PARE`
(`workers.yaml` risk pins + the if00 default correction), Pi operational state
(systemd unit gains the relay env). `agent_core` untouched.
**Spike:** `docs/superpowers/2026-09-27-uart-spike-findings.md` — every fact below
was measured against the real bench, not inferred.

## The problem, from the bench

The operator can power-cycle the bench target only by hand or by an ad-hoc script
driving the relay directly. To enumerate an unknown target through PARE — set a
baud, look at the boot output, try another — the worker needs to both switch the
target's power and read its console. It can already read the console; it cannot
switch power.

The spike also proved two things the deployed worker had wrong:
- **The target's UART is if00, not if01.** The worker's `PARE_HW_DEVICE` points at
  if01, which is silent; every capture through the worker returned zero bytes until
  a session was opened with an explicit `device=…if00…`. This design corrects the
  default. (Memory `tigard-uart-is-if01`, now corrected.)
- The relay is a **DSD TECH SH-UR04A** (CP2102 bridge, ttyUSB2, 9600 8N1), AT
  protocol: `AT+CHn=1` energises channel n, `AT+CHn=0` releases it, `AT+CHn=?`
  queries it, `AT`→`OK`. The target's `+` lead is on **NC1**, so energising CH1
  cuts power. `AT+BAUD=` must never be sent (it changes the board's own rate).

## 1. Decisions

| | Decision | Why |
|---|---|---|
| **D1** | Relay control lives in `pare-hardware-mcp`, the same worker process that owns the console. | One worker owns the bench hardware. The baud sweep needs both the relay and the console in one place; splitting them across workers would put a network hop in the tight per-candidate loop. |
| **D2** | The relay is a second, independent device declaration: `PARE_HW_RELAY_DEVICE` (a by-path device — the CP2102's serial is a generic `0001`, so by-id is ambiguous), `PARE_HW_RELAY_CHANNEL` (1–4), `PARE_HW_RELAY_POLARITY` (`nc`/`no`). | The relay is a distinct adapter from the Tigard. By-path is the only stable handle for a generic-serial CP2102. Polarity in config means every tool speaks target-power terms, never `AT+CHn=`. |
| **D3** | Tools speak **target power**, not relay channels: `power_status`, `power_set(state)`, `power_cycle(off_ms)`. The `nc`/`no` translation is internal. | A caller (model or operator) reasons about "is the target on," never about which way the relay is wired. Polarity is exactly the kind of detail that causes a wrong-way power event if it leaks into callers. |
| **D4** | All three power tools are risk tier **high**, matching the worker's floor. `power_set`/`power_cycle` are state-changing; `power_status` is read-only but stays high so the whole family is pinned together and a half-wired build cannot ship one ungated. | The `workers.yaml` FLOOR reasoning: RiskAwareToolPool gates only high/critical, and a tool that fails to advertise a tier must still land above the gate. Cutting a target's power is disruptive and belongs behind a prompt. |
| **D5** | The cycle-assisted baud sweep is a **composite tool** that, per candidate rate, sets the UART baud, cycles power, captures the boot burst, and scores it — reusing the existing `baud.score_sample()` (`console_score`) and returning **ranked per-rate evidence** (sample + score), never a bare verdict. | The operator watches the output during initial enumeration (their stated workflow), so the tool surfaces evidence for their eyes; the score is a best-guess ranking. `score_sample` already discriminates real console text from misframed noise and floating grounds — strictly better than a fresh printable-ratio scorer, and already validated. |
| **D6** | The relay and the console are **independent failure domains**. A wedged/absent relay fails the power tools with their own error and never touches an open console session; a wedged console never blocks power control. | The whole point of the bench-doctor and honesty work: one subsystem's failure must not masquerade as another's. |
| **D7** | `power_cycle`'s **resting state is always "powered on."** The off window is time-bounded and the restore runs in a `finally`, so a failure mid-cycle still restores power. `power_set(off)` is the only way to leave a target off, and only deliberately. | The relay is USB-powered from the Pi. A worker that died between energise and release (on NC) would leave the target dark until something reset the relay. Nothing should land in "off" by accident. |
| **D8** | This change corrects `PARE_HW_DEVICE` to the **if00** path for this bench. | The UART is on if00 (spike). The value is operator-declared, so the systemd unit and repo default carry the current bench's wiring. |

### What this does NOT change
- **`agent_core`.** Untouched.
- **The existing console tools** (`console_open/read/send/status/close`) and the
  passive `console_detect_baud`. The cycling sweep is additive; the passive scan
  stays for a chattering line where no power cycle is wanted.
- **The relay's `AT+BAUD`** is never sent. Only `AT+CHn=` and `AT`.
- **Which physical Tigard header is if00 vs if01** — wiring-dependent, not asserted
  here; the operator declares `PARE_HW_DEVICE`.

## 2. Architecture

### 2.1 Where each piece runs
- `pare-hardware-mcp` on `pare-bench`, one process, now owning two serial adapters:
  the Tigard UART (`PARE_HW_DEVICE`, if00) and the relay (`PARE_HW_RELAY_DEVICE`,
  the CP2102 by-path).
- The relay is opened per-command (short 9600 session, send, read `OK`, close), not
  held open. It is not a capture device; there is no session to keep. This also
  means a stuck relay handle cannot accumulate.
- The console session is owned by the existing `SessionManager` (`tools.MANAGER`),
  unchanged.

### 2.2 The polarity model
Config declares the channel and whether the target sits on NC or NO. The worker
derives, once:
- **powered = idle** for NC (energising cuts power); **powered = energised** for NO.
- `power_set(on)` / `power_set(off)` / `power_status` map through that. A caller
  never sees `AT+CHn=`.
`power_status` reads back with `AT+CHn=?` and reports the *target power* state plus
the raw channel state, so a miswired polarity is diagnosable ("channel energised,
target reported off — check PARE_HW_RELAY_POLARITY").

### 2.3 Independence
The relay tools open `PARE_HW_RELAY_DEVICE`; the console tools open
`PARE_HW_DEVICE`. Neither references the other's device. `power_cycle` does not
require an open console session (it can cycle a target nobody is watching); the
baud sweep is the one place both are used, and it owns the ordering.

## 3. Tool surface

Bare names (the daemon prefixes `hardware_`). All in `contract.py`'s `TOOL_SPECS`
with `risk_tier="high"`, handlers in `tools.py`, run via `asyncio.to_thread` like
every existing handler.

- **`power_status()`** → target power state (`on`/`off`/`unknown`), the raw channel
  state, the channel, and the declared polarity. Read-only. Refuses cleanly if no
  relay is configured.
- **`power_set(state: "on"|"off")`** → sets the target to that power state
  (idempotent: setting `on` when already on is a no-op that still returns the
  state). Returns the resulting `power_status`.
- **`power_cycle(off_ms: int = 3000)`** → target off, wait `off_ms`, target on.
  `off_ms` bounded (a floor so the target actually drops, a ceiling so a huge value
  can't strand it). Restore-to-on runs in a `finally`. Returns the final status and
  the measured off duration.
- **`console_detect_baud_cycling(rates: list[int] | None = None, capture_ms: int
  = ..., off_ms: int = ...)`** → the composite sweep (§4). Requires both a relay and
  an open console session. Returns ranked per-rate evidence.

Interfaces the sweep consumes from existing code (exact, so the plan can't drift):
- `baud.score_sample(data: bytes) -> dict` — the `console_score` discriminator.
- the `SessionManager` methods the passive `console_detect_baud` already uses to
  re-rate an open session (set rate, sample) — reused, not reimplemented.

## 4. The cycle-assisted baud sweep

Preconditions: a relay is configured, and a console session is open on the UART.
The sweep does NOT open or close the console session; the caller owns its lifetime.

For each candidate rate (default set = the same list the passive scan uses):
1. Set the open session's line to the candidate rate (no reopen — a reopen
   re-asserts DTR; the existing passive scan established this).
2. `power_cycle(off_ms)` the target, so a fresh boot burst begins at a known time.
3. Capture for `capture_ms` from the post-cycle cursor.
4. `baud.score_sample()` the captured bytes → `console_score`, printable ratio, and
   a byte sample.

Then:
- Rank candidates by `console_score`, highest first.
- Return **every** candidate's score + sample (the ranked evidence), plus the
  best-scoring rate and whether it cleared the same "clear winner" bar the passive
  scan uses. The tool never silently commits the line to a guessed rate: it reports,
  and leaves the session at a defined rate (the winner if there is a clear one, else
  restored to the entry rate — matching the passive scan's own restore rule).

Budget: the whole sweep is bounded by `CONFIG.scan_budget_s` / the request
deadline, the same wall-clock discipline `console_detect_baud` already enforces, now
including the `off_ms` per candidate. A candidate list that cannot complete one full
pass within the budget is refused before the first power cycle, not discovered
half-way through — cycling a target N times and then timing out is worse than not
starting.

Optional token boost (D5): a small set of known boot tokens (`U-Boot`,
`Linux version`, `Uncompressing`) may add a bounded bonus to a candidate that
already scores as text, to break ties between two plausible rates. It never
promotes noise: it multiplies an already-positive `console_score`, never adds to a
zero. The spike's target emits all three, so this is testable against real data.

## 5. Configuration and the if00 correction

New env (all optional; absent means "no relay configured" and the power tools
refuse cleanly):
- `PARE_HW_RELAY_DEVICE` — by-path device of the CP2102.
- `PARE_HW_RELAY_CHANNEL` — 1–4.
- `PARE_HW_RELAY_POLARITY` — `nc` or `no`.

Corrected:
- `PARE_HW_DEVICE` → the if00 by-id path
  (`…Tigard_V1.1_TG1119e7-if00-port0`), in the repo systemd unit and on the Pi.

`config.py` gains the three relay fields with the same env-parsing discipline as the
existing fields (`_positive_int_env` for the channel, a validated enum for
polarity). `list_devices` / `bench_status` should show the relay adapter when
configured, so the operator can see it the same way they see the Tigard.

## 6. Risk and safety

- All power tools tier **high**; `workers.yaml` gains forward pins for the new
  destructive-family names alongside the existing `hardware_flash_*` etc., so the
  risk-override coverage test stays honest.
- **Resting state on** (D7): `power_cycle` restores in `finally`; `off_ms` is bounded.
- **Back-powering** (spike): when the target is off but the Tigard drives TX, the
  target can be partly powered through its RX pin, so a cycle looks like it did
  nothing. The sweep sends nothing on the console during the off window; noted as an
  operator caveat, not something the worker can fully prevent.
- **The relay opens per-command**, so a crash cannot leave a relay session open; but
  a crash *after energise, before release* on NC leaves the target off. D7's
  bounded window plus `finally` covers the in-process path; a hard kill mid-window
  is a known residual (an operator or the next `power_set(on)` recovers it). Named,
  not solved, because solving it needs relay-side latching the SH-UR04A doesn't have.

## 7. Testing strategy

- **Unit, no hardware** (CI, no Pi, no pyserial-to-a-device): the polarity model
  (`nc`/`no` × on/off/status → the right `AT+CHn=` string and the right reported
  state), `off_ms` bounds, the budget refusal, the sweep's ranking and
  evidence-shaping against synthetic samples (reusing `baud`'s existing test
  vectors), and the resting-state `finally` (inject a failure mid-cycle, assert a
  restore-to-on was attempted). The relay serial is faked at the pyserial boundary,
  the way the existing tests fake the console.
- **`contract.py` risk pins:** the new tool names are tier high; the CI pin-coverage
  test (which imports `contract` with no hardware) must see them.
- **Live, on the Pi** (operator-run, not CI): `power_status`/`set`/`cycle` against
  the real relay with the target on NC1 (watch the board), and one real
  `console_detect_baud_cycling` run that must rank 115200 top with the boot log as
  its sample. This is the verify-the-surface step; its result goes in the plan's
  execution notes, not an automated test.

## 8. Not in scope
- **Passive `console_detect_baud`** stays as is; this adds a cycling sibling, does
  not replace it.
- **Artifact wiring** (`artifact_root`) — still its own design
  (`2026-09-12-artifact-wiring-design.md`).
- **The interactive UART pane** (keystroke send, in-pane open) — direction 1, its
  own spec after this.
- **Multi-target / multi-channel** — one target on one channel; the other three
  relay channels are unused.
- **Relay-side latching or a hardware watchdog** for the crash-mid-off residual —
  the SH-UR04A can't, and a different relay is a hardware decision, not this design.
- **U-Boot / recovery interrupt** (send a key during the countdown) — the spike saw
  the window; using it is later work once keystroke send exists.

## 9. Code in this document
None. This is hardware I/O, a serial protocol, a stateful sweep with a wall-clock
budget, and a crash-safety invariant — the class where prose code gets transcribed
past defects. Signatures, exact env names, exact AT strings, and the reused
functions are named; the implementer writes the code against the real files and the
real relay.
