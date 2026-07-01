# Capture Layer (agent_core capability) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a project-scoped, schema-on-read capture store to agent_core that bounds the bytes an oversized tool result contributes to the model's context window, storing the full result out-of-band behind a small handle while keeping it searchable and correlatable across workers.

**Architecture:** A `CaptureLayer` collaborator is invoked at the one wire chokepoint (`RiskAwareToolPool.call_tool`'s return). It always *stores* substantial results in a SQLite JSON store and *substitutes* a hard-bounded stub into the model's result only when over budget. Retrieval is via opt-in `Tool` subclasses that read the store directly. Storing and substituting are independent decisions. This is Plan 1 of 2; Plan 2 wires it into PARE and tears down the frida MCP's own store.

**Tech Stack:** Python 3.11+, stdlib `sqlite3` (JSON1 + FTS5), `pytest` + `pytest-asyncio`. No new third-party dependencies.

**Scope note:** This plan covers only agent_core. It is independently shippable and testable, and PAL (which opts out) must boot and run unchanged. PARE wiring, protocol `cwd` threading, `/snapshot` repoint, and the frida teardown are Plan 2.

## Global Constraints

- **Package:** all new code lives under `agent_core/agent_core/capture/`; tests under `agent_core/tests/capture/`. Copied verbatim from spec §3/§5.
- **No new dependencies** — stdlib `sqlite3`, `json`, `re`, `secrets`, `pathlib` only.
- **On-disk hardening:** every created dir is `mode=0o700` (umask-guarded via explicit `chmod`); every created file (`capture.db`, each blob) is `chmod(0o600)`. Spec §9.
- **PAL-safe:** retrieval tools are NEVER added to `BUILTIN_TOOLS`; they are opt-in agent tool classes with `requires=('capture_store',)`. agent_core hardcodes no project-marker string. Spec §7/§8.
- **FTS queries are phrase-escaped** at every `MATCH` bind site (spec §5).
- **Release:** lands as a new agent_core minor version (successor to `v1.6.2`); PARE bumps its pin in Plan 2.
- Run tests from the agent_core repo root: `cd /home/edible/Projects/agent_core`.

---

### Task 1: Capture store — schema, write, get

**Files:**
- Create: `agent_core/agent_core/capture/__init__.py`
- Create: `agent_core/agent_core/capture/store.py`
- Test: `agent_core/tests/capture/__init__.py`, `agent_core/tests/capture/test_store.py`

**Interfaces:**
- Consumes: nothing (leaf module).
- Produces:
  - `CaptureRecord` dataclass: `worker: str`, `tool: str`, `session_id: str | None`, `launch_ts: float`, `summary: str`, `body: str` (serialized JSON of the full result), `rows: int`, `addrs: list[str]`.
  - `CaptureStore.open(db_path: Path) -> CaptureStore`
  - `CaptureStore.open_memory() -> CaptureStore`
  - `CaptureStore.write(record: CaptureRecord) -> str` (returns opaque `ref`)
  - `CaptureStore.get(ref: str) -> dict | None`
  - `CaptureStore.close() -> None`

- [ ] **Step 1: Write the failing test**

```python
# agent_core/tests/capture/test_store.py
import json
from agent_core.capture.store import CaptureStore, CaptureRecord


def _rec(**kw):
    base = dict(worker="frida", tool="enumerate_modules", session_id="s1",
                launch_ts=1000.0, summary="2 modules", body=json.dumps([{"name": "libc"}]),
                rows=1, addrs=[])
    base.update(kw)
    return CaptureRecord(**base)


def test_write_returns_ref_and_get_roundtrips():
    store = CaptureStore.open_memory()
    ref = store.write(_rec())
    assert isinstance(ref, str) and len(ref) >= 6
    row = store.get(ref)
    assert row["worker"] == "frida"
    assert json.loads(row["body"]) == [{"name": "libc"}]
    assert row["rows"] == 1
    store.close()


def test_refs_are_unique():
    store = CaptureStore.open_memory()
    refs = {store.write(_rec()) for _ in range(50)}
    assert len(refs) == 50
    store.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/capture/test_store.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'agent_core.capture'`.

- [ ] **Step 3: Write minimal implementation**

```python
# agent_core/agent_core/capture/__init__.py
```

```python
# agent_core/agent_core/capture/store.py
from __future__ import annotations

import json
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS captures (
  seq INTEGER PRIMARY KEY,
  ref TEXT NOT NULL UNIQUE,
  ts REAL NOT NULL,
  worker TEXT NOT NULL,
  tool TEXT,
  session_id TEXT,
  launch_ts REAL,
  rows INTEGER,
  summary TEXT,
  body TEXT,
  blob_ref TEXT,
  addrs TEXT
);
CREATE INDEX IF NOT EXISTS idx_captures_worker ON captures(worker);
CREATE INDEX IF NOT EXISTS idx_captures_launch ON captures(launch_ts);
CREATE INDEX IF NOT EXISTS idx_captures_ts ON captures(ts);
CREATE VIRTUAL TABLE IF NOT EXISTS captures_fts
  USING fts5(body, addrs, content='captures', content_rowid='seq');
"""


@dataclass
class CaptureRecord:
    worker: str
    tool: str
    session_id: str | None
    launch_ts: float
    summary: str
    body: str
    rows: int
    addrs: list[str]


class CaptureStore:
    def __init__(self, conn: sqlite3.Connection, root: Path | None, blob_threshold: int = 65536):
        self._conn = conn
        self._root = root
        self._blob_threshold = blob_threshold

    @classmethod
    def open(cls, db_path: Path) -> "CaptureStore":
        root = Path(db_path).parent
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.chmod(0o700)
        conn = sqlite3.connect(db_path)
        Path(db_path).chmod(0o600)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.executescript(_SCHEMA)
        return cls(conn, root)

    @classmethod
    def open_memory(cls) -> "CaptureStore":
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(_SCHEMA)
        return cls(conn, None, blob_threshold=1 << 30)

    def write(self, record: CaptureRecord) -> str:
        ref = secrets.token_hex(4)
        addrs_text = " ".join(record.addrs)
        spill = len(record.body) > self._blob_threshold and self._root is not None
        stored_body = None if spill else record.body
        cur = self._conn.execute(
            "INSERT INTO captures (ref, ts, worker, tool, session_id, launch_ts, rows, summary, body, blob_ref, addrs)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (ref, time.time(), record.worker, record.tool, record.session_id,
             record.launch_ts, record.rows, record.summary, stored_body, None, addrs_text),
        )
        seq = cur.lastrowid
        blob_ref = None
        if spill:
            blobs = self._root / "blobs"
            blobs.mkdir(exist_ok=True, mode=0o700)
            blob_path = blobs / f"{seq}.bin"
            try:
                blob_path.write_bytes(record.body.encode("utf-8"))
                blob_path.chmod(0o600)
            except OSError:
                blob_path.unlink(missing_ok=True)
                raise
            blob_ref = str(blob_path)
            self._conn.execute("UPDATE captures SET blob_ref=? WHERE seq=?", (blob_ref, seq))
        # FTS always gets the full body so search works on spilled rows.
        self._conn.execute(
            "INSERT INTO captures_fts (rowid, body, addrs) VALUES (?,?,?)",
            (seq, record.body, addrs_text),
        )
        self._conn.commit()
        return ref

    def get(self, ref: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM captures WHERE ref=?", (ref,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        if d.get("body") is None and d.get("blob_ref"):
            d["body"] = Path(d["blob_ref"]).read_text(encoding="utf-8")
        return d

    def close(self) -> None:
        self._conn.close()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/capture/test_store.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add agent_core/capture/__init__.py agent_core/capture/store.py tests/capture/__init__.py tests/capture/test_store.py
git commit -m "feat(capture): schema-on-read store with write/get and blob spill"
```

---

### Task 2: Blob spill hardening + on-disk permissions

**Files:**
- Modify: `agent_core/agent_core/capture/store.py` (covered by Task 1 code; this task adds the tests that lock the behavior)
- Test: `agent_core/tests/capture/test_store_disk.py`

**Interfaces:**
- Consumes: `CaptureStore.open`, `write`, `get` from Task 1.
- Produces: no new API; pins spill + permission behavior.

- [ ] **Step 1: Write the failing test**

```python
# agent_core/tests/capture/test_store_disk.py
import json, stat
from pathlib import Path
from agent_core.capture.store import CaptureStore, CaptureRecord


def _big_rec():
    body = json.dumps([{"hex": "ab" * 50000}])  # ~100KB > 64KB threshold
    return CaptureRecord(worker="frida", tool="read_memory", session_id="s1",
                         launch_ts=1.0, summary="big", body=body, rows=1, addrs=[])


def test_large_body_spills_to_blob_and_get_restores_it(tmp_path):
    store = CaptureStore.open(tmp_path / ".pare" / "capture.db")
    ref = store.write(_big_rec())
    row = store.get(ref)
    assert row["blob_ref"] is not None
    assert json.loads(row["body"])[0]["hex"] == "ab" * 50000
    store.close()


def test_disk_permissions_are_hardened(tmp_path):
    db = tmp_path / ".pare" / "capture.db"
    store = CaptureStore.open(db)
    store.write(_big_rec())
    assert stat.S_IMODE((tmp_path / ".pare").stat().st_mode) == 0o700
    assert stat.S_IMODE(db.stat().st_mode) == 0o600
    blob = next((tmp_path / ".pare" / "blobs").glob("*.bin"))
    assert stat.S_IMODE(blob.stat().st_mode) == 0o600
    store.close()
```

- [ ] **Step 2: Run test to verify it fails, then passes with Task 1 code**

Run: `python3 -m pytest tests/capture/test_store_disk.py -v`
Expected: PASS (the Task 1 implementation already satisfies these). If either fails, fix `store.py` until both pass — this task exists to lock spill + `0o700/0o600` as regression tests.

- [ ] **Step 3: Commit**

```bash
git add tests/capture/test_store_disk.py
git commit -m "test(capture): lock blob spill and 0o700/0o600 hardening"
```

---

### Task 3: Search — FTS phrase-escaping, field filter, recent, addrs

**Files:**
- Modify: `agent_core/agent_core/capture/store.py`
- Create: `agent_core/agent_core/capture/query.py`
- Test: `agent_core/tests/capture/test_search.py`

**Interfaces:**
- Consumes: `CaptureStore` from Task 1.
- Produces:
  - `fts_phrase(text: str) -> str` in `query.py`
  - `CaptureStore.search(*, text: str = "", worker: str = "", field: str = "", contains: str = "", limit: int = 50) -> list[dict]`
  - `CaptureStore.recent(limit: int = 20) -> list[dict]`

- [ ] **Step 1: Write the failing test**

```python
# agent_core/tests/capture/test_search.py
import json
from agent_core.capture.store import CaptureStore, CaptureRecord
from agent_core.capture.query import fts_phrase


def _rec(worker="frida", body=None, addrs=None, summary="s"):
    return CaptureRecord(worker=worker, tool="t", session_id="s1", launch_ts=1.0,
                         summary=summary, body=body or json.dumps([{"name": "x"}]),
                         rows=1, addrs=addrs or [])


def test_fts_phrase_escapes_punctuation():
    assert fts_phrase('libc.so.6') == '"libc.so.6"'
    assert fts_phrase('say "hi"') == '"say ""hi"""'


def test_text_search_matches_dotted_token_without_crashing():
    store = CaptureStore.open_memory()
    store.write(_rec(body=json.dumps([{"name": "libc.so.6"}])))
    hits = store.search(text="libc.so.6")   # would raise fts5 syntax error if unescaped
    assert len(hits) == 1
    store.close()


def test_worker_filter_and_recent():
    store = CaptureStore.open_memory()
    store.write(_rec(worker="frida"))
    store.write(_rec(worker="ghidra"))
    assert len(store.search(worker="frida")) == 1
    assert len(store.recent(limit=10)) == 2
    store.close()


def test_addrs_are_searchable_after_normalization():
    store = CaptureStore.open_memory()
    store.write(_rec(body=json.dumps([{"ea": "0x401000"}]), addrs=["0000000000401000"]))
    assert len(store.search(text="0000000000401000")) == 1
    store.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/capture/test_search.py -v`
Expected: FAIL — `cannot import name 'fts_phrase'` / `CaptureStore` has no attribute `search`.

- [ ] **Step 3: Write minimal implementation**

```python
# agent_core/agent_core/capture/query.py
from __future__ import annotations

_ALLOWED_FIELDS = frozenset({"worker", "tool", "session_id"})


def fts_phrase(text: str) -> str:
    """Wrap a user string as a single FTS5 phrase so punctuation (.-:) is
    literal, not query syntax. Double embedded quotes per FTS5 rules."""
    return '"' + text.replace('"', '""') + '"'
```

Add to `store.py` (methods on `CaptureStore`):

```python
    def search(self, *, text: str = "", worker: str = "", field: str = "",
               contains: str = "", limit: int = 50) -> list[dict]:
        from agent_core.capture.query import fts_phrase, _ALLOWED_FIELDS
        clauses, params = [], []
        sql = "SELECT c.* FROM captures c"
        if text:
            sql += " JOIN captures_fts f ON f.rowid = c.seq"
            clauses.append("captures_fts MATCH ?")
            params.append(fts_phrase(text))
        if worker:
            clauses.append("c.worker = ?")
            params.append(worker)
        if field and contains:
            if field in _ALLOWED_FIELDS:
                clauses.append(f"c.{field} LIKE ? ESCAPE '\\'")
            else:
                clauses.append("json_extract(c.body, ?) LIKE ? ESCAPE '\\'")
                params.append("$." + field)
            like = "%" + contains.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            params.append(like)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY c.seq DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def recent(self, limit: int = 20) -> list[dict]:
        rows = self._conn.execute(
            "SELECT ref, worker, tool, rows, summary FROM captures ORDER BY seq DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/capture/test_search.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add agent_core/capture/query.py agent_core/capture/store.py tests/capture/test_search.py
git commit -m "feat(capture): search with FTS phrase-escaping, worker/field filter, recent"
```

---

### Task 4: Blob-aware delete + retention/purge

**Files:**
- Modify: `agent_core/agent_core/capture/store.py`
- Test: `agent_core/tests/capture/test_retention.py`

**Interfaces:**
- Consumes: `CaptureStore` from Tasks 1/3.
- Produces:
  - `CaptureStore.delete(ref: str) -> bool` (unlinks the row's blob too)
  - `CaptureStore.total_bytes() -> int` (rows + blob bytes)
  - `CaptureStore.purge(*, max_bytes: int | None = None, max_age_s: float | None = None, now: float, protected_refs: set[str] = frozenset()) -> int`

- [ ] **Step 1: Write the failing test**

```python
# agent_core/tests/capture/test_retention.py
import json
from pathlib import Path
from agent_core.capture.store import CaptureStore, CaptureRecord


def _big(worker="frida"):
    return CaptureRecord(worker=worker, tool="read_memory", session_id="s1", launch_ts=1.0,
                         summary="big", body=json.dumps([{"hex": "ab" * 50000}]), rows=1, addrs=[])


def test_delete_removes_row_and_blob(tmp_path):
    store = CaptureStore.open(tmp_path / ".pare" / "capture.db")
    ref = store.write(_big())
    blob = next((tmp_path / ".pare" / "blobs").glob("*.bin"))
    assert store.delete(ref) is True
    assert store.get(ref) is None
    assert not blob.exists()
    store.close()


def test_purge_by_age_respects_protected_refs(tmp_path):
    store = CaptureStore.open(tmp_path / ".pare" / "capture.db")
    old = store.write(_big())
    keep = store.write(_big())
    # Age everything far into the past; protect `keep`.
    removed = store.purge(max_age_s=0.0, now=1e12, protected_refs={keep})
    assert removed == 1
    assert store.get(old) is None
    assert store.get(keep) is not None
    store.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/capture/test_retention.py -v`
Expected: FAIL — `CaptureStore` has no attribute `delete`.

- [ ] **Step 3: Write minimal implementation** (add to `store.py`)

```python
    def _unlink_blob(self, blob_ref: str | None) -> None:
        if blob_ref:
            Path(blob_ref).unlink(missing_ok=True)

    def delete(self, ref: str) -> bool:
        row = self._conn.execute("SELECT seq, blob_ref FROM captures WHERE ref=?", (ref,)).fetchone()
        if row is None:
            return False
        self._conn.execute("DELETE FROM captures WHERE seq=?", (row["seq"],))
        self._conn.execute("INSERT INTO captures_fts(captures_fts) VALUES ('rebuild')")
        self._conn.commit()
        self._unlink_blob(row["blob_ref"])  # after commit: a crash orphans a file, never a row
        return True

    def total_bytes(self) -> int:
        rows = self._conn.execute(
            "SELECT COALESCE(SUM(LENGTH(body)), 0) AS b FROM captures"
        ).fetchone()["b"]
        blob_total = 0
        for r in self._conn.execute("SELECT blob_ref FROM captures WHERE blob_ref IS NOT NULL"):
            p = Path(r["blob_ref"])
            if p.exists():
                blob_total += p.stat().st_size
        return int(rows) + blob_total

    def purge(self, *, max_bytes: int | None = None, max_age_s: float | None = None,
              now: float, protected_refs: set[str] = frozenset()) -> int:
        removed = 0
        if max_age_s is not None:
            cutoff = now - max_age_s
            stale = [r["ref"] for r in self._conn.execute(
                "SELECT ref FROM captures WHERE ts < ? ORDER BY seq ASC", (cutoff,))]
            for ref in stale:
                if ref not in protected_refs and self.delete(ref):
                    removed += 1
        if max_bytes is not None:
            while self.total_bytes() > max_bytes:
                row = self._conn.execute(
                    "SELECT ref FROM captures ORDER BY seq ASC LIMIT 1").fetchone()
                if row is None or row["ref"] in protected_refs:
                    break
                if self.delete(row["ref"]):
                    removed += 1
                else:
                    break
        return removed
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/capture/test_retention.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add agent_core/capture/store.py tests/capture/test_retention.py
git commit -m "feat(capture): blob-aware delete and reachability-aware purge"
```

---

### Task 5: Shape inference + address normalization

**Files:**
- Create: `agent_core/agent_core/capture/shape.py`
- Test: `agent_core/tests/capture/test_shape.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `infer_rows(value: Any) -> list[dict]` (applies §4 guard rules)
  - `is_substantial(value: Any, rows: list[dict], serialized_bytes: int, inline_budget: int) -> bool`
  - `columns(rows: list[dict], cap: int = 12) -> list[str]`
  - `normalize_addrs(body: str) -> list[str]`

- [ ] **Step 1: Write the failing test**

```python
# agent_core/tests/capture/test_shape.py
from agent_core.capture.shape import infer_rows, columns, normalize_addrs, is_substantial


def test_array_of_objects_is_n_rows():
    assert infer_rows([{"a": 1}, {"a": 2}]) == [{"a": 1}, {"a": 2}]


def test_object_is_one_row():
    assert infer_rows({"a": 1}) == [{"a": 1}]


def test_single_array_value_object_unwraps():
    assert infer_rows({"modules": [{"n": 1}, {"n": 2}]}) == [{"n": 1}, {"n": 2}]


def test_non_object_elements_are_wrapped():
    assert infer_rows(["a", "b"]) == [{"value": "a"}, {"value": "b"}]


def test_empty_array_is_no_rows():
    assert infer_rows([]) == []


def test_columns_are_deterministic_union_capped():
    rows = [{"b": 1, "a": 2}, {"c": 3}]
    assert columns(rows) == ["a", "b", "c"]


def test_normalize_addrs_strips_and_pads():
    got = set(normalize_addrs('{"ea": "0x401000", "p": "00401000"}'))
    assert "0000000000401000" in got


def test_is_substantial():
    assert is_substantial([{"a": 1}], [{"a": 1}], 10, 4096) is True          # array -> store
    assert is_substantial("3 devices", ["3 devices"], 10, 4096) is False     # small scalar
    assert is_substantial({"x": 1}, [{"x": 1}], 99999, 4096) is True         # over budget
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/capture/test_shape.py -v`
Expected: FAIL — `No module named 'agent_core.capture.shape'`.

- [ ] **Step 3: Write minimal implementation**

```python
# agent_core/agent_core/capture/shape.py
from __future__ import annotations

import re
from typing import Any

_HEX = re.compile(r"\b(?:0x)?([0-9a-fA-F]{4,16})\b")


def infer_rows(value: Any) -> list[dict]:
    """Map a JSON value to rows per spec §4, with guard rules."""
    if isinstance(value, dict):
        # Single-array-value object -> unwrap to that array's rows.
        vals = list(value.values())
        if len(vals) == 1 and isinstance(vals[0], list):
            return infer_rows(vals[0])
        return [value]
    if isinstance(value, list):
        out = []
        for el in value:
            out.append(el if isinstance(el, dict) else {"value": el})
        return out
    # Scalar/blob -> degenerate one-column row.
    return [{"value": value}]


def columns(rows: list[dict], cap: int = 12) -> list[str]:
    seen: list[str] = []
    for row in rows:
        for k in row:
            if k not in seen:
                seen.append(k)
    ordered = sorted(seen)
    return ordered[:cap]


def normalize_addrs(body: str) -> list[str]:
    out: set[str] = set()
    for m in _HEX.finditer(body):
        tok = m.group(1).lower()
        if len(tok) >= 4:
            out.add(tok.zfill(16))
    return sorted(out)


def is_substantial(value: Any, rows: list[dict], serialized_bytes: int, inline_budget: int) -> bool:
    if isinstance(value, list) and rows:
        return True
    return serialized_bytes > inline_budget
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/capture/test_shape.py -v`
Expected: PASS (8 passed).

- [ ] **Step 5: Commit**

```bash
git add agent_core/capture/shape.py tests/capture/test_shape.py
git commit -m "feat(capture): JSON shape inference, column union, address normalization"
```

---

### Task 6: Bounded stub builder

**Files:**
- Create: `agent_core/agent_core/capture/stub.py`
- Test: `agent_core/tests/capture/test_stub.py`

**Interfaces:**
- Consumes: `columns` from Task 5.
- Produces: `build_stub(*, worker: str, ref: str, rows: int, summary: str, body_bytes: int, cols: list[str], max_bytes: int = 512) -> str` (returns serialized JSON string; guaranteed `<= max_bytes` UTF-8).

- [ ] **Step 1: Write the failing test**

```python
# agent_core/tests/capture/test_stub.py
import json
from agent_core.capture.stub import build_stub


def test_stub_is_hard_bounded_and_shows_shape_not_content():
    stub = build_stub(worker="frida", ref="a1b2c3", rows=1, summary="read_memory: 65536 bytes @ 0x401000",
                      body_bytes=65536, cols=["address", "size", "hex", "extra1", "extra2", "extra3"])
    assert len(stub.encode("utf-8")) <= 512
    doc = json.loads(stub)
    assert doc["captured"]["ref"] == "a1b2c3"
    assert 'read_capture(ref="a1b2c3")' in doc["hint"]
    # No raw payload re-inlined: the 65536-byte body must not appear.
    assert "hex" not in json.dumps(doc.get("captured", {}).get("preview", ""))
    assert "+3 more" in " ".join(doc["captured"]["columns"]) or len(doc["captured"]["columns"]) <= 4


def test_stub_never_exceeds_budget_on_long_summary():
    stub = build_stub(worker="ghidra", ref="ffff", rows=999, summary="x" * 5000,
                      body_bytes=999999, cols=["a"] * 200)
    assert len(stub.encode("utf-8")) <= 512
    json.loads(stub)  # still valid JSON
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/capture/test_stub.py -v`
Expected: FAIL — `No module named 'agent_core.capture.stub'`.

- [ ] **Step 3: Write minimal implementation**

```python
# agent_core/agent_core/capture/stub.py
from __future__ import annotations

import json


def _clip(s: str, n: int) -> str:
    b = s.encode("utf-8")
    if len(b) <= n:
        return s
    return b[:n].decode("utf-8", "ignore")


def build_stub(*, worker: str, ref: str, rows: int, summary: str, body_bytes: int,
               cols: list[str], max_bytes: int = 512) -> str:
    shown = cols[:4]
    if len(cols) > 4:
        shown = shown + [f"+{len(cols) - 4} more"]
    doc = {
        "summary": _clip(summary, 160),
        "captured": {
            "worker": worker,
            "ref": ref,
            "rows": rows,
            "columns": shown,
            "shape": f"{rows} row(s); body {body_bytes}B (elided)",
        },
        "hint": f'read_capture(ref="{ref}")',
    }
    blob = json.dumps(doc)
    if len(blob.encode("utf-8")) <= max_bytes:
        return blob
    # Fallback: drop columns, then clip summary harder, guaranteeing the bound.
    doc["captured"]["columns"] = [f"{len(cols)} cols"]
    doc["summary"] = _clip(summary, 60)
    blob = json.dumps(doc)
    if len(blob.encode("utf-8")) <= max_bytes:
        return blob
    return json.dumps({"captured": {"worker": worker, "ref": ref, "rows": rows},
                       "hint": f'read_capture(ref="{ref}")'})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/capture/test_stub.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add agent_core/capture/stub.py tests/capture/test_stub.py
git commit -m "feat(capture): hard-bounded shape-not-content stub builder"
```

---

### Task 7: CaptureLayer collaborator (store ≠ substitute)

**Files:**
- Create: `agent_core/agent_core/capture/layer.py`
- Test: `agent_core/tests/capture/test_layer.py`

**Interfaces:**
- Consumes: `CaptureStore` (Task 1/3), `infer_rows`/`columns`/`normalize_addrs`/`is_substantial` (Task 5), `build_stub` (Task 6).
- Produces:
  - `stringify_result(result: Any) -> str` — flatten a CallToolResult-shaped object to text.
  - `CaptureLayer(store, *, inline_budget: int, launch_ts: float)`
  - `CaptureLayer.maybe_substitute(worker: str, tool: str, result: Any, *, substitute: bool, session_id: str | None = None) -> Any` — always stores substantial results; returns a stub-bearing `_TextResult` only when `substitute` and over budget, else returns the original `result` untouched.

- [ ] **Step 1: Write the failing test**

```python
# agent_core/tests/capture/test_layer.py
import json
import pytest
from agent_core.capture.store import CaptureStore
from agent_core.capture.layer import CaptureLayer, stringify_result


class _Block:
    type = "text"
    def __init__(self, text): self.text = text


class _Result:
    def __init__(self, text): self.isError = False; self.content = [_Block(text)]


def _layer(budget=200):
    return CaptureLayer(CaptureStore.open_memory(), inline_budget=budget, launch_ts=1.0)


def test_small_result_passes_through_unsubstituted():
    layer = _layer()
    r = _Result(json.dumps([{"a": 1}]))
    out = layer.maybe_substitute("frida", "t", r, substitute=True)
    assert out is r  # under budget -> model sees it verbatim


def test_oversized_result_is_substituted_with_bounded_stub():
    layer = _layer(budget=50)
    big = json.dumps([{"hex": "ab" * 500}])
    out = layer.maybe_substitute("frida", "read_memory", _Result(big), substitute=True)
    text = stringify_result(out)
    assert len(text.encode("utf-8")) <= 512
    doc = json.loads(text)
    ref = doc["captured"]["ref"]
    # Full body retrievable from the store the layer wrote to.
    assert json.loads(layer.store.get(ref)["body"])[0]["hex"] == "ab" * 500


def test_operator_path_stores_but_never_substitutes():
    layer = _layer(budget=10)
    big = json.dumps([{"hex": "ab" * 500}])
    r = _Result(big)
    out = layer.maybe_substitute("frida", "enumerate_processes", r, substitute=False)
    assert out is r                     # operator sees the real payload
    assert len(layer.store.recent()) == 1  # ...but it was still stored
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/capture/test_layer.py -v`
Expected: FAIL — `No module named 'agent_core.capture.layer'`.

- [ ] **Step 3: Write minimal implementation**

```python
# agent_core/agent_core/capture/layer.py
from __future__ import annotations

import json
from typing import Any

from agent_core.capture.store import CaptureStore, CaptureRecord
from agent_core.capture.shape import infer_rows, columns, normalize_addrs, is_substantial
from agent_core.capture.stub import build_stub


class _TextResult:
    """CallToolResult-shaped stand-in carrying the substituted stub text."""
    def __init__(self, text: str) -> None:
        self.isError = False

        class _Block:
            type = "text"
        b = _Block()
        b.text = text
        self.content = [b]


def stringify_result(result: Any) -> str:
    parts = []
    for block in getattr(result, "content", []) or []:
        if getattr(block, "type", None) == "text":
            parts.append(getattr(block, "text", ""))
    return "".join(parts)


class CaptureLayer:
    def __init__(self, store: CaptureStore, *, inline_budget: int, launch_ts: float) -> None:
        self.store = store
        self._budget = inline_budget
        self._launch_ts = launch_ts

    def maybe_substitute(self, worker: str, tool: str, result: Any, *, substitute: bool,
                         session_id: str | None = None) -> Any:
        if getattr(result, "isError", False):
            return result
        text = stringify_result(result)
        try:
            value = json.loads(text)
        except (ValueError, TypeError):
            value = text  # opaque blob -> degenerate row
        rows = infer_rows(value)
        body_bytes = len(text.encode("utf-8"))
        if not is_substantial(value, rows, body_bytes, self._budget):
            return result
        ref = self.store.write(CaptureRecord(
            worker=worker, tool=tool, session_id=session_id, launch_ts=self._launch_ts,
            summary=f"{tool}: {len(rows)} row(s)", body=text if isinstance(text, str) else json.dumps(value),
            rows=len(rows), addrs=normalize_addrs(text),
        ))
        if not substitute or body_bytes <= self._budget:
            return result  # stored, but the caller sees the real payload
        stub = build_stub(worker=worker, ref=ref, rows=len(rows),
                          summary=f"{tool}: {len(rows)} row(s)", body_bytes=body_bytes,
                          cols=columns(rows))
        return _TextResult(stub)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/capture/test_layer.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add agent_core/capture/layer.py tests/capture/test_layer.py
git commit -m "feat(capture): CaptureLayer with store-not-substitute split"
```

---

### Task 8: Wire CaptureLayer into RiskAwareToolPool (opt-in, capture flag)

**Files:**
- Modify: `agent_core/agent_core/workers/risk_pool.py:39-104`
- Test: `agent_core/tests/workers/test_risk_pool_capture.py`

**Interfaces:**
- Consumes: `CaptureLayer` (Task 7).
- Produces: `RiskAwareToolPool.__init__` gains `capture_layer: CaptureLayer | None = None`; `call_tool` gains `capture: bool = True`. When `capture_layer` is set and the dispatch succeeded, the returned result passes through `capture_layer.maybe_substitute(worker, tool, result, substitute=capture)`.

- [ ] **Step 1: Write the failing test**

```python
# agent_core/tests/workers/test_risk_pool_capture.py
import json
import pytest
from agent_core.workers.risk_pool import RiskAwareToolPool
from agent_core.capture.store import CaptureStore
from agent_core.capture.layer import CaptureLayer, stringify_result


class _Block:
    type = "text"
    def __init__(self, text): self.text = text


class _Result:
    def __init__(self, text): self.isError = False; self.content = [_Block(text)]


class _InnerPool:
    async def call_tool(self, worker, tool, arguments):
        return _Result(json.dumps([{"hex": "ab" * 2000}]))  # oversized


def _pool(layer):
    # Low-risk path: construct with a gate that returns 'low' so no approval needed.
    from agent_core.workers.risk import RiskGate
    from agent_core.workers.audit import AuditLog
    from agent_core.workers.tool_approval import ToolApprovalRegistry
    pool = RiskAwareToolPool(
        inner=_InnerPool(), specs={}, risk_gate=RiskGate.permissive() if hasattr(RiskGate, "permissive") else RiskGate({}),
        approval_registry=ToolApprovalRegistry(), audit_log=AuditLog.memory() if hasattr(AuditLog, "memory") else AuditLog(),
        capture_layer=layer,
    )
    return pool


@pytest.mark.asyncio
async def test_model_path_receives_stub():
    layer = CaptureLayer(CaptureStore.open_memory(), inline_budget=100, launch_ts=1.0)
    pool = _pool(layer)
    result = await pool.call_tool("frida", "read_memory", {}, ctx=None)  # capture defaults True
    doc = json.loads(stringify_result(result))
    assert "captured" in doc


@pytest.mark.asyncio
async def test_operator_path_receives_full_payload():
    layer = CaptureLayer(CaptureStore.open_memory(), inline_budget=100, launch_ts=1.0)
    pool = _pool(layer)
    result = await pool.call_tool("frida", "enumerate_processes", {}, ctx=None, capture=False)
    assert "ab" * 2000 in stringify_result(result)   # not substituted
    assert len(layer.store.recent()) == 1            # but stored
```

> Implementer note: the exact constructor args for `RiskGate`/`AuditLog`/`ToolApprovalRegistry` may differ; adjust the `_pool` helper to the real low-risk construction used elsewhere in `tests/workers/`. The behavioral assertions are what matter.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/workers/test_risk_pool_capture.py -v`
Expected: FAIL — `__init__() got an unexpected keyword argument 'capture_layer'`.

- [ ] **Step 3: Write minimal implementation**

In `risk_pool.py`, add the constructor param (after `send_message`):

```python
        send_message: SendMessage | None = None,
        capture_layer: "CaptureLayer | None" = None,
    ) -> None:
        ...
        self._send = send_message
        self._capture = capture_layer
```

Add the import guard near the top:

```python
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from agent_core.capture.layer import CaptureLayer
```

Change `call_tool`'s signature and its return to run capture on success:

```python
    async def call_tool(self, worker: str, tool: str, arguments: dict[str, Any], ctx: Any = None,
                        capture: bool = True):
        ...
        result = await self._execute_and_audit(
            worker, tool, arguments, snapshot, declared, effective, gate_override,
            session_note, tier_source,
        )
        if self._capture is not None and not getattr(result, "isError", False):
            session_id = arguments.get("session_id") if isinstance(arguments, dict) else None
            return self._capture.maybe_substitute(worker, tool, result, substitute=capture,
                                                  session_id=session_id)
        return result
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/workers/test_risk_pool_capture.py -v`
Then the full worker suite to prove no regression: `python3 -m pytest tests/workers/ -v`
Expected: PASS; existing worker tests still green (capture defaults on but is a no-op when `capture_layer is None`, which is how every existing test constructs the pool).

- [ ] **Step 5: Commit**

```bash
git add agent_core/workers/risk_pool.py tests/workers/test_risk_pool_capture.py
git commit -m "feat(capture): wire optional CaptureLayer into RiskAwareToolPool with capture flag"
```

---

### Task 9: Retrieval tools (opt-in, dead-ref sentinel, recent mode)

**Files:**
- Create: `agent_core/agent_core/capture/tools.py`
- Test: `agent_core/tests/capture/test_tools.py`

**Interfaces:**
- Consumes: `CaptureStore` via `ctx.agent.capture_store`; `Tool` base (`agent_core/tools/base.py`).
- Produces: `SearchCapture(Tool)` (name `search_capture`) and `ReadCapture(Tool)` (name `read_capture`), each `requires = ("capture_store",)`. Not added to `BUILTIN_TOOLS`.

- [ ] **Step 1: Write the failing test**

```python
# agent_core/tests/capture/test_tools.py
import json
import pytest
from agent_core.capture.store import CaptureStore, CaptureRecord
from agent_core.capture.tools import SearchCapture, ReadCapture


class _Agent:
    def __init__(self, store): self.capture_store = store


class _Ctx:
    def __init__(self, agent): self.agent = agent


def _store_with_row():
    store = CaptureStore.open_memory()
    ref = store.write(CaptureRecord(worker="frida", tool="read_memory", session_id="s1",
                                    launch_ts=1.0, summary="big", body=json.dumps([{"hex": "abcd"}]),
                                    rows=1, addrs=[]))
    return store, ref


def test_requires_capture_store():
    assert SearchCapture.requires == ("capture_store",)
    assert ReadCapture.requires == ("capture_store",)


@pytest.mark.asyncio
async def test_read_capture_returns_body():
    store, ref = _store_with_row()
    out = await ReadCapture().run({"ref": ref}, _Ctx(_Agent(store)))
    assert "abcd" in out


@pytest.mark.asyncio
async def test_read_capture_dead_ref_is_sentinel_not_exception():
    store, _ = _store_with_row()
    out = await ReadCapture().run({"ref": "deadbeef"}, _Ctx(_Agent(store)))
    doc = json.loads(out)
    assert doc["expired"] is True
    assert "search_capture" in doc["hint"]


@pytest.mark.asyncio
async def test_search_capture_recent_mode_on_empty_args():
    store, ref = _store_with_row()
    out = await SearchCapture().run({}, _Ctx(_Agent(store)))
    doc = json.loads(out)
    assert any(r["ref"] == ref for r in doc["recent"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/capture/test_tools.py -v`
Expected: FAIL — `No module named 'agent_core.capture.tools'`.

- [ ] **Step 3: Write minimal implementation**

```python
# agent_core/agent_core/capture/tools.py
from __future__ import annotations

import json
from typing import Any

from agent_core.tools.base import Tool


class SearchCapture(Tool):
    name = "search_capture"
    description = (
        "Search captured tool results. Captures persist on disk; find them by "
        "SEARCHING, do not rely on remembering a ref. Call with no args for the "
        "most recent captures. worker is optional (defaults to all)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "full-text query"},
            "worker": {"type": "string", "description": "optional worker filter (frida/ghidra/...)"},
            "field": {"type": "string", "description": "dotted json path to filter on"},
            "contains": {"type": "string", "description": "substring the field must contain"},
            "limit": {"type": "integer"},
        },
    }
    requires = ("capture_store",)

    async def run(self, args: dict, ctx: Any) -> str:
        store = ctx.agent.capture_store
        text = args.get("text", "") or ""
        worker = args.get("worker", "") or ""
        field = args.get("field", "") or ""
        contains = args.get("contains", "") or ""
        limit = int(args.get("limit") or 50)
        if not any([text, worker, field, contains]):
            return json.dumps({"recent": store.recent(limit=min(limit, 20))})
        hits = store.search(text=text, worker=worker, field=field, contains=contains, limit=limit)
        lean = [{"ref": h["ref"], "worker": h["worker"], "tool": h["tool"],
                 "rows": h["rows"], "summary": h["summary"]} for h in hits]
        return json.dumps({"matches": lean, "returned": len(lean)})


class ReadCapture(Tool):
    name = "read_capture"
    description = (
        "Read one captured result by its ref (from a search_capture match or a "
        "captured-result stub). Returns a byte window; use offset to page."
    )
    parameters = {
        "type": "object",
        "properties": {
            "ref": {"type": "string"},
            "offset": {"type": "integer"},
            "byte_budget": {"type": "integer"},
        },
        "required": ["ref"],
    }
    requires = ("capture_store",)

    async def run(self, args: dict, ctx: Any) -> str:
        store = ctx.agent.capture_store
        ref = args.get("ref", "")
        row = store.get(ref)
        if row is None:
            return json.dumps({"expired": True,
                               "hint": "capture expired or unknown ref; use search_capture to find current data"})
        offset = int(args.get("offset") or 0)
        budget = int(args.get("byte_budget") or 3072)
        body = row["body"] or ""
        window = body[offset:offset + budget]
        next_offset = offset + len(window)
        return json.dumps({
            "ref": ref, "worker": row["worker"], "rows": row["rows"],
            "offset": offset, "next_offset": next_offset,
            "truncated": next_offset < len(body), "text": window,
        })
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/capture/test_tools.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add agent_core/capture/tools.py tests/capture/test_tools.py
git commit -m "feat(capture): opt-in search_capture/read_capture tools with dead-ref sentinel"
```

---

### Task 10: Project store resolution + config field

**Files:**
- Create: `agent_core/agent_core/capture/project.py`
- Modify: `agent_core/agent_core/config.py:22-45` (add `project_marker` field)
- Test: `agent_core/tests/capture/test_project.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `BaseConfig.project_marker: str | None = None`
  - `resolve_capture_db(cwd: Path, marker: str | None, *, home: Path, xdg_state: Path, channel_id: str) -> tuple[Path, bool]` — returns `(db_path, is_project)`. Git-style walk-up from `cwd` for `<marker>/`, with a `$HOME` ceiling; falls back to a per-launch path under `xdg_state` keyed by `channel_id`.

- [ ] **Step 1: Write the failing test**

```python
# agent_core/tests/capture/test_project.py
from pathlib import Path
from agent_core.capture.project import resolve_capture_db
from agent_core.config import BaseConfig


def test_config_has_project_marker_default_none():
    assert BaseConfig().project_marker is None


def test_walk_up_finds_marker(tmp_path):
    home = tmp_path / "home"
    proj = home / "work" / "acme"
    (proj / ".pare").mkdir(parents=True)
    sub = proj / "src" / "deep"
    sub.mkdir(parents=True)
    db, is_project = resolve_capture_db(sub, ".pare", home=home,
                                        xdg_state=tmp_path / "state", channel_id="c1")
    assert is_project is True
    assert db == proj / ".pare" / "capture.db"


def test_marker_none_means_no_project(tmp_path):
    db, is_project = resolve_capture_db(tmp_path, None, home=tmp_path,
                                        xdg_state=tmp_path / "state", channel_id="c1")
    assert is_project is False
    assert (tmp_path / "state") in db.parents


def test_home_ceiling_is_not_a_project(tmp_path):
    home = tmp_path / "home"
    (home / ".pare").mkdir(parents=True)
    db, is_project = resolve_capture_db(home, ".pare", home=home,
                                        xdg_state=tmp_path / "state", channel_id="c1")
    assert is_project is False   # a .pare exactly at $HOME is ignored
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/capture/test_project.py -v`
Expected: FAIL — `No module named 'agent_core.capture.project'` / `BaseConfig` has no `project_marker`.

- [ ] **Step 3: Write minimal implementation**

Add to `BaseConfig` (in `config.py`, after `scratchpad_max_bytes`):

```python
    project_marker: str | None = None
```

```python
# agent_core/agent_core/capture/project.py
from __future__ import annotations

from pathlib import Path


def resolve_capture_db(cwd: Path, marker: str | None, *, home: Path, xdg_state: Path,
                       channel_id: str) -> tuple[Path, bool]:
    """Resolve the capture db path. Walk up from cwd for `marker`, stopping
    before $HOME (a marker exactly at $HOME is ignored). Fall back to a
    per-launch path under xdg_state keyed by channel_id."""
    if marker:
        cwd = Path(cwd).resolve()
        home = Path(home).resolve()
        for d in [cwd, *cwd.parents]:
            if d == home or d == d.parent:  # $HOME ceiling / filesystem root
                break
            if (d / marker).is_dir():
                return d / marker / "capture.db", True
    fallback = Path(xdg_state) / "captures" / f"{channel_id}.db"
    return fallback, False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/capture/test_project.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add agent_core/capture/project.py agent_core/config.py tests/capture/test_project.py
git commit -m "feat(capture): project store resolution with HOME ceiling + project_marker config"
```

---

### Task 11: Full-suite green + capability export

**Files:**
- Modify: `agent_core/agent_core/capture/__init__.py` (export the public surface)
- Test: run the whole suite.

**Interfaces:**
- Produces: `from agent_core.capture import CaptureStore, CaptureRecord, CaptureLayer, SearchCapture, ReadCapture, resolve_capture_db` — the surface Plan 2 imports.

- [ ] **Step 1: Write the failing test**

```python
# agent_core/tests/capture/test_public_surface.py
def test_public_surface_importable():
    from agent_core.capture import (
        CaptureStore, CaptureRecord, CaptureLayer,
        SearchCapture, ReadCapture, resolve_capture_db,
    )
    assert all([CaptureStore, CaptureRecord, CaptureLayer, SearchCapture, ReadCapture, resolve_capture_db])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/capture/test_public_surface.py -v`
Expected: FAIL — `ImportError`.

- [ ] **Step 3: Write minimal implementation**

```python
# agent_core/agent_core/capture/__init__.py
from agent_core.capture.store import CaptureStore, CaptureRecord
from agent_core.capture.layer import CaptureLayer, stringify_result
from agent_core.capture.tools import SearchCapture, ReadCapture
from agent_core.capture.project import resolve_capture_db

__all__ = [
    "CaptureStore", "CaptureRecord", "CaptureLayer", "stringify_result",
    "SearchCapture", "ReadCapture", "resolve_capture_db",
]
```

- [ ] **Step 4: Run the full suite**

Run: `python3 -m pytest -q`
Expected: PASS across the repo — the new capture suite green, and every pre-existing test (especially `tests/workers/`) still green (capture is opt-in; no agent yet constructs a `capture_layer` or lists the retrieval tools).

- [ ] **Step 5: Commit**

```bash
git add agent_core/capture/__init__.py tests/capture/test_public_surface.py
git commit -m "feat(capture): export public capture surface for consumers"
```

---

## Self-Review

**Spec coverage (§ → task):**
- §4 unified capture + guard rules → Task 5 (`infer_rows`).
- §5 schema-on-read engine, FTS phrase-escape, addrs column, blob spill → Tasks 1, 2, 3, 5.
- §5 promoted/expression-index-for-hot-keys → **recorded, not built in Plan 1** (deferred to a follow-up; the `field=` path uses `json_extract` and is correct without it, just unindexed — matches spec §11's "static at first" note). Flagged here so it isn't mistaken for a gap.
- §6 store-vs-substitute + bounded stub + window-derived budget → Tasks 6, 7 (the budget value is *injected* into `CaptureLayer`; PARE computes it from `history_depth`/context in Plan 2 — agent_core takes the byte number).
- §7 opt-in retrieval tools, dead-ref sentinel, recent mode, ref-first → Task 9.
- §8 project discovery, `$HOME` ceiling, `project_marker`, XDG fallback, `busy_timeout` → Tasks 1 (busy_timeout), 10. Advisory lockfile → **deferred to Plan 2** (it belongs with the daemon that opens the store long-lived; noted, not silently dropped).
- §9 durable store, blob-aware purge, reachability-aware retention (`protected_refs`), `0o700/0o600` → Tasks 2, 4. Auto `.pare/.gitignore` → **Plan 2** (written by the PARE-side store opener that owns the project dir).
- §10 migration → Plan 2.

**Placeholder scan:** every code step carries real code; the one "adjust to real constructors" note (Task 8 `_pool` helper) is unavoidable because the low-risk `RiskGate`/`AuditLog` construction lives in existing test helpers — the behavioral assertions are exact.

**Type consistency:** `ref: str` everywhere (store, layer, stub, tools); `CaptureRecord` field names match across Tasks 1/7/9; `maybe_substitute(..., substitute=)` matches its caller in Task 8; `resolve_capture_db(...) -> (Path, bool)` consumed only in Plan 2.

**Deferred-to-Plan-2 (explicit, so nothing reads as lost):** window→byte budget computation, `.pare/.gitignore`, advisory lockfile, `/snapshot` repoint, protocol `cwd` field, frida teardown, expression-index promotion.
