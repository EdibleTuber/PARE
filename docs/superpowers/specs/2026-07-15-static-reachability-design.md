# pare-static-mcp Reachability / Code-Graph Surface — Design

**Date:** 2026-07-15 (revised 2026-07-16 after adversarial panel)
**Repo:** `pare-static-mcp` (worker); one agent_core change; spec tracked in `PARE/docs/superpowers/specs/`
**Status:** Design revised after 5-lens adversarial panel. Next: implementation plan (writing-plans).
**Builds on:** `2026-07-10-pare-static-mcp-worker-design.md` (the shipped v1 7-tool worker).

## 0. Revision note — what the panel changed

A 5-lens adversarial panel (androguard-correctness, PAL/agent_core integration, scope/YAGNI,
RE-efficacy, testability) reviewed the first draft. Two reviewers traced the **real**
`MSTG-Android-Java.apk` xref graph. The core finding: the original default
(`reachable_sinks` from exported manifest components, **forward** BFS to sinks)
**empirically returns nothing** on the KeyStore fixture — the sink is reachable only
through `OMTG_DATAST_001_KeyStore$1.onClick`, which has zero static callers (the View
framework dispatches it). That is the §9 dynamic-dispatch blind spot sitting directly
under the headline capability; an empty result reads as false "nothing reachable" comfort.

**Resolution — the design now roots from the sink catalog and walks *backward*.** This
matches the tool's real job (find hook targets), sidesteps the entry→handler dispatch
gap, and produces a smaller frontier. Consequent changes, all reflected below:
- `reachable_sinks` is **backward-from-sinks**; `callers_of` is its backbone primitive.
- `callees_of` is **cut** (no co-pilot use case).
- Manifest/exported-component reachability is **deferred to v2** (a triage/severity
  signal, not hook-discovery; unreliable due to the same dispatch gap; `read_manifest`
  already lists components for manual driving).
- Retrieval is a real cross-repo deliverable: `SearchVault` gains `tags`/`doc_id`
  (agent_core change).
- The fallback catalog is **loud, opt-in, and de-overfit**; the keystone test passes PAL
  sinks explicitly (no self-proving).
- Honest-error diagnostics envelope; engine factored for synthetic-graph unit tests;
  normalizer isolated + table-tested.

## 1. Motivation

PARE's static worker today exposes a **1-hop** symbol graph: `find_symbol kind=caller`
gives direct callers, `list_methods` gives an `xref_count`. The model still *enumerates
and guesses* hook targets. androguard has already parsed the full call graph — every
`MethodAnalysis` carries `get_xref_from()` (callers) and `get_xref_to()` (callees). We
walk one edge of a graph that is fully materialized.

This design exposes that graph as **multi-hop reachability queries**, turning "derive
hook targets from code" into a concrete tool: *given a catalogued dangerous sink, which
app methods reach it, and by what path?* Those app methods are the hook candidates.

**Framing discipline:** the static graph only **proposes**. A witness path narrows the
search to a handful of methods; the operator triggers and Frida **confirms**.
Static-proposes / dynamic-confirms — and, per the panel, the *output* must enforce this
(carry the under-approximation caveat per-result), not rely on the model to remember it.

## 2. Scope

**In (v1):** three read-only, `low`-tier tools over androguard's existing xref graph —
two primitives and one backward-reachability convenience tool — plus one agent_core
change to make PAL sink retrieval reliable.

