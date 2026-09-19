# Move `CaptureLayer`'s write off the event loop — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop `CaptureLayer.maybe_substitute → store.write` from stalling the daemon event loop on every tool result, by moving the actual sqlite write onto a dedicated writer thread inside `agent_core.CaptureStore`. PARE's Task 3 writer becomes a thin wrapper.

**Architecture:** `CaptureStore` gains an owned writer thread with its own sqlite connection (per-store, opened on that thread — sqlite is thread-affine). `write` and `get` become `async def`. Pending writes tracked in a `dict[ref, Future]`; `get(ref)` awaits any pending write of that specific ref, so a read-after-write of a just-issued ref never sees a false "expired". In-memory stores skip the writer thread (`:memory:` databases exist only on their opener's connection). Cross-repo release: **agent_core PR merges + tags first**, then PARE PR consumes the tag.

**Tech Stack:** Python 3.12+; `sqlite3` (stdlib) with WAL + `busy_timeout=5000`; `asyncio`; `threading` + `queue.SimpleQueue` for the writer; `pytest` + `pytest-asyncio` with `asyncio_mode = "auto"` in both repos.

**Spec:** [`docs/superpowers/specs/2026-09-19-on-loop-capture-write-design.md`](../specs/2026-09-19-on-loop-capture-write-design.md) — read it alongside this plan, including the "Correction 2026-09-19" note that expanded the blast radius from what the plan's earlier draft assumed.

## Global Constraints

- `agent_core` version bumps 1.10.0 → 1.11.0 in this work. Any commit that changes the public interface of `CaptureStore` or `CaptureLayer` must also update `CHANGELOG.md` in the same task.
- `agent_core` `requires-python = ">=3.12"`, PARE the same. Do not change either.
- `asyncio_mode = "auto"` in both repos' `pyproject.toml` — async tests need no decorator.
- **No `agent_core` change may be merged until PARE's full test suite passes against it** — the "consumer-suite gate" from spec §5. Run PARE against a source-checkout install of the agent_core branch (not the pinned release), and only tag agent_core after that suite is green. Per CLAUDE.md verbatim: "When changing a library, run the consuming project's suite before releasing."
- **sqlite3 connections are thread-affine (`check_same_thread=True` by default).** A connection opened on thread A cannot be used from thread B — it raises `sqlite3.ProgrammingError`. Task 3 confirmed this empirically. Every writer thread in this plan opens *and closes* its own connection, on that thread.
- **In-memory stores (`CaptureStore.open_memory()`) must not start a writer thread.** `:memory:` databases exist only on the connection that opened them; a writer thread with its own `sqlite3.connect(":memory:")` gets an empty different database, silently. `open_memory` writes run inline on the caller's thread.
- Assertions on properties, not literals — snapshots of exact strings rot on the next legitimate change. The "assert relationship" shape from the pare-tui plan applies here identically.
- **Verify-failing-first is mandatory for the discriminating tests** — Task A1's pending-writes discriminator especially. A test that passes against both correct and broken versions is testing nothing; script the mutation, watch it fail, restore.

## Repo layout

Two repos, two branches, released in sequence:

- `/mnt/secondary/projects/agent_core` on a new branch `feat/capture-async-writer` (or similar), targets agent_core `main`.
- `/mnt/secondary/projects/PARE` on a new branch `feat/pin-agent-core-1.11.0` (or similar), targets PARE `main`.

Both are separate git remotes. The PARE branch is created AFTER the agent_core PR merges + tags 1.11.0.

## File Structure

### agent_core (Part A)

| File | Responsibility |
|---|---|
| `agent_core/capture/store.py` | `CaptureStore` gains writer thread state (queue, thread, pending-writes map, per-thread connection). `write`/`get`/`close` become async or gain behavior. `open` starts the thread; `open_memory` does not. |
| `agent_core/capture/layer.py` | `maybe_substitute` becomes `async def`, awaits `store.write`. Body otherwise unchanged. |
| `agent_core/workers/risk_pool.py` | Line 435: prepend `await` to the `maybe_substitute` call. |
| `agent_core/capture/tools.py` | Line 63: `ReadCapture.run` (already async) gains `await` on `store.get(ref)`. |
| `agent_core/tests/capture/test_store.py` | Existing `store.get`/`store.write` sites become `await`. New async tests for the writer thread and pending-writes ordering land here (or a sibling file). |
| `agent_core/tests/capture/test_store_disk.py` | Same, for disk-backed test. |
| `agent_core/tests/capture/test_retention.py` | Same. |
| `agent_core/tests/capture/test_search.py` | Same. |
| `agent_core/tests/capture/test_layer.py` | `maybe_substitute` calls become `await`; sync tests become `async def`. |
| `agent_core/tests/capture/test_tools.py` | Audit — `store.write` calls need `await`, tests holding a ref lookup after write need to be async. |
| `agent_core/CHANGELOG.md` | New entry under `[1.11.0]`. |
| `agent_core/pyproject.toml` | Version bump. |

### PARE (Part B)

| File | Responsibility |
|---|---|
| `pyproject.toml` | Pin `agent_core @ git+…@v1.11.0`. |
| `pare/capture_store.py` | Remove writer thread, queue, pending-writes state, `_writer_loop`, `_ensure_writer_thread`. `write_async` becomes a thin wrapper: resolves the store, `await`s `store.write(record)`. `close_all` cascades to `store.close()`. `daemon.lock` flock stays (orthogonal). |
| `pare/agent.py` | `_pane_activity_worker` unchanged in shape — still awaits `_capture_stores.write_async`, which now awaits `store.write`. `handle_chat` gains `await` on `candidate_classes(...)` at `:771`. |
| `pare/handback.py` | `_rows_from` and `candidate_classes` become `async def`; `capture_store.get(ref)` gains `await`. |
| `pare/commands/snapshot.py` | Three `store.get(...)` calls inside `Snapshot.run` gain `await`. `_render` stays sync. |
| `tests/test_capture_store_manager.py` | Adjust to the wrapper shape — the writer-thread-internals tests go; the public-behavior tests stay. |
| `tests/test_handback.py` | ~5 test cases calling `candidate_classes` convert to `async def` + `await`. |
| `tests/test_tui_handle_other.py` | Task 3's writer-thread tests audit — most survive because they mock `write_async`, which still exists. |

---

## Part A — agent_core

### Task A1: `CaptureStore` grows a writer thread; `write` becomes async

**Files:**
- Modify: `agent_core/capture/store.py` (add writer thread infra to `CaptureStore`; convert `write` to `async def`; ref pre-allocation on caller's thread; pending-writes map; `open`/`open_memory` diverge on thread start).
- Test: `agent_core/tests/capture/test_store.py` (extend), plus a new discriminating test.

**Interfaces:**
- Consumes: `sqlite3` (stdlib), `secrets` (already imported), `threading`, `queue`, `asyncio`.
- Produces (used by A2, A3, A4, and Part B):
  - `async def CaptureStore.write(record: CaptureRecord) -> str` — returns `ref` immediately after enqueueing (does NOT await the write's future).
  - `async def CaptureStore.get(ref: str) -> dict[Any, Any] | None` — awaits any pending write for `ref` before the sqlite SELECT.
  - `def CaptureStore.close() -> None` — sends the shutdown sentinel, bounded-joins the writer thread (5 s), logs a warning naming the count if drain timed out.
  - The `pending_writes: dict[str, asyncio.Future]` is a private attribute; tests may inspect it structurally but production callers must not.
  - `open_memory()` does NOT start a writer thread. Its `write` runs the sqlite body inline and returns a completed future for the pending-writes bookkeeping (or skips the bookkeeping entirely). Contract-uniform: the async signature stays.

**No implementation code in this task, deliberately** — this is the highest-risk concurrency change in the plan, and code written into prose here would be transcribed past its defects. Write against the real file. §2.1–§2.5 of the spec give signatures and semantics; §2.6 gives the failure paths.

**Design requirements (from spec §2):**
1. Ref generation (`secrets.token_hex(8)`, currently at `store.py:72`) moves to the caller's thread inside `write`. Fast, no I/O, no thread hop.
2. `write` creates `future = loop.create_future()`, sets `pending_writes[ref] = future`, enqueues `(ref, record, future, loop)`, returns `ref`. No `await` on the future.
3. Writer thread loop opens its per-store `sqlite3.Connection` **on this thread** on first item — never handed off. `check_same_thread=True` is the default and stays.
4. Writer runs the current insert+FTS+commit body (currently `store.py:77-102`, adapted to take `ref` as a parameter rather than generating it). On success: `loop.call_soon_threadsafe(future.set_result, None)`. On exception: `loop.call_soon_threadsafe(future.set_exception, exc)`. Always: `pending_writes.pop(ref, None)` *after* the future is set (so a `get` racing with completion never sees a stale entry).
5. `close()` puts a `None` sentinel and `thread.join(timeout=5.0)`. If the thread is still alive after the timeout, log a warning naming `queue.qsize()`.
6. `open_memory` skips all of the above: `write` runs the sqlite body inline on the caller's thread and returns the ref. No writer thread, no pending-writes entries (or entries that are already completed by the time `write` returns).

**What the tests must discriminate** (spec §4):

1. `write` returns a ref that a concurrent `get(ref)` sees after the writer finishes.
2. **The critical one for D4:** an interleaved `get(ref)` BEFORE the write commits `await`s the pending-writes future rather than returning `None`. **Must be verified failing** against a version of `get` that skips the pending-writes check and reads sqlite directly. A test that passes both ways is testing nothing, and this is exactly the failure the design exists to prevent.
3. `write` returns before the write commits. Time `write` end-to-end with a deliberately slow writer; assert the wall time is *less than* the write's duration.
4. `open_memory` writes and reads inline: `assert store._writer_thread is None` (or equivalent structural check) after a `write`; the read succeeds without a writer thread ever running.

- [ ] **Step 1:** Read `agent_core/capture/store.py` in full. Identify the insert+FTS+commit body (currently ~lines 77-102 within `write`). This is the code that must move to the writer thread; the rest of `write` (ref generation, future creation, enqueue) stays on the caller's thread.

- [ ] **Step 2:** Design a **test seam** for slowing the writer thread. This is where the discriminating test lives or dies: without a reliable way to pause the writer between "record enqueued" and "sqlite commit finished", the race window either doesn't exist (writer finishes before `get` runs, test passes even against a broken `get`) or is timing-dependent (test flakes on slow CI). Pick one of two mechanisms — do NOT freestyle a third without saying why:

  a. **Injected pre-write hook.** Give the `CaptureStore` constructor an optional `_pre_write_hook: Callable[[], None] | None = None` parameter. The writer thread calls it at the top of each insert loop iteration, on the writer thread. Tests pass a `threading.Event` wait as the hook. Production omits it (default `None`, no overhead). Explicit test seam, no monkeypatching.

  b. **Monkeypatched insert method.** Name the writer thread's inner insert body as a private method (e.g. `_do_insert_on_writer_thread`), and have the test monkeypatch it with a wrapper that waits on a `threading.Event` before delegating to the original. No production surface, but couples the test to the implementation name.

  Either mechanism works. Option (a) is more honest (the seam is declared, not implied) and easier to read; option (b) keeps production API smaller. Pick one, name it in the commit, and use it consistently across the four new tests in this task.

- [ ] **Step 3:** Write the discriminating test for pending-writes ordering. Rough shape (uses seam option (a); adjust to (b) if that's what you chose):

```python
# agent_core/tests/capture/test_store.py (or test_store_async.py)
import asyncio
import threading

from agent_core.capture import CaptureStore, CaptureRecord


async def test_get_awaits_a_still_pending_write(tmp_path):
    """D4: a get() of a ref whose write has not yet committed must await
    the pending-writes future -- NOT return None (which ReadCapture.run
    renders to the model as "expired capture", a lie).

    Uses the pre-write hook (chosen in Step 2, option a) to pin the
    writer thread inside its insert loop, so the test's get() runs
    while pending_writes[ref] is still unresolved.
    """
    release = threading.Event()  # writer thread waits here until release
    def _pin_writer():
        release.wait(timeout=5.0)

    store = CaptureStore.open(tmp_path / "cap.db", _pre_write_hook=_pin_writer)
    try:
        rec = CaptureRecord(worker="w", tool="t", session_id=None,
                            launch_ts=0.0, summary="", body="hello", rows=0,
                            addrs=[])
        ref = await store.write(rec)  # returns immediately; writer is pinned

        # The writer has NOT committed. A naive get() (SELECT only) returns
        # None. The correct get() awaits pending_writes[ref] first.
        get_task = asyncio.create_task(store.get(ref))
        # Give the get_task a chance to hit the pending-writes await; then
        # release the writer.
        await asyncio.sleep(0.05)
        release.set()
        row = await get_task

        assert row is not None, "get must await the pending write, not return None"
        assert row["body"] == "hello"
    finally:
        release.set()  # in case the assertion fires before we release
        store.close()
```

The hook mechanism is what makes this test's pass/fail meaningful — without it, you cannot reliably distinguish "get awaited the pending write" from "the writer happened to finish first."

- [ ] **Step 4:** Run the test to verify it fails against the current `store.py` (which has no async `write` at all — expect `TypeError: object str can't be used in 'await' expression` or an `AttributeError`).

Run: `cd /mnt/secondary/projects/agent_core && ./.venv/bin/pytest tests/capture/test_store.py::test_get_awaits_a_still_pending_write -v`
Expected: FAIL — the current `write` is sync, so `await store.write(...)` raises.

- [ ] **Step 5:** Implement `CaptureStore`'s writer thread and pending-writes map in `agent_core/capture/store.py`. See §2.1–§2.5 of the spec for signatures and semantics. Do NOT copy code from the spec — it says "Signatures and semantics, not code" for a reason. Write against the real file.

Landmarks:
- Add `import asyncio, threading, queue` at the top.
- Add `_writer_thread`, `_writer_queue`, `_writer_conn` (per-thread), `_pending_writes` as instance attrs.
- Split the current `write` body: keep the ref generation and pre-write bookkeeping on the caller's thread; move the INSERT + FTS + blob-spill + commit body into a helper the writer thread calls.
- Make `write` `async def`. It does not `await` — it just returns the ref after enqueueing. The `async` signature is for the contract.
- Make `get` `async def`. Check `pending_writes` first, `await` the future if present, then do the SELECT.
- `close()` sends the `None` sentinel, `join(timeout=5.0)`, logs a warning if the thread survived the timeout.
- `open()` starts the writer thread lazily on first `write` (or eagerly in `__init__`).
- `open_memory()` sets a flag or skips the thread state entirely; its `write` calls the writer body inline on the caller's thread.

- [ ] **Step 6:** Run the discriminating test against your implementation. Expect PASS.

Run: `./.venv/bin/pytest tests/capture/test_store.py::test_get_awaits_a_still_pending_write -v`
Expected: PASS.

- [ ] **Step 7: The mutation check — mandatory.** Temporarily remove the pending-writes check from `get` (make it read sqlite directly, skipping the `await`). Rerun the same test. Expect FAIL — with `row is None` on the assertion. This proves the test discriminates. Then restore `get` and confirm it passes again. Record what you saw in the commit body.

- [ ] **Step 8:** Add the three other new tests from spec §4 in the same file. Each one:
  - `write` returns a ref that a concurrent `get(ref)` sees after the writer finishes (write, sleep briefly for the writer, get, assert the row).
  - `write` returns before the write commits (time it end-to-end against a deliberately slow writer; assert `elapsed < write_duration`).
  - `open_memory` never starts a writer thread (assert `store._writer_thread is None` or equivalent after `write`).

- [ ] **Step 9:** Run all four tests together and confirm they pass. Then run the full `tests/capture/` folder — some existing tests WILL fail because they still call `store.write(...)` sync. Do not fix those yet; that is Task A2. Just note the count of failures before proceeding.

Run: `./.venv/bin/pytest tests/capture/ -v --tb=no -q`
Expected: the four new tests pass; a subset of existing tests fail on `await` missing or on `TypeError: object str can't be used in 'await' expression`.

- [ ] **Step 10: Commit.**

```bash
git -C /mnt/secondary/projects/agent_core add agent_core/capture/store.py tests/capture/test_store.py
git -C /mnt/secondary/projects/agent_core commit -m "feat(capture): CaptureStore.write off the event loop, ordering via pending-writes map

Moves the sqlite INSERT+FTS+commit body onto a dedicated per-store
writer thread with its own sqlite3.Connection opened on that thread
(sqlite is thread-affine; Task 3 confirmed this empirically). write()
becomes async def -- returns the pre-allocated ref immediately after
enqueueing, without awaiting the writer's future.

get() becomes async def and awaits pending_writes[ref] if a write for
that ref is still in flight, so a read-after-write of a just-issued
ref never sees a false 'expired capture'.

open_memory() skips the writer thread -- a :memory: database exists
only on its opener's connection, so a separate writer thread would get
a different empty db. Its write runs inline on the caller's thread;
the async signature stays uniform.

Discriminating test verified by mutation: skipping the pending-writes
check in get() makes test_get_awaits_a_still_pending_write fail with
row is None (naive get returns None where the correct get awaits and
returns the row). Restored, all four new store tests pass.

Task A2 will update every caller (maybe_substitute, risk_pool, tools,
existing tests) to await; those callers currently fail on the sync ->
async signature change. That is deliberate -- staged so each task's
diff stays reviewable."
```

---

### Task A2: `CaptureLayer.maybe_substitute` becomes async

**Files:**
- Modify: `agent_core/capture/layer.py` (make `maybe_substitute` `async def`; add `await store.write(...)`).
- Test: `agent_core/tests/capture/test_layer.py` (convert ~10 test sites from sync to `async def` + `await`).

**Interfaces:**
- Consumes: `CaptureStore.write` (from A1).
- Produces: `async def CaptureLayer.maybe_substitute(worker, tool, result, *, substitute, session_id=None) -> Any` — same return semantics as today, but async.

- [ ] **Step 1:** Read `agent_core/capture/layer.py` `maybe_substitute` (currently at line 45). Note that it takes `store` from `self.store` (a property that reads `_store_provider`), and calls `ref = store.write(CaptureRecord(...))` at line 62 — that's the one line that changes.

- [ ] **Step 2:** Run the layer tests and observe the failure mode.

Run: `cd /mnt/secondary/projects/agent_core && ./.venv/bin/pytest tests/capture/test_layer.py -v`
Expected: every test failing on `await` missing or on `TypeError: object coroutine can't be used in 'await' expression`.

- [ ] **Step 3:** Convert `maybe_substitute` to `async def` and prepend `await` to the `store.write` call. Every other line in the method stays.

Landmarks:
- Line 45: `def` → `async def`.
- Line 62: `ref = store.write(CaptureRecord(...))` → `ref = await store.write(CaptureRecord(...))`.

- [ ] **Step 4:** Convert `tests/capture/test_layer.py`'s ~10 test call sites. Each test that calls `layer.maybe_substitute(...)` becomes `async def` (`asyncio_mode = "auto"` means no decorator) and `await`s the call. Test at line 60 that does `layer.store.get(ref)` also becomes `await layer.store.get(ref)`.

- [ ] **Step 5:** Run the layer tests. Expect PASS.

Run: `./.venv/bin/pytest tests/capture/test_layer.py -v`
Expected: PASS on every previously-failing test.

- [ ] **Step 6:** Run the whole `tests/capture/` folder — the remaining failures should now be in `test_store.py`, `test_store_disk.py`, `test_retention.py`, `test_search.py`, `test_tools.py`. Note the count.

- [ ] **Step 7: Commit.**

```bash
git -C /mnt/secondary/projects/agent_core add agent_core/capture/layer.py tests/capture/test_layer.py
git -C /mnt/secondary/projects/agent_core commit -m "feat(capture): CaptureLayer.maybe_substitute async, awaits store.write

The layer method now honestly reflects that it does I/O. The body
otherwise unchanged -- ref generation moved into store.write (Task A1),
so this call site is now just await + the store.write call.

Test_layer.py's ~10 sync sites converted to async def + await;
asyncio_mode='auto' means no decorator needed.

Ripple continues in A3 (risk_pool caller) and A4 (tools.py + remaining
capture tests)."
```

---

### Task A3: `RiskAwareToolPool` and `ReadCapture` gain `await`

**Files:**
- Modify: `agent_core/workers/risk_pool.py` (line 435: add `await`).
- Modify: `agent_core/capture/tools.py` (line 63: add `await`).
- Test: existing `tests/workers/` and `tests/capture/test_tools.py`.

**Interfaces:**
- Consumes: `async def CaptureLayer.maybe_substitute` (A2), `async def CaptureStore.get` (A1).
- Produces: no new interfaces; two mechanical one-line fixes.

- [ ] **Step 1:** Read `agent_core/workers/risk_pool.py` around line 435. Confirm the enclosing method is `async def call_tool` (or similar). Add `await` in front of `self._capture.maybe_substitute(...)`.

- [ ] **Step 2:** Read `agent_core/capture/tools.py:60-77`. Confirm `ReadCapture.run` is `async def`. Change `row = store.get(ref)` at `:63` to `row = await store.get(ref)`.

- [ ] **Step 3:** Run the workers and tools tests.

Run: `cd /mnt/secondary/projects/agent_core && ./.venv/bin/pytest tests/workers/ tests/capture/test_tools.py -v --tb=short`
Expected: any test that touched `maybe_substitute` sync or `store.get` sync now needs its own `await`. Fix those in the same task (they are part of the same await ripple).

- [ ] **Step 4:** Update `tests/capture/test_tools.py` — `store.write(...)` and `store.get(...)` calls in tests become `await`, tests become `async def` where needed. Same for any `tests/workers/` test that surfaces the sync-vs-async mismatch.

- [ ] **Step 5:** Rerun the two test folders and confirm green.

Run: `./.venv/bin/pytest tests/workers/ tests/capture/test_tools.py -v`
Expected: PASS.

- [ ] **Step 6: Commit.**

```bash
git -C /mnt/secondary/projects/agent_core add agent_core/workers/risk_pool.py agent_core/capture/tools.py tests/
git -C /mnt/secondary/projects/agent_core commit -m "fix(capture): await the newly-async maybe_substitute and store.get

Two production one-line fixes:
- risk_pool.py:435 awaits maybe_substitute (was: sync call)
- capture/tools.py:63 awaits store.get in ReadCapture.run

Tests in tests/capture/test_tools.py and any tests/workers/ tests that
touched these paths converted to async def + await. asyncio_mode='auto'
means no decorators.

After this commit, tests/capture/{test_store,test_store_disk,test_retention,test_search}
still fail on their remaining sync store.write/store.get sites -- fixed
in A4."
```

---

### Task A4: Convert remaining capture tests to async

**Files:**
- Modify: `agent_core/tests/capture/test_store.py` (existing `store.write`/`store.get` sites — the new ones from A1 already `await`).
- Modify: `agent_core/tests/capture/test_store_disk.py`, `test_retention.py`, `test_search.py`.

**Interfaces:** none. Test-only.

- [ ] **Step 1:** Run the failing tests and inventory what's left.

Run: `cd /mnt/secondary/projects/agent_core && ./.venv/bin/pytest tests/capture/ -v --tb=no`
Expected: failures in `test_store.py` (existing sync `store.write` calls, distinct from the new async ones you added in A1), `test_store_disk.py:15` (`store.get`), `test_store.py:17` (`store.get`), `test_retention.py:16,28,29,39,40` (`store.get`), `test_search.py` (`store.write` in fixture setup).

- [ ] **Step 2:** Convert each site. Rules:
  - If a test function directly awaits, make it `async def` (no decorator; `asyncio_mode="auto"` handles it).
  - If a test uses a fixture that does `store.write` in synchronous setup, either:
    - Convert the fixture to an async fixture (`@pytest_asyncio.fixture` or use `async def` with a plain event loop), OR
    - Move the `store.write` calls out of the fixture and into the test body itself (simpler for one-off writes).
  - `store.get`/`store.write` sites become `await store.get`/`await store.write`.

- [ ] **Step 3:** Run the whole capture folder.

Run: `./.venv/bin/pytest tests/capture/ -v`
Expected: every test passes.

- [ ] **Step 4:** Run the entire agent_core suite to catch any spillover.

Run: `./.venv/bin/pytest -q`
Expected: green, or a small number of unrelated failures on `fix/recover-from-model-not-loaded` (the ongoing branch — those aren't yours). If any capture-related test still fails, fix it before proceeding.

- [ ] **Step 5: Commit.**

```bash
git -C /mnt/secondary/projects/agent_core add tests/capture/
git -C /mnt/secondary/projects/agent_core commit -m "test(capture): remaining sync sites converted to async

Every store.write and store.get in tests/capture/{test_store,
test_store_disk,test_retention,test_search}.py becomes await; tests
that used the sync API become async def under asyncio_mode='auto'.

Ripple from A1's signature change now fully absorbed. Full
tests/capture/ suite green; agent_core suite green."
```

---

### Task A5: CHANGELOG + version bump

**Files:**
- Modify: `agent_core/CHANGELOG.md` (new `[1.11.0]` entry).
- Modify: `agent_core/pyproject.toml` (`version = "1.10.0"` → `"1.11.0"`).

**Interfaces:** none.

- [ ] **Step 1:** Read the current CHANGELOG format (the `[1.10.0]` entry). Match the same section conventions.

- [ ] **Step 2:** Add a new entry at the top of CHANGELOG:

```markdown
## [1.11.0] - 2026-09-19

`CaptureLayer.maybe_substitute` and `CaptureStore.write`/`get` become async. The
actual sqlite write moves off the event loop onto a per-store dedicated writer
thread with its own connection (sqlite is thread-affine). A synchronous
sqlite write on the daemon loop stalled every coroutine on it for the
write's duration — measured 0.401 s late against a 0.4 s write in the
consumer (PARE Task 3) on the equivalent pane-activity path; the tool-capture
path this fix addresses is hit on every tool result during a chat turn.

### Changed
- **`CaptureStore.write(record) -> str` is now `async def`.** Ref generation
  (`secrets.token_hex(8)`) stays on the caller's thread; the sqlite INSERT +
  FTS + blob-spill + commit body runs on the store's dedicated writer thread.
  `write` returns the ref immediately after enqueueing; it does NOT await the
  write's future. `open_memory()` skips the writer thread and runs the body
  inline (a `:memory:` sqlite database exists only on its opener's connection).
- **`CaptureStore.get(ref) -> dict | None` is now `async def`.** If a write for
  `ref` is still in flight, `get` awaits its future before the SELECT — so a
  read-after-write of a just-issued ref never sees a false `None` (which
  `ReadCapture` renders to the model as "expired capture", a lie). A writer
  exception is re-raised by `get`, distinguishable from `None`.
- **`CaptureLayer.maybe_substitute(...)` is now `async def`.** Callers must
  `await` it. One production caller: `RiskAwareToolPool.call_tool` at
  `risk_pool.py:435`. `ReadCapture.run` at `tools.py:63` gains `await` for
  the same reason (it's already `async`).

### Migration for consumers
- Every direct caller of `store.write` and `store.get` needs `await`. The only
  in-ecosystem consumer that touches these is PARE; its migration is scoped in
  `PARE/docs/superpowers/specs/2026-09-19-on-loop-capture-write-design.md`.
  PAL does not use `CaptureLayer` or `CaptureStore` (verified by exhaustive
  grep).
- Sync consumers can wrap in `asyncio.run(store.get(ref))` only if they hold
  no other event loop — most PARE consumers are already inside `async def`.
```

- [ ] **Step 3:** Bump the version in `pyproject.toml`:

```toml
version = "1.11.0"
```

- [ ] **Step 4:** Run the full agent_core suite one more time to confirm nothing broke on the way up.

Run: `cd /mnt/secondary/projects/agent_core && ./.venv/bin/pytest -q`
Expected: green.

- [ ] **Step 5: Commit.**

```bash
git -C /mnt/secondary/projects/agent_core add CHANGELOG.md pyproject.toml
git -C /mnt/secondary/projects/agent_core commit -m "chore: release 1.11.0 — capture write off the event loop

Full changes documented in CHANGELOG.md's [1.11.0] entry.

The consumer-suite gate (PARE full test suite against this branch)
must pass before this version is tagged and released. See PARE's
docs/superpowers/plans/2026-09-19-on-loop-capture-write.md Task RG for
the gate."
```

---

## Release Gate — RG: consumer-suite check

**This is not a task the implementer freely picks up. It gates Part B.**

The agent_core PR must NOT be tagged 1.11.0 until PARE's full test suite passes against the branch. Per CLAUDE.md verbatim: *"When changing a library, run the consuming project's suite before releasing. A contract widening can pass every one of the library's own tests and still break a consumer's test doubles or call sites."*

- [ ] **Step RG.1:** Push the agent_core branch to origin as a draft PR. Do NOT tag yet.

- [ ] **Step RG.2:** In PARE, install agent_core from the branch (not the released version yet):

```bash
cd /mnt/secondary/projects/PARE
./.venv/bin/pip install -e /mnt/secondary/projects/agent_core
```

- [ ] **Step RG.3:** Run PARE's full test suite. It WILL fail — Part B has not landed yet, and PARE still calls the sync API in places.

Run: `./.venv/bin/pytest -q`
Expected: multiple failures on `await` missing.

- [ ] **Step RG.4:** This is expected. RG's real purpose is to check that PARE and agent_core AFTER Part B lands are jointly green. So actually run this as the final step of Part B, not here. Placeholder in this position so the sequencing is explicit.

The tag happens after Part B's final commit passes PARE's full suite against the same agent_core branch. See Part B Task B5.

---

## Part B — PARE

### Task B1: Pin agent_core 1.11.0-rc (or the branch)

**Files:**
- Modify: `pyproject.toml`.

**Interfaces:** none.

- [ ] **Step 1:** In PARE, update `pyproject.toml`'s `agent_core` dependency line:

If installing from the agent_core branch during RG:
```toml
"agent_core @ git+https://github.com/EdibleTuber/agent_core.git@<branch-name>",
```
Once agent_core is tagged 1.11.0:
```toml
"agent_core @ git+https://github.com/EdibleTuber/agent_core.git@v1.11.0",
```

- [ ] **Step 2:** Install the pinned agent_core in PARE's venv.

Run: `./.venv/bin/pip install -e . --upgrade`
Expected: agent_core 1.11.0 (or the branch head) resolves.

- [ ] **Step 3:** Confirm the failure surface — some tests will fail now, in ways that Tasks B2–B4 fix. Note the failure count as a baseline.

Run: `./.venv/bin/pytest -q --tb=no`
Expected: multiple failures. Do NOT commit at this point.

- [ ] **Step 4:** (No commit — this task's only side effect is the pyproject bump, which lands atomically with B2 below to avoid a broken bisect point.)

---

### Task B2: `CaptureStoreManager` becomes a thin wrapper

**Files:**
- Modify: `pare/capture_store.py` — remove writer thread state (Task 3's `_writer_thread`, `_writer_queue`, `_writer_connections`, `_ensure_writer_thread`, `_writer_loop`); `write_async` becomes `await store.write(record)`; `close_all` cascades `store.close()` on each cached store; `daemon.lock` flock stays.
- Test: `tests/test_capture_store_manager.py` — the writer-thread-internals tests go; the public-behavior tests stay.

**Interfaces:**
- Consumes: `async def CaptureStore.write` (A1), `CaptureStore.close` (A1).
- Produces (unchanged from Task 3):
  - `async def CaptureStoreManager.write_async(db_path: Path, record: CaptureRecord) -> None`.
  - `def CaptureStoreManager.close_all() -> None`.
  - `def CaptureStoreManager.resolve(cwd, channel_id) -> CaptureStore`.
  - `def CaptureStoreManager.resolve_db_path(cwd, channel_id) -> Path`.

- [ ] **Step 1:** Read the current `pare/capture_store.py`. Identify the writer-thread block (lines ~40-145 based on grep in the design phase — verify by reading the file). The `_writer_loop`, `_ensure_writer_thread`, `_writer_queue`, `_writer_connections`, and `_writer_thread` state are what goes; `resolve`, `resolve_db_path`, `daemon.lock` flock, and the store cache stay.

- [ ] **Step 2:** Read `pare/agent.py:450`'s `_pane_activity_worker` to confirm the current shape of the `_capture_stores.write_async(db_path, record)` call. The worker should not need to change — the manager's method signature stays the same, only its implementation.

- [ ] **Step 3:** Rewrite `write_async`:

```python
async def write_async(self, db_path: Path, record: CaptureRecord) -> None:
    """Delegate to the store's own writer thread (agent_core 1.11.0).

    Task 3's per-manager writer thread is gone: one writer per store now,
    owned by agent_core.CaptureStore, so a manager holding N stores has
    N writer threads (one per db_path), not one thread with N connections.
    That's the correct pattern -- a store owns its connection, so the
    thread that writes to it must too. See
    docs/superpowers/specs/2026-09-19-on-loop-capture-write-design.md D3.
    """
    store = self._cache.get(db_path)
    if store is None:
        raise RuntimeError(f"write_async before resolve for {db_path}")
    await store.write(record)
```

Delete `_writer_thread`, `_writer_queue`, `_writer_connections`, `_ensure_writer_thread`, `_writer_loop` from the class.

- [ ] **Step 4:** Update `close_all`:

```python
def close_all(self) -> None:
    for store in self._cache.values():
        store.close()
    self._cache.clear()
    for fh in self._locks.values():
        try:
            fh.close()
        except Exception:
            pass
    self._locks.clear()
```

- [ ] **Step 5:** In `tests/test_capture_store_manager.py`, remove tests that assert on the internal writer thread state (`_writer_thread`, `_writer_queue`), and any test that mocks the writer loop. Keep tests that assert on public behavior: `resolve` returns the same store for the same `cwd`, `close_all` closes stores, `daemon.lock` behavior, `write_async` writes and reads back.

- [ ] **Step 6:** Update every test that asserted `write_async` shape internals. Run just this file to focus:

Run: `./.venv/bin/pytest tests/test_capture_store_manager.py -v`
Expected: PASS on the remaining tests; deleted-test count matches expectations.

- [ ] **Step 7:** Run the full PARE suite.

Run: `./.venv/bin/pytest -q --tb=no`
Expected: failures now concentrated in `handback.py`/`snapshot.py`/`test_handback.py`/`test_tui_handle_other.py` (Task B3 and B4 territory).

- [ ] **Step 8: Commit** (bundling Task B1 + B2 to avoid a broken bisect point):

```bash
git -C /mnt/secondary/projects/PARE add pyproject.toml pare/capture_store.py tests/test_capture_store_manager.py
git -C /mnt/secondary/projects/PARE commit -m "feat(capture): pin agent_core 1.11.0; thin CaptureStoreManager to a wrapper

agent_core 1.11.0 moved the writer-thread pattern into CaptureStore
itself -- one writer per store, opened on the writer's own thread.
PARE's Task 3 writer becomes a thin wrapper that await store.write.

CaptureStoreManager loses ~100 lines of writer-thread machinery
(_writer_thread, _writer_queue, _writer_connections, _ensure_writer_thread,
_writer_loop) and gains one line: await store.write(record).
close_all cascades store.close(). The per-project daemon.lock flock
stays -- it guards against a second daemon on the same project and is
orthogonal to the writer thread.

Test file loses the writer-internals asserts (they were testing
Task 3's specific implementation, which is now gone); public-behavior
tests all pass.

Ripple to handback.py / snapshot.py / tests/test_handback.py handled
in B3."
```

---

### Task B3: `handback.py` and `snapshot.py` gain `await`s

**Files:**
- Modify: `pare/handback.py` — `_rows_from` and `candidate_classes` become `async def`; `capture_store.get(ref)` gains `await`.
- Modify: `pare/agent.py:771` — `candidate_classes(...)` gains `await`.
- Modify: `pare/commands/snapshot.py:45, 48, 58` — three `store.get(...)` gain `await`.

**Interfaces:**
- Consumes: `async def CaptureStore.get` (A1).
- Produces:
  - `async def _rows_from(result: str, capture_store) -> list`.
  - `async def candidate_classes(result: str, pattern: str, *, capture_store=None) -> set[str]`.

- [ ] **Step 1:** In `pare/handback.py`, convert `_rows_from` (line 105) to `async def` and prepend `await` on line 116 (`rec = await capture_store.get(ref)`).

- [ ] **Step 2:** Convert `candidate_classes` (line 127) to `async def`. Its call to `_rows_from` at line 134 becomes `await _rows_from(...)`.

- [ ] **Step 3:** In `pare/agent.py` at line 771, change `candidate_classes(result, pat, capture_store=self.capture_store)` to `await candidate_classes(result, pat, capture_store=self.capture_store)`. The enclosing scope is `handle_chat`, already `async def`.

- [ ] **Step 4:** In `pare/commands/snapshot.py`, prepend `await` on lines 45, 48, and 58. The enclosing method is `Snapshot.run`, already `async def`. `_render` at line 61 stays sync — it takes a resolved `row: dict | None`, not the store.

- [ ] **Step 5:** Run PARE's handback and snapshot tests.

Run: `./.venv/bin/pytest tests/test_handback.py -v --tb=short`
Expected: failures on tests that call `candidate_classes(...)` synchronously — B4 fixes them. `tests/test_snapshot*.py` (if any) tests fail similarly.

- [ ] **Step 6: Commit** (this task's source changes stand alone; the tests fail for a task-scoped reason B4 fixes):

```bash
git -C /mnt/secondary/projects/PARE add pare/handback.py pare/agent.py pare/commands/snapshot.py
git -C /mnt/secondary/projects/PARE commit -m "fix(capture): await the newly-async store.get in every PARE caller

store.get became async in agent_core 1.11.0 (D4 of the design). Four
production call sites in PARE:

- pare/handback.py:_rows_from becomes async def, awaits capture_store.get.
- pare/handback.py:candidate_classes becomes async def (awaits _rows_from).
- pare/agent.py:771 gains await on candidate_classes (already inside
  async def handle_chat).
- pare/commands/snapshot.py:45,48,58 gain three awaits (already inside
  async def Snapshot.run). Snapshot._render stays sync -- it takes a
  resolved row, not the store.

tests/test_handback.py's ~5 candidate_classes call sites break on the
sync -> async change; B4 converts them."
```

---

### Task B4: Convert `test_handback.py` to async

**Files:**
- Modify: `tests/test_handback.py` — ~5 test cases calling `candidate_classes` convert to `async def` + `await`.

**Interfaces:** none.

- [ ] **Step 1:** Read `tests/test_handback.py`. Every function calling `candidate_classes(...)` needs to become `async def` and `await` the call.

Approximate sites: `test_candidate_classes_from_referenced_type_not_class_column` (`:23-31`), `test_candidate_classes_matches_qualified_smali_pattern` (`:33-49`), `test_candidate_classes_empty_pattern_yields_nothing` (`:51-53`), `test_candidate_classes_is_a_dumb_extractor_framework_noise_filtered_downstream` (`:65-71`), `test_candidate_classes_keeps_exact_name_app_class` (`:74…`). Confirm the exact list by reading the file.

- [ ] **Step 2:** For each site, change `def test_...` to `async def test_...` and `candidate_classes(...)` to `await candidate_classes(...)`. `asyncio_mode = "auto"` in `pyproject.toml` means no `@pytest.mark.asyncio` decorator is needed.

- [ ] **Step 3:** Run just this file.

Run: `./.venv/bin/pytest tests/test_handback.py -v`
Expected: PASS on every previously-failing test.

- [ ] **Step 4: Commit.**

```bash
git -C /mnt/secondary/projects/PARE add tests/test_handback.py
git -C /mnt/secondary/projects/PARE commit -m "test(handback): candidate_classes tests converted to async

Ripple from candidate_classes becoming async def in B3. Five test
cases converted to async def + await; asyncio_mode='auto' means no
decorator needed."
```

---

### Task B5: Full-suite check and release gate

**Files:** none. Verification step.

**Interfaces:** none.

This is the consumer-suite gate. The agent_core PR does not tag until this task passes.

- [ ] **Step 1:** Run PARE's full test suite against the agent_core branch (still installed from source).

Run: `cd /mnt/secondary/projects/PARE && ./.venv/bin/pytest -q`
Expected: **475 passed / 3 skipped** (matching post-PR-#70 baseline), 1 expected `UserWarning` about `risk_overrides pins`. Any regression here means Part A or Part B introduced a bug; fix it before tagging agent_core.

- [ ] **Step 2:** If green, this is the go-ahead to tag agent_core 1.11.0. In agent_core:

```bash
cd /mnt/secondary/projects/agent_core
git tag v1.11.0
git push origin v1.11.0
```

- [ ] **Step 3:** In PARE, if the dependency line pointed at the branch during RG, change it to `@v1.11.0` and reinstall:

```bash
cd /mnt/secondary/projects/PARE
sed -i 's|@feat/capture-async-writer|@v1.11.0|' pyproject.toml  # adjust the branch name to match reality
./.venv/bin/pip install -e . --upgrade
./.venv/bin/pytest -q
```
Expected: 475 / 3 skipped against the tagged version.

- [ ] **Step 4: Commit** (if the pyproject line changed):

```bash
git -C /mnt/secondary/projects/PARE add pyproject.toml
git -C /mnt/secondary/projects/PARE commit -m "chore: pin agent_core to tagged v1.11.0

The release gate (475 passing against the branch install) cleared, so
the tag is real. Move the pyproject dependency line from the branch to
the tag."
```

- [ ] **Step 5:** Push PARE's branch, open the PR against `main`, note in the PR body that agent_core 1.11.0 is tagged and the consumer-suite gate cleared.

---

## Done when

- [ ] agent_core 1.11.0 is tagged with the `CaptureStore` writer-thread pattern (A1–A5).
- [ ] Every discriminating test was verified failing before the fix: Task A1 Step 6 recorded the mutation (skipping the pending-writes check makes `test_get_awaits_a_still_pending_write` return `None`).
- [ ] PARE's suite is green at **475 passed / 3 skipped** against the tagged agent_core.
- [ ] The pre-existing on-loop capture write in `CaptureLayer.maybe_substitute` no longer stalls the daemon event loop — measurable via a PARE integration test if you want to make it explicit (not required by this plan; the property is inherited from A1's discriminating test).
- [ ] `agent_core/CHANGELOG.md`'s `[1.11.0]` entry names the interface changes and the migration path for PARE.

**Not done here, and deliberately:** any behavioural change to `RiskAwareToolPool`'s gating logic, the wire protocol, the CaptureStore's search/FTS paths, or PARE's `pane_activity_drops`/`pane_activity_write_failures` counters. Those all stay exactly as they are — only the write's threading model changes.
