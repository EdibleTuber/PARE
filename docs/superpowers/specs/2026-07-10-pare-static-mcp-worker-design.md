# pare-static-mcp — Static Analysis Worker (v1) Design

**Date:** 2026-07-10
**Status:** Approved (brainstorming + 5-lens adversarial panel) — pending implementation plan
**Repo:** `pare-static-mcp` (new, `https://github.com/EdibleTuber/pare-static-mcp`)
**Related:** [[project_static_analysis_direction]], [[project_context_scaling]], [[project_frida_mcp_inhouse]], [[project_risk_tier_resolution]], [[project_snapshot_capture_layer]]

> **Revision note (post-panel).** This spec was reviewed by a five-lens
> adversarial panel (Android RE toolchain, agent_core integration, small-model
> ergonomics, scope/YAGNI, hostile-input security). Verdict was unanimous "ship
> with patches, no redesign." Their findings are folded in below; the four
> decisions they surfaced were resolved as: **single-APK-open**; **add
> `grep_smali`**; **swap `list_classes`→`list_methods`**; **keep worker-first
> (the `static_` tool prefix is automatic in agent_core), card/`/load` = Spec 2.**

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

A new stdio MCP worker, `pare-static-mcp`, exposing **seven read-only tools** for
Android APK static analysis. It mirrors the in-house `pare-frida-mcp` worker
pattern: a separate repo, its own Python package, stdio transport, per-tool wire
risk tiers, validated against agent_core's `assert_stdio_conformance` contract.

For v1 the worker mounts into PARE **eagerly**, exactly like frida
(`workers.yaml`). agent_core already namespaces every tool as
`{worker_name}_{tool}` (`workers/tool_factory.py`), so the model sees
`static_load_apk`, `static_find_symbol`, … automatically — no manual prefix
needed, and the static-vs-dynamic axis is legible in every tool name. The card /
`/load` lazy-schema system is a **separate spec** (Spec 2) and is explicitly *not*
required for this worker to function.

### Out of scope (deferred, named)

- **Multi-APK-open + `apk_id` handles + `unload_apk` (v2):** v1 is
  **single-APK-open** (§7). Comparing two builds (the only workflow that
  justified multi-open) returns in v2 along with explicit `apk_id` handles and an
  `unload_apk` verb.
- **`resources.arsc` / asset string extraction (fast-follow):** v1 `extract_strings`
  covers the **DEX string pool** and tags each row `source: "dex"`. Strings in
  `res/values` (compiled into `resources.arsc`) and `assets/` are a named
  fast-follow; the `source` tag keeps v1 honest about what it did *not* scan.
- **`decompile_class` (v2):** whole-class decompilation. Nearly free later — v1's
  `decompile_method` already decompiles the whole containing class and caches it
  (§3), so this is returning the un-sliced cached result.
- **Acquire — `pull_apk` (v2):** `adb pull` an installed package's APK(s) off a
  device. Natural front door to analysis; device *read* → low/medium tier; adds an
  `adb`/platform-tools dependency. Clean seam (produces a host path → feeds
  `load_apk`).
- **Repackage / deploy (v2, likely its own worker):** `rebuild`, `sign`,
  `zipalign`, `install`/push. These **mutate device and file state** → `high`-tier,
  operator-gated, and pull in `apktool` + `apksigner` + `zipalign` + keystore
  management. Different *verb* and risk profile → its own design (`pare-repackage-mcp`).
- **Native / `.so` / ghidra**, and iOS/raw-dex subpackages. The repo *name*
  (`pare-static-mcp`) leaves room; there is no v1 code for them.

## 3. Architecture — engine split & runtime

### Engine split (hybrid)

- **androguard** (pure-Python, `pip`, in-process): parses the APK/DEX, exposes the
  class/method tables, the string/constant pool, smali-level disassembly, and the
  cross-reference / call graph. Backs `load_apk`, `list_methods`, `read_manifest`,
  `extract_strings`, `find_symbol` (via xrefs), and `grep_smali` (regex over smali
  + string pool).
- **jadx** (external binary + JRE, located via config, **not** a pip dep): shells
  out only for `decompile_method`, where readable Java matters and androguard's
  built-in DAD decompiler is weaker.

