# Static Reachability / Code-Graph Surface — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add multi-hop reachability queries over androguard's xref graph to `pare-static-mcp` — two primitives (`callers_of`, `paths_between`) and one backward-from-sinks convenience tool (`reachable_sinks`) — plus an agent_core `SearchVault` change so PAL sink retrieval is reliable.

**Architecture:** A single bounded-BFS engine (`apk/graph.py`) factored over a **neighbor callable** so BFS logic is unit-testable on synthetic graphs with no androguard dependency. Thin async tool wrappers reuse the existing `_ok`/`_err` JSON-envelope. Sink signatures are normalized (dotted-Java ↔ smali) by an isolated pure module (`apk/sink_match.py`). `reachable_sinks` roots from the sink catalog and walks **backward** (callers), so it sidesteps the framework-dispatch gap that makes forward-from-entry BFS return nothing.

**Tech Stack:** Python 3, androguard 4.1.3, FastMCP, pytest + pytest-asyncio. agent_core `Tool` base class.

## Global Constraints

- **androguard is lazy-imported** — never at module top (2s worker-discovery ceiling). Only `apk/loader.py` imports it, inside functions. `graph.py`/`sink_match.py` must NOT import androguard at module top.
- **xref must be built** via `loader.ensure_xref(state)` (lazy, `threading.Lock`-guarded) before any traversal; run traversal under `asyncio.to_thread`.
- **Envelope:** every tool returns a JSON string via `_ok(summary, **extra)` / `_err(summary, exc)`. **Exactly one top-level list** per tool (`rows` or `path`); all diagnostics go in a `diagnostics` **dict** (nested lists inside it are fine). A second top-level list collapses the capture-layer envelope to one junk row.
- **Tiers:** all new tools are `low`; each needs an explicit `ToolSpec(..., "low", ...)` in `contract.py` (build-time conformance rejects a missing tier).
- **androguard quirks:** `get_xref_from()`/`get_xref_to()` yield 3-tuples `(ClassAnalysis, MethodAnalysis, offset)` — take index 1. `ma.class_name` is smali (`Ljavax/crypto/Cipher;`). `ma.name` is the method name. `ma.descriptor` is a live string. `ma.is_external()` distinguishes framework methods. `Analysis.find_methods(classname=, methodname=)` takes **regex** and does an unanchored `re.match` — always `re.escape` + `^…$`-anchor.
- **Constants** (module-level in `apk/graph.py`, not env-config): `DEFAULT_DEPTH=3`, `MAX_DEPTH=12`, `MAX_NODES=5000`, `MAX_ROWS=200`.
- **Bite-size TDD:** write failing test → run it red → minimal impl → run green → commit. Work in the `pare-static-mcp` repo on branch `feat/reachability-v1` unless a task says agent_core.
- **Repos:** worker = `/home/edible/Projects/pare-static-mcp`; agent_core = `/home/edible/Projects/agent_core`. Run worker tests with `cd /home/edible/Projects/pare-static-mcp && python -m pytest`.

---

## File Structure

**pare-static-mcp (branch `feat/reachability-v1`):**
- Create `src/pare_static_mcp/apk/sink_match.py` — pure dotted↔smali sink parser/matcher (no androguard).
- Create `src/pare_static_mcp/apk/graph.py` — `traverse` engine + androguard neighbor adapters + constants.
- Modify `src/pare_static_mcp/tools.py` — add `callers_of`, `paths_between`, `reachable_sinks`.
- Modify `src/pare_static_mcp/contract.py` — three `ToolSpec` entries.
- Create tests: `tests/unit/test_sink_match.py`, `test_graph_engine.py`, `test_callers_of.py`, `test_paths_between.py`, `test_reachable_sinks.py`, `test_reachable_sinks_keystone.py`.

**agent_core (branch `feat/searchvault-tags-docid`):**
- Modify `agent_core/tools/_framework.py` — `SearchVault` gains `tags`/`doc_id`.
- Modify/add test under `agent_core/tests/` for the new params.

---

## Task 1: Sink normalizer (`sink_match.py`) — pure, table-tested

**Files:**
- Create: `src/pare_static_mcp/apk/sink_match.py`
- Test: `tests/unit/test_sink_match.py`

**Interfaces:**
- Produces: `parse_sink(sig: str) -> tuple[str, str] | None` returning `(class_smali, method)` or `None` if no method extractable. `edge_matches(parsed: tuple[str,str], cls_name: str, method_name: str) -> bool`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_sink_match.py
from __future__ import annotations
import pytest
from pare_static_mcp.apk import sink_match as sm


@pytest.mark.parametrize("sig,expected", [
    # dotted-Java, with params (params ignored)
    ("javax.crypto.CipherOutputStream.write(byte[] b)", ("Ljavax/crypto/CipherOutputStream;", "write")),
    # dotted, no params
    ("javax.crypto.Cipher.doFinal", ("Ljavax/crypto/Cipher;", "doFinal")),
    # smali arrow form with descriptor
    ("Ljavax/crypto/Cipher;->doFinal([B)[B", ("Ljavax/crypto/Cipher;", "doFinal")),
    # constructor
    ("java.lang.String.<init>(byte[])", ("Ljava/lang/String;", "<init>")),
    # nested/inner class ($ preserved)
    ("com.foo.Bar$1.onClick(android.view.View)", ("Lcom/foo/Bar$1;", "onClick")),
    # already-smali class, dotted method sep should not apply
    ("Landroid/util/Log;->e", ("Landroid/util/Log;", "e")),
])
def test_parse_sink_forms(sig, expected):
    assert sm.parse_sink(sig) == expected


