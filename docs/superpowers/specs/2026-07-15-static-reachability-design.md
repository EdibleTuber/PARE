# pare-static-mcp Reachability / Code-Graph Surface — Design

**Date:** 2026-07-15
**Repo:** `pare-static-mcp` (worker); spec tracked in `PARE/docs/superpowers/specs/`
**Status:** Design approved (brainstorming). Next: implementation plan (writing-plans).
**Builds on:** `2026-07-10-pare-static-mcp-worker-design.md` (the shipped v1 7-tool worker).

## 1. Motivation

PARE's static worker today exposes a **1-hop** symbol graph: `find_symbol kind=caller`
gives direct callers, `list_methods` gives an `xref_count`. The model still
*enumerates and guesses* hook targets rather than deriving them. androguard has
already parsed the full call graph — every `MethodAnalysis` carries
`get_xref_from()` (callers) and `get_xref_to()` (callees). We are only walking one
edge of a graph that is fully materialized.

This design exposes that graph as **multi-hop reachability queries**, turning
"derive hook targets from code" from aspiration into a concrete tool: *given the
declared attack surface, which methods reach a dangerous sink, and by what path?*

**Framing discipline (unchanged):** the static graph only **proposes**. A witness
path narrows the search to a handful of methods; the operator triggers and Frida
**confirms**. Static-proposes / dynamic-confirms, one layer up from v1. (Mirrors
the Cloudflare vuln-harness lesson: reachability is proven by *running*, not by the
static tool asserting it.)

## 2. Scope

**In (v1):** four read-only, `low`-tier tools over androguard's existing xref
graph — three primitives and one task-oriented convenience tool.

