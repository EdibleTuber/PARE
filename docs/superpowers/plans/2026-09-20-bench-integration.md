# Bench integration for `pare-hardware-mcp` — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deploy `pare-hardware-mcp` to `pare-bench` (the Pi) as a systemd streamable_http service, flip PARE's `workers.yaml` from stdio to networked transport, wire `pare-tui`'s UART pane to reach the deployed worker via an env var, and bake a measured R2 poll interval into the pane's default.

**Architecture:** Two phases. Phase 0 (transient, operator-run) proves the chain end-to-end against real hardware and measures R2 poll RTT; nothing on the Pi persists. Phase 1 productionizes what Phase 0 proved: `workers.yaml` flip, R2 constant update, docstring correction, deploy script + systemd unit for `pare-hardware-mcp`. Cross-repo: PARE (Tasks 1-3) and `pare-hardware-mcp` (Tasks 4-6). `agent_core` untouched.

**Tech Stack:** Python 3.12+ (PARE) / 3.14 (Pi), Textual 8.x, MCP streamable_http via `pare_worker_kit.run_worker`, systemd, bash. Tailscale-mesh transport (`tailscale0`), no application-layer auth.

**Spec:** `docs/superpowers/specs/2026-09-20-bench-integration-design.md`

## Global Constraints

- Worker bind is `tailscale0`, never a wildcard. `pare_worker_kit.serve` at `pare-worker-kit/src/pare_worker_kit/serve.py:168-175` refuses `0.0.0.0` — do not attempt to work around it.
- `risk_default: high` on the `workers.yaml` `hardware` block stays. The floor exists to gate the *model*, and moving the worker onto the tailnet does not change that.
- `agent_core` is not touched by this plan.
- `getty@tty1` on the Pi is not disabled — the operator's fallback way in stays enabled ([[never-remove-the-operators-only-way-in]]).
- `FakeConsoleSource` stays the default `pare-tui` source. The env var is opt-in; unset means test behaviour is unchanged.
- The four destructive-family pins in `workers.yaml`'s `risk_overrides` block (`hardware_flash_*`, `hardware_erase_*`, `hardware_write_*`, `hardware_glitch_*`) remain forward-declared.
- Plan-supplied code below is a sketch to be verified against real files. Deviating to fix a defect is correct.

---

## File Structure

**PARE repo** (`/mnt/secondary/projects/PARE`, branch `docs/bench-integration-design`):
- Modify: `pare/tui/app.py:121-153` — env-var branch in `_build_uart_pane`
- Modify: `pare/tui/panes/uart.py:39` — `DEFAULT_UART_POLL_INTERVAL` value + naming comment
- Modify: `workers.yaml:97-120` — hardware block flip (comments + endpoint + transport + timeouts + `artifact_root`)
- Modify: `tests/test_workers_yaml.py:44-55` — docstring update in `test_stdio_service_workers_autoload` (hardware is no longer "declared-but-unbuilt")
- Modify: `tests/test_tui_app_integration.py` — add two tests for the env-var branch

**`pare-hardware-mcp` repo** (`/mnt/secondary/projects/pare-hardware-mcp`, new branch `feat/bench-integration`):
- Modify: `src/pare_hardware_mcp/config.py:38-49` — docstring line accuracy fix
- Create: `scripts/bench_deploy.sh` — deploy from a checkout on the Pi with `sha256`/provenance
- Create: `systemd/pare-hardware-mcp.service` — service unit, modeled on PARE's `pare-bench-status.service`

**Operational (Pi, no repo state changes here for the plan itself):**
- Phase 0: ephemeral install into `/tmp/hwmcp-venv/`, run in foreground, `Ctrl-C`
- Phase 1: install to `/opt/pare/hardware-mcp/`, systemd unit at `/etc/systemd/system/pare-hardware-mcp.service`, provenance stamp at `/opt/pare/hardware-mcp/DEPLOYED_FROM`

---

## Task 1: `pare-tui` env-var wiring

**Files:**
- Modify: `pare/tui/app.py:120-153`
- Modify: `tests/test_tui_app_integration.py` (append at end of file)
- Test: `tests/test_tui_app_integration.py`

**Interfaces:**
- Consumes: `pare.tui.sources.mcp_console.McpConsoleSource.__init__(*, endpoint: str, worker_prefix: str = "", ...)` — from Task 10 of the pare-tui plan, already merged. `worker_prefix` defaults to `""` per D6 of the spec — do not pass it explicitly.
- Consumes: `pare.tui.sources.fake_console.FakeConsoleSource` — Task 5 of the pare-tui plan, already merged.
- Consumes: `pare.tui.panes.uart.UartPane` — Task 9, already merged.
- Produces: nothing new; this task only rewires an existing call site.

**Context:** Spec §5.1. Reader for `PARE_TUI_HARDWARE_ENDPOINT`; unset → `FakeConsoleSource()`, set → `McpConsoleSource(endpoint=<value>)`. This is a two-line change in `_build_uart_pane`; the docstring comment in that method explaining "there is no deployed Pi in this plan's test bed" needs a small addendum acknowledging the env-var branch now exists.

