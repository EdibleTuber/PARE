# Bench integration — Plan B for `pare-hardware-mcp`

**Status:** design, 2026-09-20. Awaiting spec review before writing-plans.
**Repos touched:** `pare-hardware-mcp` (deploy scaffolding, docstring correction), `PARE` (workers.yaml flip, small `pare-tui` env-var wiring), Pi operational state (`/opt/pare/hardware-mcp/`, systemd unit). `agent_core` untouched.

The motivating problem: `pare-hardware-mcp` is complete and tested (`main` after PR #2, bench-findings merged), but has never once been the running service PARE dispatches to. Every prior bench verification was transient — rsync the source to `/tmp/hwverify` and run pytest against the Pi's system Python (`docs/superpowers/2026-09-13-bench-verification.md:8`). PARE's `workers.yaml` still declares hardware as `transport: stdio` with a local `command:` path that resolves to nothing usable, and the worker's `config.py` docstring still says "still declared `transport: stdio` in workers.yaml" (`config.py:38-39`). `pare-tui`'s v1 default source is `FakeConsoleSource` (Task 9), because bench integration was scoped to this spec.

Prior context: [`docs/superpowers/specs/2026-09-18-pare-tui-design.md`](2026-09-18-pare-tui-design.md) §6 outlined this work as a prerequisite. [`docs/superpowers/specs/2026-09-05-networked-workers-design.md`](2026-09-05-networked-workers-design.md) established the transport, trust boundary, and operational shape this design consumes without change.

---

## 1. Decisions

| | Decision | Why |
|---|---|---|
| **D1** | Worker runs on the Pi (`pare-bench`, `100.97.133.126`), not on agenthost or via serial-forwarding. | Networked-workers §5's shape. The baud scan's tight-timing loops (`TIOCMGET`/`tcgetattr` every ~4ms per `pare-hardware-mcp/session.py`) happen locally on the Pi — never over tailnet. Only tool-level MCP calls cross the network. Frida is the working precedent. Alternatives (`ser2net`/`rfc2217`, `usbip`) either force the tight-timing path over the tailnet or add kernel-level integration on both sides for unclear benefit. |
| **D2** | Phased rollout: **Phase 0 transient, Phase 1 persistent**. | Phase 0 gets one end-to-end session working with the least ceremony — install into `/tmp/hwmcp-venv`, run manually, `Ctrl-C` when done, nothing persists. If Phase 0 reveals a design issue (poll interval feels wrong, HTTP latency worse than measured, Python 3.14 incompatibility), we catch it before writing systemd and deploy scripts. Phase 1 productionizes only what Phase 0 proved works. |
| **D3** | Bind to `tailscale0`, never a wildcard. | `pare_worker_kit/serve.py:168-175` refuses `0.0.0.0` outright — "This worker has no authentication: the bind address is the access control." D3 from the networked-workers spec, unchanged. |
| **D4** | `risk_default: high` on the `workers.yaml` `hardware` block stays exactly as it is. | The floor exists to contain **the model** dispatching through a worker whose self-report cannot be trusted (`workers.yaml:113-118`). Moving the worker onto the tailnet doesn't relax that containment — it changes only the transport, not the caller. Same reasoning as the pare-tui spec's D3, applied to the daemon's dispatch path. |
| **D5** | `pare-tui`'s hardware endpoint is env-var-driven (`PARE_TUI_HARDWARE_ENDPOINT`), NOT hardcoded. `FakeConsoleSource` stays the default. | Nothing sets the env var in test runs, so every existing pare-tui test keeps passing without modification. Phase 0 launches with the env var pointing at the Pi endpoint; Phase 1 documents that as the operator's standard invocation. A future config-file setting is a trivial addition that doesn't touch this code. |
| **D6** | `worker_prefix=""` on the pane's `McpConsoleSource`. Bare tool names (`console_read`, not `hardware_console_read`). | The pane bypasses the daemon and holds its own `MCPClient` (pare-tui spec D2). The `hardware_*` prefix is applied by agent_core's `tool_factory` only when dispatched THROUGH PARE. Direct connection to the worker uses the contract's bare names. |
| **D7** | Poll interval measured from a real Phase 0 round-trip, not picked. | Pare-tui spec R2. Median + p95 RTT recorded; interval floor = `p95 * 2`; value baked into `UartPane`'s constructor default in Phase 1 with a comment naming the measurement date and the number. |
| **D8** | `artifact_root: null` (or a placeholder path) on the `workers.yaml` hardware block for v1. | The worker's phase-1 plan explicitly stated no artifacts in phase 1. `/mnt/bench-store` exists on the Pi but nothing writes to it yet. Wiring is a separate design. |

### What this does NOT change

- **`agent_core`.** Untouched. This is a deployment + config change, not a library change.
- **The four destructive-family pins** in `workers.yaml`'s `risk_overrides` block (`hardware_flash_*`, `hardware_erase_*`, `hardware_write_*`, `hardware_glitch_*`). Forward-declared, still forward-declared. The phase-1 worker is read-only console; the pins wait for the tools they guard.
- **`test_risk_overrides_coverage.py`'s "unchecked pins" warning.** Same reasoning — the pins exist so a future dev adding a destructive tool cannot silently ship without gating.
- **The bench's operator-recovery paths.** `getty@tty1` stays enabled ([[never-remove-the-operators-only-way-in]]). `pare-bench-status` and `pare-bench-kiosk` are untouched. The hardware worker is additive, never removes an existing service.
- **PARE's daemon-side dispatch.** The tool-name prefix (`hardware_*`), the risk gate, the audit log, `handle_chat`'s tool loop — all unchanged.

---

## 2. Architecture

### 2.1 Where each piece runs

- **`pare-bench` (Pi, `100.97.133.126`, aarch64, Python 3.14.4):**
  - `pare-hardware-mcp` as a systemd service (Phase 1) bound to `tailscale0:9102`.
  - Serial I/O to `/dev/serial/by-id/usb-SecuringHardware.com_Tigard_V1.1_TG1119e7-if01-port0` (the `if01` UART channel per [[tigard-uart-is-if01]]; serial `TG1119e7`).
  - `pare-bench-status` and `pare-bench-kiosk` unchanged.
- **`agenthost` (daemon host, `100.82.222.92`):**
  - PARE daemon dispatches to the hardware worker over HTTP (streamable_http transport, per the `workers.yaml` flip in §4).
- **Operator's terminal (wherever `pare-tui` runs):**
  - `pare-tui`'s UART pane opens its OWN `MCPClient` to `http://100.97.133.126:9102/mcp` (bypassing the daemon, per the pare-tui spec D2).
  - Set `PARE_TUI_HARDWARE_ENDPOINT` at launch to activate this; unset falls back to `FakeConsoleSource`.

### 2.2 Trust boundary

Tailnet ACLs. No application-level authentication between daemon and worker; no TLS. The `tailscale0` bind (D3) is what makes this defensible — a wildcard bind would expose every hardware tool to every network the Pi is on. Same rules as frida (networked-workers §6), same rules as the four workers already on the tailnet.

### 2.3 Two clients hitting the same worker

Both PARE's daemon (for model-dispatched tool calls) and `pare-tui`'s pane (for direct pane reads) will open MCP sessions to the same worker. `pare_hardware_mcp.tools.py:59`'s `MANAGER = SessionManager(...)` is module-global — sessions belong to the worker, not to a client. This is the shape the pare-tui spec's §3 relies on: PARE's `console_open` creates a session; the pane's `console_status()` attaches to that same session by session id. Cross-client visibility is a feature of this design, not a coincidence.

The daemon's dispatch is risk-gated (`hardware:high` per `workers.yaml`). The pane's direct connection is NOT (per pare-tui spec §3.2: keystrokes are operator I/O, not model dispatch, and gating them retrained reflex approval). This asymmetry is intentional; both paths land on the worker via the same MCP session state.

---

## 3. Phase 0 — transient end-to-end

**Purpose.** Prove the full chain works with the minimum ceremony before writing deployment machinery. No files land on the Pi persistently; no `workers.yaml` change lands in PARE.

### 3.1 Prerequisites

- `pare-bench` reachable via SSH and tailscale (`100.97.133.126`). Verified 2026-09-20.
- Tigard attached; `/dev/serial/by-id/usb-SecuringHardware.com_Tigard_V1.1_TG1119e7-if01-port0` → `/dev/ttyUSB1` present. Verified 2026-09-20.
- `pare` user in `dialout` group (can open `/dev/ttyUSB*` without root). Verified 2026-09-18.
- `pare-hardware-mcp` main at PR #2's merge (bench-findings). Verified 2026-09-20 (`b49b948`).

### 3.2 Sequence

1. **Install the worker in an ephemeral venv on the Pi:**
   ```
   ssh pare@100.97.133.126
   python3 -m venv /tmp/hwmcp-venv
   /tmp/hwmcp-venv/bin/pip install git+https://github.com/EdibleTuber/pare-hardware-mcp@main
   ```
2. **Run the worker manually** (foreground), environment:
   - `AGENT_WORKER_TRANSPORT=http`
   - `AGENT_WORKER_HOST=tailscale0`
   - `AGENT_WORKER_PORT=9102`
   - `PARE_HW_DEVICE=/dev/serial/by-id/usb-SecuringHardware.com_Tigard_V1.1_TG1119e7-if01-port0`
   - `PARE_HW_EXPECT_SERIAL=TG1119e7`
3. **Confirm reachability from agenthost:** `curl -m3 http://100.97.133.126:9102/mcp` returns MCP-shaped bytes (405 or a proper response), not connection-refused.
4. **Launch `pare-tui`** with `PARE_TUI_HARDWARE_ENDPOINT=http://100.97.133.126:9102/mcp`. This step requires the small pare-tui env-var wiring change from §5 to have landed first — see §5.
5. **Open the console via PARE** (chat interface, one line to the model like "open the hardware console"). The pane calls `console_status()`, sees the live session, attaches, starts polling `console_read`.
6. **Measure R2** (see §6): log `loop.time()` around N sequential `console_read` calls; record median and p95.
7. **Silent-capture proof** (target-idle case): pane renders 0-byte slices honestly (`alive=True`, `dropped=0`, `remaining=0`, cursor unchanged).

### 3.3 Acceptance criteria

**Target-idle (Tigard attached, nothing transmitting — the current bench state):**
- MCP reachable at `http://100.97.133.126:9102/mcp` from agenthost.
- `console_open` on `if01` succeeds; `console_status` reports the session alive with `serial: TG1119e7`.
- `pare-tui`'s UART pane attaches, polls, renders silent-capture honestly without crashing.
- R2 poll interval measured and recorded.

**Target-attached (optional, if a device is wired up during Phase 0):**
- All the above, plus:
- `console_read` returns real bytes; the pane renders them through `for_display`.
- If the target's baud rate is known, `console_detect_baud` finds it.
- If the target overruns the buffer, `capture_gaps` and `dropped` exercise (closes bench-verification open item #2, `2026-09-13-bench-verification.md:71`).

### 3.4 Phase 0 explicitly does NOT

- Install a systemd unit.
- Modify `workers.yaml`'s hardware block. (§5's env-var wiring lets `pare-tui` reach the endpoint without changing the daemon's dispatch.)
- Land anything on the Pi past `Ctrl-C`.
- Merge to any main branch. Phase 0 is verification; Phase 1 productionizes.

---

## 4. Phase 1 — persistence

Runs after Phase 0 clears. Every element mirrors either the frida networked-worker precedent or the bench-status/kiosk pattern already on the Pi.

### 4.1 On the Pi

- `/opt/pare/hardware-mcp/` — checkout of `pare-hardware-mcp` main, matching the `/opt/pare/{bench,scripts}` convention.
- `/opt/pare/hardware-mcp/.venv/` — dedicated venv (Ubuntu 26.04's system Python 3.14 stays untouched).
- `/opt/pare/hardware-mcp/DEPLOYED_FROM` — matches `/opt/pare/DEPLOYED_FROM`'s format: source-machine HEAD SHA + timestamp.

### 4.2 Deploy script

New in `pare-hardware-mcp` repo: `scripts/bench_deploy.sh`, same shape as PARE's existing `bench_deploy.sh`:
1. rsync the current checkout to `/opt/pare/hardware-mcp/` on the Pi.
2. `pip install -e .` into `/opt/pare/hardware-mcp/.venv/`.
3. sha256 the shipped `server.py` and `session.py` against the source. Refuse to write `DEPLOYED_FROM` if either diverges — the bench-verification doc's own provenance discipline.
4. Write `DEPLOYED_FROM`: `<HEAD> deployed <ISO8601> from <source path>`.
5. `--check` flag re-runs the sha256 comparison against the current source without deploying; matches the existing `bench_deploy.sh --check` pattern PARE uses.

### 4.3 Systemd unit

New in `pare-hardware-mcp` repo (or PARE's `bench/systemd/`, whichever is cleaner for the deploy story): `pare-hardware-mcp.service`.

Copy of `bench/systemd/pare-bench-status.service` with:
- `After=network.target` (not `network-online.target`).
- `[Unit]` carries `StartLimitIntervalSec=0` — matches the bench-status unit's reasoning: a crash loop must not be the reason the bench is dark.
- `[Service]`:
  - `User=pare, Group=pare`.
  - `WorkingDirectory=/opt/pare/hardware-mcp`.
  - `ExecStart=/opt/pare/hardware-mcp/.venv/bin/pare-hardware-mcp`.
  - Environment vars from §3.2 step 2, unchanged.
  - `Restart=on-failure, RestartSec=5`.
- No `RequiresMountsFor=` for the artifact drive — v1 has none (D8).

### 4.4 PARE-side changes

- `workers.yaml`'s hardware block flips per §5 below.
- `pare-tui`'s env-var wiring lands per §5.1.
- `pare-hardware-mcp/config.py:38-39` docstring updates: replace "still declared `transport: stdio` in workers.yaml" with the actual state, referencing this design.

### 4.5 Health-check row on the status page

**Deferred.** `bench/status_server.py` currently shows ArcticBase reachability and worker heartbeat; wiring a hardware-worker probe row is a small addition, but not part of Plan B's critical path. Land only if operational feedback proves it's needed.

---

## 5. `workers.yaml` flip

The single most consequential config change in Plan B. Currently `workers.yaml:104-120` (approx.):

```yaml
  hardware:
    command: /mnt/secondary/projects/PARE/.venv/bin/pare-hardware-mcp
    transport: stdio
    # [multi-line comment block explaining the floor]
    risk_default: high
    autoload: false
    capability_tags: [hardware, uart, jtag, glitch]
```

After Phase 1:

```yaml
  # Networked hardware worker on pare-bench (100.97.133.126). Systemd
  # unit /etc/systemd/system/pare-hardware-mcp.service; deploys from a
  # checkout at /opt/pare/hardware-mcp/ (see pare-hardware-mcp/scripts/
  # bench_deploy.sh). risk_default remains high — the tailnet transport
  # doesn't relax the model-containment floor; see the block below.
  hardware:
    endpoint: http://100.97.133.126:9102/mcp
    transport: streamable_http
    connect_timeout: 20
    read_timeout: 60
    # FLOOR — raised from medium. RiskAwareToolPool gates only high and
    # critical (risk_pool.py:386), so a medium floor meant any hardware tool
    # that failed to advertise a wire tier — a half-wired dev build, or a
    # tampered one under-reporting — DISPATCHED WITH NO PROMPT AT ALL,
    # bypassing every approval surface. A floor of high means the worst case
    # is a prompt, not a silent flash write.
    risk_default: high
    autoload: false        # REQUIRED for networked workers
    capability_tags: [hardware, uart, jtag, glitch]
    artifact_root: null    # placeholder; wiring is a separate design
```

The floor comment stays verbatim — its reasoning is unchanged.

### 5.1 `pare-tui` env-var wiring

Small change in `pare/tui/app.py` where `UartPane` is constructed:

```python
endpoint = os.environ.get("PARE_TUI_HARDWARE_ENDPOINT")
source = McpConsoleSource(endpoint=endpoint) if endpoint else FakeConsoleSource()
```

Behavior:
- No env var → `FakeConsoleSource` (unchanged default).
- Env var set → `McpConsoleSource(endpoint=<value>)`. `worker_prefix` defaults to `""` per D6.

Two new tests in `tests/test_tui_app_integration.py` (or a new file):
- Unset env var → constructed source is a `FakeConsoleSource`. Regression guard.
- Set env var to any string → constructed source is an `McpConsoleSource`. Doesn't connect (no `attach()` in the test) — proves the branch, not the transport.

### 5.2 `pare-hardware-mcp/config.py` docstring

Line 38-39 currently says "This worker is still declared `transport: stdio` in workers.yaml…" Update to describe the actual post-flip state and reference this design.

### 5.3 Ripple to check

- `tests/test_workers_yaml.py`'s `test_hardware_is_declared_but_not_autoloaded` and `test_networked_workers_never_autoload` — both should still pass (hardware stays declared, autoload stays false).
- Any test that specifically asserts hardware is stdio — grep at implementation time; the ripple is small since most tests treat the workers by shape rather than by name.

---

## 6. R2 poll interval measurement

The pare-tui spec's R2 said the interval must come from a real round-trip measurement, not be picked. Phase 0 is where the measurement happens.

### 6.1 Method

During the Phase 0 session, once the pane has attached:
- Loop `await source.read(cursor, limit=0)` (empty read against the live session) N=100 times.
- Log `loop.time()` before and after each call.
- Compute median and p95 RTT.
- Note the tailscale wire path from `tailscale status` (direct vs. relay).

### 6.2 Choosing the interval from the measurement

- **Floor:** at least `p95 * 2`. Below that, back-to-back polls stack up on a variable link.
- **Ceiling for interactive feel:** ~500ms. Above that, a UART pane feels laggy for hands-on use.
- **Expected range from precedent:** frida's networked-workers measurements (spec §5) showed ~10ms median RTT on the same tailnet. Hardware should be in a similar range, putting the interval at ~50-100ms.

### 6.3 Where the number lands

`pare/tui/panes/uart.py`'s `UartPane` constructor default (currently 0.5s per Task 9). Phase 1's diff changes that constant with a comment naming the measurement date and the median it was based on — so a future refactor knows it wasn't picked.

### 6.4 Re-measurement triggers

Not automated. A comment in `pare/tui/panes/uart.py` names the conditions under which the number should be re-measured:
- The worker's endpoint changes.
- The Pi's tailscale wire type stays relay long-term.
- The Pi moves off the tailnet.
- A future networked-workers design changes the transport.

---

## 7. Rollback and recovery

### 7.1 Rollback paths

- **Phase 0 rollback:** `Ctrl-C` the manual worker on the Pi; `rm -rf /tmp/hwmcp-venv/`; unset `PARE_TUI_HARDWARE_ENDPOINT`. Nothing on the Pi persists; nothing in either repo changed.
- **Phase 1 rollback (systemd deployed but Plan B being reverted):** `sudo systemctl disable --now pare-hardware-mcp`; revert `workers.yaml`'s hardware block from PR history; `rm -rf /opt/pare/hardware-mcp/`. `pare-bench-status` and `pare-bench-kiosk` untouched — operator's screen still comes up.
- **`getty@tty1` stays enabled throughout.** [[never-remove-the-operators-only-way-in]] applies.

### 7.2 Recovery from a wedged worker

- Systemd's `Restart=on-failure` + `RestartSec=5` handles a crashed process. `StartLimitIntervalSec=0` in `[Unit]` keeps it restarting rather than giving up (matches `pare-bench-status`'s reasoning).
- A stale port-holder from a previous Phase 0 session surfaces cleanly through `console_open`'s existing exclusivity error. No design change needed; kill the stale process (`ss -lntp | grep 9102` on the Pi to find its pid).

---

## 8. Testing strategy

### 8.1 Automated tests

- **Phase 0 has no unit tests of its own** — its acceptance is a live session against real hardware. Nothing to automate.
- **Phase 1 §5.1 env-var branch:** two tests in `tests/test_tui_app_integration.py` per §5.1. Land with the PARE PR.
- **Phase 1 `workers.yaml` flip:** existing `test_workers_yaml.py` tests already cover the shape (autoload false, networked worker declared). Grep at implementation time for any that specifically assert stdio.

### 8.2 Manual acceptance

- **Phase 0:** the acceptance list in §3.3 is checked by hand during the transient session. Record the checked items in the Phase 0 execution notes, alongside the R2 measurement.
- **Phase 1:** after systemd unit deploys, `systemctl status pare-hardware-mcp` reports `active (running)`; `curl -m3 http://100.97.133.126:9102/mcp` reachable from agenthost; `pare-tui` launched with the env var opens a session and the pane renders honestly. Same acceptance shape as Phase 0, minus the "install manually" step.

### 8.3 What's deliberately un-automated

The health-check row on `pare-bench-status`'s page is deferred (§4.5). If it lands later, it becomes an automated check by simply existing.

---

## 9. Not in scope

- **Artifact drive wiring** (`artifact_root != null`). Separate design.
- **Additional targets or a second Tigard on the bench.** Single-target v1.
- **`pare-tui` config-file setting** for the hardware endpoint. Env var is enough; config-file is a trivial addition later.
- **A pare-tui-side "connect via daemon" fallback path** for cases where the pane can't reach the endpoint directly. If the pane can't reach the endpoint, it renders that state honestly (source's own error surface); the operator investigates via `systemctl status` on the Pi. No routing-through-the-daemon path.
- **Automatic pare-tui launch integration** — env var stays operator-set, not auto-detected. Operators who want it set every time can add it to their shell profile.
- **The three follow-ups from `agent-core-1-11-0-followups.md`.** Unrelated, different branch.

---

## 10. Code in this document

None, deliberately. This design is a deployment shape, a config flip, and a small env-var wiring — the concurrency work all lives in already-shipped code (agent_core 1.11.0, pare-tui's `McpConsoleSource` from Task 10). Code written into prose here would be unexecuted, remote from the real files, and authoritative-looking enough to be transcribed past defects that only surface at the Pi's shell. Section 2 gives the runtime shape; §3 and §4 give the sequences; §5 gives the exact config diff. The implementer writes the actual code against the real files and the real Pi.