@pytest.mark.parametrize("bad", ["", "   ", "NoMethodHere", "()"])
def test_parse_sink_rejects_unparseable(bad):
    assert sm.parse_sink(bad) is None


def test_edge_matches_class_and_method_only():
    parsed = sm.parse_sink("javax.crypto.Cipher.doFinal(byte[])")
    # androguard class_name is smali; different overload descriptor still matches (params ignored)
    assert sm.edge_matches(parsed, "Ljavax/crypto/Cipher;", "doFinal") is True
    assert sm.edge_matches(parsed, "Ljavax/crypto/Cipher;", "update") is False
    assert sm.edge_matches(parsed, "Ljavax/crypto/Mac;", "doFinal") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/edible/Projects/pare-static-mcp && python -m pytest tests/unit/test_sink_match.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'pare_static_mcp.apk.sink_match'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/pare_static_mcp/apk/sink_match.py
"""Pure sink-signature parsing/matching. NO androguard import (kept module-top clean).

Accepts sink signatures as PAL's catalog emits them (dotted-Java) or in smali form,
and matches them against androguard call-edge targets by class+method. Parameters are
intentionally ignored in v1: PAL's per-sink `overload=[...]` carries the arg detail
for the Frida hook; the worker only needs to identify the sink method.
"""
from __future__ import annotations


def _to_smali_class(cls: str) -> str:
    cls = cls.strip()
    if cls.startswith("L") and cls.endswith(";"):
        return cls
    return "L" + cls.replace(".", "/") + ";"


def parse_sink(sig: str) -> tuple[str, str] | None:
    """Return (class_smali, method) or None if no method can be extracted."""
    s = (sig or "").strip()
    if not s:
        return None
    # strip a parameter list if present:  method(...)  ->  method
    if "(" in s:
        s = s.partition("(")[0].strip()
    if not s:
        return None
    # smali arrow form:  Lfoo/Bar;->method
    if "->" in s:
        cls, _, method = s.partition("->")
        method = method.strip()
        if not method:
            return None
        return (_to_smali_class(cls), method)
    # dotted form:  a.b.C.method   (method = last dotted segment)
    if "." not in s:
        return None
    cls, _, method = s.rpartition(".")
    if not method or not cls:
        return None
    return (_to_smali_class(cls), method)


def edge_matches(parsed: tuple[str, str], cls_name: str, method_name: str) -> bool:
    want_cls, want_method = parsed
    return method_name == want_method and _to_smali_class(str(cls_name)) == want_cls
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/edible/Projects/pare-static-mcp && python -m pytest tests/unit/test_sink_match.py -v`
Expected: PASS (all parametrized cases).

- [ ] **Step 5: Commit**

```bash
cd /home/edible/Projects/pare-static-mcp
git add src/pare_static_mcp/apk/sink_match.py tests/unit/test_sink_match.py
git commit -m "feat(static): isolated dotted<->smali sink normalizer (class+method match)"
```

---

## Task 2: BFS engine (`graph.py`) — synthetic-graph unit tests

**Files:**
- Create: `src/pare_static_mcp/apk/graph.py`
- Test: `tests/unit/test_graph_engine.py`

**Interfaces:**
- Produces: constants `DEFAULT_DEPTH=3`, `MAX_DEPTH=12`, `MAX_NODES=5000`, `MAX_ROWS=200`. `traverse(neighbors_fn, roots, max_depth, node_cap=MAX_NODES) -> tuple[dict, dict, bool]` returning `(depth, parent, truncated)`: `depth[node]` = min hops from nearest root (roots=0), `parent[node]` = node it was first discovered from (`None` for roots), `truncated` = `True` if `node_cap` hit. Cycle-guarded; membership-checked before enqueue; neighbor list deduped. `path_from_root(node, parent) -> list` = `[node, …, root]` following parent pointers.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_graph_engine.py
from __future__ import annotations
from pare_static_mcp.apk import graph


def _nf(adj):
    return lambda n: adj.get(n, [])


def test_bfs_min_depth_on_diamond():
    # A->B->D and A->C->D->... plus a short A->D via B; D reachable at depth 2 both ways
    adj = {"A": ["B", "C"], "B": ["D"], "C": ["D"], "D": []}
    depth, parent, trunc = graph.traverse(_nf(adj), ["A"], max_depth=12)
    assert depth["A"] == 0 and depth["B"] == 1 and depth["D"] == 2
    assert trunc is False


def test_cycle_terminates():
    adj = {"A": ["B"], "B": ["C"], "C": ["A"]}  # 3-node cycle
    depth, parent, trunc = graph.traverse(_nf(adj), ["A"], max_depth=12)
    assert set(depth) == {"A", "B", "C"}  # each visited once


def test_depth_clamp_boundary():
    # chain A->B->C->D->E ; max_depth=2 reaches C (depth2), not D
    adj = {"A": ["B"], "B": ["C"], "C": ["D"], "D": ["E"], "E": []}
    depth, parent, trunc = graph.traverse(_nf(adj), ["A"], max_depth=2)
    assert "C" in depth and depth["C"] == 2
    assert "D" not in depth


def test_node_cap_truncates():
    adj = {"A": [f"n{i}" for i in range(10)]}
    depth, parent, trunc = graph.traverse(_nf(adj), ["A"], max_depth=12, node_cap=5)
    assert trunc is True
    assert len(depth) <= 5


def test_duplicate_neighbors_visited_once():
    adj = {"A": ["B", "B", "B"], "B": []}  # duplicate edges (mirrors repeated call offsets)
    depth, parent, trunc = graph.traverse(_nf(adj), ["A"], max_depth=12)
    assert depth == {"A": 0, "B": 1}


def test_path_from_root():
    adj = {"A": ["B"], "B": ["C"], "C": []}
    depth, parent, trunc = graph.traverse(_nf(adj), ["A"], max_depth=12)
    assert graph.path_from_root("C", parent) == ["C", "B", "A"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/edible/Projects/pare-static-mcp && python -m pytest tests/unit/test_graph_engine.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'pare_static_mcp.apk.graph'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/pare_static_mcp/apk/graph.py
"""Bounded BFS over an abstract graph, factored over a neighbor callable so the
traversal logic is testable on synthetic graphs (no androguard). androguard neighbor
adapters live at the bottom and are the only androguard-touching code — but they call
methods on objects passed in, so this module still imports nothing from androguard.
"""
from __future__ import annotations
from collections import deque

DEFAULT_DEPTH = 3
MAX_DEPTH = 12
MAX_NODES = 5000
MAX_ROWS = 200


def traverse(neighbors_fn, roots, max_depth, node_cap=MAX_NODES):
    """BFS. Returns (depth, parent, truncated). See module Interfaces in the plan."""
    depth: dict = {}
    parent: dict = {}
    q: deque = deque()
    for r in roots:
        if r not in depth:
            depth[r] = 0
            parent[r] = None
            q.append(r)
    truncated = False
    while q:
        node = q.popleft()
        if depth[node] >= max_depth:
            continue
        seen_local = set()
        for nb in neighbors_fn(node):
            if nb in seen_local or nb in depth:
                continue
            seen_local.add(nb)
            if len(depth) >= node_cap:
                truncated = True
                break
            depth[nb] = depth[node] + 1
            parent[nb] = node
            q.append(nb)
        if truncated:
            break
    return depth, parent, truncated


def path_from_root(node, parent) -> list:
    """[node, ..., root] following parent pointers; cycle-safe."""
    chain = []
    seen = set()
    cur = node
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        chain.append(cur)
        cur = parent.get(cur)
    return chain


# --- androguard neighbor adapters (called with MethodAnalysis nodes) ---

def callers(ma) -> list:
    """Backward neighbors: methods that invoke `ma` (index 1 of the 3-tuple)."""
    return [caller for _, caller, _ in ma.get_xref_from()]


def callees(ma) -> list:
    """Forward neighbors: methods `ma` invokes. External targets are included (so a
    sink edge is detectable) but callers()/callees() on them yield nothing to expand."""
    return [callee for _, callee, _ in ma.get_xref_to()]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/edible/Projects/pare-static-mcp && python -m pytest tests/unit/test_graph_engine.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
cd /home/edible/Projects/pare-static-mcp
git add src/pare_static_mcp/apk/graph.py tests/unit/test_graph_engine.py
git commit -m "feat(static): bounded BFS engine over a neighbor callable + androguard adapters"
```