- [ ] **Step 1: Add failing test — env var unset → FakeConsoleSource**

Append to `tests/test_tui_app_integration.py`:

```python
def test_build_uart_pane_defaults_to_fake_when_env_var_unset(monkeypatch):
    """Unset env var is the default: FakeConsoleSource, unchanged from Task 9.
    Regression guard against a future change that silently makes the pane
    require a live Pi endpoint."""
    monkeypatch.delenv("PARE_TUI_HARDWARE_ENDPOINT", raising=False)
    from pare.tui.app import PareTuiApp
    from pare.tui.sources.fake_console import FakeConsoleSource

    app = PareTuiApp(channel_id="test", cwd="/tmp")
    pane = app._build_uart_pane()
    assert isinstance(pane.source, FakeConsoleSource)


def test_build_uart_pane_uses_mcp_source_when_env_var_set(monkeypatch):
    """Set env var → McpConsoleSource(endpoint=<value>). Constructs the
    source; does NOT attach (no live transport in tests)."""
    monkeypatch.setenv("PARE_TUI_HARDWARE_ENDPOINT", "http://100.97.133.126:9102/mcp")
    from pare.tui.app import PareTuiApp
    from pare.tui.sources.mcp_console import McpConsoleSource

    app = PareTuiApp(channel_id="test", cwd="/tmp")
    pane = app._build_uart_pane()
    assert isinstance(pane.source, McpConsoleSource)
    assert pane.source.endpoint == "http://100.97.133.126:9102/mcp"
```

Import structure at the top of the file may already have `PareTuiApp`; if not, add it. `PareTuiApp` is the app class in `pare/tui/app.py` — verify the name at implementation time (grep `pare/tui/app.py` for `class .*App` if uncertain).

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /mnt/secondary/projects/PARE
.venv/bin/pytest tests/test_tui_app_integration.py::test_build_uart_pane_defaults_to_fake_when_env_var_unset tests/test_tui_app_integration.py::test_build_uart_pane_uses_mcp_source_when_env_var_set -v
```

Expected: the "unset" test may PASS incidentally (existing code returns `FakeConsoleSource` unconditionally); the "set" test FAILS with an assertion that the source is `FakeConsoleSource` when the code says it should be `McpConsoleSource`. That's the discriminating failure.

- [ ] **Step 3: Implement env-var branch in `_build_uart_pane`**

In `pare/tui/app.py`, edit `_build_uart_pane` (line ~121-153). The current body ends with:

```python
        return UartPane(
            source=FakeConsoleSource(),
            channel_id=self.channel_id,
            cwd=self.cwd,
            daemon_session=None,
            id="uart-pane",
        )
```

Change to construct source from the env var:

```python
        endpoint = os.environ.get("PARE_TUI_HARDWARE_ENDPOINT")
        if endpoint:
            from pare.tui.sources.mcp_console import McpConsoleSource
            source = McpConsoleSource(endpoint=endpoint)
        else:
            source = FakeConsoleSource()
        return UartPane(
            source=source,
            channel_id=self.channel_id,
            cwd=self.cwd,
            daemon_session=None,
            id="uart-pane",
        )
```

Verify `os` and `McpConsoleSource` imports:
- `os` should already be imported at the top of `pare/tui/app.py`. If not, add `import os`.
- `McpConsoleSource` is imported lazily inside the branch so the "unset" default keeps its zero-cost import path (matches the FakeConsoleSource module-top import pattern already used).

Also update the docstring in `_build_uart_pane` (currently starts at line 122): add a short paragraph acknowledging the env-var branch — e.g. "Setting `PARE_TUI_HARDWARE_ENDPOINT` at launch swaps `FakeConsoleSource` for `McpConsoleSource(endpoint=...)`; unset leaves the fake as the default so existing tests keep passing without modification. Spec: `docs/superpowers/specs/2026-09-20-bench-integration-design.md` §5.1."

- [ ] **Step 4: Run tests to verify they pass**

```bash
.venv/bin/pytest tests/test_tui_app_integration.py::test_build_uart_pane_defaults_to_fake_when_env_var_unset tests/test_tui_app_integration.py::test_build_uart_pane_uses_mcp_source_when_env_var_set -v
```

Expected: both PASS.

- [ ] **Step 5: Run full PARE suite**

```bash
.venv/bin/pytest -q
```

Expected: 1021 passed / 7 skipped (baseline after PR #71) plus the 2 new tests → 1023 passed. No regressions.

- [ ] **Step 6: Commit**

```bash
git add pare/tui/app.py tests/test_tui_app_integration.py
git commit -m "$(cat <<'EOF'
feat(tui): env-var wiring for the hardware endpoint

PARE_TUI_HARDWARE_ENDPOINT unset → FakeConsoleSource (default,
unchanged). Set → McpConsoleSource(endpoint=<value>). Existing
tests keep passing without modification.