**Out (v1):**
- **Data/value taint** (FlowDroid / CodeQL). Call-graph reachability only: a witness path
  proves *control can reach* a sink, not that tainted data flows to it. A witness path is
  a proposal about *which method*; it does not say *which argument* carries the data
  (that comes from PAL's per-sink Frida-overload metadata, §4). Ties to commit 641be6d.
- **`callees_of`** — cut; no co-pilot use case.
- **Manifest/exported-component reachability** — v2 (see §12), paired with `sources=`.
- **`sources=` root-selector** — v2.
- **Materialized graph structure** (networkx) — androguard's xref *is* the graph.

## 3. Tool surface

Three tools, **read-only, `low` tier**, surfaced as `static_*`. All reuse the existing
worker machinery: `loader.ensure_xref` (lazy, thread-locked), `asyncio.to_thread`, and
the mandatory `_ok`/`_err` JSON-string envelope.

**Envelope shape constraint (panel):** the capture layer's `infer_rows` unwraps an
envelope to its rows only when there is **exactly one list-valued top-level key**. Every
tool here therefore emits **exactly one top-level list** (`rows` or `path`); all
diagnostics go in a `diagnostics` **dict** (nested lists inside it don't count) or as
scalars. A second top-level list would collapse the whole envelope into one junk row.

### 3.1 Primitives

- **`callers_of(method, cls, signature="", depth=3)`** — the backbone. Backward BFS over
  `get_xref_from`. Returns `rows: [{class, method, signature, depth, frontier}]` — methods
  that transitively reach the target, deduped to their **minimum** depth. `frontier: true`
  marks a method whose own `get_xref_from` is empty (no static caller — typically a
  framework-dispatched callback like `onClick`; i.e. the honest edge of static knowledge,
  where Frida takes over).

- **`paths_between(from_method, from_cls, to_method, to_cls, from_signature="", to_signature="", max_depth=12)`**
  — shortest **witness path**, forward BFS from source. Returns
  `path: [{class, method, signature}, …]` ordered **source→target** (the reconstruction
  reverses the parent-pointer chain), or empty `path: []` if unreachable within
  `max_depth`. Contract note: the witness is *shortest*, which biases toward high-fan-in
  glue; it may not be the exploit-relevant route.

### 3.2 Backward reachability (the headline)

- **`reachable_sinks(to=[], from="", depth=12, allow_fallback=false)`**
  - **Sinks (`to=`):** dotted-Java or smali signatures **as PAL's catalog emits them**
    (§4.1). For each, locate its node(s) by normalized `class+method` key — **regardless
    of `is_external()`** (catches bundled-lib sinks: SQLCipher, BouncyCastle, OkHttp).
    Unmatched sinks are reported in `diagnostics.unmatched_sinks`; unparseable entries in
    `diagnostics.rejected_sinks` (never silently dropped).
  - **Traversal:** **backward** BFS from each sink node over `get_xref_from`, depth-capped.
    App-owned methods on the way are hook candidates.
  - **`from` (optional):** when given, filter to candidates whose witness path includes
    `from` — the "does this specific method reach a catalogued sink" query.
  - **Output:** `rows: [{candidate:{class,method,signature}, sink:{class,method}, path:[…candidate→sink…], frontier}]`,
    deduped by `(candidate, sink)`. `frontier` marks candidates whose callers dead-end
    (likely UI/operator-triggered — the natural hook point).
  - **`diagnostics` dict:** `sink_source: "provided"|"fallback"`, `sink_count`,
    `candidate_count`, `unmatched_sinks: []`, `rejected_sinks: []`, `truncated: bool`,
    `under_approximation: "control-flow only; reflection/callback/dispatch edges are invisible — confirm dynamically"`.

**Empty `to=` is not a silent success (panel — honesty regression risk).** With `to=[]`:
`allow_fallback=false` (default) → **`_err`** ("no sinks supplied; retrieve from PAL or set
allow_fallback"). `allow_fallback=true` → the tiny cold-start catalog is used **and**
`diagnostics.sink_source="fallback"` is set so a 4-sink check never reads as a full sweep.

## 4. Sink knowledge lives in PAL, not the worker

The sink catalog is **knowledge, not mechanism** — reference-doc content that belongs in
PAL's vault. Freezing it into worker source would rot and overfit to solved challenges.

**Architecture:** the worker **does not call PAL.** The **model is the integrator**: it
retrieves sink signatures from PAL, then passes them as `to=[…]`. PAL owns *what is
dangerous*; the worker owns *what reaches it*; the model stitches them.

**The catalog exists in PAL:** `raw/notes/android-vulnerable-sinks-reference` — "a
structured knowledge base of sensitive Android API sinks … to support the construction of
security-focused dependency graphs within PARE." Each entry carries its Frida hooking
pattern (`frida_java_hook(cls=…, method=…, overload=…)`), so a candidate reaching a
catalogued sink maps directly to a hook proposal **with the right overload/arg** — the
data-flow detail the graph itself can't supply.

### 4.1 Reliable retrieval — the `SearchVault` change (agent_core deliverable)

The first draft assumed tag-filtered retrieval; the panel found the model-facing
`SearchVault` tool exposes only `query`+`max_results` — **no `tags`/`doc_id`** — so the
"reliable path" didn't exist. The fuzzy score on this doc is 0.22, unusable alone.

**Deliverable (separate PR, crosses repos):** extend `agent_core`'s `SearchVault` with
`tags` and `doc_id` params (the underlying `RetrievalClient.search` already accepts
`tags`; `get_document`/`read_vault_doc` already fetch by id). Requires an agent_core
version bump + a PARE pin bump. This is **not** worker-local and is listed in §11 Files.
The reliable model flow becomes: fetch the sink doc by its stable id (or `tags=["sinks"]`),
extract signatures, pass as `to=`. The worker's reject-and-report (§3.2) makes any
bad extraction visible rather than a silent empty result.

### 4.2 Sink-matching normalizer (isolated, table-tested)

PAL emits dotted-Java (`javax.crypto.CipherOutputStream.write(byte[] b)`); androguard
edges are smali (`Ljavax/crypto/CipherOutputStream;->write`, descriptor `([B)V`). The
normalizer is a **standalone pure function** (its own module, no androguard dependency),
unit-tested with a `(input, expected-canonical-key)` table that MUST cover: array `[B`,
`<init>`, nested `$` classes, primitive-only signatures, no-arg/`void`, and both
dotted-input and smali-input for each. Matching: canonical `class+method`; if a param
descriptor is supplied, narrow by it, else match all overloads. Accept either input form
(lenient/shape-agnostic — the anti-overfit piece).

Any place that resolves a name to `MethodAnalysis` via `Analysis.find_methods` must
`re.escape` and `^…$`-anchor the classname pattern — `find_methods` does an unanchored
`re.match`, and inner-class `$` is a regex metacharacter (silently matches nothing).

## 5. Engine (`apk/graph.py`) — factored for synthetic-graph testing

One bounded traversal underlies all tools, factored over a **neighbor callable** so the
BFS logic is testable on hand-built graphs with **no androguard dependency** (panel):

```
traverse(neighbors_fn, roots, max_depth, stop_predicate=None, node_cap, row_cap)
  -> visited rows (min-depth) + parent-pointer map (for witness reconstruction)
```

- `neighbors_fn(node) -> iterable[node]`. androguard adapters wrap `get_xref_from`
  (backward) / `get_xref_to` (forward); each yields 3-tuples `(ClassAnalysis,
  MethodAnalysis, offset)` — take index 1, the `MethodAnalysis`.
- **Dedup edges + check-visited-before-enqueue:** a method's xref repeats the same target
  once per call offset (measured up to 59×). Membership-check before enqueue and dedupe
  each node's neighbor list, or `node_cap` trips early on trivial work.
- **Do not expand `is_external()` nodes** as frontier (no bodies in the APK) — but *do*
  match sinks on the edge target regardless of externality (§3.2).
- **Cycle guard:** visited set keyed by `(class, name, descriptor)`.
- **Witness paths:** parent-pointer map; reconstruct on first (shortest) hit and
  **reverse** to source→target.
- Runs under `asyncio.to_thread` after `ensure_xref`.

## 6. Bounds & first-call latency

Guards are **module-level constants** for v1 (not env-config — panel YAGNI; promote to
config the day a caller needs a different value). The guards themselves must exist and be
tested:

| Constant | Value | Purpose |
| :--- | :--- | :--- |
| `DEFAULT_DEPTH` | 3 | `callers_of` default hop budget |
| `MAX_DEPTH` | 12 | hard clamp (request > ceiling is clamped) |
| `MAX_NODES` | 5000 | per-traversal visit guard → `diagnostics.truncated=true` |
| `MAX_ROWS` | 200 | returned-row cap → `diagnostics.truncated=true` |

Truncation is **reported, never silent**. First-call latency: `create_xref` measured
~0.63 s on this fixture (2,737 classes) and can be seconds on 100k-method apps; it is
one-time behind `ensure_xref`+`to_thread` (clears the 2 s discovery ceiling), but the
*first* graph query pays it. `load_apk` may warm `ensure_xref`, or the first call's
summary should read "building call graph" so it doesn't look like a hang.

## 7. Honest-error contract (panel — v1.7.3 auditing regression risk)

Empty `rows`/`path` must never masquerade as a real failure. Each condition below gets an
explicit signal, and each has a test:

| Condition | Result |
| :--- | :--- |
| `from=`/target method not in APK | `_err` `root_not_found` (not empty-ok) |
| sink in `to=` matches zero edges | success, listed in `diagnostics.unmatched_sinks` |
| unparseable `to=` entry | listed in `diagnostics.rejected_sinks` (not silently dropped) |
| `to=[]`, `allow_fallback=false` | `_err` "no sinks supplied" |
| fallback catalog used | `diagnostics.sink_source="fallback"` |
| genuine no path | success, empty `rows`, `under_approximation` note present |

Errors use the in-band `{"error": true}` envelope that agent_core v1.7.3 now audits honestly.

## 8. Known blind spots (in tool descriptions AND per-result envelope)

- **No taint:** a path means control *can* reach the sink, not that tainted data does; and
  not *which* argument carries it (PAL's overload metadata supplies that).
- **Reflection / dynamic dispatch / runtime-registered callbacks** break static edges — the
  graph **under-approximates** (misses real paths). This is why backward-from-sinks +
  `frontier` flags exist, and why dynamic confirmation exists. The `under_approximation`
  field carries this into every result, so "empty" never reads as "safe."
- **Shortest witness** may skip the exploit-relevant route (§3.1).

## 9. Testing

Real fixture: OMTG `MSTG-Android-Java.apk` (via `PARE_STATIC_TEST_APK`).

**Engine units on synthetic graphs (no APK, run in CI):** cycle termination; depth-clamp
boundary (path of length exactly `MAX_DEPTH` found, `MAX_DEPTH+1` empty); `MAX_NODES`/
`MAX_ROWS` truncation sets `truncated=true`; diamond graph → min-depth dedup; witness-path
order is source→target; single-top-level-list envelope shape.

**Normalizer units (no APK):** the `(input, expected-key)` table of §4.2 including `[B`,
`<init>`, `$`, primitives, `void`, dotted+smali inputs.

**Keystone (real APK), reframed to prove the real loop — not self-proving:**
`reachable_sinks(to=[<crypto sinks passed explicitly, as from PAL>])` on the fixture
returns `encryptString`/`decryptString` as candidates with a witness path down to
`CipherOutputStream.write` / `Cipher.doFinal`, and marks them `frontier=true` (their only
caller is the framework-dispatched `onClick`). This proves: catalogued sink → backward walk
→ correct hook candidate → honest frontier marker. Sinks are passed explicitly (the loud
path), so the test cannot pass by a frozen fallback constant.

**Honest-error units:** one per row of §7 (esp. non-existent `from=` → `_err`; zero-match
sink → `unmatched_sinks`; `to=[]` no-fallback → `_err`).

## 10. Files

**Worker (`pare-static-mcp`):**
- **new** `apk/graph.py` — `traverse` engine (neighbor-callable), androguard adapters,
  backward/forward drivers.
- **new** `apk/sink_match.py` — the isolated normalizer (§4.2).
- `tools.py` — three async tool fns (single-top-level-list envelope, `diagnostics` dict).
- `apk/constants.py` (or existing config) — §6 constants.
- `server.py` / `contract.py` — register three tools with explicit `low` `ToolSpec` meta
  (build-time conformance rejects a missing tier); tool descriptions carry §8 caveats.
- PARE `workers.yaml` — `static:` floor already `low`; verify, no per-tool change.

**agent_core (separate PR, §4.1):** `SearchVault` gains `tags`/`doc_id`; version bump;
then PARE pin bump. This is the one non-worker-local deliverable.

## 11. v2 preview (not this spec)

- **Manifest/exported-component reachability** — forward "is a catalogued sink reachable
  from the declared external attack surface" as a **triage/severity** enrichment on top of
  the backward candidates. Requires modeling framework-dispatched entry edges (register
  `View.OnClickListener.onClick` implementers, `Intent` targets, `onReceive`) to be
  reliable — real ICC/callback modeling, out of v1.
- **`sources=[…]`** — symmetric root-selector: roots = methods that call a PAL-catalog
  source API. PAL owns both catalogs; worker stays pure mechanism.
- Heavier **taint tier** (values, not calls) — FlowDroid-style.
