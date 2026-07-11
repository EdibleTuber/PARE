# pare-static-mcp — Static Analysis Worker (v1) Design

**Date:** 2026-07-10
**Status:** Approved (brainstorming) — pending implementation plan
**Repo:** `pare-static-mcp` (new, `https://github.com/EdibleTuber/pare-static-mcp`)
**Related:** [[project_static_analysis_direction]], [[project_context_scaling]], [[project_frida_mcp_inhouse]], [[project_risk_tier_resolution]], [[project_snapshot_capture_layer]]

## 1. Motivation

PARE is currently **dynamic-only** (the frida worker). In the KeyStore solve
(`OMTG_DATAST_001_KeyStore`), the decisive insight — that `encryptString` only
takes the key alias `"Dummy"`, and the plaintext becomes a `byte[]` only at
`CipherOutputStream.write` — came from **decompiling the APK**, which PARE cannot
do. The agent could only *enumerate* classes at runtime, not *read the code* to
find the hook target, so it flailed and leaned on hand-written runbooks.

Static analysis is the missing leg. Static + dynamic is the classic RE pairing:
static lets the agent **derive** hook targets from code instead of memorizing
them. This worker adds that leg.

## 2. Scope

### In scope (v1)

A new stdio MCP worker, `pare-static-mcp`, exposing **six read-only tools** for
Android APK static analysis. It mirrors the in-house `pare-frida-mcp` worker
pattern: a separate repo, its own Python package, stdio transport, per-tool wire
risk tiers, validated against agent_core's `assert_stdio_conformance` contract.

For v1 the worker mounts into PARE **eagerly**, exactly like frida
(`workers.yaml`). The card / `/load` lazy-schema system is a **separate spec**
(Spec 2) and is explicitly *not* required for this worker to function.

### Out of scope (deferred, named)

- **Acquire surface (v2):** `pull_apk` — `adb pull` an installed package's APK(s)
  off a device to the host. Natural front door to analysis; device *read*, so
  low/medium tier; adds an `adb`/platform-tools dependency. Deferred so v1 stays
  a self-contained, no-device worker.