Spec: docs/superpowers/specs/2026-09-20-bench-integration-design.md §5.1

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01VatXHkDJSMKf57UVL76KbN
EOF
)"
```

---

## Operator Step A: Phase 0 transient verification (SDD skips; controller runs with the user)

**When:** after Task 1's PARE commit lands on the branch, before Task 2.

**What:** the sequence in spec §3.2 (transient install on Pi, foreground run, reachability probe, `pare-tui` launch with the env var, R2 measurement). Nothing on the Pi persists past `Ctrl-C`.

**Output:** an R2 measurement (median + p95 poll RTT, in seconds), recorded in the ledger for Task 2 to consume, plus the acceptance items in spec §3.3 marked done.

**Rollback:** `Ctrl-C` the worker; `rm -rf /tmp/hwmcp-venv/`; unset `PARE_TUI_HARDWARE_ENDPOINT`.

This step is NOT dispatched to a subagent. The controller pauses SDD, walks the operator through it, records the number, then resumes with Task 2.

---

## Task 2: bake the measured R2 into `UartPane`'s default

**Files:**
- Modify: `pare/tui/panes/uart.py:39` (`DEFAULT_UART_POLL_INTERVAL`)
- Test: existing `pare/tui/panes/uart.py` tests must all still pass; no new tests.

**Interfaces:**
- Consumes: the R2 measurement from Operator Step A (median + p95 RTT).
- Produces: `DEFAULT_UART_POLL_INTERVAL` updated to a value that respects the spec §6.2 floor (`p95 * 2`) and the interactive ceiling (~0.5s).

**Context:** Spec §6.3. The current value (`0.5`) was Task 9's placeholder from the pare-tui plan — a stall floor picked without measurement. The measured value replaces it; the comment records the date and median so a later reader knows it was measured, not picked.

- [ ] **Step 1: Compute the new value from Operator Step A's numbers**

Rule per spec §6.2:
- Floor: `max(0.05, p95 * 2)`. Below 0.05 back-to-back polls stack on a variable link.
- Ceiling: `0.5`. Above that the pane feels laggy.

If the floor exceeds 0.5, that's a Phase 0 red flag (the transport is slower than the pane can absorb); pause and record it in the ledger before proceeding. Otherwise pick `round(max(0.05, p95 * 2), 2)`.

- [ ] **Step 2: Update the constant and its comment**

In `pare/tui/panes/uart.py:39`, replace:

```python
DEFAULT_UART_POLL_INTERVAL = 0.5
```

with (example, substituting the measured value; if the number below happens to match the measurement it's coincidence, not a shortcut around measuring):

```python
# Measured 2026-09-20 against pare-bench (100.97.133.126) over tailscale0.
# Median console_read RTT: <MEDIAN>s, p95: <P95>s.
# Floor rule (spec §6.2): p95 * 2 = <FLOOR>s; interactive ceiling 0.5s.
# Re-measure when the endpoint, tailscale wire type, or transport changes.
DEFAULT_UART_POLL_INTERVAL = <VALUE>
```

Fill `<MEDIAN>`, `<P95>`, `<FLOOR>`, and `<VALUE>` from Operator Step A. Keep them to 2 decimal places for readability.

- [ ] **Step 3: Run the uart-pane tests**

```bash
.venv/bin/pytest tests/test_tui_uart_pane.py -q
```

Expected: no regressions. If any test asserted a literal 0.5 (spec's guidance is to assert properties, not magic constants — check `[[quote-the-spec-in-plans-dont-restate-it]]` and `[[a-broken-test-is-not-a-reason-to-delete-the-control]]`), update to assert `> 0` or the range property rather than the number.

- [ ] **Step 4: Run full PARE suite**

```bash
.venv/bin/pytest -q
```

Expected: same count as after Task 1, no regressions.

- [ ] **Step 5: Commit**

```bash
git add pare/tui/panes/uart.py
git commit -m "$(cat <<'EOF'
feat(tui): bake measured R2 poll interval from Phase 0

Median console_read RTT <MEDIAN>s, p95 <P95>s, measured 2026-09-20
against pare-bench over tailscale0. Poll interval set from
max(0.05, p95 * 2) rounded to 2 decimals, under the 0.5s ceiling.
Comment names the conditions under which to re-measure.

Spec: docs/superpowers/specs/2026-09-20-bench-integration-design.md §6

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01VatXHkDJSMKf57UVL76KbN
EOF
)"
```

---

## Task 3: `workers.yaml` flip

**Files:**
- Modify: `workers.yaml:97-120` (the pre-flip comment block + hardware entry)
- Modify: `tests/test_workers_yaml.py:44-55` (docstring in `test_stdio_service_workers_autoload`)
- Test: `tests/test_workers_yaml.py`

**Interfaces:**
- Consumes: the Pi's endpoint `http://100.97.133.126:9102/mcp` (fixed, verified in spec §2.1).
- Produces: PARE's daemon dispatch path now targets the deployed worker over streamable_http instead of a local stdio subprocess.

