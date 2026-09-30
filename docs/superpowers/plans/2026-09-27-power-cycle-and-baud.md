# Relay Power Control + Cycle-Assisted Baud Detection — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give `pare-hardware-mcp` the ability to switch the bench target's power through the DSD TECH relay, and a cycle-assisted baud sweep that forces a fresh boot at each candidate rate.

**Architecture:** A new `relay.py` owns the relay adapter (open by-path, AT protocol, polarity↔power translation, bounded power-cycle with finally-restore). Three thin tool handlers (`power_status`/`power_set`/`power_cycle`) wrap it, tier high. A composite `console_detect_baud_cycling` tool reuses the relay's power-cycle plus the existing `baud.score_sample`/`rank_candidates` and the open session's rate-setting to sweep rates against forced boot bursts, returning ranked per-rate evidence. A config task corrects the worker's default UART device to if00 and declares the relay.

**Tech Stack:** Python 3.14 (Pi) / 3.12 (dev), pyserial, FastMCP via `pare_worker_kit`, systemd. Relay: CP2102 @ 9600 8N1, AT protocol.

**Spec:** `docs/superpowers/specs/2026-09-27-power-cycle-and-baud-design.md`

## Global Constraints

- Relay AT protocol: `AT+CHn=1` energises channel n, `AT+CHn=0` releases it, `AT+CHn=?` queries it, `AT`→`OK`. **`AT+BAUD=` is NEVER sent** — it changes the board's own rate and would lock out control.
- The relay is addressed by **by-path** device (`PARE_HW_RELAY_DEVICE`); its serial is a generic `0001`, so by-id is ambiguous.
- Polarity lives in config (`PARE_HW_RELAY_POLARITY` = `nc`|`no`). Tools speak **target power** (`on`/`off`), never `AT+CHn=`. For `nc`, energising the channel CUTS power; for `no`, energising SUPPLIES power.
- **Resting state is always "powered on."** `power_cycle` restores power in a `finally`; `off_ms` is bounded. Only `power_set("off")` leaves a target off, and only deliberately.
- All power tools and the cycling sweep are risk tier **high** (the worker's floor; RiskAwareToolPool gates only high/critical).
- `contract.py` must not import hardware or reach pyserial-to-a-device — CI imports it on a Pi-less runner to validate risk pins.
- Every tool handler runs its blocking work via `asyncio.to_thread`, matching every existing handler in `tools.py`.
- The relay and the console are independent failure domains: a relay failure never touches an open console session, and a console failure never blocks power control.
- `NC` polarity + a mid-cycle hard kill of the worker is a known residual (target stays off until the next `power_set("on")`); do not attempt to solve it in software — the SH-UR04A has no latching.

## Review Focus

- **Relay device configured but unplugged or wrong by-path** → the open fails; every power tool must return a clean error naming the device, never hang or raise out of the handler. (Task 1 tests)
- **Relay accepts the write but returns no `OK` / garbage** (wedged, or a non-relay device at that path) → the tool must report failure, never claim the power state changed. (Task 1 tests)
- **`off_ms` out of range** (0, negative, or huge) → clamped to the documented floor/ceiling, never a zero-length "cycle" that doesn't drop the rail, never an unbounded strand. (Task 1 tests)
- **Baud sweep whose candidate list can't complete one full pass within the budget** → refuse before the first power cycle, not half-way through. (Task 3 tests)
- **Polarity misdeclared** (`nc` set but wired `no`, or vice versa) → `power_status` reports the raw channel state alongside the derived target-power state so the discrepancy is visible, rather than silently reporting the wrong thing. (Task 1 tests)

---

## File Structure

**`pare-hardware-mcp`** (`/mnt/secondary/projects/pare-hardware-mcp`, new branch `feat/power-cycle-and-baud` off `main`):
- Create: `src/pare_hardware_mcp/relay.py` — `RelayController`: open by-path, AT protocol, polarity↔power, bounded `power_cycle`. The only module that reaches the relay device.
- Modify: `src/pare_hardware_mcp/config.py` — three relay fields + env parsing.
- Modify: `src/pare_hardware_mcp/tools.py` — `power_status`/`power_set`/`power_cycle`/`console_detect_baud_cycling` handlers.
- Modify: `src/pare_hardware_mcp/contract.py` — `TOOL_SPECS` entries for the four new tools, tier `high`.
- Modify: `systemd/pare-hardware-mcp.service` — `PARE_HW_DEVICE`→if00, add relay env vars.
- Create: `tests/unit/test_relay.py`, `tests/unit/test_tools_power.py`, `tests/unit/test_tools_detect_baud_cycling.py`.
- Modify: `tests/unit/test_config.py` — the relay fields.

**`PARE`** (`/mnt/secondary/projects/PARE`, this plan's branch `docs/power-cycle-and-baud-design` already holds the spec; do the config edits here):
- Modify: `workers.yaml` — forward risk pins for the new `hardware_power_*` / `hardware_console_detect_baud_cycling` names; correct the if00 comment.

---

## Task 1: Relay driver + config

**Files:**
- Create: `src/pare_hardware_mcp/relay.py`
- Modify: `src/pare_hardware_mcp/config.py`
- Create: `tests/unit/test_relay.py`
- Modify: `tests/unit/test_config.py`

**Interfaces:**
- Consumes: nothing from other tasks. `serial.Serial` (pyserial) at the boundary; tests fake it.
- Produces:
  ```
  # config.py additions (Config dataclass):
  relay_device: str | None = None        # PARE_HW_RELAY_DEVICE (by-path)
  relay_channel: int | None = None       # PARE_HW_RELAY_CHANNEL (1-4)
  relay_polarity: str | None = None      # PARE_HW_RELAY_POLARITY ("nc"|"no")

  # relay.py:
  class RelayError(RuntimeError): ...
  class RelayNotConfigured(RelayError): ...   # raised when any relay field is absent
  class RelayController:
      def __init__(self, device: str, channel: int, polarity: str,
                    *, serial_factory=None): ...   # serial_factory is the test seam
      def status(self) -> dict:
          # {"target_power": "on"|"off", "channel_state": "energised"|"idle",
          #  "channel": int, "polarity": "nc"|"no"}
      def set_power(self, state: str) -> dict:     # state in {"on","off"}; returns status()
      def power_cycle(self, off_ms: int) -> dict:
          # off -> sleep(off_ms) -> on, ALWAYS restoring on in finally;
          # returns {"status": <status()>, "off_ms_actual": int}
  # module constants:
  OFF_MS_FLOOR = 250       # below this the rail may not actually drop
  OFF_MS_CEILING = 30000   # above this a caller can strand the target
  ```

**Context:** Spec §2.2, §3, §6, §7. `relay.py` is the only module that reaches the relay device — every AT string and the polarity math live here, so the tool handlers and the sweep never construct `AT+CHn=` themselves. Follow the config env-parsing pattern already in `config.py` (`_positive_int_env`, `os.environ.get(...) or None`), and the serial-faking test pattern in `tests/unit/` (a fake `Serial` object injected via a factory, as the existing session/tool tests do).

The polarity math, stated once so the implementer transcribes it verbatim:
- `nc`: target **on** ⇔ channel **idle** (`AT+CHn=0`); target **off** ⇔ channel **energised** (`AT+CHn=1`).
- `no`: target **on** ⇔ channel **energised** (`AT+CHn=1`); target **off** ⇔ channel **idle** (`AT+CHn=0`).
- `status()` reads with `AT+CHn=?` → reply `OK+CHn=<0|1>`; `1` = energised, `0` = idle; derive `target_power` through the polarity above and report BOTH.

AT exchange discipline: write `f"{cmd}\r\n"`, read the reply, and require it to start with `OK` (query replies are `OK+CHn=…`). A reply that is empty or does not start with `OK` is a `RelayError` naming the command and the raw bytes — never a silent success.

- [ ] **Step 1: Write the failing test — polarity math for nc**

In `tests/unit/test_relay.py`, using a fake serial that records writes and returns scripted replies:

```python
def test_nc_polarity_maps_power_to_channel():
    fake = FakeRelaySerial(replies={
        "AT+CH1=0\r\n": b"OK\n\x00",     # set on -> idle for nc
        "AT+CH1=1\r\n": b"OK\n\x00",     # set off -> energised for nc
        "AT+CH1=?\r\n": b"OK+CH1=0\n\x00",
    })
    r = RelayController("/dev/relay", 1, "nc", serial_factory=lambda dev: fake)
    r.set_power("on")
    assert fake.last_write == "AT+CH1=0\r\n"      # nc on = idle
    r.set_power("off")
    assert fake.last_write == "AT+CH1=1\r\n"      # nc off = energised
    st = r.status()                                # channel idle -> nc target on
    assert st["target_power"] == "on"
    assert st["channel_state"] == "idle"
```

`FakeRelaySerial` is a small helper in the test file: records the last written string, returns the scripted reply for the last write (or a default `b"OK\n\x00"`), and no-ops `flush`/`reset_input_buffer`/`close`. Model it on the existing serial fakes under `tests/unit/`.

- [ ] **Step 2: Run it, verify it fails** — `.venv/bin/pytest tests/unit/test_relay.py -x` → FAIL (no `relay` module).

- [ ] **Step 3: Implement `config.py` fields + `RelayController` enough for Step 1**

Add the three fields to the `Config` dataclass and parse them in the loader (channel via a bounded int parse, 1–4; polarity validated to `nc`/`no`, anything else is a config error). Implement `RelayController.__init__`, `set_power`, `status` with the polarity math above.

- [ ] **Step 4: Run it, verify it passes.**

- [ ] **Step 5: Test — no polarity is not implied.** Add `test_no_polarity_maps_power_to_channel` (the mirror of Step 1: `no` on = `AT+CH1=1`, off = `AT+CH1=0`, and a channel-energised query → target on). Run, implement if needed, pass.

- [ ] **Step 6: Test — a reply without OK is an error (Review Focus).**

```python
def test_garbage_reply_is_an_error_not_a_silent_success():
    fake = FakeRelaySerial(replies={"AT+CH1=1\r\n": b"\x00\xff garbage"})
    r = RelayController("/dev/relay", 1, "nc", serial_factory=lambda dev: fake)
    with pytest.raises(RelayError):
        r.set_power("off")
```

Run (fail), implement the `OK`-prefix check, pass.

- [ ] **Step 7: Test — device open failure is a clean RelayError (Review Focus).**

Factory raises `serial.SerialException("No such file or directory")`; assert `set_power`/`status` raise `RelayError` (not the raw `SerialException`), and the message names the device.

- [ ] **Step 8: Test — off_ms bounds (Review Focus).**

```python
def test_power_cycle_clamps_off_ms():
    fake = FakeRelaySerial()
    r = RelayController("/dev/relay", 1, "nc", serial_factory=lambda dev: fake)
    assert r.power_cycle(0)["off_ms_actual"] == OFF_MS_FLOOR
    assert r.power_cycle(10**9)["off_ms_actual"] == OFF_MS_CEILING
    assert r.power_cycle(1000)["off_ms_actual"] == 1000
```

Implement `power_cycle`: derive off/on channel commands from polarity, set off, `time.sleep(clamped_ms/1000)`, then set on **in a `finally`** so an exception during the sleep or the off-write still restores power. Run, pass.

- [ ] **Step 9: Test — resting state is restored even on failure.**

Script the fake so the *off* write succeeds but injify a failure right after (e.g. the fake raises on the second-to-last call), and assert the final write was the *on* command (power restored) even though `power_cycle` raised. This pins the `finally`.

- [ ] **Step 10: Test — RelayNotConfigured.** A helper the tools will call (e.g. `RelayController.from_config(config)`) raises `RelayNotConfigured` when any of the three fields is absent. Add it, test it.

- [ ] **Step 11: Test config parsing** in `tests/unit/test_config.py`: the three env vars populate the fields; an out-of-range channel and a bad polarity are rejected; all-absent leaves them `None`.

- [ ] **Step 12: Full worker suite + commit.**

```bash
cd /mnt/secondary/projects/pare-hardware-mcp && .venv/bin/pytest -q
git add src/pare_hardware_mcp/relay.py src/pare_hardware_mcp/config.py tests/unit/test_relay.py tests/unit/test_config.py
git commit -m "$(cat <<'EOF'
feat(relay): RelayController + relay config (AT protocol, polarity, bounded cycle)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01VatXHkDJSMKf57UVL76KbN
EOF
)"
```

---

## Task 2: Power tools

**Files:**
- Modify: `src/pare_hardware_mcp/tools.py`
- Modify: `src/pare_hardware_mcp/contract.py`
- Create: `tests/unit/test_tools_power.py`

**Interfaces:**
- Consumes: `RelayController`, `RelayError`, `RelayNotConfigured`, `RelayController.from_config` (Task 1); `CONFIG` (module-global in `tools.py`); `_ok`/`_err` (existing helpers in `tools.py`).
- Produces: MCP tools `power_status()`, `power_set(state: str)`, `power_cycle(off_ms: int = 3000)`, each returning a `_ok(...)`/`_err(...)` JSON string like the existing handlers.

**Context:** Spec §3, §4 (D4 gating), §6. These are thin async wrappers: resolve a `RelayController` from `CONFIG`, run the blocking relay call via `asyncio.to_thread`, translate `RelayNotConfigured`→a clear "no relay configured" `_err`, `RelayError`→an `_err` naming the failure, success→`_ok` with the status. They must never raise out of the handler (the daemon dispatch treats an exception as a worker fault). Register all three in `contract.py`'s `TOOL_SPECS` at `risk_tier="high"` with bare names `power_status`/`power_set`/`power_cycle` (the daemon prefixes `hardware_`).

- [ ] **Step 1: Write the failing test — power_set success path.**

In `tests/unit/test_tools_power.py`, monkeypatch `tools.CONFIG` to a config with relay fields set and monkeypatch `tools.RelayController` (or `from_config`) to return a fake controller; call `await tools.power_set("off")`, assert the reply parses to `target_power == "off"` and the fake's `set_power` was called with `"off"`.

- [ ] **Step 2: Run, verify fail** (no `power_set`). 

- [ ] **Step 3: Implement `power_status`/`power_set`/`power_cycle`** in `tools.py` and add the three `ToolSpec`s to `contract.py` (tier high, input schemas: `power_set` takes `state` enum `on`/`off`; `power_cycle` takes optional `off_ms` int; `power_status` takes none). Run, pass.

- [ ] **Step 4: Test — no relay configured returns a clean error.** `CONFIG` with relay fields `None`; `await tools.power_status()` returns an `_err` whose message says a relay is not configured; no exception escapes.

- [ ] **Step 5: Test — RelayError becomes an _err, not a crash.** Fake controller raises `RelayError("relay open failed: /dev/relay")`; `await tools.power_cycle()` returns an `_err` carrying that message.

- [ ] **Step 6: Test — power_cycle reports the restored (on) resting state.** Fake controller's `power_cycle` returns a status with `target_power == "on"`; assert the tool surfaces it.

- [ ] **Step 7: Test — contract pins.** In `tests/unit/test_contract.py` (or the power test file), assert the three names are present in `TOOL_SPECS` with `risk_tier == "high"`.

- [ ] **Step 8: Full suite + commit** (`feat(tools): power_status/power_set/power_cycle over the relay, tier high`, same attribution footer).

---

## Task 3: Cycle-assisted baud sweep

**Files:**
- Modify: `src/pare_hardware_mcp/tools.py`
- Modify: `src/pare_hardware_mcp/contract.py`
- Create: `tests/unit/test_tools_detect_baud_cycling.py`

**Interfaces:**
- Consumes: `RelayController.power_cycle` (Task 1); `baud.score_sample(data: bytes) -> dict` (has `console_score`, `printable_ratio`), `baud.rank_candidates`, `baud.sanitize_rates` (existing); `CONFIG.scan_budget_s`, `CONFIG.request_deadline_s`; the open session via `MANAGER.get(session)` and its rate-setting + windowed read (study how the existing `console_detect_baud` / `session.scan_baud` set a rate and sample a window — reuse that mechanism; do NOT reimplement termios rate-setting).
- Produces: MCP tool `console_detect_baud_cycling(session: str, rates: list[int] | None = None, off_ms: int = 3000, capture_ms: int | None = None)` returning `_ok` with `candidates` (ranked list of `{rate, console_score, printable_ratio, sample_b64}`) and `best` (`{rate}` or null when nothing clears the winner bar).

**Context:** Spec §4. This is the one place both the relay and the console are used, and it owns the ordering. It is NOT `session.scan_baud` (that is a passive, no-transmit, no-cycle sweep) — it is a new loop that, per candidate rate: sets the session's line rate, calls `RelayController.power_cycle(off_ms)` to force a fresh boot, captures a `capture_ms` window from the post-cycle cursor, and scores it with `baud.score_sample`. Reuse `baud`'s ranking and the session's rate-set primitive; write the cycle-per-candidate loop new.

**Budget refusal (Review Focus):** before the FIRST power cycle, compute the worst-case wall time = `len(rates) * (off_ms/1000 + capture_ms/1000 + margin)` and refuse with an `_err` if it exceeds `min(CONFIG.scan_budget_s, CONFIG.request_deadline_s)`. Cycling the target N times and then timing out is worse than not starting. This mirrors the discipline in the existing `console_detect_baud`.

**Preconditions:** requires a relay configured (else `_err` "no relay configured") AND an open, alive session (else `_err`, matching the existing detect_baud's session checks). The tool does not open or close the session.

- [ ] **Step 1: Write the failing test — ranking and evidence shape.**

Fake the session's rate-set + windowed read to return, per rate, a scripted byte sample (reuse `tests/unit/_uart.py`'s misframed-vs-real vectors: a real boot log at the correct rate, misframed garbage at the others). Fake the relay's `power_cycle` to a no-op that records each call. Assert: `power_cycle` was called once per candidate; the returned `candidates` are ranked by `console_score` descending; each carries `rate`, `console_score`, `printable_ratio`, `sample_b64`; `best.rate` is the real-boot-log rate.

- [ ] **Step 2: Run, verify fail.**

- [ ] **Step 3: Implement `console_detect_baud_cycling`** + its `contract.py` spec (tier `high` — it cycles power). Reuse `sanitize_rates`, `score_sample`, `rank_candidates`. Run, pass.

- [ ] **Step 4: Test — budget refusal before any cycle (Review Focus).** A candidate list + off_ms/capture_ms whose worst case exceeds the budget; assert an `_err` mentioning the budget AND that the relay's `power_cycle` was never called.

- [ ] **Step 5: Test — evidence surfaced even when nothing wins.** All samples score as noise (misframed vectors only); assert `best` is null but every candidate still appears with its (low) score and sample, so the operator can eyeball them.

- [ ] **Step 6: Test — requires relay and session.** No relay configured → `_err`; no/!alive session → `_err`. Neither calls `power_cycle`.

- [ ] **Step 7: Optional token boost (spec §4).** Add the bounded boost (`U-Boot`/`Linux version`/`Uncompressing` multiply an already-positive `console_score`, never lift a zero). Test: a real boot-log sample with tokens outranks a same-`console_score` sample without, and a noise sample containing the literal token text is NOT promoted (its base `console_score` is ~0). If this proves fiddly, it may be dropped — it is a tie-breaker, not core; note the decision in the task report.

- [ ] **Step 8: Full suite + commit** (`feat(baud): cycle-assisted baud sweep returning ranked per-rate evidence`, attribution footer).

---

## Task 4: Deployment config — systemd unit + workers.yaml

**Files:**
- Modify: `systemd/pare-hardware-mcp.service` (pare-hardware-mcp repo)
- Modify: `workers.yaml` (PARE repo)

**Interfaces:**
- Consumes: the env-var names from Task 1 (`PARE_HW_RELAY_DEVICE`/`_CHANNEL`/`_POLARITY`), the corrected `PARE_HW_DEVICE` if00 path, and the tool names from Tasks 2–3.
- Produces: no code; the deployed worker reads if00 and has a relay declared; the daemon's risk overrides pin the new tools.

**Context:** Spec §5, §6. Mechanical config, two repos. The exact relay device by-path was captured in the spike: `platform-xhci-hcd.0-usb-0:2:1.0` → use the `/dev/serial/by-path/…` form. Target is on CH1, polarity `nc`.

- [ ] **Step 1: Correct `PARE_HW_DEVICE` to if00 and add the relay env** in `systemd/pare-hardware-mcp.service`:

```
Environment=PARE_HW_DEVICE=/dev/serial/by-id/usb-SecuringHardware.com_Tigard_V1.1_TG1119e7-if00-port0
Environment=PARE_HW_RELAY_DEVICE=/dev/serial/by-path/platform-xhci-hcd.0-usb-0:2:1.0-port0
Environment=PARE_HW_RELAY_CHANNEL=1
Environment=PARE_HW_RELAY_POLARITY=nc
```

`systemd-analyze verify systemd/pare-hardware-mcp.service` stays clean (allow the expected "not executable" note for the not-locally-present venv path).

- [ ] **Step 2: Update the `hardware` block comment in `workers.yaml`** (PARE) to say the console is if00 (not if01), and confirm `SupplementaryGroups=dialout` on the unit still covers the second `/dev/ttyUSB*` (it does — group-wide).

- [ ] **Step 3: Add forward risk pins** for the new tool names in `workers.yaml`'s `risk_overrides`, alongside the existing `hardware_flash_*` etc.: `hardware_power_set`, `hardware_power_cycle`, and `hardware_console_detect_baud_cycling` pinned at their high tier (match the shape of the existing pins). `hardware_power_status` may be pinned too for symmetry.

- [ ] **Step 4: Run PARE's `workers.yaml` tests** (`tests/test_workers_yaml.py`, `tests/test_risk_overrides_coverage.py`) — the new pins must match real tool names once the worker contract is installed, and the existing "declared/networked/autoload" assertions must still pass. If the coverage test can't see the new tools' contract on a Pi-less runner, follow the pattern the existing hardware pins already use (they are forward-declared and warned-not-failed).

- [ ] **Step 5: Commit both repos** (separate commits, one per repo, each with the attribution footer). Do NOT deploy here — deployment is an operator step after review (needs the Tigard re-auth and a `bench_deploy.sh` run on the Pi).

---

## Operator Step: deploy + live verification (not a task; run with the user)

After Tasks 1–4 merge: re-auth Tailscale, `git pull` on the Pi's pare-hardware-mcp checkout, `sudo ./scripts/bench_deploy.sh`, restart the worker, then live-verify (spec §7): `power_status`/`set`/`cycle` against the real relay with the target on NC1 (watch the board), and one `console_detect_baud_cycling` run that must rank 115200 top with the boot log as its sample. Record the results in the execution notes.

---

## Self-Review

**1. Spec coverage.** D1 (same worker) → all code tasks in pare-hardware-mcp; D2 (relay device/channel/polarity config) → Task 1; §5's relay-visibility sentence ("`list_devices` / `bench_status` should show the relay adapter when configured") was silently dropped by this self-review's D2 mapping — the whole-branch review caught it; implemented post-review (2026-09-30) as a `bench_status` `relay` field ({device, channel, polarity} when all three `PARE_HW_RELAY_*` are set, null otherwise); the spec's "list_devices / bench_status" is read as either-or, and `bench_status` is the fitting half because a by-path CP2102 cannot self-label among `list_devices`' by-id entries; D3 (tools speak target power) → Task 1 polarity math + Task 2; D4 (tier high) → Task 2/3 contract entries + Task 4 pins; D5 (composite sweep, reuse score_sample, ranked evidence) → Task 3; D6 (independent failure domains) → Task 1 (relay errors isolated) + Task 3 (separate precondition checks); D7 (resting state on) → Task 1 Steps 8–9; D8 (if00 correction) → Task 4. §7 testing → each task's unit tests + the Operator Step for live. §8 not-in-scope respected (no artifact wiring, no interactive pane, no U-Boot interrupt, no latching).

**2. Placeholder scan.** No TBD/TODO/"handle edge cases". Task 3 Step 7 is explicitly optional with a stated fallback and a report note, not a placeholder.

**3. Type consistency.** `RelayController(device, channel, polarity, *, serial_factory)`, `status()`/`set_power(state)`/`power_cycle(off_ms)`, `RelayError`/`RelayNotConfigured`, `OFF_MS_FLOOR`/`OFF_MS_CEILING`, config fields `relay_device`/`relay_channel`/`relay_polarity` — all used consistently across Tasks 1–4. Tool names `power_status`/`power_set`/`power_cycle`/`console_detect_baud_cycling` consistent between Tasks 2/3 and the Task 4 pins.

**4. Review Focus.** All five lines have owning-task tests: unplugged relay (T1 S7), no-OK reply (T1 S6), off_ms bounds (T1 S8), budget refusal (T3 S4), polarity discrepancy in status (T1 S1/S5 report both states). 