---

## Task 3: `callers_of` tool (the backbone)

**Files:**
- Modify: `src/pare_static_mcp/tools.py`
- Test: `tests/unit/test_callers_of.py`

**Interfaces:**
- Consumes: `graph.traverse`, `graph.callers`, `graph.DEFAULT_DEPTH/MAX_DEPTH`, `loader.ensure_xref`, existing `_ok`/`_err`/`_require_current`.
- Produces: `async def callers_of(method: str, cls: str = "", signature: str = "", depth: int = 3) -> str`. Rows: `{class, method, signature, depth, frontier}`. Adds private helpers `_resolve_methods(analysis, cls, method, signature)`, `_method_row(ma, depth)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_callers_of.py
from __future__ import annotations
import json
import pytest
from pare_static_mcp import tools
from tests.fixtures.locate import test_apk, requires_apk, TEST_METHOD


@requires_apk
@pytest.mark.asyncio
async def test_callers_of_multi_hop_reaches_frontier():
    await tools.load_apk(str(test_apk()))
    # encryptString is called only from OMTG_DATAST_001_KeyStore$1.onClick (a
    # framework-dispatched callback with no static caller => frontier).
    out = json.loads(await tools.callers_of(TEST_METHOD, depth=5))
    assert out.get("error") is not True
    assert len(out["rows"]) > 0
    onclick = [r for r in out["rows"] if r["method"] == "onClick"]
    assert onclick, "expected onClick among transitive callers"
    assert onclick[0]["frontier"] is True
    for r in out["rows"]:
        assert {"class", "method", "signature", "depth", "frontier"} <= set(r)


@requires_apk
@pytest.mark.asyncio
async def test_callers_of_unknown_method_errors_honestly():
    await tools.load_apk(str(test_apk()))
    out = json.loads(await tools.callers_of("thisMethodDoesNotExist_xyz"))
    assert out.get("error") is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/edible/Projects/pare-static-mcp && python -m pytest tests/unit/test_callers_of.py -v`
Expected: FAIL — `AttributeError: module 'pare_static_mcp.tools' has no attribute 'callers_of'`.

- [ ] **Step 3: Write minimal implementation** — append to `src/pare_static_mcp/tools.py` (add `import re` at top if absent):