**Context:** Spec §5. The floor comment stays verbatim; the surrounding block replaces "still declared as stdio" language with the deployed state. Also removes the pre-block comment paragraph explaining "declared as stdio because pare-hardware-mcp does not exist yet" — that reasoning is now stale.

- [ ] **Step 1: Read the current block to confirm anchors**

```bash
sed -n '95,125p' /mnt/secondary/projects/PARE/workers.yaml
```

Expected: the block starting with `# Declared but NOT connected at boot: hardware work is occasional...` (line 97) and ending with `capability_tags: [hardware, uart, jtag, glitch]` (line 120).

- [ ] **Step 2: Replace the block**

Replace lines 97-120 of `workers.yaml` with (indentation matches the surrounding `workers:` map):

```yaml
  # Declared but NOT connected at boot: hardware work is occasional, and its
  # tool schemas cost context in every turn that isn't doing it. Load it when a
  # target actually has hardware in play:  /worker load hardware
  #
  # Networked hardware worker on pare-bench (100.97.133.126). Systemd unit
  # /etc/systemd/system/pare-hardware-mcp.service; deploys from a checkout at
  # /opt/pare/hardware-mcp/ (see pare-hardware-mcp/scripts/bench_deploy.sh).
  # risk_default remains high -- the tailnet transport does not relax the
  # model-containment floor; see the block below.
  hardware:
    endpoint: http://100.97.133.126:9102/mcp
    transport: streamable_http
    connect_timeout: 20
    read_timeout: 60
    # FLOOR -- raised from medium. RiskAwareToolPool gates only high and
    # critical (risk_pool.py:386), so a medium floor meant any hardware tool
    # that failed to advertise a wire tier -- a half-wired dev build, or a
    # tampered one under-reporting -- DISPATCHED WITH NO PROMPT AT ALL,
    # bypassing every approval surface. A floor of high means the worst case
    # is a prompt, not a silent flash write.
    risk_default: high
    autoload: false        # REQUIRED for networked workers
    capability_tags: [hardware, uart, jtag, glitch]
    artifact_root: null    # placeholder; wiring is a separate design
```

The FLOOR comment block is copied verbatim from the current file — preserve the wording exactly, including the risk_pool.py line reference. The `autoload: false` inline comment is new; it names the reason (networked-workers spec makes it required, not merely advisable).

- [ ] **Step 3: Update `test_stdio_service_workers_autoload` docstring**

In `tests/test_workers_yaml.py:44-55`, the current docstring says:

> "hardware is excluded because it is declared-but-unbuilt (see test_hardware_is_declared_but_not_autoloaded)."

That reasoning is now stale — hardware is built AND deployed. Replace with:

> "hardware is excluded because it is now networked (see test_networked_workers_never_autoload above); the flip landed 2026-09-20 (spec: 2026-09-20-bench-integration-design.md §5)."

Keep the rest of the docstring intact. No code change to the test body — the test already covers only `static` and `mitm`.

- [ ] **Step 4: Run the workers.yaml tests**

```bash
.venv/bin/pytest tests/test_workers_yaml.py -v
```

Expected: all pass, including `test_networked_workers_never_autoload` (hardware now falls under it) and `test_hardware_is_declared_but_not_autoloaded` (autoload stays false).

- [ ] **Step 5: Grep for any test that asserts hardware is stdio**

```bash
grep -rn "hardware.*stdio\|stdio.*hardware" tests/ 2>/dev/null | grep -v ".pyc"
```

Expected: only the docstring line just updated. If any live assertion appears, update it per spec §5.3.

- [ ] **Step 6: Run full PARE suite**

```bash
.venv/bin/pytest -q
```

Expected: same count as after Task 2, no regressions.

- [ ] **Step 7: Commit**

```bash
git add workers.yaml tests/test_workers_yaml.py
git commit -m "$(cat <<'EOF'
feat(workers): flip hardware from stdio to streamable_http on pare-bench

Endpoint http://100.97.133.126:9102/mcp (see spec §5). risk_default
stays high -- the tailnet transport does not relax the
model-containment floor. FLOOR comment preserved verbatim. Updates
test_stdio_service_workers_autoload's docstring: hardware is no
longer declared-but-unbuilt, it is now networked.

Spec: docs/superpowers/specs/2026-09-20-bench-integration-design.md §5

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01VatXHkDJSMKf57UVL76KbN
EOF
)"
```

---

## Task 4: `pare-hardware-mcp` — fix stale docstring line

**Repo:** `/mnt/secondary/projects/pare-hardware-mcp` (branch: `feat/bench-integration`, off `main`).

**Files:**
- Modify: `src/pare_hardware_mcp/config.py:38-49`
- Test: `pytest -q` for the pare-hardware-mcp suite.

**Interfaces:**
- Consumes: PARE Task 3 has been merged, so `workers.yaml` now declares hardware as streamable_http.
- Produces: no interface change; docstring accuracy fix only.

