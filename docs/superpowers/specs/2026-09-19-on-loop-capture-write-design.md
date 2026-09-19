# Move `CaptureLayer`'s write off the event loop

**Status:** design, 2026-09-19. Awaiting spec review before writing-plans.
**Repos touched:** `agent_core` (library change + version bump), `PARE` (consumer pin + Task 3 migration). PAL is untouched.

The motivating problem: `agent_core/capture/layer.py:62`'s `store.write(record)` in `CaptureLayer.maybe_substitute` is synchronous, called from `agent_core/workers/risk_pool.py:435` on **every** tool result during a chat turn. It blocks the daemon's event loop for the sqlite write's duration — every other coroutine on that loop, including the `readline()` in the connection handler and the routing of `ToolApprovalResponseMessage` at `daemon.py:176`, sits idle while the write commits. Same failure class as PARE Task 3's pane-activity bug, on a path reached many times more often. Task 3 measured the equivalent stall at 0.401s late against a 0.4s write.

Prior context: [`docs/superpowers/plans/2026-09-18-pare-tui.md`](../plans/2026-09-18-pare-tui.md) Task 3 solved this for pane activity via a dedicated writer thread in PARE's `CaptureStoreManager`. That fix stays; this design moves the underlying primitive into `agent_core` so *every* capture write benefits, and PARE's writer becomes a thin wrapper.

---

## Correction 2026-09-19 (post-review, pre-plan)

An earlier draft claimed "PARE needs zero source changes" and that the async
ripple was contained to agent_core. The `writing-plans` self-review surfaced
that this was wrong: `store.get` also becomes async (D4), and it has
production callers in PARE that I did not inventory during brainstorming.
Corrected below in D1/D4 and in §3. Additional gap: `CaptureStore.open_memory()`
uses `sqlite3.connect(":memory:")`, and an in-memory sqlite database exists
only on the connection that opened it — a writer thread with its own
connection would get a different, empty database. The design has to skip
the writer thread for in-memory stores (writes run inline, synchronously).
Added as §2.5.

The user asked to log this correction plainly rather than silently patch — the
instinct to read the code before signing off was earned; the spec was
speculating on a blast radius it hadn't proven. Every citation below is now
verified against the real files.

## 1. Decisions

| | Decision | Why |
|---|---|---|
| **D1** | `CaptureLayer.maybe_substitute` becomes `async def`. | One production caller (`risk_pool.py:435`), already inside `async def call_tool` — a one-word `await` change. PAL doesn't use CaptureLayer at all. PARE constructs `CaptureLayer` at `pare/agent.py:200` and hands it to `RiskAwareToolPool` at `:211` but never calls `maybe_substitute` directly — so this specific method's async ripple stops at agent_core. **D4's ripple is different** — see the D4 row. |
| **D2** | `CaptureStore` owns a dedicated writer thread, one per store. | sqlite connections are thread-affine (`check_same_thread=True`; empirically confirmed during Task 3). A thread that opens *and closes* its connection produces no `ProgrammingError`. One writer per store means one connection dedicated to writes on that db file, coordinating with read connections through WAL. |
| **D3** | PARE's `CaptureStoreManager.write_async` becomes a thin wrapper delegating to `CaptureStore.write`. Task 3's writer-thread code is removed. | One writer per store means Option A's two-writer arrangement (agent_core's + PARE's) is unnecessary. The Task 3 pattern was correct at the time; its correct home is beside the store it writes to. |
| **D4** | `store.get(ref)` becomes `async def` and awaits any pending write for that specific ref. | Ordering property (write-before-read of a just-issued ref) becomes explicit and enforceable rather than a latency assumption. Bounded wait: only the write of *that* ref, not the queue. Common case (LLM round-trip elapsed) the write is long done and there's nothing to await. **Ripple:** `store.get` has 4 non-test call sites — 1 in agent_core (`capture/tools.py:63`, `ReadCapture.run`, already async) and 3 in PARE. Two PARE call sites are already inside `async def` (`snapshot.py:45,48,58` inside `Snapshot.run`; `agent.py:771` inside `handle_chat`), so they add `await`. One is `pare/handback.py:105` `_rows_from` (sync), whose sole caller `candidate_classes` (`:127`) becomes async too; its only production caller is `handle_chat` (`agent.py:771`), already async. Test-side: `tests/test_handback.py` has ~5 sync test cases calling `candidate_classes` that convert to `async def` + `await`. |
| **D5** | Writer-queue is unbounded. | The bounded queue in PARE Task 3 was for a background daemon logger where a wedged writer could pile up records forever. Here every `store.write` is on a turn's critical path; if the writer is wedged, one more record is not the problem. Unbounded means no `QueueFull` path to reason about. |
| **D6** | `agent_core` version bumps 1.10.0 → 1.11.0. | Interface change (sync → async on two public methods). Minor bump is honest — it's a break for anyone calling `maybe_substitute` or `store.get` sync, but the only such consumer inside this ecosystem is agent_core's own test suite. |

