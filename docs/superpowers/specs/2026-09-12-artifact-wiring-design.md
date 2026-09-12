# Artifact wiring — design

**Date:** 2026-09-12
**Status:** v1 — designed, not implemented
**Depends on:** `2026-09-06-arcticbase-artifacts-design.md` (steps 0–4 complete)
**Unblocks:** `pare-hardware-mcp` (its own spec, next)

Step 5 of the ArcticBase/artifacts design is *"`pare-hardware-mcp` — its own
spec, now with a transport, a liveness story, an artifact contract and a working
bench."* Three of those four exist. **The artifact contract does not** — it is
declared, ratcheted and validated, and nothing consumes it.

This spec is the wiring that makes it real, across `agent_core` and
`pare-worker-kit`. It is deliberately *not* the hardware worker: that is a new
repo with different risk, and putting both in one document would give the
security-invariant half the same review weight as a new package's scaffolding.

## 1. What is actually missing

Verified on 2026-09-12 by reading the files, not by recalling the previous
session:

- `validate_descriptor` (`agent_core/workers/artifacts.py:46`), `artifact_path`
  (`pare_worker_kit/artifacts.py:67`), `PRODUCES_META_KEY` and `artifact_root`
  have **no non-test consumers** in either repo. A grep for `produces` across
  `agent_core/` and `PARE/pare/` finds conformance, docstrings and prose.
- Nothing reads `WorkerSpec.artifact_root` (`types.py:112`) or
  `artifact_drive_id` (`types.py:135`) at dispatch. Both docstrings say so in
  situ.
- `RiskAwareToolPool.call_tool` (`risk_pool.py:397`) — the single dispatch
  chokepoint — does not mention artifacts at all.

Two things the previous session's follow-up list records as outstanding are
**already done**, and this spec does not re-do them:

- The `or {}` defect is fixed. `risk_pool.py:346` now reads
  `meta = getattr(tool, "meta", None)` and records a non-dict container as-is,
  with the reasoning inline.
- Both repos' CI installs the sibling from source at `main` and has a
  **"Cross-package guards must run, not skip"** step
  (`agent_core/.github/workflows`, `pare-worker-kit/.github/workflows`). The
  green-against-stale hole is closed.

`WorkerSpec` already carries `model_config = ConfigDict(extra="forbid")`
(`types.py:64`), so §5.4's fail-closed requirement is met.

## 2. Scope

**In:** the descriptor wire contract; `open_artifact` in `pare-worker-kit`;
descriptor routing, argument injection and validation in
`RiskAwareToolPool.call_tool`; the cross-package constant guard; release
ordering; acceptance on the bench.

**Out:** `pare-hardware-mcp` itself — its tools, its Tigard bindings, its
`workers.yaml` entry. Out: any change to ArcticBase, which is consumed and not
modified. Out: making a self-reported digest authentic, which nothing in this
design or any other can do (§9).

## 3. Decisions

**A1 — The worker reports only what only the worker can know.**
A field the worker never sends is a field it cannot lie about, and a check that
does not exist cannot rot. This single rule decides the wire contract in §4 and
dissolves one of the three deferred follow-ups instead of implementing it.

**A2 — `host` and `produced_by` leave the wire; the daemon synthesizes them.**
The follow-up list asks for *"`host` checked against the WorkerSpec endpoint."*
The better answer is that the worker should never have sent it. The daemon knows
which spec it dispatched to and which `worker.tool` it called. `_HOST_RE`
(`artifacts.py:24`) survives, its job changed from validating an inbound field to
constraining an outbound one — a `WorkerSpec` endpoint is operator-written, not
trusted by construction, and the synthesized host still lands in
`scp <host>:<path>`.

**A3 — `validate_descriptor` takes the `WorkerSpec`, not a worker name.**
`validate_descriptor(payload, *, spec, tool)`. This is what makes containment and
the drive-id comparison expressible at all; the docstring at `artifacts.py:118`
asks for it in so many words. `worker` was only ever the error prefix, which
`spec.name` supplies.