```
load_apk ────────► androguard: parse APK, extract metadata + distrust signals
                   (Analysis object built; xref graph built LAZILY — see below)
list_methods ─────► androguard   (class → methods + descriptors + xref counts)
read_manifest ────► androguard
extract_strings ──► androguard   (DEX string pool; source="dex")
find_symbol ──────► androguard xref  (definitions + callers of a method/field)
grep_smali ───────► androguard   (regex over smali instructions + string pool)
decompile_method ─► jadx subprocess: decompile the *containing class* once
                    (cached), slice out the method (name + descriptor)
```

### Runtime concurrency & boot (panel-driven, load-bearing)

The worker is a single process handling stdio MCP requests on its own event loop.
androguard's `Analysis`/xref build and the jadx subprocess are **seconds-to-tens-
of-seconds** synchronous work, so:

- **Lazy-import androguard.** Importing androguard 4.x is heavy; agent_core's
  discovery wraps connect+`initialize`+`list_tools` in a **non-tunable 2s ceiling**
  and *silently skips* a worker that exceeds it (`workers/discovery.py`). So
  `import androguard` must happen **inside `load_apk`**, never at module load —
  otherwise a cold-cache boot makes the worker vanish from the fleet with no error.
  jadx is already lazy (subprocess).
- **Thread the blocking work.** Wrap the androguard `Analysis`/xref build and the
  jadx subprocess in `asyncio.to_thread` / `run_in_executor` so a long op does not
  freeze the worker's loop (MCP pings, request handling). Guard the shared APK
  cache with a lock.
- **Lazy xref graph.** `Analysis.create_xref()` is O(all instructions) and is
  androguard's classic RAM/time trap. Defer it until the first `find_symbol` /
  `grep_smali`; `load_apk` / `read_manifest` / `extract_strings` / `list_methods`
  stay cheap. Add a wall-clock timeout on the build with a clear error.

### jadx name reconciliation

jadx renames illegal/clashing identifiers (and, with `--deobf`, invents new
names), which would diverge from androguard's DEX names and break the method
slice. **Pin jadx flags to disable renaming/deobfuscation**, drive it per-class
(`--single-class <dex-fqcn>` or equivalent), and reconcile via jadx's rename
mapping if any renaming remains. `decompile_method` **falls back to the smali
path** (androguard, no JVM) when jadx is missing/errors, so it degrades instead of
hard-failing.

## 4. The seven tools

**Output contract (mandatory).** Every tool returns a **`json.dumps` string in a
`text` content block**, using the frida worker's `_ok`/`_err` envelope shape. This
is not optional: the PARE capture layer's `stringify_result` concatenates only
`text` blocks and `json.loads` them; a bare dict / structured-only result
collapses to a single junk `{"value": …}` row and silently breaks every "N rows"
/ `search_capture` claim below. §9 includes an envelope test asserting the text
block parses.

**Capture shaping.** Array results become N searchable rows; a small object is
inlined verbatim to the model (fast, but *not* stored/searchable — do not rely on
searching it); an oversized blob is captured and the model gets a bounded stub +
`search_capture`/`read_capture` (see [[project_snapshot_capture_layer]]).

**Single-APK-open (§7):** there is one current APK; tools take **no `apk_id`** in
v1. Every response echoes the active `package` so a wrong-APK situation is visible
in-band.

| Tool (model sees `static_…`) | Signature | Returns | Notes |
|------|-----------|---------|-------|
| `load_apk` | `(path)` | `{package, min_sdk, target_sdk, class_count, dex_count, native_libs[], dynamic_load[]}` | Sets the current APK (replaces any prior). `dynamic_load[]` = DexClassLoader/`loadLibrary` indicators. **Distrust signals** tell the agent when static is blind and it should fall back to frida. |
| `find_symbol` | `(symbol, kind?, class?)` | rows of `{class, method, signature, kind}` | Matches **method/field NAMES via xref** — *not* string literals. `kind` defaults to `def` (definitions), also `caller`; `class` scopes the match. Returns the `(class, method, signature)` tuple `decompile_method` consumes. |
| `grep_smali` | `(pattern)` | rows of `{class, method, insn, match}` | Regex over **smali instructions + string pool** — reaches API/text patterns name-xref can't (e.g. `Ljavax/crypto/CipherOutputStream;->write`). |
| `list_methods` | `(class)` | rows of `{method, descriptor, flags, xref_count}` | The methods of a found class — how you pick a hook/decompile target without decompiling the whole class. |
| `extract_strings` | `(filter?)` | rows of `{value, class, method?, kind, source}` | DEX string pool. `kind` = `string`\|`const`; `source` = `dex` (arsc deferred). `class`/`method` via string xref (string→method pivot, the durable path on obfuscated apps). |
| `decompile_method` | `(class, method, signature?, lang?)` | `{class, method, lang, source}` (large `source` → its own blob capture) | Decompiles containing class via jadx (cached per class), slices the method by **name + descriptor**; returns **all overloads** when `signature` omitted and ambiguous. `lang` = `java` (jadx, default) \| `smali` (androguard fallback). |
| `read_manifest` | `()` | `{package, permissions[], activities[], services[], receivers[], providers[], application_class, exported[], debuggable, allow_backup}` | `exported[]` computed with the pre-31 intent-filter rule. `application_class` is a prime init/hook target. |