### What this does NOT change

- **PAL is unaffected.** Zero uses of `CaptureLayer`, `maybe_substitute`, or any `agent_core.capture` symbol. Verified by exhaustive grep of `/mnt/secondary/projects/PAL`.
- **`RiskAwareToolPool`'s risk-gating logic**. Only the one line at `risk_pool.py:435` changes (add `await`).
- **The wire protocol.** No new message types, no changed semantics for the model — a captured ref still returns from `read_capture` the same way, once the write commits.
- **The store's search/FTS paths.** Reads stay synchronous outside the pending-writes await; WAL makes concurrent reads fine.
- **PARE Task 3's `pane_activity_drops` / `pane_activity_write_failures` counters** or where they live. They're still on `PareAgent`, still bumped from the pane-activity worker path, now routed through `store.write` awaits.

---

## 2. The new `CaptureStore` interface

Signatures and semantics, not code — code written into prose is unexecuted and gets transcribed past its defects.

### 2.1 `async def write(record: CaptureRecord) -> str`

- Generates `ref = secrets.token_hex(8)` on the calling thread. Cheap, no I/O, no thread hop. (Currently generated inside `store.py:72`, inside the write itself.)
- Creates a per-write `Future` on the caller's loop, inserts `pending_writes[ref] = future`.
- Enqueues `(ref, record, future, loop)` onto the writer thread's `queue.SimpleQueue`.
- Returns `ref` **without awaiting the future.** The caller has its ref before the sqlite write commits.

### 2.2 Writer thread loop