**A4 — The daemon supplies slug and expected drive id as reserved tool
arguments,** injected at the chokepoint and overwriting whatever the model
supplied. §5.4 already requires *"the daemon supplies a slug, never a path"*
and no mechanism existed: nothing in `client.py` or `client_pool.py` injects
anything into a `tools/call`. Reserved arguments are visible in the tool schema,
land in the capture store with the rest of the call, and need no MCP feature
workers do not already use.

**A5 — `artifact_drive_id` is required whenever `artifact_root` is set.**
`open_artifact` takes `expect_drive_id` with no skip mode. An optional security
input means a branch in a security path whose absent case is the untested one.
`.bench-store-id` is a file, not a property of removable media, so a worker
writing to fixed storage still declares one.

**A6 — Containment is enforced on the worker with descriptors, not paths.**
`artifact_path` keeps its contract — its docstring is explicit that returning an
fd *"is not the contract this function has"* — and gains a sibling,
`open_artifact`, which does the `openat`/`O_NOFOLLOW` walk. `artifact_path`
becomes the lexical half that `open_artifact` builds on.

**A7 — `open_artifact` requires Linux, and the refusal is the control.**
The `openat` walk has no portable equivalent. The statement is scoped to the
function, never to the package: `run_worker`, the constants and `artifact_path`
are portable, and "this package is Linux-only" would be false to anyone reading
carefully. `pyproject.toml` gains the POSIX/Linux classifier for discoverability
**with a comment saying it enforces nothing**, and the enforcement is a runtime
refusal with a test that watches it refuse.

**A8 — A refused descriptor returns an error to the model and is captured
verbatim.** A refused descriptor is the most interesting row in an audit trail.
The file stays on the bench: the daemon cannot delete it and must not imply that
it did.

## 4. The wire contract

| Field | Reported by | Why |
|---|---|---|
| `path` | worker | only the worker has the filesystem |
| `size` | worker | ” |
| `sha256` | worker | ” |
| `hashed_at` | worker | §5.5 — the daemon cannot know when the worker hashed, and that gap is what a chain of custody exists to make visible |
| `media_type` | worker | the producing tool knows what it wrote |
| `drive_id` | worker | read from `{root}/.bench-store-id`; the daemon has no view of that filesystem |
| `host` | **daemon** | synthesized from the dispatched `WorkerSpec` (A2) |
| `produced_by` | **daemon** | it is `worker.tool` at the chokepoint |

Six fields on the wire, **all required, none optional**. Every one is either a
security input or a custody record, and "absent means default" is how a
half-wired dev build slips past. `media_type` in particular gets no default: a
default hides a tool that never considered the question.

The daemon adds its two after validation, yielding §5.1's eight-field object
unchanged for the capture store and for `publish_descriptor`
(`pare/arcticbase.py:209`).

`_REQUIRED` (`artifacts.py:39`) changes accordingly. Both this and A3 are
breaking changes to the released `agent_core` 1.10.0, and both are **free right
now** precisely because §1 established the function has no non-test consumers.
That window closes when this lands.

## 5. The worker side — `open_artifact`

This section specifies interfaces, invariants and failure modes. **It contains
no implementation code, deliberately, and the implementation plan derived from
it must not either.** The walk below is a security invariant with an ordering
requirement and a race in it; code written into prose is unexecuted and remote
from the real file — no compiler, no test, and no neighbouring function whose
existing guard would have shown the input was untrusted — and it then looks
authoritative and gets transcribed faithfully. Specify signatures, invariants and
what the tests discriminate; let the implementer write the code against the
actual codebase.

**Signature.** `open_artifact(root, slug, name, *, expect_drive_id,
expected_size)` in `pare_worker_kit.artifacts`, a context manager yielding a
writer. The writer exposes `write(b)`, hashing inline — §5.5's "nearly free",
since the producing tool already has every byte in hand — and, after a clean
exit, `.descriptor`: the six fields of §4.

**The walk, as ordering.** Every step after the first is relative to a directory
*descriptor*, never re-derived from a path. Re-opening by path is what
reintroduces the race this function exists to close.

