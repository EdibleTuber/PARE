# pare-static-mcp Worker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `pare-static-mcp`, a stdio MCP worker giving PARE seven read-only Android APK static-analysis tools, so the agent can derive hook targets from code instead of runbooks.

**Architecture:** A separate Python package mirroring `pare-frida-mcp`: `contract.py` (ToolSpecs + `WorkerContractAdapter`), `server.py` (`FastMCP`, stdio), `tools.py` (async handlers returning the `_ok`/`_err` JSON-string envelope), and an `apk/` subpackage of engine adapters. Hybrid engine — **androguard** in-process for parse/manifest/strings/xref/smali, **jadx** subprocess only for readable-Java decompilation. Single-APK-open state; androguard imported lazily and heavy work threaded.

**Tech Stack:** Python 3.12, `mcp` (FastMCP), `androguard` (pinned 4.x), external `jadx` binary + JRE, `agent_core` (contract/conformance, dev-only), pytest + pytest-asyncio.

## Global Constraints

- **Repo root:** all paths below are relative to the `pare-static-mcp` repo root (`~/Projects/pare-static-mcp`), except Task 10's `workers.yaml`/README edits which are in the PARE repo (`~/Projects/PARE`).
- **Tool prefix is automatic:** agent_core exposes tools as `{worker}_{tool}` (`workers/tool_factory.py`). Define tools **bare** (`load_apk`, …); the model sees `static_load_apk`. Do NOT hand-prefix.
- **Output envelope (mandatory):** every tool returns `json.dumps({"summary": str, ...})` as a **string** in a `text` block, via `_ok`/`_err`. A bare dict collapses to a junk `{"value":…}` row in PARE's capture layer.
- **All seven tools are wire tier `low`.** `risk_default: low`, no pins.
- **Single-APK-open:** one current APK; tools take **no `apk_id`**; `load_apk` replaces any prior; every response echoes the active `package`.
- **Lazy androguard import:** `import androguard` happens **inside** `load_apk`, never at module load (agent_core's 2s discovery ceiling silently drops slow-booting workers).
- **Thread blocking work:** wrap androguard `Analysis`/xref build and the jadx subprocess in `asyncio.to_thread`; guard the shared APK-state with a lock.
- **androguard pinned exact: `androguard==4.1.3`** (its API paths — `androguard.core.apk.APK`, `androguard.core.dex.DEX`, `androguard.core.analysis.analysis.Analysis` — are verified present in this version). **jadx** installed at `/home/edible/.local/bin/jadx` (v1.5.0); tests set `JADX_PATH=/home/edible/.local/bin/jadx`. androguard method names still verified at test time — the TDD cycle is ground truth where a call drifts.
- **Silence androguard logging.** androguard logs verbosely via loguru; the worker must call `from loguru import logger; logger.remove()` before/at first androguard use (inside `loader.load`) so logs cannot muddy the stdio channel or test output.
- **Environment (verified):** worker venv at `/home/edible/Projects/pare-static-mcp/.venv` (has `agent_core` editable + `androguard==4.1.3`); `RISK_TIER_META_KEY == "agent_core/risk_tier"`. Android build-tools at `/home/edible/Android/Sdk/build-tools/37.0.0/` if needed.
- **Test APK (real OMTG, referenced not committed):** `tests/fixtures/locate.py::test_apk()` reads env `PARE_STATIC_TEST_APK`, default `/home/edible/Projects/bsides/off_the_leash/MSTG-Android-Java.apk`; skip the test when absent. Shared identifier constants (verified against that APK):
  - `TEST_PACKAGE = "sg.vp.owasp_mobile.omtg_android"`
  - `TEST_CLASS = "sg.vp.owasp_mobile.OMTG_Android.OMTG_DATAST_001_KeyStore"` (note: class package `OMTG_Android` differs in case from app package `omtg_android`)
  - `TEST_METHOD = "encryptString"`, descriptor `"(Ljava/lang/String;)V"`
  - `TEST_STRING = "Dummy"`
  Every fixture-based test uses `test_apk()` + these constants — NOT synthetic identifiers.
- Tests: `asyncio_mode = "auto"`, `testpaths = ["tests"]`.

---

### Task 1: Repo scaffold, contract, stubbed server, conformance

**Files:**
- Create: `pyproject.toml`, `src/pare_static_mcp/__init__.py`, `src/pare_static_mcp/config.py`, `src/pare_static_mcp/contract.py`, `src/pare_static_mcp/server.py`, `src/pare_static_mcp/apk/__init__.py`
- Test: `tests/__init__.py`, `tests/unit/__init__.py`, `tests/unit/test_contract.py`, `tests/integration/__init__.py`, `tests/integration/test_conformance.py`, `tests/integration/test_wire_risk_tier.py`

**Interfaces:**
- Produces: `contract.TOOL_SPECS: list[ToolSpec]` (7 specs), `contract.WorkerContractAdapter` (`.contract_version()`, `.list_tools()`), `server.build_server() -> FastMCP`, `server.main()`. Tool names: `load_apk, find_symbol, grep_smali, list_methods, extract_strings, decompile_method, read_manifest`.

- [ ] **Step 1: Write `pyproject.toml`**

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "pare-static-mcp"
version = "0.1.0"
description = "Static-analysis MCP worker for PARE (Android APK)"
requires-python = ">=3.12"
dependencies = [
    "androguard==4.1.3",
    "mcp>=1.27.0",
]

[project.optional-dependencies]
dev = ["pytest", "pytest-asyncio"]

[project.scripts]
pare-static-mcp = "pare_static_mcp.server:main"

[tool.hatch.build.targets.wheel]
packages = ["src/pare_static_mcp"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

- [ ] **Step 2: Write `config.py`**

```python
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    jadx_path: str
    jadx_timeout_s: int
    jadx_stdout_cap: int          # bytes of jadx stdout to retain
    max_apk_bytes: int            # reject APKs larger than this before parsing
    max_zip_entries: int          # zip-bomb guard
    max_decompressed_bytes: int   # decompression-amplification guard

    @property
    def jadx_available(self) -> bool:
        from shutil import which
        return which(self.jadx_path) is not None or Path(self.jadx_path).is_file()


def load_config() -> Config:
    return Config(
        jadx_path=os.environ.get("JADX_PATH", "jadx"),
        jadx_timeout_s=int(os.environ.get("PARE_STATIC_JADX_TIMEOUT", 120)),
        jadx_stdout_cap=int(os.environ.get("PARE_STATIC_JADX_STDOUT_CAP", 4_000_000)),
        max_apk_bytes=int(os.environ.get("PARE_STATIC_MAX_APK_BYTES", 500 * 1024 * 1024)),
        max_zip_entries=int(os.environ.get("PARE_STATIC_MAX_ZIP_ENTRIES", 100_000)),
        max_decompressed_bytes=int(
            os.environ.get("PARE_STATIC_MAX_DECOMPRESSED_BYTES", 2 * 1024 * 1024 * 1024)
        ),
    )
```

- [ ] **Step 3: Write `contract.py`** (full descriptions — these are a deliverable)

```python
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

CONTRACT_VERSION = 1

_BOUNDED_OUT = {"type": "object", "properties": {"summary": {"type": "string"}}}


@dataclass(frozen=True)
class ToolSpec:
    name: str
    risk_tier: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] = field(default_factory=lambda: dict(_BOUNDED_OUT))


def _in(**props) -> dict[str, Any]:
    return {"type": "object", "properties": props}


TOOL_SPECS: list[ToolSpec] = [
    ToolSpec("load_apk", "low",
             "STATIC (reads the APK file; no device/attach). Load an APK from a "
             "host path and make it the current target (replaces any previously "
             "loaded APK). Returns package, sdk versions, class/dex counts, "
             "native_libs, and dynamic_load indicators (DexClassLoader/loadLibrary) "
             "- when those are present, static analysis is partially blind and you "
             "should corroborate with the frida (dynamic) worker.",
             _in(path={"type": "string"})),
    ToolSpec("find_symbol", "low",
             "STATIC. Find a Java METHOD or FIELD by NAME via cross-references - "
             "NOT string literals (for a string constant use static_extract_strings; "
             "for an API/text pattern use static_grep_smali). Returns rows of "
             "{class, method, signature, kind}; kind='def' is the implementation "
             "(feed its class+method+signature to static_decompile_method), "
             "kind='caller' is who invokes it. kind defaults to 'def'. Pass 'class' "
             "to scope the search to one class.",
             _in(symbol={"type": "string"},
                 kind={"type": "string", "enum": ["def", "caller", "both"]},
                 cls={"type": "string"})),
    ToolSpec("grep_smali", "low",
             "STATIC. Regex search over smali instructions and the DEX string pool "
             "- reaches API/text patterns that find_symbol (name xref) cannot, e.g. "
             "'Ljavax/crypto/CipherOutputStream;->write'. Returns rows of "
             "{class, method, insn, match}. On a real APK pass a specific pattern; "
             "broad patterns are captured and slow to page.",
             _in(pattern={"type": "string"})),
    ToolSpec("list_methods", "low",
             "STATIC. List the methods of ONE class (to find a class or a symbol "
             "use static_find_symbol). Returns rows of {method, descriptor, flags, "
             "xref_count}; use this to choose a hook/decompile target without "
             "decompiling the whole class.",
             _in(cls={"type": "string"})),
    ToolSpec("extract_strings", "low",
             "STATIC. Extract string/constant literals from the DEX string pool "
             "(NOT resources.arsc/assets). Returns rows of {value, class, method, "
             "kind, source}; source='dex'. Pass 'filter' (substring) on a real APK "
             "- unfiltered results are captured and slow to page.",
             _in(filter={"type": "string"})),
    ToolSpec("decompile_method", "low",
             "STATIC. Decompile a single method to readable Java (jadx) or smali. "
             "Pass class+method; pass 'signature' (the descriptor from a find_symbol "
             "row) to disambiguate overloads - if omitted and the method is "
             "overloaded, all overloads are returned. lang='java' (default) or "
             "'smali' (no jadx needed).",
             _in(cls={"type": "string"}, method={"type": "string"},
                 signature={"type": "string"},
                 lang={"type": "string", "enum": ["java", "smali"]})),
    ToolSpec("read_manifest", "low",
             "STATIC. Parse AndroidManifest: package, permissions, "
             "activities/services/receivers/providers, application_class (a prime "
             "init/hook target), exported components (pre-31 intent-filter rule "
             "applied), debuggable, allow_backup.",
             _in()),
]


class WorkerContractAdapter:
    """Exposes the agent_core WorkerContract shape for assert_conformance."""

    def contract_version(self) -> int:
        return CONTRACT_VERSION

    def list_tools(self) -> list[dict[str, Any]]:
        return [
            {"name": s.name, "risk_tier": s.risk_tier,
             "input_schema": s.input_schema, "output_schema": s.output_schema}
            for s in TOOL_SPECS
        ]
```

- [ ] **Step 4: Write `server.py`** (stub handlers until Tasks 3-9 fill `tools.py`)

```python
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from agent_core.workers.risk import RISK_TIER_META_KEY
from pare_static_mcp.contract import TOOL_SPECS

try:
    from pare_static_mcp import tools as tools_mod
except Exception:            # tools not yet implemented
    tools_mod = None


def build_server() -> FastMCP:
    server = FastMCP("pare-static-mcp")
    for spec in TOOL_SPECS:
        handler = getattr(tools_mod, spec.name, None) if tools_mod else None
        if handler is None:
            handler = _stub_for(spec.name)
        server.add_tool(handler, name=spec.name, description=spec.description,
                        meta={RISK_TIER_META_KEY: spec.risk_tier})
    return server


def _stub_for(name: str):
    async def _stub(**kwargs) -> str:
        import json
        return json.dumps({"summary": f"{name} not implemented in this build"})
    _stub.__name__ = name
    return _stub


def main() -> None:
    build_server().run(transport="stdio")
```

- [ ] **Step 5: Write the tests**

`tests/unit/test_contract.py`:
```python
from pare_static_mcp.contract import TOOL_SPECS, WorkerContractAdapter

EXPECTED = {"load_apk", "find_symbol", "grep_smali", "list_methods",
            "extract_strings", "decompile_method", "read_manifest"}

def test_seven_tools_named():
    assert {s.name for s in TOOL_SPECS} == EXPECTED

def test_adapter_lists_seven():
    assert len(WorkerContractAdapter().list_tools()) == 7
```

`tests/integration/test_conformance.py`:
```python
import pytest
from pare_static_mcp.contract import WorkerContractAdapter

def test_assert_conformance_passes():
    conformance = pytest.importorskip("agent_core.workers.conformance")
    conformance.assert_conformance(WorkerContractAdapter())
```

`tests/integration/test_wire_risk_tier.py`:
```python
from pare_static_mcp.contract import TOOL_SPECS

def test_all_tools_low_tier():
    assert all(s.risk_tier == "low" for s in TOOL_SPECS)
```

- [ ] **Step 6: Run tests**

Run: `pip install -e '.[dev]' && pytest tests -v`
Expected: `test_seven_tools_named`, `test_adapter_lists_seven`, `test_all_tools_low_tier` PASS; `test_assert_conformance_passes` PASS if `agent_core` importable, else SKIP.

- [ ] **Step 7: Commit**

```bash
git add -A && git commit -m "feat: scaffold pare-static-mcp worker (contract, stub server, conformance)"
```

---

### Task 2: Test fixture locator (real OMTG by env var)

**Files:**
- Create: `tests/fixtures/__init__.py`, `tests/fixtures/locate.py`
- Test: `tests/unit/test_fixtures.py`

**Interfaces:**
- Produces: `tests.fixtures.locate.test_apk() -> pathlib.Path` (from env `PARE_STATIC_TEST_APK`, default the known OMTG path); `tests.fixtures.locate.requires_apk` (a `pytest.mark.skipif` when the APK is absent); and the identifier constants `TEST_PACKAGE`, `TEST_CLASS`, `TEST_METHOD`, `TEST_DESCRIPTOR`, `TEST_STRING`.

The fixture is the **real OMTG APK, referenced not committed** (per the fixture decision). Fixture-based tests skip when it's absent.

- [ ] **Step 1: Write the locator + constants**

```python
# tests/fixtures/locate.py
from __future__ import annotations
import os
from pathlib import Path
import pytest

_DEFAULT = "/home/edible/Projects/bsides/off_the_leash/MSTG-Android-Java.apk"

TEST_PACKAGE = "sg.vp.owasp_mobile.omtg_android"
TEST_CLASS = "sg.vp.owasp_mobile.OMTG_Android.OMTG_DATAST_001_KeyStore"
TEST_METHOD = "encryptString"
TEST_DESCRIPTOR = "(Ljava/lang/String;)V"
TEST_STRING = "Dummy"

def apk_path() -> Path:
    return Path(os.environ.get("PARE_STATIC_TEST_APK", _DEFAULT))

def test_apk() -> Path:
    p = apk_path()
    if not p.is_file():
        pytest.skip(f"test APK not present at {p} (set PARE_STATIC_TEST_APK)")
    return p

requires_apk = pytest.mark.skipif(
    not apk_path().is_file(),
    reason="OMTG test APK not present (set PARE_STATIC_TEST_APK)",
)
```

- [ ] **Step 2: Write the test**

```python
# tests/unit/test_fixtures.py
from tests.fixtures.locate import apk_path, requires_apk, TEST_PACKAGE

@requires_apk
def test_apk_present():
    assert apk_path().stat().st_size > 0

def test_constants_shape():
    assert TEST_PACKAGE.startswith("sg.vp.owasp_mobile")
```

- [ ] **Step 3: Run + commit**

Run: `PARE_STATIC_TEST_APK=/home/edible/Projects/bsides/off_the_leash/MSTG-Android-Java.apk pytest tests/unit/test_fixtures.py -v` → PASS (or SKIP if APK absent)
```bash
git add -A && git commit -m "test: OMTG test-apk locator + identifier constants"
```

> **Naming reconciliation note (used in Task 9):** jadx must run with `--rename-flags none` so its emitted identifiers match androguard's (OMTG is unobfuscated, so names align cleanly). No separate obfuscated/multidex fixtures are built in v1 — the real OMTG APK is a single-dex unobfuscated target; the obfuscation/multidex hardening cases from the spec are noted as a fast-follow once a second fixture APK is sourced.

---

### Task 3: `load_apk` — single-open state, lazy import, distrust signals, input guards

**Files:**
- Create: `src/pare_static_mcp/apk/state.py`, `src/pare_static_mcp/apk/loader.py`, `src/pare_static_mcp/tools.py`
- Test: `tests/unit/test_load_apk.py`

**Interfaces:**
- Consumes: `config.load_config`, `tests.fixtures.locate.fixture_path`.
- Produces: `apk.state.APKState` (holds `apk`, `analysis`, `path`, `package`, `_xref_built`, `lock`), module-singleton `apk.state.CURRENT`; `apk.loader.load(path, cfg) -> APKState`; `apk.loader.ensure_xref(state)`; `tools.load_apk(path: str) -> str`; helpers `tools._ok`, `tools._err`, `tools._require_current()`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_load_apk.py
import json
from pare_static_mcp import tools
from tests.fixtures.locate import test_apk, requires_apk, TEST_PACKAGE

@requires_apk
async def test_load_apk_returns_package_and_signals():
    out = json.loads(await tools.load_apk(str(test_apk())))
    assert out.get("error") is not True
    assert out["package"] == TEST_PACKAGE
    assert out["class_count"] >= 1
    assert out["dex_count"] >= 1
    assert "native_libs" in out and "dynamic_load" in out

@requires_apk
async def test_load_apk_replaces_previous():
    await tools.load_apk(str(test_apk()))
    out = json.loads(await tools.load_apk(str(test_apk())))
    assert out.get("error") is not True

async def test_load_apk_rejects_missing_file():
    out = json.loads(await tools.load_apk("/no/such.apk"))
    assert out["error"] is True
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/unit/test_load_apk.py -v` → FAIL (`tools` has no `load_apk`).

- [ ] **Step 3: Implement state + loader + tool**

```python
# src/pare_static_mcp/apk/state.py
from __future__ import annotations
import asyncio
from dataclasses import dataclass, field
from typing import Any

@dataclass
class APKState:
    path: str
    package: str
    apk: Any
    analysis: Any
    dex_count: int
    class_count: int
    native_libs: list[str]
    dynamic_load: list[str]
    _xref_built: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

CURRENT: APKState | None = None

def set_current(state: APKState) -> None:
    global CURRENT
    CURRENT = state
```

```python
# src/pare_static_mcp/apk/loader.py
from __future__ import annotations
import os
import zipfile
from pare_static_mcp.apk.state import APKState

_DYNAMIC_MARKERS = ("Ldalvik/system/DexClassLoader", "Ldalvik/system/PathClassLoader",
                    "->loadLibrary", "->load(")

def _guard_input(path: str, cfg) -> None:
    if not os.path.isfile(path):
        raise FileNotFoundError(f"not a regular file: {path}")
    size = os.path.getsize(path)
    if size > cfg.max_apk_bytes:
        raise ValueError(f"APK too large: {size} > {cfg.max_apk_bytes}")
    with zipfile.ZipFile(path) as z:
        infos = z.infolist()
        if len(infos) > cfg.max_zip_entries:
            raise ValueError(f"too many zip entries: {len(infos)}")
        total = sum(i.file_size for i in infos)
        if total > cfg.max_decompressed_bytes:
            raise ValueError(f"decompressed size too large: {total}")

def load(path: str, cfg) -> APKState:
    _guard_input(path, cfg)
    # Silence androguard's verbose loguru output so it can't muddy the stdio
    # channel; then lazy-import — MUST NOT be at module top (2s discovery ceiling).
    from loguru import logger
    logger.remove()
    from androguard.core.apk import APK
    from androguard.core.dex import DEX
    from androguard.core.analysis.analysis import Analysis
    apk = APK(path)
    analysis = Analysis()
    dex_count = 0
    for dex_bytes in apk.get_all_dex():
        analysis.add(DEX(dex_bytes))
        dex_count += 1
    class_count = sum(1 for _ in analysis.get_classes())
    native = [f for f in apk.get_files() if f.startswith("lib/") and f.endswith(".so")]
    dynamic = _detect_dynamic(analysis)
    return APKState(path=path, package=apk.get_package(), apk=apk, analysis=analysis,
                    dex_count=dex_count, class_count=class_count,
                    native_libs=native, dynamic_load=dynamic)

def _detect_dynamic(analysis) -> list[str]:
    found = set()
    for s in analysis.get_strings():
        v = s.get_value()
        for m in _DYNAMIC_MARKERS:
            if m.strip("L->(") in v:
                found.add(m.strip("L->("))
    return sorted(found)

def ensure_xref(state: APKState) -> None:
    if not state._xref_built:
        state.analysis.create_xref()
        state._xref_built = True
```

```python
# src/pare_static_mcp/tools.py
from __future__ import annotations
import asyncio
import json
from typing import Any
from pare_static_mcp.config import load_config
from pare_static_mcp.apk import loader as loader_mod
from pare_static_mcp.apk import state as state_mod

CFG = load_config()

def _ok(summary: str, **extra: Any) -> str:
    pkg = state_mod.CURRENT.package if state_mod.CURRENT else None
    return json.dumps({"summary": summary, "package": pkg, **extra})

def _err(summary: str, exc: Exception | None = None) -> str:
    payload = {"summary": summary, "error": True}
    if exc is not None:
        payload["detail"] = str(exc)
    return json.dumps(payload)

def _require_current() -> state_mod.APKState:
    if state_mod.CURRENT is None:
        raise LookupError("no APK loaded - call load_apk first")
    return state_mod.CURRENT

async def load_apk(path: str) -> str:
    try:
        st = await asyncio.to_thread(loader_mod.load, path, CFG)
        state_mod.set_current(st)
        return _ok(f"loaded {st.package}", package=st.package,
                   min_sdk=st.apk.get_min_sdk_version(),
                   target_sdk=st.apk.get_target_sdk_version(),
                   class_count=st.class_count, dex_count=st.dex_count,
                   native_libs=st.native_libs, dynamic_load=st.dynamic_load)
    except Exception as e:
        return _err("load_apk failed", e)
```

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/unit/test_load_apk.py -v` → PASS. (If androguard 4.x import paths differ from the pinned version, adjust the three imports in `loader.py` — the tests are the ground truth.)

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: load_apk (single-open, lazy import, distrust signals, input guards)"
```

---

### Task 4: `read_manifest`

**Files:** Modify `src/pare_static_mcp/tools.py`; Create `src/pare_static_mcp/apk/manifest.py`; Test `tests/unit/test_read_manifest.py`

**Interfaces:**
- Consumes: `tools._require_current`, `APKState.apk`.
- Produces: `apk.manifest.parse(apk) -> dict`; `tools.read_manifest() -> str`.

- [ ] **Step 1: Failing test**

```python
# tests/unit/test_read_manifest.py
import json
from pare_static_mcp import tools
from tests.fixtures.locate import test_apk, requires_apk

@requires_apk
async def test_read_manifest_shape():
    await tools.load_apk(str(test_apk()))
    out = json.loads(await tools.read_manifest())
    for k in ("permissions", "activities", "services", "receivers",
              "providers", "exported", "application_class"):
        assert k in out

async def test_read_manifest_requires_load():
    tools.state_mod.CURRENT = None
    out = json.loads(await tools.read_manifest())
    assert out["error"] is True
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement**

```python
# src/pare_static_mcp/apk/manifest.py
from __future__ import annotations

def _exported(apk, kind_getter, has_intent_filter) -> list[str]:
    out = []
    for name in kind_getter():
        val = apk.get_element("activity", "exported", name=name) if False else None
        # exported if android:exported="true", or (pre-31) has an intent-filter
        # and exported not explicitly false. Compute with apk.get_intent_filters.
        out.append(name)
    return out

def parse(apk) -> dict:
    target = apk.get_effective_target_sdk_version()
    activities = apk.get_activities()
    services = apk.get_services()
    receivers = apk.get_receivers()
    providers = apk.get_providers()
    exported = []
    for kind, names in (("activity", activities), ("service", services),
                        ("receiver", receivers), ("provider", providers)):
        for n in names:
            exp = apk.get_element(kind, "exported", name=n)
            ifs = apk.get_intent_filters(kind, n)
            is_exp = (str(exp).lower() == "true") or (exp is None and ifs and target < 31)
            if is_exp:
                exported.append(n)
    return {
        "permissions": apk.get_permissions(),
        "activities": activities, "services": services,
        "receivers": receivers, "providers": providers,
        "application_class": apk.get_attribute_value("application", "name"),
        "exported": exported,
        "debuggable": apk.get_element("application", "debuggable") == "true",
        "allow_backup": apk.get_element("application", "allowBackup") != "false",
    }
```

```python
# add to tools.py
from pare_static_mcp.apk import manifest as manifest_mod

async def read_manifest() -> str:
    try:
        st = _require_current()
        m = await asyncio.to_thread(manifest_mod.parse, st.apk)
        return _ok(f"manifest for {st.package}", **m)
    except Exception as e:
        return _err("read_manifest failed", e)
```

- [ ] **Step 4: Run → PASS.** (Adjust androguard manifest accessors to the pinned API if needed.)

- [ ] **Step 5: Commit** `feat: read_manifest (components, exported inference, flags)`

---

### Task 5: `extract_strings`

**Files:** Modify `tools.py`; Create `src/pare_static_mcp/apk/strings.py`; Test `tests/unit/test_extract_strings.py`

**Interfaces:**
- Produces: `apk.strings.extract(analysis, filt) -> list[dict]` (rows `{value, class, method, kind, source}`); `tools.extract_strings(filter: str = "") -> str`.

- [ ] **Step 1: Failing test**

```python
# tests/unit/test_extract_strings.py
import json
from pare_static_mcp import tools
from tests.fixtures.locate import test_apk, requires_apk, TEST_STRING

@requires_apk
async def test_extract_finds_known_string():
    await tools.load_apk(str(test_apk()))
    out = json.loads(await tools.extract_strings(TEST_STRING))
    assert out.get("error") is not True
    vals = [r["value"] for r in out["rows"]]
    assert any(TEST_STRING in v for v in vals)
    assert all(r["source"] == "dex" for r in out["rows"])
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement**

```python
# src/pare_static_mcp/apk/strings.py
from __future__ import annotations

def extract(analysis, filt: str) -> list[dict]:
    rows = []
    for sa in analysis.get_strings():
        value = sa.get_value()
        if filt and filt not in value:
            continue
        xrefs = list(sa.get_xref_from())  # (ClassAnalysis, MethodAnalysis) pairs
        if xrefs:
            for ca, ma in xrefs:
                rows.append({"value": value, "class": str(ca.name),
                             "method": getattr(ma, "name", None),
                             "kind": "string", "source": "dex"})
        else:
            rows.append({"value": value, "class": None, "method": None,
                         "kind": "string", "source": "dex"})
    return rows
```

```python
# add to tools.py
from pare_static_mcp.apk import strings as strings_mod

async def extract_strings(filter: str = "") -> str:
    try:
        st = _require_current()
        rows = await asyncio.to_thread(strings_mod.extract, st.analysis, filter)
        return _ok(f"{len(rows)} strings"
                   + ("" if filter else " (pass filter= to narrow)"), rows=rows)
    except Exception as e:
        return _err("extract_strings failed", e)
```

- [ ] **Step 4: Run → PASS.**

- [ ] **Step 5: Commit** `feat: extract_strings (dex pool, string->method xref)`

---

### Task 6: `list_methods`

**Files:** Modify `tools.py`; Create `src/pare_static_mcp/apk/classes.py`; Test `tests/unit/test_list_methods.py`

**Interfaces:**
- Produces: `apk.classes.list_methods(analysis, cls) -> list[dict]` (rows `{method, descriptor, flags, xref_count}`); `tools.list_methods(cls: str) -> str`.

- [ ] **Step 1: Failing test**

```python
# tests/unit/test_list_methods.py
import json
from pare_static_mcp import tools
from tests.fixtures.locate import test_apk, requires_apk, TEST_CLASS, TEST_METHOD

@requires_apk
async def test_list_methods_finds_encrypt():
    await tools.load_apk(str(test_apk()))
    out = json.loads(await tools.list_methods(TEST_CLASS))
    assert out.get("error") is not True
    assert any(r["method"] == TEST_METHOD for r in out["rows"])
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement**

```python
# src/pare_static_mcp/apk/classes.py
from __future__ import annotations

def _match(name: str, cls: str) -> bool:
    # accept dotted (com.example.Crypto) or smali (Lcom/example/Crypto;)
    smali = "L" + cls.replace(".", "/") + ";"
    return name in (cls, smali) or name.rstrip(";").endswith(cls.replace(".", "/"))

def list_methods(analysis, cls: str) -> list[dict]:
    rows = []
    for ca in analysis.get_classes():
        if not _match(str(ca.name), cls):
            continue
        for ma in ca.get_methods():
            em = ma.get_method()
            rows.append({
                "method": ma.name,
                "descriptor": str(getattr(em, "descriptor", getattr(ma, "descriptor", ""))),
                "flags": str(getattr(em, "access_flags_string", "")),
                "xref_count": len(list(ma.get_xref_from())),
            })
    return rows
```

```python
# add to tools.py
from pare_static_mcp.apk import classes as classes_mod

async def list_methods(cls: str) -> str:
    try:
        st = _require_current()
        loader_mod.ensure_xref(st)   # xref_count needs the graph
        rows = await asyncio.to_thread(classes_mod.list_methods, st.analysis, cls)
        return _ok(f"{len(rows)} methods in {cls}", rows=rows)
    except Exception as e:
        return _err("list_methods failed", e)
```

- [ ] **Step 4: Run → PASS.**

- [ ] **Step 5: Commit** `feat: list_methods (methods+descriptors+xref_count)`

---

### Task 7: lazy xref + `find_symbol`

**Files:** Modify `tools.py`; Create `src/pare_static_mcp/apk/symbols.py`; Test `tests/unit/test_find_symbol.py` (+ add a multidex fixture if exercising cross-dex xref)

**Interfaces:**
- Produces: `apk.symbols.find(analysis, symbol, kind, cls) -> list[dict]` (rows `{class, method, signature, kind}`); `tools.find_symbol(symbol, kind="def", cls="") -> str`.

- [ ] **Step 1: Failing test**

```python
# tests/unit/test_find_symbol.py
import json
from pare_static_mcp import tools
from tests.fixtures.locate import test_apk, requires_apk, TEST_METHOD

@requires_apk
async def test_find_symbol_def():
    await tools.load_apk(str(test_apk()))
    out = json.loads(await tools.find_symbol(TEST_METHOD))
    assert out.get("error") is not True
    defs = [r for r in out["rows"] if r["kind"] == "def"]
    assert any(r["method"] == TEST_METHOD
               and "KeyStore" in r["class"] for r in defs)

@requires_apk
async def test_find_symbol_default_kind_is_def():
    await tools.load_apk(str(test_apk()))
    out = json.loads(await tools.find_symbol(TEST_METHOD))
    assert all(r["kind"] == "def" for r in out["rows"])
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement**

```python
# src/pare_static_mcp/apk/symbols.py
from __future__ import annotations

def _rows_for(ma, kind: str) -> list[dict]:
    em = ma.get_method()
    sig = str(getattr(em, "descriptor", ""))
    base = {"class": str(ma.class_name), "method": ma.name, "signature": sig}
    out = []
    if kind in ("def", "both") and not ma.is_external():
        out.append({**base, "kind": "def"})
    if kind in ("caller", "both"):
        for _, caller, _ in ma.get_xref_from():
            out.append({"class": str(caller.class_name), "method": caller.name,
                        "signature": str(getattr(caller.get_method(), "descriptor", "")),
                        "kind": "caller"})
    return out

def find(analysis, symbol: str, kind: str, cls: str) -> list[dict]:
    rows = []
    classname = ("L" + cls.replace(".", "/") + ";") if cls else "."
    for ma in analysis.find_methods(classname=classname, methodname=f"^{symbol}$"):
        rows.extend(_rows_for(ma, kind))
    return rows
```

```python
# add to tools.py
from pare_static_mcp.apk import symbols as symbols_mod

async def find_symbol(symbol: str, kind: str = "def", cls: str = "") -> str:
    try:
        st = _require_current()
        loader_mod.ensure_xref(st)
        rows = await asyncio.to_thread(symbols_mod.find, st.analysis, symbol, kind, cls)
        return _ok(f"{len(rows)} {kind} rows for {symbol}", rows=rows)
    except Exception as e:
        return _err("find_symbol failed", e)
```

- [ ] **Step 4: Run → PASS.** (`find_methods` takes regex; verify param names against the pinned androguard.)

- [ ] **Step 5: Commit** `feat: find_symbol (xref def/caller, class-scoped, def default) + lazy xref`

---

### Task 8: `grep_smali`

**Files:** Modify `tools.py`; Create `src/pare_static_mcp/apk/smali.py`; Test `tests/unit/test_grep_smali.py`

**Interfaces:**
- Produces: `apk.smali.grep(analysis, pattern) -> list[dict]` (rows `{class, method, insn, match}`); `tools.grep_smali(pattern: str) -> str`.

- [ ] **Step 1: Failing test**

```python
# tests/unit/test_grep_smali.py
import json
from pare_static_mcp import tools
from tests.fixtures.locate import test_apk, requires_apk, TEST_STRING

@requires_apk
async def test_grep_smali_matches_string_pool():
    await tools.load_apk(str(test_apk()))
    out = json.loads(await tools.grep_smali(TEST_STRING))
    assert out.get("error") is not True
    assert len(out["rows"]) >= 1

@requires_apk
async def test_grep_smali_bad_regex_errors():
    await tools.load_apk(str(test_apk()))
    out = json.loads(await tools.grep_smali("("))
    assert out["error"] is True
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement**

```python
# src/pare_static_mcp/apk/smali.py
from __future__ import annotations
import re

def grep(analysis, pattern: str) -> list[dict]:
    rx = re.compile(pattern)               # raises on bad regex -> tool _err
    rows = []
    for ca in analysis.get_classes():
        for ma in ca.get_methods():
            em = ma.get_method()
            if em is None or ma.is_external():
                continue
            try:
                instructions = em.get_instructions()
            except Exception:
                continue
            for ins in instructions:
                text = f"{ins.get_name()} {ins.get_output()}"
                m = rx.search(text)
                if m:
                    rows.append({"class": str(ca.name), "method": ma.name,
                                 "insn": text.strip(), "match": m.group(0)})
    return rows
```

```python
# add to tools.py
from pare_static_mcp.apk import smali as smali_mod

async def grep_smali(pattern: str) -> str:
    try:
        st = _require_current()
        rows = await asyncio.to_thread(smali_mod.grep, st.analysis, pattern)
        return _ok(f"{len(rows)} smali matches", rows=rows)
    except Exception as e:
        return _err("grep_smali failed", e)
```

- [ ] **Step 4: Run → PASS.**

- [ ] **Step 5: Commit** `feat: grep_smali (regex over smali + string pool)`

---

### Task 9: `decompile_method` — jadx (guarded) + smali fallback + overloads

**Files:** Modify `tools.py`; Create `src/pare_static_mcp/apk/decompile.py`; Test `tests/unit/test_decompile_method.py` (+ build the obfuscated fixture here)

**Interfaces:**
- Consumes: `config` (jadx path/timeout/cap), `APKState`.
- Produces: `apk.decompile.decompile(state, cls, method, signature, lang, cfg) -> dict` (`{class, method, lang, source}` or `{overloads: [...]}`); `tools.decompile_method(cls, method, signature="", lang="java") -> str`.

- [ ] **Step 1: Failing test**

```python
# tests/unit/test_decompile_method.py
import json
import pytest
from pare_static_mcp import tools
from pare_static_mcp.tools import CFG
from tests.fixtures.locate import test_apk, requires_apk, TEST_CLASS, TEST_METHOD

@requires_apk
async def test_decompile_smali_always_available():
    await tools.load_apk(str(test_apk()))
    out = json.loads(await tools.decompile_method(
        TEST_CLASS, TEST_METHOD, lang="smali"))
    assert out.get("error") is not True
    assert out["lang"] == "smali"
    assert TEST_METHOD in out["source"]

@requires_apk
@pytest.mark.skipif(not CFG.jadx_available, reason="JADX_PATH unresolved")
async def test_decompile_java_via_jadx():
    await tools.load_apk(str(test_apk()))
    out = json.loads(await tools.decompile_method(
        TEST_CLASS, TEST_METHOD, lang="java"))
    assert out.get("error") is not True
    assert out["lang"] == "java"
    assert TEST_METHOD in out["source"]
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement** (argv jadx, `--`, timeout, stdout cap, mkdtemp; smali via androguard)

```python
# src/pare_static_mcp/apk/decompile.py
from __future__ import annotations
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

def _smali_source(state, cls, method) -> str | None:
    smali = "L" + cls.replace(".", "/") + ";"
    for ca in state.analysis.get_classes():
        if str(ca.name) not in (cls, smali) and not str(ca.name).endswith(
                cls.replace(".", "/") + ";"):
            continue
        parts = []
        for ma in ca.get_methods():
            if ma.name != method:
                continue
            em = ma.get_method()
            parts.append(f"# {ma.name} {getattr(em, 'descriptor', '')}")
            for ins in em.get_instructions():
                parts.append(f"    {ins.get_name()} {ins.get_output()}")
        if parts:
            return "\n".join(parts)
    return None

def _slice_java(java_text: str, method: str) -> list[str]:
    # best-effort brace-matched slice of each method named `method`
    out, i = [], 0
    for m in re.finditer(rf"\b{re.escape(method)}\s*\(", java_text):
        start = java_text.rfind("\n", 0, m.start()) + 1
        depth, j, seen = 0, m.end(), False
        while j < len(java_text):
            c = java_text[j]
            if c == "{": depth += 1; seen = True
            elif c == "}":
                depth -= 1
                if seen and depth == 0:
                    out.append(java_text[start:j + 1]); break
            j += 1
    return out

def _jadx_class(state, cls, cfg) -> str:
    fqcn = cls  # dotted
    outdir = Path(tempfile.mkdtemp(prefix="pare-static-", dir=None))
    try:
        cmd = [cfg.jadx_path, "--rename-flags", "none", "--no-res",
               "--single-class", fqcn, "-d", str(outdir), "--", state.path]
        proc = subprocess.run(cmd, capture_output=True, timeout=cfg.jadx_timeout_s,
                              text=True)
        _ = proc.stdout[:cfg.jadx_stdout_cap]
        java_files = list(outdir.rglob("*.java"))
        if not java_files:
            raise RuntimeError(f"jadx produced no source (rc={proc.returncode})")
        return max(java_files, key=lambda p: p.stat().st_size).read_text(
            errors="replace")[:cfg.jadx_stdout_cap]
    finally:
        shutil.rmtree(outdir, ignore_errors=True)

def decompile(state, cls, method, signature, lang, cfg) -> dict:
    if lang == "smali":
        src = _smali_source(state, cls, method)
        if src is None:
            raise LookupError(f"{cls}.{method} not found")
        return {"class": cls, "method": method, "lang": "smali", "source": src}
    java = _jadx_class(state, cls, cfg)
    slices = _slice_java(java, method)
    if not slices:
        raise LookupError(f"{method} not found in decompiled {cls}")
    if len(slices) > 1 and not signature:
        return {"class": cls, "method": method, "lang": "java",
                "overloads": slices, "summary_note": "multiple overloads; pass signature"}
    return {"class": cls, "method": method, "lang": "java", "source": slices[0]}
```

```python
# add to tools.py
from pare_static_mcp.apk import decompile as decompile_mod

async def decompile_method(cls: str, method: str, signature: str = "",
                           lang: str = "java") -> str:
    try:
        st = _require_current()
        if lang == "java" and not CFG.jadx_available:
            lang = "smali"          # graceful degrade
        res = await asyncio.to_thread(decompile_mod.decompile, st, cls, method,
                                      signature, lang, CFG)
        return _ok(f"decompiled {cls}.{method} ({res['lang']})", **res)
    except Exception as e:
        return _err("decompile_method failed", e)
```

- [ ] **Step 4: Run → PASS** (smali test always; java test runs when `JADX_PATH` resolves, else SKIP).

- [ ] **Step 5: Commit** `feat: decompile_method (guarded jadx + smali fallback + overloads)`

> jadx runs with `--rename-flags none` so its identifiers match androguard's dex names. OMTG is unobfuscated (names align directly); a dedicated obfuscated fixture + name-reconciliation test is a fast-follow once a second APK is sourced (noted in Task 2).

---

### Task 10: PARE registration, README, eval smoke

**Files:**
- Create: `README.md` (this repo)
- Modify (PARE repo): `~/Projects/PARE/workers.yaml`
- Test: `tests/integration/test_keystore_chain.py` (fixture-deterministic) + a skippable live OMTG smoke

**Interfaces:** Consumes all tools. No new production interface.

- [ ] **Step 1: Failing integration test (the §9 chain, fixture-based)**

```python
# tests/integration/test_keystore_chain.py
import json
from pare_static_mcp import tools
from tests.fixtures.locate import test_apk, requires_apk, TEST_METHOD, TEST_STRING

@requires_apk
async def test_derive_target_chain():
    await tools.load_apk(str(test_apk()))
    sym = json.loads(await tools.find_symbol(TEST_METHOD))
    row = next(r for r in sym["rows"] if r["kind"] == "def")
    dec = json.loads(await tools.decompile_method(
        row["class"], row["method"], row["signature"], lang="smali"))
    assert TEST_METHOD in dec["source"]
    strings = json.loads(await tools.extract_strings(TEST_STRING))
    assert any(TEST_STRING in r["value"] for r in strings["rows"])
```

- [ ] **Step 2: Run → PASS** (all pieces exist by now).

- [ ] **Step 3: Register in PARE `workers.yaml`**

Add under `workers:` in `~/Projects/PARE/workers.yaml`:
```yaml
  static:
    command: /home/edible/Projects/PARE/.venv/bin/pare-static-mcp
    transport: stdio
    risk_default: low
    capability_tags: [static, apk, android]
```

- [ ] **Step 4: Write `README.md`** documenting: purpose, the 7 tools, `JADX_PATH`/androguard deps, single-open model, and the guard env vars from `config.py`.

- [ ] **Step 5: Live OMTG smoke (manual, skippable)**

Document in the README: install the worker into PARE's venv (`pip install -e .`), `load_apk` the `OMTG_DATAST_001_KeyStore` APK, run `find_symbol("encryptString")` → `decompile_method` → confirm the crypto target is derivable. Mark the automated version `@pytest.mark.skipif` on an env flag pointing at the OMTG APK.

- [ ] **Step 6: Commit** (two repos)

```bash
git add -A && git commit -m "feat: KeyStore-chain integration test + README"
# in ~/Projects/PARE:
git add workers.yaml && git commit -m "feat: register static-analysis worker (eager mount, low tier)"
```

---

## Self-Review

**Spec coverage:** load_apk+distrust signals (T3) ✓; find_symbol def/caller/class-scope/def-default (T7) ✓; grep_smali (T8) ✓; list_methods (T6) ✓; extract_strings dex+source tag+xref (T5) ✓; decompile_method descriptor/overloads/jadx-flags/smali-fallback/blob (T9) ✓; read_manifest providers/application_class/exported/flags (T4) ✓; low tier + conformance (T1) ✓; lazy import + threading + lazy xref (T3/T6/T7) ✓; json envelope (T1 stub + every tool) ✓; hostile-input guards — androguard ceiling (T3), jadx argv/`--`/timeout/stdout-cap/mkdtemp (T9) ✓; descriptions as deliverable (T1) ✓; single-open + package echo (T3 `_ok`) ✓; fixtures tiny/obfuscated/multidex (T2/T9/T7) ✓; smali proves §9 without jadx (T9/T10) ✓; workers.yaml + README (T10) ✓; card system explicitly NOT here (Spec 2) ✓.

**Placeholder scan:** no TBD/TODO; every code step carries real code. androguard 4.x API calls are best-effort against the pinned version and flagged in Global Constraints as TDD-verified (external-library ground truth), not internal placeholders.

**Type consistency:** row dicts use consistent keys across tasks (`class`/`method`/`signature`/`kind` in find_symbol→decompile_method; `rows=[...]` envelope key throughout); `_ok`/`_err`/`_require_current`/`ensure_xref`/`set_current`/`CURRENT` names consistent T3→T9.
