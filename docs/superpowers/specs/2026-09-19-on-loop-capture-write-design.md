# Move `CaptureLayer`'s write off the event loop

**Status:** design, 2026-09-19. Awaiting spec review before writing-plans.
**Repos touched:** `agent_core` (library change + version bump), `PARE` (consumer pin + Task 3 migration). PAL is untouched.

The motivating problem: `agent_core/capture/layer.py:62`'s `store.write(record)` in `CaptureLayer.maybe_substitute` is synchronous, called from `agent_core/workers/risk_pool.py:435` on **every** tool result during a chat turn. It blocks the daemon's event loop for the sqlite write's duration — every other coroutine on that loop, including the `readline()` in the connection handler and the routing of `ToolApprovalResponseMessage` at `daemon.py:176`, sits idle while the write commits. Same failure class as PARE Task 3's pane-activity bug, on a path reached many times more often. Task 3 measured the equivalent stall at 0.401s late against a 0.4s write.

Prior context: [`docs/superpowers/plans/2026-09-18-pare-tui.md`](../plans/2026-09-18-pare-tui.md) Task 3 solved this for pane activity via a dedicated writer thread in PARE's `CaptureStoreManager`. That fix stays; this design moves the underlying primitive into `agent_core` so *every* capture write benefits, and PARE's writer becomes a thin wrapper.

---

## 1. Decisions

| | Decision | Why |
|---|---|---|
| **D1** | `CaptureLayer.maybe_substitute` becomes `async def`. | One production caller (`risk_pool.py:435`), already inside `async def call_tool` — a one-word `await` change. No other consumers: PAL doesn't use CaptureLayer, and PARE never calls `maybe_substitute` directly (it constructs `CaptureLayer` at `pare/agent.py:200` and hands it to `RiskAwareToolPool` at `:211`). The interface reads honestly — a method that does I/O is async. |
| **D2** | `CaptureStore` owns a dedicated writer thread, one per store. | sqlite connections are thread-affine (`check_same_thread=True`; empirically confirmed during Task 3). A thread that opens *and closes* its connection produces no `ProgrammingError`. One writer per store means one connection dedicated to writes on that db file, coordinating with read connections through WAL. |
| **D3** | PARE's `CaptureStoreManager.write_async` becomes a thin wrapper delegating to `CaptureStore.write`. Task 3's writer-thread code is removed. | One writer per store means Option A's two-writer arrangement (agent_core's + PARE's) is unnecessary. The Task 3 pattern was correct at the time; its correct home is beside the store it writes to. |
| **D4** | `store.get(ref)` becomes `async def` and awaits any pending write for that specific ref. | Ordering property (write-before-read of a just-issued ref) becomes explicit and enforceable rather than a latency assumption. Bounded wait: only the write of *that* ref, not the queue. Common case (LLM round-trip elapsed) the write is long done and there's nothing to await. |
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

### 2.5 Failure paths (spec R4 spirit — no silent losses)

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
4. `agent_core/tests/capture/test_layer.py`: rewrite ~10 sites to `async def` + `await`. Mechanical.
5. `agent_core/tests/capture/test_store.py` / `test_store_disk.py` / `test_retention.py` / `test_search.py`: audit — direct `store.write(...)` calls in these tests need `await` (or a sync test helper if the test itself does not need to be async).
6. New tests in `test_store.py` (or a new `test_store_async.py`):
   - `write` returns a ref that a concurrent `get(ref)` sees after the writer finishes.
   - **The critical one:** an interleaved `get(ref)` before the write commits `await`s the pending-writes future rather than returning `None`. Verify failing against a `get` that reads sqlite directly with no pending-writes check. This is the discriminating test — a version of `get` that doesn't await pending writes returns `None` here and lies to the model as "expired capture".
   - A `write` whose writer raises propagates to a subsequent `get(ref)`.
   - `close()` drains pending writes within the bound and warns past it.
7. `agent_core/CHANGELOG.md` + version bump to 1.11.0.

**PARE PR (second, consumes agent_core 1.11.0):**
1. Pin `agent_core @ v1.11.0` in `pyproject.toml`.
2. `pare/capture_store.py`: remove the Task 3 writer thread; `CaptureStoreManager.write_async` becomes a wrapper: `await store.write(record)`. The per-project `daemon.lock` flock (`capture_store.py:98-105`) stays — it is orthogonal, guards against a second daemon on the same project.
3. `PareAgent._pane_activity_worker` (`pare/agent.py:450` region): the worker now `await`s `_capture_stores.write_async` which awaits `store.write`. `pane_activity_write_failures` counter behavior unchanged: raised exceptions bump it, queue-full-at-intake bumps `pane_activity_drops` (unchanged), shutdown-drain-timeout bumps `pane_activity_drops` (unchanged; the underlying drain is now `store.close`'s but the counter is bumped from the same manager-level wrapper).
4. Suite must be green: **475 passed / 3 skipped** baseline (post PR #70) plus whatever counter-tests move around. Per CLAUDE.md: consumer-suite gate is the release condition, not an aspiration.
5. Remove now-redundant Task 3 tests (the writer-thread-internals ones); keep the ashutdown-drain and QueueFull semantics tests — the behaviour is still contractual, just implemented downstream now.

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