- Owns a per-store `sqlite3.Connection` opened *on this thread* (never handed off). Fixes the `check_same_thread=True` constraint (empirically confirmed by Task 3's mutation).
- Consumes `(ref, record, future, loop)` items.
- Runs the current insert+FTS+commit body from `store.py:77-102`.
- On success: `loop.call_soon_threadsafe(future.set_result, None)`.
- On exception: `loop.call_soon_threadsafe(future.set_exception, exc)`.
- Always: `pending_writes.pop(ref, None)` **after** setting the future.
- Terminates on a `None` sentinel put by `close()`.

### 2.3 `async def get(ref: str) -> dict | None`

- If `ref in pending_writes`: `await pending_writes[ref]`. If that raises, `get` propagates the same exception — the row genuinely does not exist and pretending otherwise (returning `None` → "expired") lies to the model.
- Then the current sync SELECT body from `store.py`. Reads against a WAL-mode db do not need to hop threads.

### 2.4 `def close() -> None` (or `async` if the drain warrants it)

- Enqueues the shutdown sentinel.
- Joins the writer thread with a bounded timeout (5 s, matching Task 3's precedent at PARE's `capture_store.py:182`).
- If the join times out, logs a warning naming the count of unflushed items. Records are lost; that is honest at shutdown.
- Closes the writer's owned connection *on the writer thread's `finally`*, never in the caller.

### 2.5 In-memory stores

`CaptureStore.open_memory()` uses `sqlite3.connect(":memory:")`. In-memory
databases exist only on the connection that opened them — a separate writer
thread opening its own `sqlite3.connect(":memory:")` would get a different,
empty database, and every test using `open_memory` would break silently.

So `open_memory` does not start a writer thread. Its `write` runs the sqlite
insert body **inline on the caller's thread** and returns an already-completed
future for pending-writes bookkeeping (or skips it — no other thread will
race). The `async def write` signature stays uniform with disk-backed stores
so callers cannot tell; the difference is contained inside the store.

The alternative (shared-cache URI, `file::memory:?cache=shared&uri=1`) works
but adds complexity for zero real gain — `open_memory` exists as a test
substrate, and its callers do not benefit from off-loop writes.

### 2.6 Failure paths (spec R4 spirit — no silent losses)

| Path | What happens today | What must happen |
|---|---|---|
| Writer raises during the sqlite write | Exception bubbles to `maybe_substitute`'s caller (event loop stalled) | Future rejected; `store.write`'s caller sees it, subsequent `get(ref)` sees it. Row does not exist. |
| Writer thread dies unexpectedly | N/A (no thread today) | Every pending future rejected with a distinguishable exception. `store.write` on a store whose thread has died is a hard error, not silent. |
| Shutdown timeout with items queued | N/A (no queue today) | Warning log names the count. PARE-side, its existing `pane_activity_drops` counter is bumped from the manager's wrapper. |
| Read of a ref for a write that failed | N/A | `get` re-raises the writer's exception. Distinguishable from `None` ("expired capture") — this row was *supposed* to exist and did not. |

---

## 3. Migration and release order

**agent_core PR (first):**
1. `agent_core/capture/store.py`: add writer thread + pending-writes map to `CaptureStore`; make `write` and `get` async; keep the sync insert body as the writer thread's inline work.
2. `agent_core/capture/layer.py`: `maybe_substitute` → `async def`, `await`s `store.write`.
3. `agent_core/workers/risk_pool.py:435`: prepend `await` to the `maybe_substitute` call. Verify nothing else in `_execute_and_audit` broke.
4. `agent_core/capture/tools.py:63`: `ReadCapture.run` (already `async def`) calls `store.get(ref)` — becomes `await store.get(ref)`. One-line change. **Correction 2026-09-19:** this caller was missed in the original draft; adding it here.
5. `agent_core/tests/capture/test_layer.py`: rewrite ~10 sites to `async def` + `await`. Mechanical.
6. `agent_core/tests/capture/test_store.py` / `test_store_disk.py` / `test_retention.py` / `test_search.py`: audit — direct `store.write(...)` and `store.get(...)` calls in these tests need `await` (or a sync test helper if the test itself does not need to be async). `store.get` sites verified: `test_store_disk.py:15`, `test_store.py:17`, `test_layer.py:60`, `test_retention.py:16,28,29,39,40`.
7. New tests in `test_store.py` (or a new `test_store_async.py`):
   - `write` returns a ref that a concurrent `get(ref)` sees after the writer finishes.
   - **The critical one:** an interleaved `get(ref)` before the write commits `await`s the pending-writes future rather than returning `None`. Verify failing against a `get` that reads sqlite directly with no pending-writes check. This is the discriminating test — a version of `get` that doesn't await pending writes returns `None` here and lies to the model as "expired capture".
   - A `write` whose writer raises propagates to a subsequent `get(ref)`.
   - `close()` drains pending writes within the bound and warns past it.
   - `open_memory` writes and reads inline without starting a writer thread — verified by asserting the store has no writer thread attribute (or equivalent structural check) after `write`.
8. `agent_core/CHANGELOG.md` + version bump to 1.11.0.

**PARE PR (second, consumes agent_core 1.11.0):**
1. Pin `agent_core @ v1.11.0` in `pyproject.toml`.
2. `pare/capture_store.py`: remove the Task 3 writer thread; `CaptureStoreManager.write_async` becomes a wrapper: `await store.write(record)`. The per-project `daemon.lock` flock (`capture_store.py:98-105`) stays — it is orthogonal, guards against a second daemon on the same project.
3. `PareAgent._pane_activity_worker` (`pare/agent.py:450` region): the worker now `await`s `_capture_stores.write_async` which awaits `store.write`. `pane_activity_write_failures` counter behavior unchanged: raised exceptions bump it, queue-full-at-intake bumps `pane_activity_drops` (unchanged), shutdown-drain-timeout bumps `pane_activity_drops` (unchanged; the underlying drain is now `store.close`'s but the counter is bumped from the same manager-level wrapper).
4. **`store.get`'s async ripple (correction 2026-09-19):**
   - `pare/handback.py:105` `_rows_from(result, capture_store) -> list` becomes `async def`; the `capture_store.get(ref)` call at `:116` gains `await`.
   - `pare/handback.py:127` `candidate_classes(result, pattern, *, capture_store=None)` becomes `async def` (it awaits `_rows_from`).
   - `pare/agent.py:771` — `candidate_classes(result, pat, capture_store=self.capture_store)` gains `await`. Already inside `async def handle_chat`.
   - `pare/commands/snapshot.py:45, 48, 58` — three `store.get(...)` calls inside `async def Snapshot.run` gain `await`. `_render` (`:61`) stays sync — it takes a resolved `row`, not the store.
   - `tests/test_handback.py` — every test calling `candidate_classes` converts to `async def` + `await`. ~5 sites, mechanical, and `asyncio_mode = "auto"` in `pyproject.toml` means no decorator is needed.
5. Suite must be green: **475 passed / 3 skipped** baseline (post PR #70) plus whatever counter-tests move around. Per CLAUDE.md: consumer-suite gate is the release condition, not an aspiration.
6. Remove now-redundant Task 3 tests (the writer-thread-internals ones); keep the ashutdown-drain and QueueFull semantics tests — the behaviour is still contractual, just implemented downstream now.

**Order matters:** agent_core PR merges + tag first. PARE PR consumes the tag. Not the other way around. A PARE PR pinning an unreleased agent_core version would fail CI at install time.

---

## 4. What the tests must discriminate

Assertions on properties, not literals — a snapshot rots on the next legitimate change.

1. **`get` awaits pending writes.** A test that interleaves `write → get(same_ref)` with a deliberately-slow writer must see `get` block until the write commits, then return the row. Verify failing against a naive `get` that skips the pending-writes check (that version returns `None`, misinterpreted as "expired"). This is the discriminating test for D4; if it passes both ways, D4 is testing nothing.
2. **`write` returns before the write commits.** Time `store.write` end-to-end with a slow writer; assert it returns in less than the write's duration. Discriminates a regression that turned the write back into a blocking `store.write(...)` on the caller's thread.
3. **Writer exceptions surface.** A writer that raises during INSERT causes the awaiting `write` (via `maybe_substitute`) to raise the same exception. A subsequent `get(ref)` raises the same exception. Neither silently swallows.
4. **Shutdown drain honours the bound.** `close()` with N pending writes and a writer that sleeps `> timeout` produces a warning naming the count and returns within `timeout + epsilon`, not later.

Two anti-patterns from prior rounds to avoid: an assertion made vacuous by test setup (D4's test on a fake that never actually returns a pending-writes future would pass forever), and a test that exercises a path accidentally already correct (D4's test where the writer commits before the reader looks at pending-writes hides the property under test — script the interleaving explicitly).

---

## 5. Cross-repo release gate

**Consumer suite must be green.** Before tagging agent_core 1.11.0, run the entire PARE test suite (currently 475 passing) against the new library. Any failure there is a bug in agent_core or in the migration, and must be diagnosed before the tag lands. This is the CLAUDE.md rule verbatim: "When changing a library, run the consuming project's suite before releasing. A contract widening can pass every one of the library's own tests and still break a consumer's test doubles or call sites."

**No PAL suite gate.** PAL doesn't consume `CaptureLayer` — verified by exhaustive grep — but running PAL's suite against the new agent_core is still worth doing as a sanity check, since PAL does consume other `agent_core` subsystems that share the same package. Not a hard gate.

---

## 6. Not in scope

- **A public "flush pending writes" API.** The internal `close()` drain covers shutdown. Nothing today needs mid-life flush semantics, and adding one now would speculate on a caller we don't have.
- **Making the sqlite reads async.** Reads against WAL are fast; `store.get`'s async signature exists only for the pending-writes await, not for the SELECT itself.
- **Reworking `CaptureStore`'s FTS or spill-to-blob paths.** Unchanged.
- **PAL migration.** Not affected.
- **The three ongoing unrelated agent_core changes on `fix/recover-from-model-not-loaded`** (`10ed45f`, `7d3bd97`, `de13c50`, etc.). They land on their own timeline; this design branches from whatever `agent_core` main is at the time of implementation.

---

## 7. Code in this document

None, deliberately. This design is a cross-repo interface change and a concurrency contract: the sync→async ripple, the thread affinity, the pending-writes ordering, the release gate. Code written into prose here would be unexecuted, remote from the real files, and authoritative-looking enough to be transcribed past its own defects. Section 2 gives signatures and semantics; §3 the migration steps; §4 what the tests must tell apart. The implementer writes the code against the actual codebase.