```python
import re
from pare_static_mcp.apk import graph as graph_mod


def _resolve_methods(analysis, cls: str, method: str, signature: str = "") -> list:
    """Resolve (cls, method[, signature]) to MethodAnalysis objects. Anchors + escapes
    the regex (find_methods does an unanchored re.match; inner-class '$' is a metachar)."""
    classname = ("^" + re.escape("L" + cls.replace(".", "/") + ";") + "$") if cls else "."
    name = "^" + re.escape(method) + "$"
    out = []
    for ma in analysis.find_methods(classname=classname, methodname=name):
        if signature and str(getattr(ma, "descriptor", "")) != signature:
            continue
        out.append(ma)
    return out


def _method_row(ma, depth: int | None = None) -> dict:
    row = {
        "class": str(ma.class_name),
        "method": ma.name,
        "signature": str(getattr(ma, "descriptor", "")),
        "frontier": next(iter(ma.get_xref_from()), None) is None,
    }
    if depth is not None:
        row["depth"] = depth
    return row


def _callers_of_blocking(state, method: str, cls: str, signature: str, depth: int):
    loader_mod.ensure_xref(state)
    roots = _resolve_methods(state.analysis, cls, method, signature)
    if not roots:
        return None  # signal: not found
    md = min(depth, graph_mod.MAX_DEPTH)
    dmap, _parent, trunc = graph_mod.traverse(graph_mod.callers, roots, max_depth=md)
    rows = []
    for ma, d in dmap.items():
        if d == 0:
            continue  # skip the target itself
        rows.append(_method_row(ma, d))
        if len(rows) >= graph_mod.MAX_ROWS:
            trunc = True
            break
    return {"rows": rows, "truncated": trunc}


async def callers_of(method: str, cls: str = "", signature: str = "", depth: int = 3) -> str:
    try:
        st = _require_current()
        res = await asyncio.to_thread(
            _callers_of_blocking, st, method, cls, signature, depth
        )
        if res is None:
            return _err(f"root_not_found: {cls or '*'}.{method}")
        return _ok(f"{len(res['rows'])} transitive callers of {method}",
                   rows=res["rows"], diagnostics={"truncated": res["truncated"]})
    except Exception as e:
        return _err("callers_of failed", e)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/edible/Projects/pare-static-mcp && python -m pytest tests/unit/test_callers_of.py -v`
Expected: PASS (2 tests; skipped if no APK).

- [ ] **Step 5: Commit**

```bash
cd /home/edible/Projects/pare-static-mcp
git add src/pare_static_mcp/tools.py tests/unit/test_callers_of.py
git commit -m "feat(static): callers_of multi-hop backbone (frontier flag, honest root_not_found)"
```

---

## Task 4: `paths_between` tool

**Files:**
- Modify: `src/pare_static_mcp/tools.py`
- Test: `tests/unit/test_paths_between.py`

**Interfaces:**
- Consumes: `_resolve_methods`, `graph.traverse`, `graph.callees`, `graph.path_from_root`.
- Produces: `async def paths_between(from_method, from_cls="", to_method="", to_cls="", from_signature="", to_signature="", max_depth=12) -> str`. Single top-level list `path: [{class, method, signature}, …]` ordered source→target (empty if unreachable).

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_paths_between.py
from __future__ import annotations
import json
import pytest
from pare_static_mcp import tools
from tests.fixtures.locate import test_apk, requires_apk


@requires_apk
@pytest.mark.asyncio
async def test_paths_between_encrypt_to_cipherstream():
    await tools.load_apk(str(test_apk()))
    out = json.loads(await tools.paths_between(
        from_method="encryptString",
        to_method="write", to_cls="javax.crypto.CipherOutputStream",
    ))
    assert out.get("error") is not True
    assert len(out["path"]) >= 2
    assert out["path"][0]["method"] == "encryptString"          # source first
    assert out["path"][-1]["method"] == "write"                 # target last


@requires_apk
@pytest.mark.asyncio
async def test_paths_between_unreachable_is_empty_not_error():
    await tools.load_apk(str(test_apk()))
    out = json.loads(await tools.paths_between(
        from_method="encryptString",
        to_method="exec", to_cls="java.lang.Runtime",
    ))
    assert out.get("error") is not True
    assert out["path"] == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/edible/Projects/pare-static-mcp && python -m pytest tests/unit/test_paths_between.py -v`
Expected: FAIL — no attribute `paths_between`.

- [ ] **Step 3: Write minimal implementation** — append to `tools.py`:

```python
def _paths_between_blocking(state, from_method, from_cls, to_method, to_cls,
                            from_sig, to_sig, max_depth):
    loader_mod.ensure_xref(state)
    sources = _resolve_methods(state.analysis, from_cls, from_method, from_sig)
    targets = _resolve_methods(state.analysis, to_cls, to_method, to_sig)
    if not sources:
        return {"error": "root_not_found", "which": f"{from_cls or '*'}.{from_method}"}
    if not targets:
        return {"error": "root_not_found", "which": f"{to_cls or '*'}.{to_method}"}
    md = min(max_depth, graph_mod.MAX_DEPTH)
    target_ids = {id(t) for t in targets}
    dmap, parent, _trunc = graph_mod.traverse(graph_mod.callees, sources, max_depth=md)
    hit = next((n for n in dmap if id(n) in target_ids), None)
    if hit is None:
        return {"path": []}
    chain = graph_mod.path_from_root(hit, parent)      # [target, ..., source]
    chain.reverse()                                    # -> [source, ..., target]
    return {"path": [{"class": str(m.class_name), "method": m.name,
                      "signature": str(getattr(m, "descriptor", ""))} for m in chain]}


async def paths_between(from_method: str, from_cls: str = "", to_method: str = "",
                        to_cls: str = "", from_signature: str = "", to_signature: str = "",
                        max_depth: int = 12) -> str:
    try:
        st = _require_current()
        res = await asyncio.to_thread(
            _paths_between_blocking, st, from_method, from_cls, to_method, to_cls,
            from_signature, to_signature, max_depth,
        )
        if res.get("error"):
            return _err(f"{res['error']}: {res.get('which', '')}")
        n = len(res["path"])
        return _ok(f"path of {n} nodes" if n else "no static path (control-flow only; "
                   "reflection/callbacks invisible - confirm dynamically)",
                   path=res["path"])
    except Exception as e:
        return _err("paths_between failed", e)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/edible/Projects/pare-static-mcp && python -m pytest tests/unit/test_paths_between.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