**Out (v1):**
- **Data/value taint** (FlowDroid / CodeQL territory). We do call-graph
  reachability only: a witness path proves *control can reach* a sink, not that
  tainted data provably flows to it. Ties to commit 641be6d ("hook the data-flow
  point, not the named method's argument") — that precision is a later tier.
- **`sources=` root-selector** — deferred to **v2** (see §7).
- **Materialized graph structure** (networkx et al.) — androguard's xref *is* the
  graph; a second copy adds a dependency and an upfront build cost against the 2s
  discovery ceiling. Revisit only if query latency becomes real.

## 3. Tool surface

All four are **read-only, `low` tier**, surfaced as `static_*` (agent_core
auto-prefixes by worker name). All reuse the existing worker machinery:
`loader.ensure_xref` (lazy, thread-safe via `threading.Lock`), `asyncio.to_thread`
for the blocking traversal, and the mandatory `_ok`/`_err` JSON-string envelope
(the capture layer collapses to junk rows without it).

### 3.1 Primitives (Shape A)

- **`callers_of(method, cls, signature="", depth=3)`**
  Reverse BFS over `get_xref_from`. Returns
  `rows: [{class, method, signature, depth}]` — methods that transitively reach
  the target, deduped to their minimum depth. The multi-hop generalization of
  today's `find_symbol kind=caller`.

- **`callees_of(method, cls, signature="", depth=3)`**
  Forward BFS over `get_xref_to`. What the target transitively calls.

- **`paths_between(from_method, from_cls, to_method, to_cls, from_signature="", to_signature="", max_depth=12)`**
  Shortest **witness path**, forward BFS. Returns
  `path: [{class, method, signature}, …]` (ordered source→target), or empty if
  unreachable within `max_depth`.

### 3.2 Convenience (Shape B)

- **`reachable_sinks(from="", to=[], max_depth=12)`**
  - **Roots:**
    - `from="Class.method"` given → that method is the sole root.
    - `from` omitted → **auto-discover** from the manifest (§5).
  - **Sinks:** `to=[…]` — dotted `Class.method` strings **exactly as PAL's sink
    catalog emits them** (params optional). Empty `to=` → a tiny built-in fallback
    catalog (cold-start only; not the source of truth — see §4).
  - **Output:** one row per `(entry, sink)` hit —
    `{entry:{class,method}, sink:{class,method}, path:[…shortest witness…]}` —
    deduped by `(entry, sink)`. One witness path per hit (BFS-shortest), never full
    path enumeration (exponential; would blow the envelope).

## 4. Sink knowledge lives in PAL, not the worker

The sink catalog is **knowledge, not mechanism**. A categorized list of dangerous
Android APIs is reference-doc-shaped content — the natural fit for PAL's vault —
and freezing it into worker source would rot, overfit to already-solved
challenges, and require a code change to improve.

**Architecture:** the worker **does not call PAL.** Coupling two MCP servers breaks
the N-independent-agents model. The **model is the integrator**: it retrieves sink
signatures from PAL, then passes them into the worker as `to=[…]`. PAL owns *what
is dangerous*; the worker owns *what reaches it*; the model stitches them.

**The catalog already exists in PAL** (authored 2026-07-16):
`raw/notes/android-vulnerable-sinks-reference` — "a structured knowledge base of
sensitive Android API sinks … to support the construction of security-focused
dependency graphs within PARE, mapping untrusted sources to high-risk sinks." Each
entry also carries its Frida hooking pattern
(`frida_java_hook(cls=…, method=…, overload=…)`), so a witness path terminating at
a sink maps **directly** to a hook proposal — the loop closes with no glue code.

**Worker fallback catalog:** a minimal built-in default (a handful of obvious
sinks: `Cipher.doFinal`, `CipherOutputStream.write`, `Runtime.exec`, `Log.*`) so
`reachable_sinks` degrades gracefully when a retrieval returns empty. It is a
cold-start fallback, **not** the source of truth.

### 4.1 Sink-matching contract (the crux)

PAL emits **dotted-Java** signatures (`javax.crypto.CipherOutputStream.write(byte[] b)`);
androguard's call edges are **smali** (`Ljavax/crypto/CipherOutputStream;->write`).
The worker **normalizes both** to a canonical smali key and matches against
call-edge targets:

- Accept `to=` entries in either dotted-Java or smali form (lenient, shape-agnostic
  — per the no-overfitting principle: match what PAL actually emits, don't invent a
  brittle format).
- Match on `class + method`. If a param descriptor is supplied, narrow by it;
  otherwise match all overloads.
- Sinks are framework methods → they appear as `is_external()` targets of call
  edges. Reachability **stops at the edge into the sink** (their bodies are not in
  the APK).

## 5. Entry-point auto-discovery (`reachable_sinks` with no `from`)

Reuses the existing `manifest.parse`. A component is a **root** if it is exported —
`android:exported="true"`, or it declares an `<intent-filter>` and is not explicitly
`exported="false"`. Each exported component class is mapped to its lifecycle /
handler methods, which become BFS roots:

| Component | Root methods |
| :--- | :--- |
| activity | `onCreate`, `onStart`, `onResume`, `onNewIntent` |
| service | `onCreate`, `onStartCommand`, `onBind`, `onHandleIntent` |
| receiver | `onReceive` |
| provider | `onCreate`, `query`, `insert`, `update`, `delete`, `call` |

Only methods actually present on the component class in the analysis become roots.
This is the *declared attack surface* — the real security question is "what in it
reaches a sink." The PAL sinks doc also lists intent entry points (its Section 2),
but the **manifest is per-APK ground truth** for roots, so we use it rather than a
generic list.

## 6. Engine (`apk/graph.py`)

A single bounded traversal underlies all four tools (Approach 1 — shared core +
thin wrappers; no per-tool duplication, one place to test the cycle/depth logic):

```
traverse(analysis, roots, direction, max_depth, stop_predicate=None)
  -> visited rows + parent-pointer map (for witness-path reconstruction)
```

- **Direction:** `forward` = `get_xref_to`, `backward` = `get_xref_from`. androguard
  yields 3-tuples; we take the method element.
- **Cycle guard:** visited set keyed by `(class, name, descriptor)`.
- **Witness paths:** parent-pointer map, reconstructed on first (shortest) hit.
- **Thread-safety:** runs under `asyncio.to_thread` after `ensure_xref`
  (lock-guarded lazy build), consistent with existing tools.

`reachable_sinks` = forward `traverse` from the roots with
`stop_predicate = edge-target matches a normalized sink key`, emitting the shortest
witness per `(entry, sink)`.

## 7. Bounds & defaults (`config.py`, env-overridable)

| Setting | Default | Purpose |
| :--- | :--- | :--- |
| `default_depth` | 3 | `callers_of` / `callees_of` default hop budget |
| `max_depth_ceiling` | 12 | hard clamp — a caller asking for more is clamped |
| `max_nodes` | ~5000 | per-traversal visit guard; sets `truncated=true` |
| `max_rows` | ~200 | returned-row cap; sets `truncated=true` |

Truncation is **reported, never silent** (`truncated: true` in the envelope) — a
capped result must not read as "covered everything."

## 8. Output envelope

Existing `_ok(summary, **extra)` JSON-string with `rows` / `path` plus `truncated`
flags; `_err(summary, exc)` on failure. Preserves the capture-layer contract and
the honest-error auditing landed in agent_core v1.7.3.

## 9. Known blind spots (documented in tool descriptions, not hidden)

- **No taint:** a witness path means control *can* reach the sink, not that tainted
  data does. Frida confirms.
- **Reflection / dynamic dispatch / runtime-registered receivers** break static
  edges — the graph **under-approximates** (may miss real paths). This is *why*
  dynamic confirmation exists; a documented limitation, not a bug. Reflection was
  already flagged as a static-graph breaker in prior debriefs.
- **Framework sinks:** we stop at the call edge; their bodies aren't in the APK.

## 10. Integration risks (PARE-side, noted for the co-pilot flow)

- **Weak fuzzy retrieval:** the sinks doc scored **0.22** on a semantic query — the
  co-pilot cannot reliably *find* it by fuzzy search alone. It has a stable id
  (`raw/notes/android-vulnerable-sinks-reference`) and `sinks` / `PARE-ref` tags,
  and `RetrievalClient.search` accepts a `tags=` filter — **tag-filtered retrieval
  (or a pinned doc id) is the reliable path**, not fuzzy score. The worker's
  fallback catalog covers a cold start. This is a PARE integration concern, not the
  worker's, but it must be handled for the loop to work end-to-end.
- **Format drift:** if PAL changes its signature notation, the worker's lenient
  normalizer (§4.1) is the shock absorber — it accepts dotted or smali, so drift
  within those forms is tolerated.

## 11. Testing

Real fixture: OMTG `MSTG-Android-Java.apk` (via `PARE_STATIC_TEST_APK`).

**Keystone test:** `reachable_sinks` with **no `from`** auto-discovers the exported
activity and yields a witness path to `Cipher.doFinal` / `CipherOutputStream.write`
— proving the KeyStore hook target is derivable from the graph **alone**. This
extends the shipped §9 1-hop KeyStore-chain test to multi-hop.

**Engine units:**
- cycle graph terminates (visited guard)
- depth clamp (request > ceiling is clamped)
- unreachable → empty path / no rows
- node-cap and row-cap truncation set `truncated=true`
- dotted ↔ smali sink-key normalization
- manifest root discovery (exported vs non-exported components)

## 12. Files

- **new** `apk/graph.py` — bounded traversal engine + manifest root discovery + sink
  normalization/matching.
- `tools.py` — four new async tool fns (thin wrappers, existing envelope pattern).
- `config.py` — the §7 bounds.
- `server.py` / `contract.py` — register the four tools; tool descriptions are a
  first-class deliverable (state the blind spots of §9).
- PARE `workers.yaml` — the `static:` entry already mounts the worker; the new tools
  inherit `low` tier, no per-tool change needed (verify).

## 13. v2 preview (not this spec)

- **`sources=[…]`** — a symmetric, optional root-selector mirroring `to=`: roots
  become every method that *calls* a PAL-catalog source API (a 1-hop reverse
  lookup, then the same BFS). Realizes the sinks doc's stated source→sink purpose.
  PAL owns both catalogs; the worker stays pure mechanism. Deferred so v1 ships
  sinks-first. (Sources research underway in PAL.)
- Heavier **taint tier** (values, not just calls) — FlowDroid-style, if ever.
