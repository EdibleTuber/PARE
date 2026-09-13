# pare-hardware-mcp Phase 1 (the worker) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A `pare-worker-kit` MCP worker exposing a Tigard's UART console, runnable and drivable against a pty and against the real adapter.

**Architecture:** A new repo, `pare-hardware-mcp`, laid out like the three existing workers: a `contract.py` of bare-named `ToolSpec`s, a `server.py` that turns them into FastMCP tools carrying wire metadata, and handler modules. Capture begins when the port opens and lands in a ring buffer; readers hold a cursor. The worker owns exactly one serial session at a time.

**Tech Stack:** Python ≥3.12, `mcp`, `pare-worker-kit`, `pyserial`. No `agent_core`, ever.

**Spec:** `docs/superpowers/specs/2026-09-12-pare-hardware-mcp-design.md`

**Not in this plan:** the systemd unit, `bench_deploy.sh`, `workers.yaml`, `POLL_TOOLS`, PARE's CI and pins, and the diagnosis probes. Those are Plan B (bench integration) and depend on this plan landing first.

## Global Constraints

- `requires-python = ">=3.12"`. The Pi runs 3.14.4; CI pins 3.12 like the sibling workers.
- Dependencies are **exactly** `mcp>=1.27.0,<2`, `pare-worker-kit` (git tag), `pyserial`. **Never `agent_core`** — it is the client side, and importing it drags 21 modules plus `trafilatura`, `markitdown[...]`, `rich` and `prompt-toolkit` onto a Pi for a constant.
- `[tool.hatch.metadata] allow-direct-references = true` — without it hatchling hard-fails on the `pare-worker-kit` git URL.
- **Tool names in `contract.py` are BARE.** `tool_factory.py:52` builds `f"{worker.name}_{tool_name}"` and `risk.py:126` builds the pin-match target the same way, so `console_send` here dispatches as `hardware_console_send`. Writing the prefix in the contract yields `hardware_hardware_console_send` and silently breaks the pin.
- **`contract.py` must import with no `pyserial` and no hardware present.** PARE's CI will import it on a runner to validate risk pins (Plan B). Hardware imports live in handler modules, never at contract import time.
- Every tool declares a valid `risk_tier` and a `produces`. Phase 1 is `produces = PRODUCES_RESULT` throughout; nothing produces an artifact.
- `FastMCP("pare-hardware-mcp")` — the server name must equal the distribution name, because `stamp_version` resolves the reported version from installed package metadata keyed by that name, and `serverInfo` is a networked worker's only provenance.
- Target bytes are **untrusted**. They reach the model, the capture store, `/snapshot` and the approval prompt. They travel base64-encoded and are never rendered raw into an operator surface.
- `open_artifact`, `artifact_root` and descriptors are **out of scope**. Phase 1 produces no artifacts.

---

### Task 1: Repo scaffold and the hardware-free contract

**Files:**
- Create: `pyproject.toml`, `src/pare_hardware_mcp/__init__.py`, `src/pare_hardware_mcp/contract.py`, `.github/workflows/test.yml`, `README.md`, `.gitignore`
- Test: `tests/unit/test_contract.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `ToolSpec(name, risk_tier, description, input_schema, output_schema, produces)` frozen dataclass; `TOOL_SPECS: list[ToolSpec]`; `CONTRACT_VERSION = 1`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_contract.py
"""The contract is the one module PARE's CI imports on a runner with no Pi
attached, so it must not reach hardware at import time."""
from __future__ import annotations

import subprocess
import sys

from pare_worker_kit import PRODUCES_RESULT, VALID_RISK_TIERS

from pare_hardware_mcp.contract import CONTRACT_VERSION, TOOL_SPECS

EXPECTED = {
    "list_devices", "bench_status", "console_detect_baud", "console_open",
    "console_read", "console_send", "console_status", "console_close",
}


def test_the_contract_declares_exactly_the_phase_1_surface():
    assert {s.name for s in TOOL_SPECS} == EXPECTED


def test_no_tool_name_carries_the_worker_prefix():
    # tool_factory prefixes with the workers.yaml key, so a name written
    # `hardware_console_send` here dispatches as hardware_hardware_console_send
    # and the operator pin silently matches nothing.
    for spec in TOOL_SPECS:
        assert not spec.name.startswith("hardware_"), spec.name


def test_every_tool_advertises_a_valid_tier_and_produces_result():
    for spec in TOOL_SPECS:
        assert spec.risk_tier in VALID_RISK_TIERS, spec.name
        assert spec.produces == PRODUCES_RESULT, spec.name


def test_send_is_high_and_open_is_medium_and_the_rest_are_low():
    tiers = {s.name: s.risk_tier for s in TOOL_SPECS}
    assert tiers["console_send"] == "high"
    assert tiers["console_open"] == "medium"
    for name in EXPECTED - {"console_send", "console_open"}:
        assert tiers[name] == "low", name


def test_the_contract_imports_without_pyserial():
    # PARE's CI imports this on a runner to validate risk pins. If the contract
    # pulls in pyserial (or anything that opens a device) that import fails and
    # the pin check silently degrades to "unchecked".
    code = (
        "import sys; sys.modules['serial'] = None;"
        "import pare_hardware_mcp.contract as c; print(len(c.TOOL_SPECS))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert int(out.stdout.strip()) == len(EXPECTED)


def test_contract_version_is_declared():
    assert CONTRACT_VERSION >= 1
```

- [ ] **Step 2: Run it and watch it fail**

Run: `pytest tests/unit/test_contract.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'pare_hardware_mcp'`

- [ ] **Step 3: Write `pyproject.toml`**

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "pare-hardware-mcp"
version = "0.1.0"
description = "Hardware-bench MCP worker for PARE (Tigard UART console)"
readme = "README.md"
requires-python = ">=3.12"
dependencies = [
    "mcp>=1.27.0,<2",   # 2.0 renamed streamablehttp_client; agent_core targets 1.x
    "pare-worker-kit @ git+https://github.com/EdibleTuber/pare-worker-kit.git@v0.1.2",
    "pyserial>=3.5",
]

[project.optional-dependencies]
dev = ["pytest", "pytest-asyncio"]

[project.scripts]
pare-hardware-mcp = "pare_hardware_mcp.server:main"

[tool.hatch.metadata]
# Required for the pare-worker-kit git dependency. Without it hatchling refuses
# ANY direct URL reference -- a hard metadata-generation failure, not a warning.
allow-direct-references = true