cd /home/edible/Projects/pare-static-mcp
git add src/pare_static_mcp/tools.py tests/unit/test_paths_between.py
git commit -m "feat(static): paths_between shortest witness (source->target order)"
```

---

## Task 5: `reachable_sinks` tool (backward, catalog-driven, honest envelope)

**Files:**
- Modify: `src/pare_static_mcp/tools.py`
- Test: `tests/unit/test_reachable_sinks.py`

**Interfaces:**
- Consumes: `sink_match.parse_sink`/`edge_matches`, `graph.traverse`/`graph.callers`/`graph.path_from_root`, `_resolve_methods`.
- Produces: `async def reachable_sinks(to: list[str] | None = None, depth: int = 12, allow_fallback: bool = False) -> str`. Rows: `{candidate:{class,method,signature}, sink:{class,method}, path:[{class,method,signature} … candidate→sink], frontier}`. `diagnostics` dict: `sink_source`, `sink_count`, `candidate_count`, `unmatched_sinks`, `rejected_sinks`, `truncated`, `under_approximation`. Module const `FALLBACK_SINKS`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_reachable_sinks.py
from __future__ import annotations
import json
import pytest
from pare_static_mcp import tools
from tests.fixtures.locate import test_apk, requires_apk


@pytest.mark.asyncio
async def test_reachable_sinks_empty_to_without_fallback_errors():
    # no APK needed: guard fires before traversal
    out = json.loads(await tools.reachable_sinks(to=[]))
    assert out.get("error") is True
    assert "no sinks" in out["summary"].lower()


@requires_apk
@pytest.mark.asyncio
async def test_reachable_sinks_reports_unmatched_and_rejected():
    await tools.load_apk(str(test_apk()))
    out = json.loads(await tools.reachable_sinks(
        to=["com.nonexistent.Foo.bar", "!!!garbage!!!"]))
    assert out.get("error") is not True
    di: dict = out["diagnostics"]
    assert "com.nonexistent.Foo.bar" in diagnostics_flat(di["unmatched_sinks"])
    assert "!!!garbage!!!" in di["rejected_sinks"]
    assert di["sink_source"] == "provided"
    assert "under_approximation" in di


def diagnostics_flat(v):
    return v if isinstance(v, list) else list(v)


@requires_apk
@pytest.mark.asyncio
async def test_reachable_sinks_fallback_is_loud():
    await tools.load_apk(str(test_apk()))
    out = json.loads(await tools.reachable_sinks(to=[], allow_fallback=True))
    assert out.get("error") is not True
    assert out["diagnostics"]["sink_source"] == "fallback"


@requires_apk
@pytest.mark.asyncio
async def test_reachable_sinks_single_top_level_list():
    await tools.load_apk(str(test_apk()))
    out = json.loads(await tools.reachable_sinks(
        to=["javax.crypto.CipherOutputStream.write"]))
    list_keys = [k for k, v in out.items() if isinstance(v, list)]
    assert list_keys == ["rows"]        # exactly one top-level list
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/edible/Projects/pare-static-mcp && python -m pytest tests/unit/test_reachable_sinks.py -v`
Expected: FAIL — no attribute `reachable_sinks`.

- [ ] **Step 3: Write minimal implementation** — append to `tools.py`:

```python
from pare_static_mcp.apk import sink_match as sink_match_mod

FALLBACK_SINKS = [
    "java.lang.Runtime.exec",
    "java.lang.reflect.Method.invoke",
    "android.util.Log.e",
]

_UNDER_APPROX = ("control-flow only; reflection/callback/dispatch edges are invisible "
                 "- an empty result is NOT proof of safety, confirm dynamically")


def _find_sink_nodes(analysis, parsed):
    """MethodAnalysis nodes matching a parsed (class_smali, method), incl. external."""
    smali_cls, method = parsed
    dotted = smali_cls[1:-1].replace("/", ".")     # Lfoo/Bar; -> foo.Bar
    return _resolve_methods(analysis, dotted, method)


def _reachable_sinks_blocking(state, to, depth, allow_fallback):
    loader_mod.ensure_xref(state)
    parsed, rejected = [], []
    for sig in to:
        p = sink_match_mod.parse_sink(sig)
        (parsed.append((sig, p)) if p else rejected.append(sig))
    sink_source = "provided"
    if not parsed:
        if not allow_fallback:
            return {"error": "no sinks supplied; retrieve from PAL or set allow_fallback=true"}
        sink_source = "fallback"
        parsed = [(s, sink_match_mod.parse_sink(s)) for s in FALLBACK_SINKS]

    md = min(depth, graph_mod.MAX_DEPTH)
    rows, unmatched, seen = [], [], set()
    truncated = False
    for sig, p in parsed:
        sink_nodes = _find_sink_nodes(state.analysis, p)
        if not sink_nodes:
            unmatched.append(sig)
            continue
        dmap, parent, trunc = graph_mod.traverse(graph_mod.callers, sink_nodes, max_depth=md)
        truncated = truncated or trunc
        sink_label = {"class": p[0], "method": p[1]}
        for ma, d in dmap.items():
            if d == 0 or ma.is_external():
                continue                       # skip the sink itself and framework frames
            key = (str(ma.class_name), ma.name, str(getattr(ma, "descriptor", "")), p)
            if key in seen:
                continue
            seen.add(key)
            chain = graph_mod.path_from_root(ma, parent)      # [candidate, ..., sink]
            rows.append({
                "candidate": {"class": str(ma.class_name), "method": ma.name,
                              "signature": str(getattr(ma, "descriptor", ""))},
                "sink": sink_label,
                "path": [{"class": str(m.class_name), "method": m.name,
                          "signature": str(getattr(m, "descriptor", ""))} for m in chain],
                "frontier": next(iter(ma.get_xref_from()), None) is None,
            })
            if len(rows) >= graph_mod.MAX_ROWS:
                truncated = True
                break
        if truncated:
            break
    return {"rows": rows, "diagnostics": {
        "sink_source": sink_source, "sink_count": len(parsed),
        "candidate_count": len(rows), "unmatched_sinks": unmatched,
        "rejected_sinks": rejected, "truncated": truncated,
        "under_approximation": _UNDER_APPROX}}


async def reachable_sinks(to: list[str] | None = None, depth: int = 12,
                          allow_fallback: bool = False) -> str:
    try:
        to = list(to or [])
        st = _require_current()
        res = await asyncio.to_thread(_reachable_sinks_blocking, st, to, depth, allow_fallback)
        if res.get("error"):
            return _err(res["error"])
        return _ok(f"{res['diagnostics']['candidate_count']} hook candidates reach "
                   f"{res['diagnostics']['sink_count']} sink(s) "
                   f"[{res['diagnostics']['sink_source']}]",
                   rows=res["rows"], diagnostics=res["diagnostics"])
    except Exception as e:
        return _err("reachable_sinks failed", e)
```