1. `root` is opened as a directory, **following symlinks**. Deliberate, and
   consistent with `artifact_path`, which declines to check whether the root is a
   symlink: it is the operator's declaration in `workers.yaml`, the trust anchor
   for this whole mechanism, and there is nothing more trusted to check it
   against.
2. `.bench-store-id` is read relative to that descriptor, `O_NOFOLLOW`, and
   compared to `expect_drive_id` **before anything is created**. This subsumes
   §5.5's `os.path.ismount` check for the case that actually happens — a drive
   that failed to mount leaves an empty directory on the SD card, where the
   sentinel is simply absent. `ismount` answers "is this a mount"; the sentinel
   answers "which drive", which is the question.
3. Free space is checked against `expected_size` from the same descriptor, so the
   answer cannot come from a different filesystem than the one written to.
4. The project directory is opened `O_DIRECTORY|O_NOFOLLOW` relative to root,
   created if absent. This is the race-free counterpart of the `lexists`/`islink`
   pair in `artifact_path`, which checks a *persistent* redirection and says so.
5. `{name}.partial` is created relative to *that* descriptor,
   `O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW`.
6. The held descriptor is `fstat`-ed: regular file, `st_nlink == 1`, `st_dev`
   equal to the root directory's. `fstat` on the fd, never `lstat` on a path —
   same reason as the relativity above.
7. **On clean exit only:** flush, `fsync` the file, rename `.partial` → `name`
   relative to the project descriptor, `fsync` the project directory.

**Invariants, in priority order.** No component after `root` is ever resolved
through a symlink. No path string is re-opened after being checked. Drive
identity is established before the first byte is written. A descriptor exists
only for a file that reached step 7.

**Failure modes, each with a distinct error.**

- `EROFS` is named as read-only, never as permission denied. §5.5 is explicit
  that "permission denied" sends the operator to the wrong problem — and ext4's
  default `errors=remount-ro` makes read-only the *expected* end state of a
  failing drive, not an exotic one.
- `ENOSPC` surfaces at write or fsync, never at the buffered `write()`, and is
  not swallowed.
- `EFBIG` names FAT32's 4 GiB ceiling. The store is ext4; a replacement stick
  might not be.
- A sentinel mismatch names both UUIDs.
- A pre-existing `.partial` fails `O_EXCL`; the message names the file and says
  to remove it. **Left in place deliberately** — §5.5 wants the orphan visible to
  `ls` at the bench, and that is worth more than a convenient retry.

**What the tests must discriminate.** Each must be verified *failing* against an
`artifact_path`-only implementation. A test that passes there is exercising a
path that was already correct, and the fix belongs somewhere else.

- A symlink swapped in at the project-directory component *between* a lexical
  check and the open. This is the one case `artifact_path` cannot catch and the
  reason this function exists.
- A hardlink to a file outside the root — no symlink exists to find, so only
  `st_nlink` sees it.
- A sentinel on a different filesystem from the write target.
- `ENOSPC` partway through: assert no descriptor is produced **and** that the
  `.partial` remains. Asserting only the raise is vacuous against a design whose
  claim is the `.partial`.
- A truncated dump is **not** distinguishable by hash — §5.5 says so directly.
  The test asserts the `.partial` mechanism caught it. An assertion that the
  digest differs would be testing a property this design explicitly disclaims.

## 6. The daemon side

**Where the slug comes from.** `agent_core` is a library and knows nothing about
`.pare/`; §5.2's resolution lives in PARE. The channel exists:
`call_tool(..., ctx=None)` is how host-application context reaches the pool, and
`_resolve_send(ctx)` (`risk_pool.py:439`) is the precedent. PARE supplies the
slug through `ctx`; `agent_core` asks and never derives. §5.2's "no project, no
publish" becomes: an artifact dispatch with no resolvable slug is refused, and
the error **names the cwd**, not the ctx object — the cwd is what the operator
can act on.

**Order of operations in `call_tool`.** Today it snapshots arguments at
`risk_pool.py:399`, resolves the tier, gates, and dispatches at `:517`. The
artifact work interleaves at three points and the ordering is load-bearing.