[tool.hatch.build.targets.wheel]
packages = ["src/pare_hardware_mcp"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

- [ ] **Step 4: Write `src/pare_hardware_mcp/contract.py`**

```python
"""What this worker exposes, and at what risk tier.

NAMES ARE BARE. The daemon prefixes with the workers.yaml key, so `console_send`
here is dispatched and pinned as `hardware_console_send`. Writing the prefix in
this file produces `hardware_hardware_console_send` on the wire, and the operator
pin -- the only control on the one dangerous tool in phase 1 -- matches nothing,
with no error anywhere.

NOTHING HERE MAY IMPORT HARDWARE. PARE's CI imports this module on a runner with
no Pi and no pyserial in order to validate risk pins against real tool names. An
import that reaches a device turns that check into a silent skip.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pare_worker_kit import PRODUCES_RESULT

CONTRACT_VERSION = 1

_OBJ: dict[str, Any] = {"type": "object", "properties": {}}
_SUMMARY_OUT: dict[str, Any] = {"type": "object", "properties": {
    "summary": {"type": "string"},
}}


@dataclass(frozen=True)
class ToolSpec:
    name: str
    risk_tier: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] = field(default_factory=lambda: dict(_SUMMARY_OUT))
    produces: str = PRODUCES_RESULT


def _in(**props: Any) -> dict[str, Any]:
    return {"type": "object", "properties": props}


TOOL_SPECS: list[ToolSpec] = [
    ToolSpec("list_devices", "low",
             "List serial adapters present, by stable by-id path and serial "
             "number. Target VOLTAGE IS OPERATOR-SET on a physical switch and "
             "is not readable from software -- it is never reported here.",
             dict(_OBJ)),
    ToolSpec("bench_status", "low",
             "Bench health this worker can see: adapters present, artifact "
             "root present and writable, drive id. Cheap, no session needed.",
             dict(_OBJ)),
    ToolSpec("console_detect_baud", "low",
             "Sample the line at each candidate rate and return them RANKED "
             "WITH SCORES, not a verdict: a wrong rate yields plausible "
             "garbage rather than an error, so the caller must see the "
             "evidence. Needs the target to be transmitting; a silent line "
             "reports no-data-at-any-rate rather than guessing.",
             _in(device={"type": "string"},
                 rates={"type": "array", "items": {"type": "integer"}})),
    ToolSpec("console_open", "medium",
             "Open the console and START CAPTURING. Tier medium because this "
             "is not a read-only act: opening asserts DTR/RTS and many boards "
             "reset on DTR.",
             _in(device={"type": "string"}, baud={"type": "integer"},
                 flow={"type": "string"}, dtr={"type": "boolean"},
                 rts={"type": "boolean"})),
    ToolSpec("console_read", "low",
             "Read captured bytes since a cursor. Returns base64 (UART output "
             "is arbitrary bytes and is untrusted), the next cursor, how many "
             "bytes were LOST to buffer wrap, and how many remain unread.",
             _in(session={"type": "string"}, cursor={"type": "integer"},
                 limit={"type": "integer"})),
    ToolSpec("console_send", "high",
             "Send bytes to the target. PINNED high in workers.yaml: a console "
             "at a bootloader prompt can write flash.",
             _in(session={"type": "string"}, data_b64={"type": "string"})),
    ToolSpec("console_status", "low",
             "Session state: open?, device, baud, flow, session age, buffer "
             "head, and whether anything has been dropped.",
             dict(_OBJ)),
    ToolSpec("console_close", "low",
             "Release the port and end the session. The buffer is discarded; "
             "read what you need first.",
             _in(session={"type": "string"})),
]
```

- [ ] **Step 5: Write `src/pare_hardware_mcp/__init__.py`**

```python
"""The PARE hardware-bench worker: a Tigard's UART console, over MCP.

Runs on the machine the hardware is wired to. Depends on `mcp`,
`pare-worker-kit` and `pyserial` -- never `agent_core`, which is the client
side and would drag a daemon's dependency tree onto a Raspberry Pi.
"""
__version__ = "0.1.0"
```

- [ ] **Step 6: Write `.github/workflows/test.yml`**

```yaml
name: tests

on:
  push:
    branches: [main]
  pull_request:

jobs:
  pytest:
    runs-on: ubuntu-latest
    strategy:
      fail-fast: false
      matrix:
        # Matches requires-python = ">=3.12" and the sibling workers.
        python-version: ["3.12"]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python-version }}
      - name: Install
        run: |
          python -m pip install --upgrade pip
          pip install -e ".[dev]"
      - name: Run tests
        run: pytest -v
      - name: The contract must import with no hardware and no pyserial
        # PARE's CI imports this module on a runner to validate risk pins. If it
        # ever needs a device, that check degrades to a silent skip.
        run: |
          python - <<'CHECK'
          import subprocess, sys
          code = ("import sys; sys.modules['serial'] = None;"
                  "import pare_hardware_mcp.contract as c; print(len(c.TOOL_SPECS))")
          r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
          if r.returncode != 0:
              sys.exit("contract.py cannot be imported without pyserial:\n" + r.stderr)
          print("contract imports clean:", r.stdout.strip(), "tools")
          CHECK
```

- [ ] **Step 7: Run the tests**

Run: `pip install -e ".[dev]" && pytest tests/unit/test_contract.py -v`
Expected: PASS, 6 tests.

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml src tests .github README.md .gitignore
git commit -m "feat: repo scaffold and a hardware-free contract

Tool names are bare: the daemon prefixes with the workers.yaml key, so
console_send dispatches and is pinned as hardware_console_send. Writing the
prefix here would produce hardware_hardware_console_send and the pin would
match nothing, silently.

contract.py is importable with no pyserial and no hardware, because PARE's CI
imports it on a runner to validate risk pins against real tool names."
```

---

### Task 2: Device resolution — by-id, with the serial asserted

**Files:**
- Create: `src/pare_hardware_mcp/devices.py`
- Test: `tests/unit/test_devices.py`

**Interfaces:**
- Consumes: nothing from Task 1 beyond the package.
- Produces:
  - `class DeviceError(RuntimeError)` — every refusal in this module.
  - `resolve_device(by_id_path: str, *, expect_serial: str | None) -> ResolvedDevice`
  - `@dataclass(frozen=True) ResolvedDevice(by_id: str, tty: str, serial: str | None, interface: str | None)`
  - `list_serial_devices(root: str = "/dev/serial/by-id") -> list[ResolvedDevice]`

**Invariants — the implementer must preserve these, and the tests below pin them:**

1. **A `by-id` path is the only accepted input.** A caller passing `/dev/ttyUSB0` is refused, not resolved. `ttyUSBn` numbering depends on enumeration order and changes when a second adapter appears — which it will, in phase 2, when the relay adds a third serial node.
2. **A declared `expect_serial` that does not match is a refusal**, naming both values. A missing serial on the device when one was expected is also a refusal. Fail closed.
3. **`expect_serial=None` means the operator did not declare one** and resolution proceeds — but the resolved serial is always reported so the caller can see what it got.
4. **Resolution never falls back.** No "closest match", no "the only FTDI present", no scanning `/dev/ttyUSB*`. A path that does not resolve is an error naming the path.
5. **Voltage is never inferred or reported.** The Tigard's level selector is a physical switch with no software read-back; any value here would be a guess an operator might trust.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_devices.py
from __future__ import annotations

import os

import pytest

from pare_hardware_mcp.devices import (DeviceError, ResolvedDevice,
                                       list_serial_devices, resolve_device)

TIGARD = "usb-SecuringHardware.com_Tigard_V1.1_TG1119e7-if01-port0"


@pytest.fixture
def fake_by_id(tmp_path):
    """A /dev/serial/by-id lookalike: symlinks pointing at stand-in ttys."""
    root = tmp_path / "by-id"
    root.mkdir()
    tty = tmp_path / "ttyUSB1"
    tty.write_text("")
    (root / TIGARD).symlink_to(tty)
    return root, tty


def test_resolving_a_by_id_path_reports_the_tty_and_the_serial(fake_by_id):
    root, tty = fake_by_id
    dev = resolve_device(str(root / TIGARD), expect_serial="TG1119e7")
    assert isinstance(dev, ResolvedDevice)
    assert dev.tty == str(tty)
    assert dev.serial == "TG1119e7"
    assert dev.interface == "if01"


def test_a_mismatched_serial_is_refused_and_names_both_values(fake_by_id):
    root, _ = fake_by_id
    with pytest.raises(DeviceError) as e:
        resolve_device(str(root / TIGARD), expect_serial="TG0000aa")
    assert "TG1119e7" in str(e.value) and "TG0000aa" in str(e.value)


def test_a_raw_tty_path_is_refused_rather_than_resolved(fake_by_id):
    _, tty = fake_by_id
    with pytest.raises(DeviceError) as e:
        resolve_device(str(tty), expect_serial=None)
    assert "by-id" in str(e.value)


def test_an_absent_path_names_the_path_and_does_not_scan_for_alternatives(tmp_path):
    missing = tmp_path / "by-id" / "usb-Nothing_Here-if00-port0"
    with pytest.raises(DeviceError) as e:
        resolve_device(str(missing), expect_serial=None)
    assert str(missing) in str(e.value)


def test_no_expected_serial_still_reports_what_was_found(fake_by_id):
    root, _ = fake_by_id
    dev = resolve_device(str(root / TIGARD), expect_serial=None)
    assert dev.serial == "TG1119e7"


def test_listing_reports_every_by_id_entry(fake_by_id):
    root, _ = fake_by_id
    found = list_serial_devices(str(root))
    assert [d.serial for d in found] == ["TG1119e7"]


def test_listing_an_absent_root_is_empty_not_an_error(tmp_path):
    # A bench with no adapter plugged in is a normal state, not a failure.
    assert list_serial_devices(str(tmp_path / "nope")) == []
```

- [ ] **Step 2: Run them and watch them fail**

Run: `pytest tests/unit/test_devices.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'pare_hardware_mcp.devices'`

- [ ] **Step 3: Implement `devices.py` against the invariants above**

Write it yourself against the real filesystem semantics — this plan deliberately
does not hand you the body. Two notes that will save you a cycle:

- The serial and interface are parseable from the `by-id` **filename**, which
  udev builds from the device's own descriptors. The stock rule produces
  `usb-<vendor>_<model>_<serial>-<ifNN>-port0`. Parse from the right: the last
  two hyphen-separated segments are the interface and port, and the serial is
  the final `_`-separated field of what remains. Do **not** regex the whole
  thing loosely — a vendor string containing `-` or `_` is legal.
- Use `os.path.realpath` to follow the symlink, and check `os.path.islink` on the
  input so a raw tty path is refused by invariant 1 rather than accidentally
  accepted.

- [ ] **Step 4: Run the tests**

Run: `pytest tests/unit/test_devices.py -v`
Expected: PASS, 7 tests.

- [ ] **Step 5: Verify against the real bench**

Run on the Pi (`ssh pare@100.97.133.126`), with the package installed in a venv:

```bash
python -c "
from pare_hardware_mcp.devices import list_serial_devices, resolve_device
for d in list_serial_devices():
    print(d.by_id, d.tty, d.serial, d.interface)
print(resolve_device(
  '/dev/serial/by-id/usb-SecuringHardware.com_Tigard_V1.1_TG1119e7-if01-port0',
  expect_serial='TG1119e7'))
"
```

Expected: both Tigard channels listed with serial `TG1119e7` and interfaces
`if00`/`if01`; the explicit resolve returns the `if01` device. **This is the step
that catches a by-id filename parser that works on the fixture and not on the
real udev string.**

- [ ] **Step 6: Commit**

```bash
git add src/pare_hardware_mcp/devices.py tests/unit/test_devices.py
git commit -m "feat: resolve serial devices by by-id, asserting the serial

ttyUSBn is assigned by enumeration order and is not stable across reboots or
across a second adapter appearing -- which phase 2 guarantees, since the relay
adds a third serial node. Resolution never falls back to scanning: a path that
does not resolve is an error naming the path."
```

---

### Task 3: The ring buffer

**Files:**
- Create: `src/pare_hardware_mcp/ringbuffer.py`
- Test: `tests/unit/test_ringbuffer.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `class CaptureBuffer(capacity: int)`
  - `.append(data: bytes) -> None`
  - `.read(cursor: int, limit: int | None) -> tuple[bytes, int, int, int]` returning `(data, next_cursor, dropped, remaining)`
  - `.head: int` — total bytes ever written (the cursor a reader gets at open)
  - `.capacity: int`

**THIS TASK CONTAINS NO IMPLEMENTATION CODE, DELIBERATELY.** The arithmetic is
the kind that passes a happy-path test and is wrong at the wrap boundary, and a
body written into a plan would be transcribed faithfully. What follows is the
contract and the cases the tests must discriminate. Write the implementation
against these, not against a sketch.

**Invariants:**

1. **Cursors are absolute byte offsets since the session opened**, never ring
   indices. A cursor stays meaningful after any number of wraps.
2. **`dropped` is the number of bytes that existed and are gone** — written to
   the buffer after `cursor` and overwritten before this read reached them. It is
   **never silently zero**: a boot log with a hole in it that reports `dropped=0`
   is worse than an error, because the model reasons confidently across the gap.
3. **`remaining` is what is still unread after this call** — `head - next_cursor`.
   This is what lets a caller drain without guessing how many calls it needs.
4. **A cursor ahead of `head` is a caller error**, not silently clamped.
5. **A cursor behind the oldest retained byte is not an error** — it is the normal
   consequence of falling behind. It returns the oldest data still held, with
   `dropped` accounting for the gap.
6. **`limit` bounds the returned bytes, not the cursor advance.** After a limited
   read, `next_cursor` reflects only what was actually returned, so the caller can
   come back for the rest.
7. **Appending more than `capacity` in one call is legal.** Only the last
   `capacity` bytes survive, and the accounting stays correct.

**Sizing, from the spec:** default capacity **64 MiB**. At 115200 8N1 the line
carries 10 bits per byte → 11 520 B/s ≈ 11.25 KiB/s, so 64 MiB is ≈95 minutes at
full rate. An hour unread is ~40 MB. The Pi has ~7.4 GiB available and there is at
most one session, so under-sizing is the only real risk. Keep the derivation in a
comment next to the constant so a future faster rate can be reasoned about.

**What the tests must discriminate.** Each of these fails against a naive
implementation, which is the point:

- A read spanning a wrap returns contiguous correct bytes, not two halves in the
  wrong order.
- A reader that falls behind by more than `capacity` gets `dropped > 0` **and**
  the oldest retained bytes — not an exception, and not a silent empty result.
- `dropped` is exact, not approximate: write 100, read 50, write `capacity`, read
  → `dropped` equals precisely the number of bytes evicted past the cursor.
- A single `append` larger than `capacity` leaves `head` correct and the last
  `capacity` bytes retained.
- `limit` smaller than what is available advances the cursor by exactly the
  bytes returned, and `remaining` is non-zero.
- Reading at `head` returns empty, `dropped == 0`, `remaining == 0`, and the same
  cursor back — the common "line is quiet" case, which must be cheap and must not
  look like an error.
- A cursor beyond `head` raises.

- [ ] **Step 1: Write the failing tests** for every bullet above. Each gets its
  own test function with a name that states the property, not the mechanics.

- [ ] **Step 2: Run them and watch them fail**

Run: `pytest tests/unit/test_ringbuffer.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement `CaptureBuffer` against the invariants**

- [ ] **Step 4: Run the tests**

Run: `pytest tests/unit/test_ringbuffer.py -v`
Expected: PASS.

- [ ] **Step 5: Add a property test for the accounting identity**

The relationship that must hold for any sequence of appends and reads:

```
bytes_returned + dropped == next_cursor - cursor
```

Assert **that**, not a magic number — it survives every legitimate change to
capacity, chunk size and read pattern, where a literal would not. Drive it with a
few hundred randomised append/read sequences.

- [ ] **Step 6: Commit**

```bash
git add src/pare_hardware_mcp/ringbuffer.py tests/unit/test_ringbuffer.py
git commit -m "feat: capture ring buffer with exact drop accounting

Cursors are absolute offsets, so they stay meaningful across wraps. dropped is
never silently zero: a boot log with a hole that reports no loss is worse than
an error, because the model reasons confidently across the gap."
```

---

### Task 4: Session lifecycle — open, close, status

**Files:**
- Create: `src/pare_hardware_mcp/session.py`
- Test: `tests/unit/test_session.py`

**Interfaces:**
- Consumes: `resolve_device`/`DeviceError` (Task 2), `CaptureBuffer` (Task 3).
- Produces:
  - `class SessionError(RuntimeError)`
  - `class ConsoleSession` with `.id: str`, `.device: ResolvedDevice`,
    `.baud: int`, `.flow: str`, `.opened_at: float`, `.buffer: CaptureBuffer`,
    `.alive: bool`, `.death_reason: str | None`, and `.write(data: bytes) -> None`
  - `class SessionManager` with:
    - `.open(*, device: str, expect_serial: str | None, baud: int, flow: str, dtr: bool, rts: bool) -> ConsoleSession`
    - `.get(session_id: str) -> ConsoleSession` — raises `KeyError` for an unknown id
    - `.close(session_id: str) -> None` — raises `KeyError` for an unknown id
    - `.status() -> dict` — valid with no session open
    - `.current: ConsoleSession | None`

  Tasks 5 and 6 call `.get()`, `.write()` and read `.death_reason`; they are
  listed here because a Task 4 implementer sees only Task 4.

**THIS TASK CONTAINS NO IMPLEMENTATION CODE, DELIBERATELY.** It is concurrency
plus hardware ordering: a reader thread, an exclusive resource, and a device that
can vanish mid-read. Prose code here would be unexecuted and transcribed.

**Invariants:**

1. **One session per worker.** A second `open` while one is live raises
   `SessionError` naming the existing session's id, device and age. It does not
   queue, and it does not steal.
2. **Capture starts inside `open`, before it returns.** The boot log arrives
   before anyone can ask for it; a capture that begins on first `read` has
   already missed the thing worth having.
3. **DTR and RTS are set explicitly, never inherited from the driver default.**
   Both are parameters, both are reported in the result. Opening a port asserts
   these lines and many boards reset on DTR, so the caller must be able to say
   what happens and must be told what did.
4. **Flow control is explicit**, defaulting to none. RTS/CTS against a target that
   never asserts CTS hangs `send` or silently drops bytes.
5. **A vanished device is a distinct, named failure.** The session is marked not
   alive and the `by-id` path that no longer resolves is named. It does **not**
   hang and does **not** return an empty read — indistinguishable from a quiet
   target.
6. **The buffer outlives the device.** After a disappearance the captured bytes
   remain readable until `close`. What was captured before the unplug is evidence.
7. **`close` is the only thing that ends a session.** Not a read error, not a
   device disappearance, not a client disconnect.
8. **The reader thread never blocks the tool call.** Reads are served from the
   buffer; the device is drained by a background reader.
9. **A daemon disconnect does not end the session.** The worker keeps the session
   and the buffer, and a reconnecting daemon **adopts** them. The capture is the
   evidence, and discarding it on a tailnet blip — which this bench produces —
   would lose exactly what the bench exists to collect. Only `console_close`
   ends a session.

**What the tests must discriminate** (use `os.openpty()` for the device; the
far end is the test's "target"):

- Bytes written to the pty before any `read` call are present in the first read —
  proving invariant 2 rather than assuming it.
- A second `open` raises, and the message contains the first session's id.
- Closing the far end of the pty marks the session not alive with a named error,
  and a subsequent `read` still returns the bytes captured beforehand.
- `open` reports the DTR/RTS it set.
- `status` with no session open is a valid answer, not an error.
- `close` on an unknown session id is an error naming the id.
- A session survives a simulated client disconnect: nothing in the manager's
  public surface ends a session except `close`, and the buffer still holds what
  was captured before the disconnect.

- [ ] **Step 1: Write the failing tests** for every bullet.
- [ ] **Step 2: Run them and watch them fail.**
- [ ] **Step 3: Implement `session.py` against the invariants.**
- [ ] **Step 4: Run the tests.**

- [ ] **Step 5: Verify on the real adapter**

On the Pi with the Tigard attached and TX looped to RX on the header:

```bash
python -c "
from pare_hardware_mcp.session import SessionManager
m = SessionManager()
s = m.open(device='/dev/serial/by-id/usb-SecuringHardware.com_Tigard_V1.1_TG1119e7-if01-port0',
           expect_serial='TG1119e7', baud=115200, flow='none', dtr=False, rts=False)
print(m.status())
m.close(s.id)
"
```

Expected: a session opens against the real device and `status` reports it.
**A pty cannot catch a wrong channel** — this step is what does.

- [ ] **Step 6: Commit**

```bash
git add src/pare_hardware_mcp/session.py tests/unit/test_session.py
git commit -m "feat: exclusive console session with capture from open

Capture starts inside open(), not on first read: the boot log arrives before
anyone can ask for it. DTR/RTS are set explicitly and reported, because opening
a port is not a read-only act -- many boards reset on DTR."
```

---

### Task 5: `console_read`, `console_status`, `console_close` handlers

**Files:**
- Create: `src/pare_hardware_mcp/tools.py`
- Test: `tests/unit/test_tools_read.py`

**Interfaces:**
- Consumes: `SessionManager` (Task 4), `CaptureBuffer.read` (Task 3).
- Produces: async handlers `console_read`, `console_status`, `console_close`, each returning a JSON string.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_tools_read.py
from __future__ import annotations

import base64
import json

import pytest

from pare_hardware_mcp import tools


@pytest.fixture
def live(monkeypatch):
    """A session manager whose buffer is pre-loaded, with no real device."""
    from pare_hardware_mcp.ringbuffer import CaptureBuffer

    class FakeSession:
        id = "s-1"
        alive = True
        baud = 115200
        flow = "none"
        def __init__(self):
            self.buffer = CaptureBuffer(capacity=1024)

    class FakeManager:
        def __init__(self):
            self.current = FakeSession()
        def get(self, session_id):
            if session_id != self.current.id:
                raise KeyError(session_id)
            return self.current

    mgr = FakeManager()
    monkeypatch.setattr(tools, "MANAGER", mgr)
    return mgr


async def test_read_returns_base64_not_raw_text(live):
    live.current.buffer.append(b"\x1b[2Kboot\xff\xfe")
    out = json.loads(await tools.console_read(session="s-1", cursor=0))
    # Raw target bytes must never travel as text: they are untrusted, they reach
    # the operator's approval prompt, and they are not valid UTF-8 in general.
    assert base64.b64decode(out["data_b64"]) == b"\x1b[2Kboot\xff\xfe"
    assert "data" not in out


async def test_read_reports_dropped_and_remaining(live):
    out = json.loads(await tools.console_read(session="s-1", cursor=0))
    assert out["dropped"] == 0
    assert out["remaining"] == 0
    assert out["next_cursor"] == 0


async def test_a_limited_read_leaves_remaining_nonzero(live):
    live.current.buffer.append(b"0123456789")
    out = json.loads(await tools.console_read(session="s-1", cursor=0, limit=4))
    assert base64.b64decode(out["data_b64"]) == b"0123"
    assert out["next_cursor"] == 4
    assert out["remaining"] == 6


async def test_reading_an_unknown_session_is_an_error_naming_it(live):
    out = json.loads(await tools.console_read(session="nope", cursor=0))
    assert out["error"]
    assert "nope" in out["error"]
```

- [ ] **Step 2: Run and watch it fail**

Run: `pytest tests/unit/test_tools_read.py -v`
Expected: FAIL — `tools` module does not exist.

- [ ] **Step 3: Implement the three handlers**

```python
# src/pare_hardware_mcp/tools.py  (partial — the read/status/close handlers)
"""Tool handlers. Names match contract.py exactly; server.py binds by name.

TARGET BYTES ARE UNTRUSTED AND TRAVEL BASE64. UART output is arbitrary bytes,
not text, and it reaches the model's context, the capture store, /snapshot and
-- via the next call's argument snapshot -- the operator's approval prompt. The
artifact design applies a control-character check to `path` and `drive_id` for
exactly this reason; this is the same problem with far more volume. Encode here,
strip at render time, and keep the capture byte-exact because it is evidence.
"""
from __future__ import annotations

import base64
import json
from typing import Any

from pare_hardware_mcp.session import SessionManager

MANAGER = SessionManager()


def _ok(**fields: Any) -> str:
    return json.dumps(fields)


def _err(message: str) -> str:
    return json.dumps({"error": message})


async def console_read(session: str, cursor: int = 0,
                       limit: int | None = None) -> str:
    try:
        sess = MANAGER.get(session)
    except KeyError:
        return _err(f"no such session {session!r}")
    data, next_cursor, dropped, remaining = sess.buffer.read(cursor, limit)
    return _ok(session=session,
               data_b64=base64.b64encode(data).decode("ascii"),
               next_cursor=next_cursor, dropped=dropped, remaining=remaining,
               alive=sess.alive)


async def console_status() -> str:
    return _ok(**MANAGER.status())


async def console_close(session: str) -> str:
    try:
        MANAGER.close(session)
    except KeyError:
        return _err(f"no such session {session!r}")
    return _ok(closed=session)
```

- [ ] **Step 4: Run the tests**

Run: `pytest tests/unit/test_tools_read.py -v`
Expected: PASS, 4 tests.

- [ ] **Step 5: Commit**

```bash
git add src/pare_hardware_mcp/tools.py tests/unit/test_tools_read.py
git commit -m "feat: console_read/status/close, with target bytes base64-encoded

UART output is arbitrary bytes and is untrusted. It reaches model context, the
capture store and the operator's approval prompt, so it travels encoded and is
stripped at render time rather than at capture time -- the capture is evidence."
```

---

### Task 6: `console_open` and `console_send` handlers

**Files:**
- Modify: `src/pare_hardware_mcp/tools.py`
- Test: `tests/unit/test_tools_write.py`

**Interfaces:**
- Consumes: `SessionManager.open` (Task 4).
- Produces: async handlers `console_open`, `console_send`.

**Invariants:**

1. **`console_send` accepts base64 only.** Same reasoning as `read`, in the other
   direction: the caller may need to send bytes that are not text (a `Ctrl-C`, a
   break, a binary payload at a bootloader).
2. **`send` does not append to the capture buffer.** The target's echo does, if
   it echoes. Synthesising sent bytes into the capture would make the log claim
   the target said something it did not.
3. **`send` on a not-alive session is refused**, naming why the session died.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_tools_write.py
from __future__ import annotations

import base64
import json

import pytest

from pare_hardware_mcp import tools


async def test_send_writes_exactly_the_decoded_bytes(monkeypatch):
    written = []

    class FakeSession:
        id, alive = "s-1", True
        def write(self, data): written.append(data)

    class FakeManager:
        current = FakeSession()
        def get(self, sid):
            if sid != "s-1": raise KeyError(sid)
            return self.current

    monkeypatch.setattr(tools, "MANAGER", FakeManager())
    payload = base64.b64encode(b"\x03reboot\r").decode()
    out = json.loads(await tools.console_send(session="s-1", data_b64=payload))
    assert written == [b"\x03reboot\r"]
    assert out["sent"] == 8


async def test_send_does_not_fabricate_capture_entries(monkeypatch):
    # The capture is what the TARGET said. Echoing our own bytes into it would
    # make the log assert something the target never emitted.
    from pare_hardware_mcp.ringbuffer import CaptureBuffer

    class FakeSession:
        id, alive = "s-1", True
        def __init__(self): self.buffer = CaptureBuffer(capacity=256)
        def write(self, data): pass

    class FakeManager:
        def __init__(self): self.current = FakeSession()
        def get(self, sid): return self.current

    mgr = FakeManager()
    monkeypatch.setattr(tools, "MANAGER", mgr)
    await tools.console_send(session="s-1", data_b64=base64.b64encode(b"hi").decode())
    assert mgr.current.buffer.head == 0


async def test_send_to_a_dead_session_is_refused_with_the_reason(monkeypatch):
    class FakeSession:
        id, alive, death_reason = "s-1", False, "device /dev/serial/by-id/... vanished"
        def write(self, data): raise AssertionError("must not write")

    class FakeManager:
        def __init__(self): self.current = FakeSession()
        def get(self, sid): return self.current

    monkeypatch.setattr(tools, "MANAGER", FakeManager())
    out = json.loads(await tools.console_send(session="s-1", data_b64="aGk="))
    assert out["error"]
    assert "vanished" in out["error"]


async def test_malformed_base64_is_an_error_not_a_partial_write(monkeypatch):
    class FakeManager:
        def get(self, sid): raise AssertionError("must not reach the session")
    monkeypatch.setattr(tools, "MANAGER", FakeManager())
    out = json.loads(await tools.console_send(session="s-1", data_b64="not!base64"))
    assert out["error"]
```

- [ ] **Step 2: Run and watch them fail**

Run: `pytest tests/unit/test_tools_write.py -v`
Expected: FAIL — `console_send` not defined.

- [ ] **Step 3: Implement both handlers**

```python
# append to src/pare_hardware_mcp/tools.py

async def console_open(device: str, baud: int = 115200, flow: str = "none",
                       dtr: bool = False, rts: bool = False,
                       expect_serial: str | None = None) -> str:
    try:
        sess = MANAGER.open(device=device, expect_serial=expect_serial,
                            baud=baud, flow=flow, dtr=dtr, rts=rts)
    except Exception as exc:                      # noqa: BLE001 - surface it
        return _err(str(exc))
    # Report the lines we asserted. The caller cannot see the physical switch
    # and cannot know what the driver would have defaulted to.
    return _ok(session=sess.id, device=sess.device.by_id,
               serial=sess.device.serial, baud=sess.baud, flow=sess.flow,
               dtr=dtr, rts=rts, cursor=sess.buffer.head,
               note="target voltage is set by a physical switch on the Tigard "
                    "and is not readable from software")


async def console_send(session: str, data_b64: str) -> str:
    # Decode BEFORE touching the session, so a malformed payload cannot leave a
    # half-written line on the wire.
    try:
        payload = base64.b64decode(data_b64, validate=True)
    except Exception:                             # noqa: BLE001
        return _err("data_b64 is not valid base64")
    try:
        sess = MANAGER.get(session)
    except KeyError:
        return _err(f"no such session {session!r}")
    if not sess.alive:
        return _err(f"session {session!r} is not alive: "
                    f"{getattr(sess, 'death_reason', 'unknown')}")
    sess.write(payload)
    return _ok(session=session, sent=len(payload))
```

- [ ] **Step 4: Run the tests**

Run: `pytest tests/unit/test_tools_write.py -v`
Expected: PASS, 4 tests.

- [ ] **Step 5: Commit**

```bash
git add src/pare_hardware_mcp/tools.py tests/unit/test_tools_write.py
git commit -m "feat: console_open and console_send

send takes base64 so a Ctrl-C or a binary bootloader payload survives the wire,
and decodes before touching the session so a malformed payload cannot leave a
half-written line. Sent bytes are never synthesised into the capture: the
capture is what the target said."
```

---

### Task 7: `console_detect_baud`

**Files:**
- Create: `src/pare_hardware_mcp/baud.py`
- Modify: `src/pare_hardware_mcp/tools.py`
- Test: `tests/unit/test_baud.py`

**Interfaces:**
- Consumes: `ConsoleSession` (Task 4).
- Produces:
  - `score_sample(data: bytes) -> dict` → `{"printable_ratio": float, "has_crlf": bool, "nulls": int}`
  - `DEFAULT_RATES: tuple[int, ...]`
  - async handler `console_detect_baud`

**THE SCAN ITSELF CONTAINS NO PLAN-SUPPLIED CODE.** Changing line speed on a
live fd in the right order is hardware-ordering-sensitive and getting it wrong
reboots the operator's target repeatedly. `score_sample` is pure and does get
code.

**Invariants for the scan:**

1. **Change speed on the held descriptor via termios. Never close and reopen.**
   Reopening re-asserts DTR, so a reopen-per-rate scan reboots a DTR-reset board
   once per candidate — and the target never stays up long enough to emit a clean
   sample.
2. **Never set speed 0.** POSIX defines a zero output baud rate as "deassert the
   modem control lines", which drops DTR/RTS — the exact hazard invariant 1
   exists to avoid, reachable from a tier-`low` tool. Reject `0` from any
   caller-supplied rate list before touching the port.
3. **Detection never transmits.** It samples only. A wake byte would give a
   tier-`low`, never-prompted tool a write path to the target, bypassing the
   `high` tier `console_send` carries — and at a wrong rate those bytes are
   framing garbage arriving at a bootloader.
4. **The budget is bounded and derived, not assumed.** Total scan time is
   `len(rates) × sample_seconds` and must be **checked against the worker's
   configured request deadline**, refusing a candidate list that would exceed it
   rather than discovering it as a timeout. This is phase 1's longest call.
5. **Return ranked evidence, never a bare verdict.** Every candidate appears with
   its scores and a short sample.
6. **A silent line reports no-data-at-any-rate** and names the physical causes
   worth checking before trying more rates — ground and TX/RX crossover — because
   a floating ground produces framing errors that look exactly like a wrong rate.

- [ ] **Step 1: Write the failing tests for `score_sample`**

```python
# tests/unit/test_baud.py
from __future__ import annotations

import pytest

from pare_hardware_mcp.baud import DEFAULT_RATES, score_sample


def test_readable_text_scores_far_above_high_bit_noise():
    good = score_sample(b"U-Boot 2021.01\r\nHit any key to stop autoboot\r\n")
    bad = score_sample(bytes(range(128, 256)) * 2)
    assert good["printable_ratio"] > 0.9
    assert bad["printable_ratio"] < 0.2
    assert good["printable_ratio"] > bad["printable_ratio"]


def test_crlf_is_reported_because_console_output_is_line_oriented():
    assert score_sample(b"line one\r\nline two\r\n")["has_crlf"] is True
    assert score_sample(b"no newlines here")["has_crlf"] is False


def test_an_empty_sample_scores_zero_rather_than_dividing_by_zero():
    s = score_sample(b"")
    assert s["printable_ratio"] == 0.0


def test_nulls_are_counted_since_a_wrong_rate_produces_them():
    assert score_sample(b"\x00\x00ab")["nulls"] == 2


def test_zero_is_not_a_candidate_rate():
    # termios treats speed 0 as "drop the modem control lines", which asserts
    # the DTR reset this scan exists to avoid.
    assert 0 not in DEFAULT_RATES
    assert all(r > 0 for r in DEFAULT_RATES)


def test_the_default_rates_are_ordered_and_cover_the_common_console_speeds():
    assert list(DEFAULT_RATES) == sorted(DEFAULT_RATES)
    for r in (9600, 115200):
        assert r in DEFAULT_RATES
```

- [ ] **Step 2: Run and watch them fail**

Run: `pytest tests/unit/test_baud.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement `score_sample` and `DEFAULT_RATES`**

```python
# src/pare_hardware_mcp/baud.py
"""Baud detection: scoring, and the candidate list.

The SCAN lives in the session (it needs the held descriptor); this module is the
pure half. A wrong rate produces plausible garbage rather than an error, which is
why callers get ranked scores and never a bare verdict.
"""
from __future__ import annotations

import string

DEFAULT_RATES: tuple[int, ...] = (
    9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600,
)
"""Common console speeds, ascending.

ZERO IS NOT AND MUST NOT BE A CANDIDATE. POSIX defines setting the output baud
rate to zero as deasserting the modem control lines; on ftdi_sio that drops DTR
and RTS, which is a target reset reachable from a tier-`low` tool. Any
caller-supplied list is filtered against this before the port is touched.
"""

_PRINTABLE = frozenset(
    (string.printable).encode("ascii")
)


def score_sample(data: bytes) -> dict:
    """Evidence about whether `data` looks like console output at this rate."""
    if not data:
        return {"printable_ratio": 0.0, "has_crlf": False, "nulls": 0}
    printable = sum(1 for b in data if b in _PRINTABLE)
    return {
        "printable_ratio": printable / len(data),
        "has_crlf": b"\r\n" in data,
        "nulls": data.count(0),
    }
```

- [ ] **Step 4: Run the tests**

Run: `pytest tests/unit/test_baud.py -v`
Expected: PASS, 6 tests.

- [ ] **Step 5: Implement the scan on the session, against the invariants above.**
  Add tests using a pty that: reject a rate list containing `0`; reject a list
  whose budget exceeds the deadline; confirm no bytes are transmitted during a
  scan; and confirm a silent line yields the no-data result with the ground and
  crossover hint.

- [ ] **Step 6: Verify on the real bench**

With the target attached and transmitting (power-cycle it by hand so it boots
during the scan):

```bash
python -c "
import asyncio, json
from pare_hardware_mcp import tools
print(json.dumps(json.loads(asyncio.run(tools.console_detect_baud(
    device='/dev/serial/by-id/usb-SecuringHardware.com_Tigard_V1.1_TG1119e7-if01-port0'
))), indent=2))
"
```

Expected: the target's true rate ranks first with a high `printable_ratio` and a
readable sample. **If every rate scores badly, check the Tigard's voltage
selector, ground and TX/RX crossover before touching this code** — that is the
failure this tool's own output is supposed to send you toward.

- [ ] **Step 7: Commit**

```bash
git add src/pare_hardware_mcp/baud.py src/pare_hardware_mcp/session.py \
        src/pare_hardware_mcp/tools.py tests/unit/test_baud.py
git commit -m "feat: baud detection returning ranked evidence, not a verdict

A wrong rate produces plausible garbage rather than an error, so the scores
travel with the answer. The scan changes speed on the held fd rather than
reopening, because reopen re-asserts DTR and would reboot a DTR-reset board
once per candidate rate. Zero is never a candidate: termios reads it as
'drop the modem control lines'."
```

---

### Task 8: `list_devices` and `bench_status`

**Files:**
- Modify: `src/pare_hardware_mcp/tools.py`
- Create: `src/pare_hardware_mcp/config.py`
- Test: `tests/unit/test_tools_status.py`

**Interfaces:**
- Consumes: `list_serial_devices` (Task 2).
- Produces: async handlers `list_devices`, `bench_status`; `Config` read from environment.

**Why `bench_status` exists:** `pare/commands/health.py:130-151` already says *"§8.4
puts the live answer behind a low-tier `bench_status` tool on the worker, which
does not exist yet."* A consumer is waiting; this is it. It answers about the
artifact root **without dispatching anything that writes**.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_tools_status.py
from __future__ import annotations

import json

import pytest

from pare_hardware_mcp import tools


async def test_list_devices_reports_by_id_and_serial_but_never_voltage(monkeypatch):
    from pare_hardware_mcp.devices import ResolvedDevice
    monkeypatch.setattr(tools, "list_serial_devices", lambda *a, **k: [
        ResolvedDevice(by_id="/dev/serial/by-id/usb-...-if01-port0",
                       tty="/dev/ttyUSB1", serial="TG1119e7", interface="if01"),
    ])
    out = json.loads(await tools.list_devices())
    assert out["devices"][0]["serial"] == "TG1119e7"
    # The Tigard's level selector is a physical switch with no software
    # read-back. Reporting a value an operator might trust would be a guess.
    assert "voltage" not in json.dumps(out).lower()


async def test_bench_status_reports_a_missing_artifact_root_as_absent(monkeypatch, tmp_path):
    monkeypatch.setenv("PARE_HW_ARTIFACT_ROOT", str(tmp_path / "nope"))
    out = json.loads(await tools.bench_status())
    assert out["artifact_root"]["present"] is False


async def test_bench_status_reads_the_drive_id_when_present(monkeypatch, tmp_path):
    (tmp_path / ".bench-store-id").write_text("0361c41f-e680-4d4e-b9c3-39af8a33d067\n")
    monkeypatch.setenv("PARE_HW_ARTIFACT_ROOT", str(tmp_path))
    out = json.loads(await tools.bench_status())
    assert out["artifact_root"]["present"] is True
    assert out["artifact_root"]["drive_id"] == "0361c41f-e680-4d4e-b9c3-39af8a33d067"


async def test_bench_status_works_with_no_session_open(monkeypatch):
    out = json.loads(await tools.bench_status())
    assert "session" in out
```

- [ ] **Step 2: Run and watch them fail.**
- [ ] **Step 3: Implement `config.py` and the two handlers.** `Config` reads
  `PARE_HW_DEVICE`, `PARE_HW_EXPECT_SERIAL`, `PARE_HW_ARTIFACT_ROOT` and
  `PARE_HW_BUFFER_BYTES` from the environment, because for a `streamable_http`
  worker `MCPClient.from_spec` forwards only `endpoint` and the two timeouts —
  `env` is stdio-only, so the unit's `Environment=` is the only channel.
- [ ] **Step 4: Run the tests.**
- [ ] **Step 5: Commit.**

```bash
git commit -m "feat: list_devices and bench_status

health.py already names a low-tier bench_status tool 'which does not exist
yet'. This is it. Voltage is never reported: the Tigard's selector is a
physical switch with no software read-back, and a guessed value is one an
operator might trust."
```

---

### Task 9: `server.py` — bind the contract to handlers

**Files:**
- Create: `src/pare_hardware_mcp/server.py`
- Test: `tests/integration/test_wire_contract.py`

**Interfaces:**
- Consumes: `TOOL_SPECS` (Task 1), all handlers in `tools` (Tasks 5–8).
- Produces: `build_server() -> FastMCP`, `main() -> None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_wire_contract.py
"""Every contract tool must reach a real handler and carry both wire keys."""
from __future__ import annotations

import pytest
from pare_worker_kit import (PRODUCES_META_KEY, PRODUCES_RESULT,
                             RISK_TIER_META_KEY, VALID_RISK_TIERS)

from pare_hardware_mcp.contract import TOOL_SPECS
from pare_hardware_mcp.server import build_server


async def test_every_contract_tool_is_registered_with_both_meta_keys():
    server = build_server()
    tools = {t.name: t for t in await server.list_tools()}
    assert set(tools) == {s.name for s in TOOL_SPECS}
    for spec in TOOL_SPECS:
        meta = tools[spec.name].meta
        assert meta[RISK_TIER_META_KEY] == spec.risk_tier
        assert meta[PRODUCES_META_KEY] == PRODUCES_RESULT
        assert meta[RISK_TIER_META_KEY] in VALID_RISK_TIERS


async def test_no_tool_is_a_stub():
    # A stub that returns "not implemented" advertises a tier and a schema and
    # looks live to the daemon. Phase 1 ships no stubs.
    import pare_hardware_mcp.tools as tools_mod
    for spec in TOOL_SPECS:
        assert hasattr(tools_mod, spec.name), f"{spec.name} has no handler"


def test_the_server_is_named_for_its_distribution():
    # stamp_version resolves the reported version from installed package
    # metadata keyed by the FastMCP instance name, and serverInfo is a
    # networked worker's only provenance. A mismatched name silently reports
    # no version at all.
    assert build_server().name == "pare-hardware-mcp"
```

- [ ] **Step 2: Run and watch it fail**

Run: `pytest tests/integration/test_wire_contract.py -v`
Expected: FAIL — no `server` module.

- [ ] **Step 3: Write `server.py`**

```python
"""Bind the contract to handlers and serve.

The FastMCP instance is named for the DISTRIBUTION on purpose: stamp_version
resolves the version it reports in serverInfo from installed package metadata
keyed by this name, and for a networked worker serverInfo is the only provenance
the daemon ever sees. Rename one without the other and the wire quietly reports
no version.
"""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP
from pare_worker_kit import (PRODUCES_META_KEY, RISK_TIER_META_KEY, run_worker)

from pare_hardware_mcp import tools as tools_mod
from pare_hardware_mcp.contract import TOOL_SPECS


def build_server() -> FastMCP:
    server = FastMCP("pare-hardware-mcp")
    for spec in TOOL_SPECS:
        handler = getattr(tools_mod, spec.name)   # no stubs in phase 1
        server.add_tool(
            handler,
            name=spec.name,
            description=spec.description,
            meta={RISK_TIER_META_KEY: spec.risk_tier,
                  PRODUCES_META_KEY: spec.produces},
        )
    return server


def main() -> None:
    run_worker(build_server())
```

- [ ] **Step 4: Run the tests**

Run: `pytest -v`
Expected: all green.

- [ ] **Step 5: Run it for real over stdio**

```bash
AGENT_WORKER_TRANSPORT=stdio pare-hardware-mcp
```

Expected: it starts and waits on stdin. Ctrl-C to exit. **This is the first proof
the console script, the entry point and `run_worker` agree.**

- [ ] **Step 6: Commit**

```bash
git add src/pare_hardware_mcp/server.py tests/integration/test_wire_contract.py
git commit -m "feat: server wiring, named for its distribution

serverInfo is a networked worker's only provenance and stamp_version keys it by
the FastMCP instance name, so the name must equal the distribution name."
```

---

### Task 10: Worker-side request logging in `pare-worker-kit`

**Files:**
- Modify: `pare-worker-kit/src/pare_worker_kit/serve.py`
- Test: `pare-worker-kit/tests/test_request_log.py`

**This task is in a different repo.** It is here because phase 1 is the first
networked worker and the obligation lands with it.

**Why:** the networked-workers design calls this **Required**. Over stdio, PARE's
audit log is a total record of what a worker did, because the daemon is the only
thing that can reach it. Over HTTP it is not — anything on the tailnet can call
the worker directly and PARE never sees it. This is the worker where *"after a
bricked target, the operator cannot establish whether PARE did it"* stops being
rhetorical. There is no logging of any kind in `pare_worker_kit` today.

**Interfaces:**
- Produces: request logging inside `run_worker`'s HTTP path, recording **peer
  address, tool name, timestamp, and a hash of the arguments** — never the
  argument values.

**Invariants:**

1. **Arguments are hashed, never logged verbatim.** A console `send` payload may
   carry credentials typed at a target's login prompt. The hash answers "was this
   the same call?" without creating a new place secrets live.
2. **The log survives the daemon.** It is the worker's own record; its value is
   precisely that it does not depend on PARE being honest or present.
3. **Logging failure never fails the request.** A full disk must not take the
   bench offline.
4. **stdio workers are unaffected.** The gap this closes exists only for
   networked transports.

- [ ] **Step 1: Write the failing tests** covering: a call is recorded with peer,
  tool and timestamp; the arguments appear only as a hash and no value from them
  appears anywhere in the record; a logging failure does not propagate; and the
  stdio path records nothing.
- [ ] **Step 2: Run and watch them fail.**
- [ ] **Step 3: Implement in `serve.py` against the invariants.**
- [ ] **Step 4: Run the kit's full suite** — `pytest -v` in `pare-worker-kit`.
- [ ] **Step 5: Run the THREE consuming workers' suites against the modified kit.**
  A contract widening can pass every one of the library's own tests and still
  break a consumer. Install the worktree kit into each of `pare-static-mcp`,
  `pare-frida-mcp`, `pare-mitm-mcp` and run their suites before releasing.
- [ ] **Step 6: Commit in `pare-worker-kit`.**

```bash
git commit -m "feat: worker-side request logging for networked transports

Over stdio the daemon's audit log is a total record of what a worker did. Over
HTTP it is not: anything on the tailnet can call the worker directly and PARE
never sees it. Arguments are hashed rather than recorded, because a console
send may carry credentials typed at a target's login prompt."
```

---

## Definition of done

- [ ] `pytest -v` green in `pare-hardware-mcp`, and in `pare-worker-kit`.
- [ ] The three existing workers' suites green against the modified kit.
- [ ] `AGENT_WORKER_TRANSPORT=stdio pare-hardware-mcp` starts.
- [ ] On the Pi, with the Tigard attached: `list_devices` reports both channels
      with serial `TG1119e7`; a session opens against `if01`; a loopback TX→RX
      test round-trips bytes at a known rate.
- [ ] With the real target attached: `console_detect_baud` ranks the true rate
      first, and a boot log is captured from `console_open` through
      `console_read` with `dropped == 0`.

**Not done here, and deliberately:** the worker is not deployed, not in
`workers.yaml`, not reachable from PARE, and `console_read` is not yet exempt
from the spin guard. That is Plan B.