Note: the empty-`to`-no-fallback guard must fire *before* `_require_current()` so the first test needs no APK. Implement by checking at the top of `reachable_sinks`:

```python
        if not to and not allow_fallback:
            return _err("no sinks supplied; retrieve from PAL or set allow_fallback=true")
```
Place that immediately after `to = list(to or [])`.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/edible/Projects/pare-static-mcp && python -m pytest tests/unit/test_reachable_sinks.py -v`
Expected: PASS (4 tests; 3 skip without APK, 1 runs without APK).

- [ ] **Step 5: Commit**

```bash
cd /home/edible/Projects/pare-static-mcp
git add src/pare_static_mcp/tools.py tests/unit/test_reachable_sinks.py
git commit -m "feat(static): reachable_sinks backward-from-catalog with honest diagnostics envelope"
```

---

## Task 6: Register the three tools in the contract

**Files:**
- Modify: `src/pare_static_mcp/contract.py:24-78` (the `TOOL_SPECS` list)
- Test: `tests/unit/test_contract.py` (extend)

**Interfaces:**
- Consumes: `ToolSpec`, `_in`. Produces: three new specs; `server.build_server()` auto-wires them (it maps `spec.name` → `tools.<name>`).

- [ ] **Step 1: Write the failing test** — append to `tests/unit/test_contract.py`:

```python
def test_reachability_tools_registered_low():
    from pare_static_mcp.contract import TOOL_SPECS
    names = {s.name: s for s in TOOL_SPECS}
    for n in ("callers_of", "paths_between", "reachable_sinks"):
        assert n in names, f"{n} missing from TOOL_SPECS"
        assert names[n].risk_tier == "low"


def test_server_wires_reachability_handlers():
    from pare_static_mcp import tools
    for n in ("callers_of", "paths_between", "reachable_sinks"):
        assert callable(getattr(tools, n))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/edible/Projects/pare-static-mcp && python -m pytest tests/unit/test_contract.py -k reachability -v`
Expected: FAIL — names missing from `TOOL_SPECS`.

- [ ] **Step 3: Write minimal implementation** — add three `ToolSpec`s to the `TOOL_SPECS` list in `contract.py`, before the closing `]`:

```python
    ToolSpec("callers_of", "low",
             "STATIC. Multi-hop REVERSE reachability: methods that transitively CALL "
             "the target (generalizes find_symbol kind=caller to N hops). Rows of "
             "{class, method, signature, depth, frontier}; frontier=true means the "
             "method has no static caller (a framework-dispatched callback like onClick "
             "- the honest edge of static knowledge, where you hook and let Frida "
             "confirm). depth defaults to 3, capped at 12.",
             _in(method={"type": "string"}, cls={"type": "string"},
                 signature={"type": "string"}, depth={"type": "integer"})),
    ToolSpec("paths_between", "low",
             "STATIC. Shortest witness call-path from a source method to a target "
             "method (forward). Returns 'path' ordered source->target, or empty if "
             "unreachable within max_depth (control-flow only; reflection/callbacks are "
             "invisible - empty is not proof of safety). Use to confirm/expand a "
             "hypothesized route.",
             _in(from_method={"type": "string"}, from_cls={"type": "string"},
                 to_method={"type": "string"}, to_cls={"type": "string"},
                 from_signature={"type": "string"}, to_signature={"type": "string"},
                 max_depth={"type": "integer"})),
    ToolSpec("reachable_sinks", "low",
             "STATIC. Given dangerous SINK signatures (from PAL's sink catalog, dotted "
             "or smali), walk BACKWARD to the app methods that reach each sink - those "
             "are your hook candidates. Rows of {candidate, sink, path (candidate->sink), "
             "frontier}. Pass 'to' as a list of 'Class.method' strings. diagnostics "
             "reports unmatched_sinks, rejected_sinks, sink_source (provided|fallback), "
             "and an under_approximation note - an empty result is NOT proof of safety. "
             "Empty 'to' errors unless allow_fallback=true (a tiny generic catalog).",
             _in(to={"type": "array", "items": {"type": "string"}},
                 depth={"type": "integer"},
                 allow_fallback={"type": "boolean"})),