**Context:** Spec §4.4 and §5.2. `config.py:38-39` currently says "This worker is still declared `transport: stdio` in workers.yaml…" — this stopped being true when Task 3 landed. Per `[[a-broken-test-is-not-a-reason-to-delete-the-control]]` and CLAUDE.md's "prefer fixes that stop the clock over fixes that reset it," rewrite the docstring to describe the CURRENT deployed state and reference the design doc, so the next reader isn't misled and a future change to the deploy shape doesn't leave a fresh lie.

- [ ] **Step 1: Set up worktree**

```bash
cd /mnt/secondary/projects/pare-hardware-mcp
git checkout main
git pull
git checkout -b feat/bench-integration
```

- [ ] **Step 2: Read the current docstring line**

```bash
sed -n '35,55p' /mnt/secondary/projects/pare-hardware-mcp/src/pare_hardware_mcp/config.py
```

Expected: the paragraph starting "This worker is still declared `transport: stdio`…" spanning lines 38-49 (the `request_deadline_s` field's docstring above the assignment).

- [ ] **Step 3: Update the docstring**

Replace the specific sentence "This worker is still declared `transport: stdio` in workers.yaml (see the module docstring), so there is no real `read_timeout` to read yet -- 60 matches the `read_timeout` already used by another networked worker's entry there (frida), so this tracks a real precedent rather than an invented number." with:

> "As of 2026-09-20 this worker is declared `transport: streamable_http` in PARE's `workers.yaml` and has its own `read_timeout: 60` declared there — that number is the operator's authority; this default is a local fallback when the worker is exercised out of process (tests, manual runs). See PARE `docs/superpowers/specs/2026-09-20-bench-integration-design.md` §4-5."

Keep the rest of the docstring's reasoning intact (the `PARE_HW_REQUEST_DEADLINE_S` explanation, the operator-override language). This is a single-sentence surgical fix, not a rewrite of the whole paragraph.

Also check the module docstring at the top of `config.py` for the same "still declared stdio" language and update it in the same commit if present:

```bash
head -30 /mnt/secondary/projects/pare-hardware-mcp/src/pare_hardware_mcp/config.py
```

- [ ] **Step 4: Run pare-hardware-mcp suite**

```bash
cd /mnt/secondary/projects/pare-hardware-mcp
.venv/bin/pytest -q 2>&1 | tail -5
```

Expected: no regressions. If `.venv` doesn't exist on this repo, sync deps first (`pip install -e '.[dev]'` in a fresh venv).

- [ ] **Step 5: Commit**

```bash
git add src/pare_hardware_mcp/config.py
git commit -m "$(cat <<'EOF'
docs(config): the worker is now streamable_http on pare-bench

The 'still declared stdio in workers.yaml' language stopped being
true when PARE's workers.yaml flip landed 2026-09-20. Rewrites the
one sentence to describe the CURRENT deployed state and points at
the design doc for the wiring.

Related: PARE docs/superpowers/specs/2026-09-20-bench-integration-design.md

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01VatXHkDJSMKf57UVL76KbN
EOF
)"
```

---

## Task 5: `pare-hardware-mcp` — bench deploy script

**Repo:** `/mnt/secondary/projects/pare-hardware-mcp`, same branch as Task 4.

**Files:**
- Create: `scripts/bench_deploy.sh` (executable)

**Interfaces:**
- Consumes: a git checkout of `pare-hardware-mcp` present on the Pi at some path. The script deploys from that checkout to `/opt/pare/hardware-mcp/`.
- Produces: `/opt/pare/hardware-mcp/{__init__.py, src/..., pyproject.toml, ...}` copied via rsync; `.venv/` created with `pip install -e .`; provenance stamp at `/opt/pare/hardware-mcp/DEPLOYED_FROM`; systemd unit installed at `/etc/systemd/system/pare-hardware-mcp.service`.

**Context:** Spec §4.2. Model on PARE's `scripts/bench_deploy.sh` (118 lines; readable at `/mnt/secondary/projects/PARE/scripts/bench_deploy.sh`). Same shape:
1. `--check` flag runs read-only sha256 comparison against the deployed copy, exits 1 on drift.
2. Refuses to run outside a git checkout.
3. Refuses to run under a modified working tree without an override (matches PARE's discipline).
4. Copies files, sha256-verifies each after copy, writes `DEPLOYED_FROM` only when every file matches.
5. Reloads the systemd unit iff any unit file changed.

**IMPORTANT:** This script runs ON THE PI (aarch64, Ubuntu 26.04, Python 3.14). It expects `rsync`, `sha256sum`, `git`, `systemctl` — all available in the Pi's base image.

- [ ] **Step 1: Read PARE's bench_deploy.sh for the exact shape**

```bash
cat /mnt/secondary/projects/PARE/scripts/bench_deploy.sh
```

Note especially: the `FILES=` array format (`repo-path:destination`), the sha256 comparison loop, the drift printout, and the sudo/root check.

- [ ] **Step 2: Create `scripts/bench_deploy.sh`**

Create `/mnt/secondary/projects/pare-hardware-mcp/scripts/bench_deploy.sh` with:
- Shebang: `#!/usr/bin/env bash`
- `set -uo pipefail` (matches PARE's).
- `REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"` and `DEST=/opt/pare/hardware-mcp`.
- A file map covering:
  - `pyproject.toml`
  - `src/pare_hardware_mcp/` (recursive — the script can rsync the tree rather than list files, but the sha256 loop needs a way to compare it; simplest: rsync the tree, then compare `find src -type f`).
  - `systemd/pare-hardware-mcp.service:/etc/systemd/system/pare-hardware-mcp.service`.
- After rsync, run `pip install -e .` inside `$DEST/.venv` (create it if missing: `python3 -m venv "$DEST/.venv"`).
- After success, write `DEPLOYED_FROM` with `git -C "$REPO" rev-parse HEAD` and an ISO8601 timestamp.
- `--check` short-circuits after the drift report, no sudo needed.
- If any unit file changed, `systemctl daemon-reload` and print a note that the unit needs enabling/restarting (do NOT enable it automatically in this script — that's Operator Step B).

Because the source tree includes a directory (`src/pare_hardware_mcp/`), the sha256 pattern needs adjusting: instead of one hash per file in the map, produce a stable hash of the tree (`tar --sort=name -cf - src/pare_hardware_mcp | sha256sum`) and compare that. Or list every source file explicitly, which is more verbose but matches PARE's per-file approach. Pick whichever leaves the drift report legible; both work.

The implementer's judgment call: whichever pattern is cleaner given the actual repo layout at implementation time. Prefer per-file if the source tree is under ~10 files (currently it is: `server.py`, `session.py`, `tools.py`, `config.py`, `baud.py`, `__init__.py`, and a couple more).

- [ ] **Step 3: `chmod +x` the script**

```bash
chmod +x /mnt/secondary/projects/pare-hardware-mcp/scripts/bench_deploy.sh
```

- [ ] **Step 4: Local dry-run (--check)**

```bash
cd /mnt/secondary/projects/pare-hardware-mcp
./scripts/bench_deploy.sh --check 2>&1 | head -40
```

Expected: prints the checkout HEAD, then for each file lists STALE (deployed absent) because we're not on the Pi and `/opt/pare/hardware-mcp/` doesn't exist locally. Exits 1. No syntax errors, no missing-command errors. The exact exit code and shape confirm the script is well-formed without requiring the Pi.

- [ ] **Step 5: Shellcheck (if available)**

```bash
shellcheck scripts/bench_deploy.sh 2>&1 | head -20 || echo "(shellcheck not installed, skipping)"
```

Fix any warnings shellcheck raises about actual bugs (unquoted expansions, undefined vars). Style-only warnings can be ignored.

- [ ] **Step 6: Commit**

```bash
git add scripts/bench_deploy.sh
git commit -m "$(cat <<'EOF'
feat(deploy): bench_deploy.sh for pare-bench (Pi)

Deploys pare-hardware-mcp from a git checkout on the Pi to
/opt/pare/hardware-mcp/, with sha256 verification per file and a
DEPLOYED_FROM provenance stamp. Same shape as PARE's
scripts/bench_deploy.sh. --check is read-only and needs no sudo.

Does NOT enable the systemd unit -- that's the operator's step
after first deploy, per spec §4-7.

Spec: PARE docs/superpowers/specs/2026-09-20-bench-integration-design.md §4.2

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01VatXHkDJSMKf57UVL76KbN
EOF
)"
```

---

## Task 6: `pare-hardware-mcp` — systemd unit

**Repo:** `/mnt/secondary/projects/pare-hardware-mcp`, same branch as Tasks 4-5.

**Files:**
- Create: `systemd/pare-hardware-mcp.service`

**Interfaces:**
- Consumes: `/opt/pare/hardware-mcp/.venv/bin/pare-hardware-mcp` (installed by Task 5's script).
- Produces: a systemd unit that, when enabled and started, runs the worker as user `pare` bound to `tailscale0:9102`.

**Context:** Spec §4.3. Model on `bench/systemd/pare-bench-status.service` (readable in PARE at `/mnt/secondary/projects/PARE/bench/systemd/pare-bench-status.service`). Copy its shape and adapt.

- [ ] **Step 1: Read the reference unit**

```bash
cat /mnt/secondary/projects/PARE/bench/systemd/pare-bench-status.service
```

Note the `[Unit]` `After=network.target` and `StartLimitIntervalSec=0`, the `[Service]` hardening block (`NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=strict`, `ProtectHome`, `ReadOnlyPaths`), and the `[Install] WantedBy=multi-user.target`.

- [ ] **Step 2: Create `systemd/pare-hardware-mcp.service`**

Create `/mnt/secondary/projects/pare-hardware-mcp/systemd/pare-hardware-mcp.service` with:

```ini
# Networked hardware MCP worker on pare-bench. Serves streamable_http on
# tailscale0:9102 -- the bind address is the trust boundary
# (pare-worker-kit refuses 0.0.0.0). PARE's daemon dispatches through
# /etc/pare/workers.yaml's hardware: entry.
#
# Install:  sudo cp systemd/pare-hardware-mcp.service /etc/systemd/system/
#           sudo systemctl daemon-reload
#           sudo systemctl enable --now pare-hardware-mcp
#
# Depends on hardware: /dev/serial/by-id/usb-SecuringHardware.com_Tigard_V1.1_TG1119e7-if01-port0
# must be present for the worker's console tools to open a session -- but the
# worker itself starts regardless and reports the honest error at tool time.

[Unit]
Description=PARE hardware MCP worker (Tigard UART / JTAG / glitch)
# network.target only, NOT network-online.target: the worker binds
# tailscale0 which will exist as soon as tailscaled is up; we don't want
# to block on external reachability that may never come.
After=network.target tailscaled.service
Wants=tailscaled.service

# A crash loop must not silently disappear the worker. Matches
# pare-bench-status.service's reasoning -- StartLimitBurst=5 would give
# up after five restarts and PARE's daemon would see connect-refused
# with no signal from the Pi about why.
StartLimitIntervalSec=0

[Service]
Type=simple
User=pare
Group=pare
# dialout for /dev/ttyUSB* access (verified 2026-09-18 on pare-bench).
SupplementaryGroups=dialout
WorkingDirectory=/opt/pare/hardware-mcp
ExecStart=/opt/pare/hardware-mcp/.venv/bin/pare-hardware-mcp
Environment=AGENT_WORKER_TRANSPORT=http
Environment=AGENT_WORKER_HOST=tailscale0
Environment=AGENT_WORKER_PORT=9102
Environment=PARE_HW_DEVICE=/dev/serial/by-id/usb-SecuringHardware.com_Tigard_V1.1_TG1119e7-if01-port0
Environment=PARE_HW_EXPECT_SERIAL=TG1119e7
Environment=PYTHONUNBUFFERED=1
Restart=on-failure
RestartSec=5

# Least privilege. The worker opens a TCP listen socket and reads/writes
# a specific /dev/ttyUSB* character device -- nothing else on the box
# should be reachable from a compromised worker process.
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadOnlyPaths=/opt/pare/hardware-mcp
# /dev needs a specific carve-out so udev symlinks resolve and the ttyUSB
# device node is openable. DeviceAllow narrows the /dev exposure.
DevicePolicy=closed
DeviceAllow=char-ttyUSB rw
# tailscaled's AF_UNIX socket needs write to /run/tailscale (see the
# reference unit's comment on why -- connect() needs write on the inode).
# The leading `-` makes it optional so the unit still starts if the dir
# isn't there yet at first boot.
ReadWritePaths=-/run/tailscale

[Install]
WantedBy=multi-user.target
```

The unit above is a sketch: verify `char-ttyUSB` is the right `DeviceAllow` shape on Ubuntu 26.04 (spec-check via `systemd-analyze verify` at Step 3); if it fails, drop `DevicePolicy=closed`/`DeviceAllow` and rely on group `dialout` membership alone. The reference unit doesn't need `DeviceAllow` at all because it doesn't open a device — the hardware worker does, so this needs its own verification.

- [ ] **Step 3: Verify unit syntax**

```bash
systemd-analyze verify /mnt/secondary/projects/pare-hardware-mcp/systemd/pare-hardware-mcp.service 2>&1 | head -20
```

Expected: no syntax errors. Warnings about missing paths (`/opt/pare/hardware-mcp/.venv/bin/pare-hardware-mcp` doesn't exist locally) are fine and expected — `systemd-analyze verify` only checks the unit's syntactic correctness against the systemd schema, not the executability of paths.

If any real error appears (unknown directive, malformed section), fix it and re-verify. If `DeviceAllow=char-ttyUSB rw` produces a warning about unrecognized syntax, drop that line and its `DevicePolicy` companion — the `dialout` group membership is enough.

- [ ] **Step 4: Grep for any test asserting the unit's contents**

```bash
grep -rn "pare-hardware-mcp.service\|pare-hardware-mcp" tests/ 2>/dev/null | head -10
```

Expected: no matches (this is a new file). If matches appear, review them.

- [ ] **Step 5: Commit**

```bash
git add systemd/pare-hardware-mcp.service
git commit -m "$(cat <<'EOF'
feat(systemd): pare-hardware-mcp.service unit for pare-bench

Runs the worker as user pare bound to tailscale0:9102, dialout group
for /dev/ttyUSB* access, systemd-hardened per pare-bench-status
precedent. StartLimitIntervalSec=0 keeps the worker restarting on
transient failures rather than giving up silently after five restarts.
After=network.target (not network-online.target) so a boot with no
external network still starts the worker.

Spec: PARE docs/superpowers/specs/2026-09-20-bench-integration-design.md §4.3

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01VatXHkDJSMKf57UVL76KbN
EOF
)"
```

---

## Operator Step B: Phase 1 deployment (SDD skips; controller runs with the user)

**When:** after Tasks 4-6 have merged to `main` on `pare-hardware-mcp` AND Tasks 1-3 have merged on PARE.

**What:**
1. SSH to `pare-bench`, `git clone` (or `git pull`) the pare-hardware-mcp repo into `/home/pare/pare-hardware-mcp` (or wherever the operator prefers).
2. Run `./scripts/bench_deploy.sh --check` to see the drift report.
3. Run `sudo ./scripts/bench_deploy.sh` to install.
4. `sudo systemctl enable --now pare-hardware-mcp`.
5. Verify: `systemctl status pare-hardware-mcp` (should be `active (running)`), `curl -m3 http://100.97.133.126:9102/mcp` from agenthost (should return MCP-shaped bytes, not connection-refused).
6. Launch `pare-tui` with `PARE_TUI_HARDWARE_ENDPOINT=http://100.97.133.126:9102/mcp` and confirm the pane attaches — [[verify-the-surface-you-hand-the-user-to]] applies: an API 200 is not the pane rendering; open what the user will open.

**Rollback:** `sudo systemctl disable --now pare-hardware-mcp`; revert the PARE workers.yaml commit; leave `/opt/pare/hardware-mcp/` in place or `rm -rf` it — either is safe.

This step is NOT dispatched to a subagent. The controller pauses, walks the operator through it, records success or the specific failure, then decides whether the plan is complete or needs a fix round.

---

## Self-Review

**1. Spec coverage.** Each spec section maps to a task or an operator step:
- Spec §1 D1-D8 (decisions): D1 (worker on Pi) is enacted by Tasks 5-6 + Operator Step B; D2 (phased) is the plan's overall shape; D3 (bind tailscale0) is in Task 6's Environment= block; D4 (risk_default high stays) is in Task 3's yaml; D5 (env-var, fake default) is in Task 1; D6 (worker_prefix "") is in Task 1's code; D7 (measured R2) is Operator Step A + Task 2; D8 (artifact_root null) is in Task 3's yaml.
- Spec §2 (architecture): informs Task 6's unit and Task 3's yaml comment.
- Spec §3 (Phase 0 transient): Operator Step A.
- Spec §4 (Phase 1 persistence): Tasks 5-6 + Operator Step B.
- Spec §5 (workers.yaml flip): Task 3.
- Spec §5.1 (env-var wiring): Task 1.
- Spec §5.2 (config.py docstring): Task 4.
- Spec §6 (R2 measurement): Operator Step A + Task 2.
- Spec §7 (rollback/recovery): documented in each Operator Step.
- Spec §8 (testing strategy): Task 1 adds 2 tests, Task 3 updates 1 docstring; Phase 0/1 acceptance is Operator Steps.
- Spec §9 (not in scope): plan honors it — no artifact_root wiring, no config file for pare-tui, no health-check row on status page.

**2. Placeholder scan.** No "TBD" or "TODO" left. `<MEDIAN>`, `<P95>`, `<FLOOR>`, `<VALUE>` in Task 2 Step 2 are deliberate placeholders that Operator Step A fills — they are not plan gaps.

**3. Type consistency.** `McpConsoleSource(endpoint=...)` — verified against `pare/tui/sources/mcp_console.py:77-104`. `FakeConsoleSource()` — verified as no-arg. `UartPane(source=..., channel_id=..., cwd=..., daemon_session=None, id=...)` — verified against existing `_build_uart_pane`. `DEFAULT_UART_POLL_INTERVAL` — verified at `pare/tui/panes/uart.py:39`.

**4. Cross-repo ordering.** Task 4 (config.py docstring) says "Task 3 has merged" — reflects the real ordering (PARE workers.yaml flip must land before pare-hardware-mcp docstring can honestly describe the state). But the pare-hardware-mcp tasks (4-6) don't functionally depend on PARE having merged — the docstring update can precede or follow the merge, as long as it accurately describes the deployed state at the time it's committed. Rework: Task 4's docstring text says "As of 2026-09-20 this worker is declared..." which is a factual claim that must be true when the reader reads it — so the merge order is either (a) PARE first then pare-hardware-mcp docstring, or (b) both branches ready, PARE merges, docstring commit follows immediately. Either works. This is called out as an implementation-time choice, not a plan defect.

**5. Task granularity.** Tasks 1-3 each have their own review surface. Tasks 4-6 are all on pare-hardware-mcp; they touch different files (docstring, script, unit) with different failure modes, so keeping them separate lets a reviewer gate them individually. Operator steps A and B are correctly out-of-scope for SDD dispatch.

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-09-20-bench-integration.md`.**

Per Auto mode: proceeding to Subagent-Driven Development for Tasks 1-6, with Operator Steps A and B run inline with the user.

REQUIRED SUB-SKILL: superpowers:subagent-driven-development