1. **Resolve `produces` first**, from a `_tool_produces` cache with the ratchet
   `_tier_highwater` gets (`risk_pool.py:121`) and that `_bump()` deliberately
   never clears (`:224`). §5.3 requires this: without it, a reload is a channel
   for turning descriptor validation *off*, exactly as it would otherwise be a
   tier-downgrade channel.
2. **If `artifact`, validate the spec and refuse before the gate.** No
   `artifact_root` → refused (§5.4, fail closed). No `artifact_drive_id` → refused
   (A5). No slug → refused. Before `_await_operator`, because prompting an
   operator to approve a dispatch that cannot succeed teaches them to approve
   without reading.
3. **Inject the reserved arguments, then snapshot.** `snapshot` at `:399` is what
   reaches the approval prompt. Injecting first means the operator sees the slug
   and drive the call will actually use; injecting after would show the operator
   the model's values and send different ones — an approval surface displaying
   something other than what it approves.
4. **Gate and dispatch unchanged.**
5. **After `:517`, validate.** `validate_descriptor(payload, spec=spec,
   tool=tool)` now performs containment against `spec.artifact_root` and compares
   the reported `drive_id` to `spec.artifact_drive_id`. The daemon then adds
   `host` and `produced_by`.

**How the descriptor is extracted, and why strictly.** Nothing in any of the
three repos uses `structuredContent` — `_stringify_result`
(`tool_factory.py:19`) reads only `content`, and every worker returns text
blocks. So a `produces=artifact` result must be **exactly one text content block
parsing as a JSON object**. Not "the first block that parses": that is a guess
dressed as a rule, and it silently tolerates a tool that logs a line before its
descriptor. Exactly-one is refusable and therefore checkable, and it becomes a
conformance assertion beside `_assert_valid_produces_meta`
(`conformance.py:63`), so a worker that gets it wrong fails at build time rather
than mid-dump.

**Placement.** All of this lives in `RiskAwareToolPool.call_tool` rather than in
a separate wrapper. The alternative — an `ArtifactAwarePool` layered outside it —
would be bypassed by every consumer holding a reference to the risk pool, and
`manager.py:361` records that consumers call `tool_pool.call_tool` by hard-coded
name. A bypassable security control is the realistic outcome there, not a
hypothetical one. The cost is that a class named for risk gains a second concern;
it is paid down by putting the validation body in `workers/artifacts.py` as a
function taking the spec explicitly, so `call_tool` gains calls rather than
blocks.

## 7. Contract drift and release order

**A new drift surface.** The descriptor field set lives in one place today —
`_REQUIRED` at `artifacts.py:39` — because the kit never *builds* a descriptor.
`open_artifact().descriptor` changes that: the field set becomes stated twice, on
two machines that never share a Python environment. That is exactly the condition
`PRODUCES_META_KEY`, `RISK_TIER_META_KEY` and `SLUG_RE` are each stated-twice-and-
guarded for, and it gets the same treatment: a named constant on both sides and a
bidirectional guard joining `test_risk_agreement.py:24` and its agent_core
counterpart.

Without it the failure is the quiet one: the kit produces five fields, agent_core
requires six, and every dump is refused with a message about a missing field
rather than about the mismatch that caused it.

**Order, and why nothing is red in between.** The kit's change is additive; no
existing behaviour is touched. agent_core's is breaking, but only for its own
tests (§1).

1. **kit PR merges to main.** Its guard *skips* agent_core's new constant, which
   is absent from agent_core main — honest and visible, which is the distinction
   the follow-up list draws between a skip and a pass-against-stale.
2. **agent_core PR merges.** Both CI jobs install the sibling from `git+…` at
   **main**, not at a tag, so the guard sees the kit's constant as soon as step 1
   lands. No tag is needed for CI to go green.
3. **Tag `pare-worker-kit v0.2.0` and `agent_core v1.11.0`.** Tags exist for
   consumer pins, not for CI.
4. **PARE bumps its pin** from `agent_core@v1.10.0`.

**Before either tag, the consuming suites run.** A contract widening can pass
every one of a library's own tests and still break a consumer's test doubles.
`pare-mitm-mcp/tests/test_server.py`'s exact-equality meta assertion is the known
instance (§5.3 names it), and it is the shape that breaks silently.