```

- [ ] **Step 4: Run test to verify it passes, plus conformance + full suite**

Run: `cd /home/edible/Projects/pare-static-mcp && python -m pytest tests/unit/test_contract.py -v && python -m pytest`
Expected: PASS — new contract tests green, existing suite still green, conformance accepts the specs.

- [ ] **Step 5: Commit**

```bash
cd /home/edible/Projects/pare-static-mcp
git add src/pare_static_mcp/contract.py tests/unit/test_contract.py
git commit -m "feat(static): register callers_of/paths_between/reachable_sinks (low tier + descriptions)"
```

---

## Task 7: Keystone integration test (real loop, not self-proving)

**Files:**
- Test: `tests/unit/test_reachable_sinks_keystone.py`

**Interfaces:** Consumes `tools.reachable_sinks`. Proves: catalogued crypto sink (passed **explicitly**, as if from PAL) → backward walk → `encryptString`/`decryptString` candidate → `frontier` marks the callback boundary.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_reachable_sinks_keystone.py
from __future__ import annotations
import json
import pytest
from pare_static_mcp import tools
from tests.fixtures.locate import test_apk, requires_apk

# Passed EXPLICITLY (mirrors the model retrieving these from PAL) — NOT via fallback,
# so the test cannot pass on a frozen worker constant.
PAL_CRYPTO_SINKS = [
    "javax.crypto.CipherOutputStream.write(byte[] b)",
    "javax.crypto.Cipher.doFinal(byte[] input)",
]


@requires_apk
@pytest.mark.asyncio
async def test_keystore_hook_target_derived_from_graph():
    await tools.load_apk(str(test_apk()))
    out = json.loads(await tools.reachable_sinks(to=PAL_CRYPTO_SINKS, depth=8))
    assert out.get("error") is not True
    cand_methods = {r["candidate"]["method"] for r in out["rows"]}
    assert "encryptString" in cand_methods, (
        f"expected encryptString as a hook candidate; got {sorted(cand_methods)}")
    assert out["diagnostics"]["sink_source"] == "provided"
    # the candidate's witness path ends at the catalogued sink
    enc = next(r for r in out["rows"] if r["candidate"]["method"] == "encryptString")
    assert enc["path"][-1]["method"] in ("write", "doFinal")
```

- [ ] **Step 2: Run test to verify it fails (or is red for the right reason)**

Run: `cd /home/edible/Projects/pare-static-mcp && python -m pytest tests/unit/test_reachable_sinks_keystone.py -v`
Expected: FAIL only if a real bug exists; if Tasks 1-6 are correct it may already PASS. If it fails, debug against the real xref (use `paths_between`/`callers_of` to inspect) — do NOT weaken the assertion to make it pass.

- [ ] **Step 3: (only if red) fix the implementation, not the test.**

If `encryptString` is absent: confirm with `callers_of("write", cls="javax.crypto.CipherOutputStream")` that the sink resolves and has app callers; check `_find_sink_nodes` dotted-class round-trip and that external sink nodes are found by `find_methods`.

- [ ] **Step 4: Run to verify it passes**

Run: `cd /home/edible/Projects/pare-static-mcp && python -m pytest tests/unit/test_reachable_sinks_keystone.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /home/edible/Projects/pare-static-mcp
git add tests/unit/test_reachable_sinks_keystone.py
git commit -m "test(static): keystone - KeyStore hook target derived from graph (PAL sinks explicit)"
```

---

## Task 8: agent_core — `SearchVault` gains `tags` + `doc_id`