### Tool descriptions are a first-class deliverable

In the frida worker, the model-facing **description strings** carry the
anti-footgun hints that keep a weak model on-rails (e.g. `enumerate_classes`
spends sentences on the case-sensitivity trap). The static tools have *more*
confusable neighbors, so descriptions are a **spec deliverable**, not an
implementation afterthought. Each description MUST state:

1. An opener: **"STATIC (reads the APK file; no device/attach) — …"** (separates
   the static world from frida's dynamic world).
2. **What it matches vs. does NOT** — the load-bearing disambiguations:
   - `find_symbol`: "matches METHOD and FIELD names, not string literals — for a
     string constant use `static_extract_strings`; for an API/text pattern use
     `static_grep_smali`."
   - `list_methods`: "lists methods of ONE class; to find a class or symbol use
     `static_find_symbol`."
   - `extract_strings`: "DEX string pool only (not resources.arsc/assets)."
3. **The next tool in the chain** (e.g. `find_symbol` → feed the `def` row's
   `(class, method, signature)` to `decompile_method`; the `def` row is the
   implementation, `caller` rows are who invokes it).
4. **Filter-first** where applicable: "on a real APK, always pass `filter`/`class`
   — unfiltered results are captured and slow to page." When a call is captured
   for being unfiltered, the summary line should say "captured N rows — re-call
   with a filter to narrow" (the capture stub itself only advertises
   `read_capture`, so the tool must steer the model to filter).

The `find_symbol → decompile_method` **parameter-name symmetry** (`class`,
`method`, `signature` flow verbatim) is the design's best affordance — preserve it
rigorously so the tuple hand-off needs no invention by the model.

## 5. Risk tiers

Every v1 tool is **read-only static analysis** — nothing touches a device,
nothing mutates state. On the **approval axis** this is correct:

- `risk_default: low` (floor in `workers.yaml`); every tool advertises wire tier
  `low` in `_meta` under `agent_core/risk_tier`; resolves via
  `effective = max(floor, wire, pin)` (see [[project_risk_tier_resolution]]).
- **No operator pins, no per-tool escalation.** All seven auto-execute and are
  audited; none prompt. This is the deliberate inverse of frida's `high` floor —
  the device-*mutating* tools that would justify `high` all live in deferred v2
  surfaces.

**Read-only ≠ resource-safe**, however — see §6. The `low` tier is right for
*approval*; hostile-input safety is handled by guards, not gating (gating every
decompile behind a prompt would defeat the worker).

## 6. Hostile-input & resource guards

The worker parses **untrusted APKs (potentially malware — that is the point)**.
The threat model is a crafted APK that crashes/hangs/exhausts the worker, or a
class name that misbehaves on the jadx command line — not remote privilege
escalation (solo single-operator lab). v1 guards:

- **jadx invocation:** argv **array**, `shell=False`, a `--` separator before any
  path (so a hostile filename/class starting with `-` can't be read as a flag), a
  **wall-clock timeout** (kill the process group on expiry), and a **stdout byte
  cap** (jadx can emit hundreds of MB on a pathological class — cap protects the
  *worker*, distinct from the capture layer protecting the *model's context*).
- **androguard input guards:** a pre-parse APK **size / entry-count / decompressed-
  size ceiling** (zip-bomb / decompression amplification / synthetic-method blowup)
  before handing bytes to androguard; a timeout on the xref build (§3).
- **Temp files:** jadx working dirs via `tempfile.mkdtemp()` (**0700**,
  unpredictable name — not a fixed `/tmp/pare-static` path that holds decompiled
  malware world-readable), scoped per load, cleaned on worker exit.
- **`load_apk(path)`** reading an arbitrary path is acceptable at `low` for a local
  single-operator lab (no privilege boundary to cross). It should reject a
  non-regular-file / directory with a clean error rather than a parser stack trace,
  and the path must never flow into a subprocess except via the argv/`--`
  discipline above.
- Single-APK-open (§7) removes the unbounded multi-APK cache-growth risk for free.

## 7. Session & caching lifecycle

- **Single-APK-open.** `load_apk` replaces the resident androguard `Analysis` (the
  prior one is freed). One target at a time — matches solo-dev reality and §9
  (which loads one APK), eliminates the "wrong-APK default" footgun, and needs no
  `unload` verb. Tools take **no `apk_id`** in v1.
- jadx output is cached lazily, per class, on first `decompile_method`; the xref
  graph is built lazily on first `find_symbol`/`grep_smali` (§3). Both caches are
  scoped to the current APK and dropped on the next `load_apk`.
- Multiple-open + `apk_id` handles + `unload_apk` + automatic eviction are the
  named v2 surface (build comparison).

## 8. Packaging

Mirrors `pare-frida-mcp`:

- `src/pare_static_mcp/` package with `server.py` (stdio MCP entrypoint),
  `tools.py`, `contract.py`, `config.py`, and an `apk/` subpackage for the
  androguard/jadx adapters.
- Console script `pare-static-mcp`. PARE mounts it in `workers.yaml`:

  ```yaml
  static:
    command: /home/edible/Projects/PARE/.venv/bin/pare-static-mcp
    transport: stdio
    risk_default: low
    capability_tags: [static, apk, android]
  ```

  → tools surface to the model as `static_load_apk`, `static_find_symbol`, etc.
- Dependencies: `androguard` **pinned to an exact tested 4.x version** (4.x has had
  version-specific xref/perf regressions), and an **external jadx binary + JRE**
  located via config (`JADX_PATH`, default `jadx` on `PATH`; a version floor that
  supports single-class decompilation). jadx is a runtime/system dependency, not a
  pip dependency.

## 9. Conformance & testing

Mirror `pare-frida-mcp`'s layout:

- Per-tool unit tests (`tests/unit/`).
- `tests/integration/test_conformance.py` — `assert_stdio_conformance`.
- `tests/integration/test_wire_risk_tier.py` — every tool advertises `low`.
- **Envelope test** — each tool's result is a `text` block that `json.loads`
  cleanly into the `_ok`/`_err` shape (guards the §4 capture contract).
- **Fixtures:** a small purpose-built APK **plus** an **obfuscated** fixture and a
  **multidex** fixture — the toy APK exercises none of the real failure modes
  (xref cost, jadx renaming, overloads, multidex). Live validation against
  `OMTG_DATAST_001_KeyStore` from the eval harness ([[project_pare_eval_harness]]).
- The **smali fallback path must be able to demonstrate §9.2 end-to-end**, so the
  eval is never gated on the unpinned jadx binary. Java-path tests **skip with a
  clear "JADX_PATH unresolved" message** when jadx is absent, rather than fail.

## 10. Success criteria

1. The seven tools are callable over stdio from PARE and pass conformance +
   envelope tests.
2. Given `OMTG_DATAST_001_KeyStore`, the agent can: `load_apk` →
   `find_symbol("encryptString")` (returns the def's `class`/`method`/`signature`)
   → `decompile_method(...)` reads the method → the crypto hook target is
   **derivable from static output alone, without a runbook**. `extract_strings`
   surfaces the `"Dummy"` alias; `grep_smali` reaches the `CipherOutputStream`
   usage.
3. Large `list_methods` / `extract_strings` / decompiled-source results flow
   through the capture layer without blowing the model's context window.
4. All tools resolve to `low` and auto-execute (audited); none prompt.
5. The worker survives discovery (lazy androguard import) and does not freeze its
   loop on a long decompile (threaded blocking ops).

## 11. Follow-on specs (not this effort)

- **Spec 2 — card / `/load` system (PARE):** lazy schema injection, card stubs
  always in context, `/load` + `/workers`, session-scoped loaded set. Retrofits
  frida + static as the first two cards (no worker change — capture and mounting
  are generic at dispatch). The next priority after this worker
  ([[project_context_scaling]]).
- **v2 — multi-APK-open** (`apk_id` handles, `unload_apk`, build comparison),
  **`decompile_class`**, **arsc/asset string extraction**.
- **v2 acquire — `pull_apk`** (adb, low/med tier).
- **v2 repackage/deploy — `rebuild`/`sign`/`zipalign`/`install`** (high tier,
  apktool/apksigner; likely its own worker).