**All three workers move to `pare-worker-kit v0.2.0`** in the same round, though
none needs `open_artifact` — so that "does this worker have the artifact API?" is
answerable without opening three files. This is the class the previous session
recorded: `agent_core` sat at 1.9.0 through thirteen commits and the staleness was
invisible by version number. The suites must run anyway; the pin bump is the cheap
half.

## 8. Acceptance

**The wiring's end-to-end property does not need a Tigard.** §14 phrases
acceptance as "a dump on `pare-bench`", which could be read as blocking this work
— there is no Tigard attached. It does not. Every check in §4–§7 concerns a file
on a drive, and the drive exists, is mounted, carries its sentinel and has been
write-verified. A probe that writes bytes exercises the path a flash dump would.

**Level 1 — local, on `agenthost`.** A stdio probe worker, `artifact_root` at a
temp directory carrying a `.bench-store-id`. Exercises injection ordering, the
`produces` ratchet across a reload, containment, the drive-id comparison, every
refusal path, and both races from §5. This is where the adversarial tests live.

**Level 2 — the bench.** The same probe on `pare-bench` over streamable_http,
`artifact_root: /mnt/bench-store`, `artifact_drive_id:
0361c41f-e680-4d4e-b9c3-39af8a33d067` — read off the drive on 2026-09-12, and the
one value here that must never be typed from memory. Adds the network, the real
filesystem, ArcticBase publication and the Pi's screen.

The probe lives in PARE's `scripts/`, not in `pare-worker-kit`: the kit's
identity is one dependency and no examples module, and a fixture worker shipped
there would be the second thing that has to stay small. Its `workers.yaml` entry
is **temporary, and leaving it declared is a finding.**

**Level 2 is not complete until the finding has been read on the Pi's screen.**
Not an API 200, not a successful `PUT`, not a descriptor in the capture store —
the page, on the DSI panel, at the bench.

**Test discipline.** Every regression test is verified failing against the
pre-change code. No assertion on a count of fields or of tests: assert the
relationship — the kit's constant equals agent_core's; a descriptor the kit
produces validates against the daemon's validator — because a literal breaks on
the next legitimate change and a relationship survives it.

## 9. What this cannot establish

That a dump's bytes are the chip's bytes. The producer supplies both the file and
its digest, so a worker that truncates at production returns a correct sha256 of
the truncated bytes and the operator verifies it successfully. `sha256` here is
**integrity across the transfer and a stable identity for audit** — both real,
neither authenticity. Authenticity needs a digest the producer did not supply.
Three places already say this at length
(`agent_core/workers/artifacts.py:46`, `pare_worker_kit/artifacts.py:67`, and
§5.4); this spec adds no fourth mechanism and claims no more.

The probe in §8 cannot stand in for a Tigard on this point. `pare-hardware-mcp`
owns it, and it needs a Tigard on the bench.

## 10. Risks

- **The injection overwrites a model-supplied argument.** A tool author who does
  not know `project_slug` is reserved writes a tool whose argument silently never
  arrives. Mitigated by the conformance assertion in §6, not by documentation.
- **`open_artifact` is Linux-only and the classifier enforces nothing** (A7). The
  runtime refusal is the control, and it is only a control once a test has
  watched it refuse.
- **Two breaking changes to `agent_core` ride together** (§4). Free today
  because nothing consumes the function; the cost of being wrong about that is
  the whole point of §1 being read rather than recalled.
- **The `.partial` left behind is a deliberate ergonomic cost.** If it proves
  intolerable in practice the fix is a named recovery command, never silently
  overwriting on `O_EXCL`.

## 11. Provenance

Every file:line in this document was read on 2026-09-12. Machine state — the
daemon active, ArcticBase 200 on api and ui, `agent_core` 1.10.0 in PARE's venv,
`/mnt/bench-store` mounted with 116 GiB free and its sentinel readable, the
JMicron enclosure present, no Tigard — was re-read on the machines the same day
rather than carried over from `2026-09-11-bench-state.md`.