**Files:**
- Modify: `agent_core/agent_core/tools/_framework.py:84-145` (the `SearchVault` class)
- Test: `agent_core/tests/` — add `test_search_vault_tags_docid.py` (match the repo's existing test layout/framework; use pytest-asyncio if present).

**Interfaces:**
- Consumes: `ctx.agent.retrieval.search(query, limit, tags=...)` and `ctx.agent.retrieval.get_document(doc_id)` (both already exist in `agent_core/retrieval.py`).
- Produces: `SearchVault` accepts optional `tags: list[str]` and `doc_id: str`. `doc_id` → fetch that document directly; else `search(query, limit, tags=tags)`.

- [ ] **Step 1: Write the failing test**

```python
# agent_core/tests/test_search_vault_tags_docid.py
from __future__ import annotations
import json
import pytest
from agent_core.tools._framework import SearchVault


class _FakeRetrieval:
    def __init__(self):
        self.search_calls = []
        self.doc_calls = []

    async def search(self, query, limit=5, tags=None):
        self.search_calls.append({"query": query, "limit": limit, "tags": tags})
        return [{"id": "raw/notes/android-vulnerable-sinks-reference",
                 "name": "sinks", "summary": "s", "score": 0.9}]

    async def get_document(self, doc_id):
        self.doc_calls.append(doc_id)
        return {"id": doc_id, "name": "sinks", "summary": "s",
                "content": "# sinks\n...", "metadata": {}}


class _FakeAgent:
    def __init__(self):
        self.retrieval = _FakeRetrieval()


class _FakeCtx:
    def __init__(self):
        self.agent = _FakeAgent()


@pytest.mark.asyncio
async def test_search_vault_forwards_tags():
    ctx = _FakeCtx()
    out = json.loads(await SearchVault().run({"query": "sinks", "tags": ["sinks"]}, ctx))
    assert out["status"] == "ok"
    assert ctx.agent.retrieval.search_calls[0]["tags"] == ["sinks"]


@pytest.mark.asyncio
async def test_search_vault_doc_id_fetches_document():
    ctx = _FakeCtx()
    out = json.loads(await SearchVault().run(
        {"doc_id": "raw/notes/android-vulnerable-sinks-reference"}, ctx))
    assert out["status"] == "ok"
    assert ctx.agent.retrieval.doc_calls == ["raw/notes/android-vulnerable-sinks-reference"]
    assert "content" in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/edible/Projects/agent_core && python -m pytest tests/test_search_vault_tags_docid.py -v`
Expected: FAIL — `tags` not forwarded / `doc_id` branch absent.

- [ ] **Step 3: Write minimal implementation** — edit `SearchVault` in `_framework.py`:

Add to `parameters["properties"]`:
```python
            "tags": {"type": "array", "items": {"type": "string"},
                     "description": "Optional tag filter (AND-ed with the query)."},
            "doc_id": {"type": "string",
                       "description": "Fetch this document by id directly (skips search)."},
```
Remove `"required": ["query"]` (a `doc_id`-only call is valid) — replace with `"required": []`.

Replace the body of `run` after the `query`/`max_results` parsing with a `doc_id` short-circuit and a `tags` pass-through:
```python
    async def run(self, args, ctx):
        doc_id = (args.get("doc_id") or "").strip()
        if doc_id:
            try:
                doc = await ctx.agent.retrieval.get_document(doc_id)
            except Exception as exc:
                return json.dumps({"status": "error", "doc_id": doc_id,
                                   "reason": f"Fetch error: {type(exc).__name__}: {exc}"})
            return json.dumps({"status": "ok", "doc_id": doc_id,
                               "name": doc.get("name", ""),
                               "content": doc.get("content", "")})
        query = (args.get("query") or "").strip()
        if not query:
            return json.dumps({"status": "error",
                               "reason": "'query' or 'doc_id' is required."})
        max_results = max(1, min(int(args.get("max_results", 5)), 20))
        tags = args.get("tags") or None
        try:
            results = await ctx.agent.retrieval.search(query, limit=max_results, tags=tags)
        except Exception as exc:
            return json.dumps({"status": "error", "query": query,
                               "reason": f"Search error: {type(exc).__name__}: {exc}"})
        structured = []
        for r in results:
            if isinstance(r, dict):
                id_val = r.get("id", ""); name = r.get("name") or id_val
                summary = r.get("summary", ""); score = r.get("score", 0.0)
            else:
                id_val = getattr(r, "id", ""); name = getattr(r, "name", None) or id_val
                summary = getattr(r, "summary", ""); score = getattr(r, "score", 0.0)
            structured.append({"path": f"{id_val}.md" if id_val else "", "name": name,
                               "summary": _truncate(summary, 200),
                               "score": round(float(score), 3)})
        return json.dumps({"status": "ok", "query": query,
                           "count": len(structured), "results": structured})
```
Also update the `description` string to mention `tags`/`doc_id`.

- [ ] **Step 4: Run test to verify it passes + full agent_core suite**

Run: `cd /home/edible/Projects/agent_core && python -m pytest tests/test_search_vault_tags_docid.py -v && python -m pytest -q`
Expected: PASS — new tests green, existing suite unaffected.

- [ ] **Step 5: Commit**

```bash
cd /home/edible/Projects/agent_core
git add agent_core/tools/_framework.py tests/test_search_vault_tags_docid.py
git commit -m "feat(tools): search_vault gains tags filter + doc_id direct fetch"
```

---

## Task 9: Full-suite verification + branch prep

**Files:** none (verification only).

- [ ] **Step 1: Worker full suite**

Run: `cd /home/edible/Projects/pare-static-mcp && python -m pytest -q`
Expected: all green (existing 25 + new tests). If the APK is present, keystone runs; else it skips — note which in the PR body.

- [ ] **Step 2: agent_core full suite**

Run: `cd /home/edible/Projects/agent_core && python -m pytest -q`
Expected: all green.

- [ ] **Step 3: Confirm branches + push (do NOT merge/tag)**

```bash
cd /home/edible/Projects/pare-static-mcp && git log --oneline main..HEAD
cd /home/edible/Projects/agent_core && git log --oneline main..HEAD
```
Expected: worker branch has Tasks 1-7 commits; agent_core branch has Task 8. Push both; open PRs. Leave the PARE agent_core-pin bump queued (gated on maintainer merge+tag of agent_core).

---

## Self-Review (author)

- **Spec coverage:** §3.1 callers_of → T3; §3.1 paths_between → T4; §3.2 reachable_sinks (backward, catalog, fallback loud/opt-in, honest errors) → T5+T6; §4.1 SearchVault tags/doc_id → T8; §4.2 normalizer isolated+table → T1; §5 engine over neighbor-callable + synthetic tests → T2; §6 constants + truncation → T2/T3/T5; §7 honest-error contract → T3 (root_not_found), T5 (unmatched/rejected/fallback/empty-guard); §8 under_approximation in envelope → T4/T5; §9 keystone (explicit sinks) → T7; single-top-level-list → T5 test. Manifest/`sources=`/`callees_of` correctly absent (v2/cut).
- **Placeholder scan:** none — every step carries real code/commands.
- **Type consistency:** `traverse` returns `(depth, parent, truncated)` everywhere; `path_from_root` used in T4/T5; `_resolve_methods`/`_method_row` defined T3, reused T4/T5; `parse_sink`→`(class_smali, method)` consistent T1/T5; row shapes match the contract descriptions in T6.
```