- **Repackage / deploy surface (v2, likely its own worker):** `rebuild`, `sign`,
  `zipalign`, `install`/push. These **mutate device and file state** → `high`-tier,
  operator-gated (like frida's dangerous tools), and pull in
  `apktool` + `apksigner` + `zipalign` + keystore management. Different *verb*
  (modify/deploy vs analyze) and different risk profile → deserves its own design,
  probably its own worker (`pare-repackage-mcp`) or a frida-side extension.
- **Native / `.so` / ghidra**, `decompile_class` (whole-class decompilation),
  `unload_apk` / cache eviction, and iOS/raw-dex subpackages. The repo *name*
  (`pare-static-mcp`, not `pare-apk-mcp`) leaves room for these, but there is no
  v1 code for them.

## 3. Architecture — the engine split

The worker is backed by a **hybrid** of two engines, each used for what it is
best at:

- **androguard** (pure-Python, `pip`-installed, in-process): parses the APK/DEX,
  builds the `Analysis` (class list, string/constant pool, and the cross-reference
  / call graph). Fast, no JVM, no subprocess. Backs `load_apk`, `list_classes`,
  `read_manifest`, `extract_strings`, and `search_code` (via xrefs).
- **jadx** (external binary + JRE, located via config, **not** a pip dep): shells
  out only for `decompile_method`, where readable Java output matters and
  androguard's built-in DAD decompiler is weaker.

```
load_apk ────────► androguard: parse APK, build Analysis (xref/call graph), cache
list_classes ─────► androguard   (in-process)
read_manifest ────► androguard
extract_strings ──► androguard
search_code ──────► androguard xref (definitions + callers of a symbol)
decompile_method ─► jadx subprocess: decompile the *containing class* once
                    (cached per (apk_id, class)), slice out the method body
```

**jadx granularity note:** jadx decompiles at class/file granularity, not per
method. `decompile_method` therefore decompiles the whole containing class on
first request (caching the result per `(apk_id, class)`), then extracts and
returns just the requested method. jadx cost is paid once per class; output to
the model stays method-sized.

## 4. The six tools

All tools return **well-formed JSON shaped for the PARE capture layer**: an array
becomes N searchable rows; an object becomes one row; an oversized blob is
captured and the model receives a bounded stub plus `search_capture`/`read_capture`
(see [[project_snapshot_capture_layer]]). `apk_id` is optional on every tool and
defaults to the most-recently-loaded APK (mirrors frida's "default active
session" convenience).

| Tool | Signature | Returns | Capture shape |
|------|-----------|---------|---------------|
| `load_apk` | `(path)` | `{apk_id, package, min_sdk, target_sdk, class_count}` | small object → 1 row |
| `list_classes` | `(filter?, apk_id?)` | rows of `{class, superclass, method_count, flags}` | N rows (filterable) |
| `search_code` | `(symbol, kind?, apk_id?)` | rows of `{class, method, signature, kind}` | N rows |
| `extract_strings` | `(filter?, apk_id?)` | rows of `{value, class, method?, kind}` | N rows (filterable) |
| `decompile_method` | `(class, method, apk_id?, lang?)` | `{class, method, lang, source}` | blob → captured |
| `read_manifest` | `(apk_id?)` | `{package, permissions[], activities[], services[], receivers[], exported[]}` | object → 1 row |

Tool-specific decisions:

- **`search_code`** returns **both** definitions and callers of `symbol` by
  default, each row tagged `kind: "def" | "caller"`. An optional `kind` argument
  narrows to one. Rationale: a small model should not have to guess which it
  wants; the natural question is "where does `encryptString` live and who calls
  it?" — answer both, let the model read. Symbol match is against method/field
  names via androguard xrefs (precise, structural — not a text grep).
- **`extract_strings`** `kind` on each row is `"string"` (literal from the string
  pool) or `"const"` (numeric/other constant). `filter` narrows by substring.
- **`decompile_method`** `lang` is `"java"` (default, jadx) or `"smali"`
  (androguard disassembly — no jadx needed, cheaper fallback).
- **`list_classes`** / **`extract_strings`** `filter` is essential: real APKs have
  thousands of classes and tens of thousands of strings; unfiltered results rely
  on the capture layer but a filter keeps the common case tight.

## 5. Risk tiers

Every v1 tool is **read-only static analysis** — nothing touches a device,
nothing mutates state. Therefore:

- `risk_default: low` (the worker's floor in `workers.yaml`).
- Every tool advertises wire tier `low` in MCP `_meta` under `agent_core/risk_tier`,
  so it resolves cleanly through `effective = max(floor, wire, pin)`
  (see [[project_risk_tier_resolution]]).
- **No operator pins, no per-tool escalation.** This is the deliberate opposite of
  the frida worker's `high` floor. All six tools auto-execute and are audited; none
  prompt for approval.
- The only device-adjacent consideration, `load_apk` reading an arbitrary
  filesystem path, is acceptable `low` for an operator-driven local RE lab. (The
  device-*mutating* tools that would justify `high` all live in the deferred v2
  surfaces.)

## 6. Session & caching lifecycle

- `load_apk` builds the androguard `Analysis` (the expensive step) and caches it
  server-side under a generated `apk_id`. jadx output is cached lazily, per class,
  on first `decompile_method`.
- **Multiple APKs may be open at once** (distinct `apk_id`s) — enables comparing
  two builds in a future workflow.
- The most-recently-loaded `apk_id` is the default target when a tool omits it.
- **No `unload_apk` / eviction in v1.** The worker process is per-session
  (launched by the PARE daemon, dies with it); an explicit unload or LRU eviction
  is a deferred nicety, called out so it is a conscious omission rather than a gap.

## 7. Packaging

Mirrors `pare-frida-mcp`:

- `src/pare_static_mcp/` package with `server.py` (stdio MCP entrypoint),
  `tools.py`, `contract.py`, `config.py`, and an `android/` (or `apk/`) subpackage
  for the androguard/jadx adapters.
- Console script `pare-static-mcp`.
- PARE mounts it in `workers.yaml`:

  ```yaml
  static:
    command: /home/edible/Projects/PARE/.venv/bin/pare-static-mcp
    transport: stdio
    risk_default: low
    capability_tags: [static, apk, android]
  ```

- Dependencies: `androguard` (pinned to a 4.x release), and an **external jadx
  binary + JRE** located via config (`JADX_PATH`, default `jadx` on `PATH`). jadx
  is a runtime/system dependency, not a pip dependency.

## 8. Conformance & testing

Mirror `pare-frida-mcp`'s test layout:

- Per-tool unit tests (`tests/unit/`).
- `tests/integration/test_conformance.py` — `assert_stdio_conformance` against the
  agent_core worker contract.
- `tests/integration/test_wire_risk_tier.py` — every tool advertises `low`.
- Tool-envelope tests — each tool returns the capture-layer-shaped JSON described
  in §4.
- **Fixture:** a small, **purpose-built** APK checked into the repo for
  deterministic unit tests (avoids OMTG licensing/size baggage). Live validation
  against `OMTG_DATAST_001_KeyStore` from the eval harness
  ([[project_pare_eval_harness]]) — the worker must let the agent *derive* the
  `encryptString` / `CipherOutputStream.write` hook target that motivated it.

## 9. Success criteria

1. The six tools are callable over stdio from PARE and pass conformance.
2. Given `OMTG_DATAST_001_KeyStore`, an operator (or the agent) can:
   `load_apk` → `search_code("encryptString")` finds the definition + callers →
   `decompile_method` reads the method → the crypto hook target is derivable from
   static output alone, without a runbook.
3. All output flows through the capture layer without blowing the model's context
   window (large `list_classes` / `extract_strings` results are captured, not
   inlined).
4. All tools resolve to `low` tier and auto-execute (audited); none prompt.

## 10. Follow-on specs (not this effort)

- **Spec 2 — card / `/load` system (PARE):** lazy schema injection, card stubs
  always in context, `/load` + `/workers`, session-scoped loaded set. Retrofits
  frida + static as the first two cards. This is what makes the N-worker future
  fit a small model's context ([[project_context_scaling]]).
- **v2 acquire — `pull_apk`** (adb, low/med tier).
- **v2 repackage/deploy — `rebuild`/`sign`/`zipalign`/`install`** (high tier,
  apktool/apksigner; likely its own worker).
