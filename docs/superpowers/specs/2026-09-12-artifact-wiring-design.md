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

- **The two things this spec changes breakingly have no non-test consumers:**
  `validate_descriptor` (`agent_core/workers/artifacts.py:46`) and its
  `_REQUIRED` tuple (`:39`). Every call site of `validate_descriptor` in all
  three repos is in `agent_core/tests/workers/test_artifacts.py`. Same for
  `artifact_path` (`pare_worker_kit/artifacts.py:67`).

  An earlier draft of this section said that of `PRODUCES_META_KEY` and
  `artifact_root` too, and **that was false in both cases** — stated from a grep
  scoped too narrowly to find its own counter-examples. `PRODUCES_META_KEY` is
  consumed in production at `risk_pool.py:365`; `artifact_root` is read by
  PARE's `/health` at `pare/commands/health.py:145`. Neither changes shape here,
  so the "free breaking change" argument survives — but it rests on the two names
  above, not on a blanket claim.
- Nothing reads `WorkerSpec.artifact_root` or `artifact_drive_id` **at
  dispatch** (`types.py:112`, `:135`; both docstrings say so in situ).
  `/health` reads the root outside the dispatch path.
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

**Three pre-existing defects the review of this spec surfaced. None is caused by
this design and none is fixed by it; they are recorded so they are not
rediscovered, and so nobody mistakes them for regressions introduced here.**

1. **`conformance.py:55` still has `meta = getattr(tool, "meta", None) or {}`.**
   Its sibling `_assert_valid_produces_meta` (`:79`) does the strict `isinstance`
   check instead, so the two validators disagree about a non-dict `_meta`. Traced
   rather than assumed: with `meta=[]` the risk-tier version still *fails*, just
   with a message blaming the tier rather than the malformed container. A
   diagnosis defect, not a bypass — milder than the `risk_pool` instance it was
   modelled on, which is already fixed.
2. **`risk_pool.py:399` snapshots `{}` for a non-dict `arguments`** while `:517`
   dispatches the original object. The operator's approval prompt and the audit
   row would show no arguments for a call that carries a payload. Reachable: tool
   arguments come from `json.loads` of model-emitted text with no runtime type
   check.
3. **`validate_descriptor`'s leading-dash check covers only the last path
   component** (`artifacts.py:174-179`). Inert for a single-file `scp`, live
   under `scp -r`, where an intermediate component named `-oProxyCommand=...`
   becomes a local directory whose name the next `tar`/`rm` glob hands over as an
   option.

## 2. Scope

**In:** the descriptor wire contract; `open_artifact` in `pare-worker-kit`;
descriptor routing, argument injection and validation in
`RiskAwareToolPool.call_tool`; the cross-package constant guard; release
ordering; acceptance on the bench.

**And PARE, which an earlier draft of this section excluded while the rest of the
spec went on requiring it.** §6 has PARE supplying the slug through `ctx`; §7 has
PARE bumping its pin; §8 puts the probe in PARE's `scripts/` with a
`workers.yaml` entry and makes ArcticBase publication an acceptance criterion.
Naming the two libraries and omitting the consumer was a scope statement three
later sections contradict. PARE is in scope for: slug supply, descriptor
publication, the probe, and the bench run.

**Out:** `pare-hardware-mcp` itself — its tools, its Tigard bindings, its
`workers.yaml` entry. Out: any change to ArcticBase, which is consumed and not
modified. Out: making a self-reported digest authentic, which nothing in this
design or any other can do (§9).

## 3. Decisions

**A1 — The worker reports only what only the worker can know.**
A field the worker never sends is a field it cannot lie about, and a check that
does not exist cannot rot. This single rule decides the wire contract in §4 and
dissolves one of the three deferred follow-ups instead of implementing it.

**A2 — `produced_by` leaves the wire. `host` becomes operator-declared, in a new
`WorkerSpec.artifact_host`.**

`produced_by` is `worker.tool` at the chokepoint; the worker echoing it back adds
nothing, and A1 applies cleanly.

**`host` does not, and an earlier draft of this spec got it wrong.** That draft
deleted `host` and had the daemon synthesize it from `spec.endpoint`. Three facts
defeat it:

- **The parent chose the field deliberately.** §11, under *Deferred —
  cross-machine artifact transfer*: *"The descriptor carries `host` as a field
  rather than an assumption, so it can carry this later."* Synthesis converts it
  back into an assumption and forecloses the IoT-Android case the parent names.
- **`endpoint` is `None` for every stdio worker** (`types.py:78`;
  `validate_transport_fields` at `:203` requires it only for the HTTP
  transports). §8's own Level 1 probe is stdio, so synthesis has no source for
  the first acceptance level this spec specifies.
- **A forwarded endpoint synthesizes a confidently wrong answer.**
  `http://127.0.0.1:9101/mcp` — the form already sitting commented in
  `workers.yaml` — yields host `127.0.0.1`, and
  `scp 127.0.0.1:/mnt/bench-store/dump.bin` reads **the daemon's own filesystem**.

That last point generalises, and it is why this decision reversed: a check has
three outcomes — agree, disagree, absent — and **disagreement is the diagnostic
one**. Synthesis has two, and makes the daemon's belief unfalsifiable. Deleting a
field is an improvement only when the daemon's answer is *better*, not merely
*unchallenged*.

So `artifact_host` joins `artifact_root` and `artifact_drive_id` on `WorkerSpec`,
operator-declared in `workers.yaml` — D6's trust anchor, the file the worker
cannot touch — defaulting to the endpoint's hostname via `urlsplit().hostname`
where there is one, and **required explicitly when `transport: stdio`**. The
worker still reports `host`, and `_HOST_RE` (`artifacts.py:24`) keeps its job
unchanged.

**What reconciliation means, precisely, because "refuse a mismatch" would be
wrong here.** The two values are not the same kind of thing: the worker's is what
it calls *itself* (`pare-bench`), the operator's is a *reachable address*
(`100.97.133.126`). They legitimately differ, so an equality refusal would fire on
every correct dispatch at this bench — and a refusal that fires constantly is one
an operator disables, which is worse than not having it. Therefore:

- **`artifact_host` is what retrieval uses.** Always. The worker's claim never
  determines where the operator is sent, which is the whole point of A2's trust
  anchor and is what closes `artifacts.py:130-137`'s lateral-aiming pivot.
- **The worker's `host` must be well-formed** (`_HOST_RE`) or the descriptor is
  refused — that check is about what lands in the operator's shell, and it is
  unconditional.
- **Disagreement is recorded and surfaced, not refused.** It goes in the capture
  row and the descriptor published to the workbench. This is the third outcome
  A2 exists to preserve: a worker that starts claiming a host it did not claim
  yesterday is a fact worth seeing, and it is exactly the signal synthesis threw
  away.

An endpoint is never silently reused as a retrieval address. `_HOST_RE` admits no colons or brackets, so a
literal IPv6 `artifact_host` is refused at config load — fail-closed, and recorded
here as a known limit rather than a discovery at the bench.

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

**A9 — The capture holds the worker's text; the augmented object is what gets
published.** An earlier draft said the eight-field object reached "the capture
store and `publish_descriptor`". It cannot reach the capture store without
rewriting the worker's result: the capture layer stores `stringify_result(result)`
— the worker's own content text (`capture/layer.py:55`, into `CaptureRecord.body`
at `:64`). So the capture row is verbatim (A8, and the forensic point of a
capture is that it is what the worker said); the augmented object goes to the
workbench, where `produced_by` and the reconciled host belong; and the
reconciliation outcome goes in the audit row. Without this split the two custody
fields this design adds would live only in memory and appear in no durable
record, which is the one place custody matters.

**A10 — Descriptor publication and the retrieval command are this spec's work,
not assumed to exist.** `publish_descriptor` (`pare/arcticbase.py:209`) has zero
production callers — its only caller is a live test. §8 makes publication an
acceptance criterion, so something must call it, and this spec assigns it.

The workbench object carries a **retrieval command**, per the parent's §11
(*"v1 leaves transfer manual, with the workbench showing the command"*), which
nothing in PARE builds today — `grep -rn "scp\|sftp\|rsync" pare/ scripts/`
returns nothing. It is built from `spec.artifact_host` (A2), never from the
worker's claim, and it **must not be a legacy `scp -O`**: under the legacy SCP
protocol the remote shell re-parses the path, so quoting protects the operator's
local shell only. `scp` in its default SFTP mode, `sftp`, or `rsync -s`. OpenSSH
≥9.0 defaults to SFTP, so this is a rule about not overriding the default.
A2 raises the stakes here rather than lowering them: the daemon now *authors*
the host in that command.

## 4. The wire contract

| Field | Reported by | Why |
|---|---|---|
| `path` | worker | only the worker has the filesystem |
| `size` | worker | ” |
| `sha256` | worker | ” |
| `hashed_at` | worker | §5.5 — the daemon cannot know when the worker hashed, and that gap is what a chain of custody exists to make visible |
| `media_type` | worker | the producing tool knows what it wrote |
| `drive_id` | worker | read from `{root}/.bench-store-id`; the daemon has no view of that filesystem |
| `host` | worker, **reconciled** | must be well-formed; retrieval uses `spec.artifact_host`, disagreement is recorded (A2) |
| `produced_by` | **daemon** | it is `worker.tool` at the chokepoint |

**Four of these have no semantics anywhere in the code yet** — `hashed_at`,
`media_type`, `drive_id` and (daemon-side) `produced_by` appear in no source
file; `_REQUIRED` (`artifacts.py:39`) is still the original four. So this spec
states them, because "required" without a grammar is not a contract:

- `hashed_at` — RFC 3339 UTC. A format is needed for the parent's §14 test to be
  writable at all (*"`hashed_at` equals the dump time when hashed inline, and is
  later when supplied by `hash_artifact`"*).
- `media_type` — an IANA type/subtype, validated as such. No default (above).
- `drive_id` — the same grammar as the sentinel file it is read from, and
  **control-character-checked**, because §5's mismatch message prints it to an
  operator. `path` gets `_CONTROL_RE` (`artifacts.py:34`) for exactly this
  reason; a worker-supplied field that reaches a terminal gets it too.

**`hash_artifact` is deferred, and named rather than dropped.** §5.5 specifies it
as the separate, I/O-bound *"this is what is on disk now"* tool, distinct from the
free inline hash. Nothing here builds it; `hashed_at` exists so that when it is
built, a late hash cannot masquerade as a creation-time one.

Seven fields on the wire, **all required, none optional**. Every one is either a
security input or a custody record, and "absent means default" is how a
half-wired dev build slips past. `media_type` in particular gets no default: a
default hides a tool that never considered the question.

The daemon adds `produced_by` after validation, yielding §5.1's eight-field
object for `publish_descriptor` (`pare/arcticbase.py:209`). What reaches the
capture store is a different thing — see A9.

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

**Signature.** `open_artifact(root, slug, name, *, media_type, expect_drive_id,
expected_size)` in `pare_worker_kit.artifacts`, a context manager yielding a
writer. The writer exposes `write(b)`, hashing inline — §5.5's "nearly free",
since the producing tool already has every byte in hand — and, after a clean
exit, `.descriptor`: the worker-reported fields of §4.

**`expected_size` is a contract, not a hint, and this is the spec's one defence
against a short read.** An earlier draft spent it solely on the free-space check
at step 3, which `ENOSPC` already backstops, and then claimed §5.5's `.partial`
mechanism caught truncation. It does not: `.partial` fires on *abnormal* exit. A
tool that asks for 2 GB, gets short reads off the chip, and leaves its `with`
block **cleanly** reaches the rename and publishes a correct size and a correct
hash of the truncated bytes — which is the ordinary hardware failure, not an
exotic one. So step 7 additionally refuses a clean exit when
`bytes_written != expected_size`.

**Where these two arguments come from, since A4 does not inject them.** A4's
reserved arguments carry the *slug* and the *expected drive id* — values the
worker must not take from its own configuration. `media_type` and
`expected_size` are different: they are facts about the artifact the calling
tool is producing, so the tool supplies both. Neither may be `None`: a tool that
cannot state its media type has not decided what it is writing, and a tool that
cannot state its size in advance cannot have the short-read check. Where a size
genuinely is not knowable ahead of time — compressed output, a stream of unknown
length — that is a different tool contract and this spec does not cover it;
saying so is better than admitting a `None` that silently disables the check.

This does not make a hostile producer honest — §9 still holds, and nothing here
changes it. It closes the *buggy* half, which §9's disclaimer was being read as a
licence to leave open. `expected_size` is the only value in the walk that did not
come from the bytes, and spending it on `statvfs` alone wastes it.

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

   **Only `ismount` is subsumed, not §5.5's write-probe.** The parent added a
   write-probe precisely because *"`ismount` is true for a read-only mount"* — and
   the sentinel is equally readable on a read-only mount, so it does not answer
   that either. In practice step 5's `O_CREAT|O_EXCL` raises `EROFS` before any
   bytes are written, which *is* the write-probe, done at the moment it matters
   rather than earlier. Stated explicitly because an earlier draft claimed
   subsumption for both and had only argued it for one.
3. Free space is checked against `expected_size` from the same descriptor, so the
   answer cannot come from a different filesystem than the one written to.
4. The project directory is opened `O_DIRECTORY|O_NOFOLLOW` relative to root,
   created if absent. This is the race-free counterpart of the `lexists`/`islink`
   pair in `artifact_path`, which checks a *persistent* redirection and says so.
   "Created if absent" implies an open/mkdir retry: `O_NOFOLLOW` makes a racing
   symlink **safe** (`ELOOP`, never a follow), so this is not an escape — but the
   loop needs a **bounded** number of attempts and a named give-up error, because
   anything with write access inside the root can otherwise drive it
   indefinitely.
5. A temporary name is created relative to *that* descriptor,
   `O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW`. **It must not be `{name}.partial`**, and
   this is not cosmetic: `_NAME_RE` (`pare_worker_kit/artifacts.py:53`) is
   `[A-Za-z0-9][A-Za-z0-9._-]{0,127}`, which admits a literal `.`, so
   `dump.partial` is itself a legal artifact name. Verified by running the
   regex — `dump`, `dump.partial` and `dump.partial.partial` all match. The
   temporary name must therefore contain at least one character **outside
   `_NAME_RE`'s alphabet**, so that no legal `name` can ever collide with any
   temporary, and it must **not** begin with `.`, because §5.5 wants the orphan
   visible to a plain `ls` at the bench and a dotfile is not. `{name}+partial`
   satisfies both (`+` is outside the class); the property is what binds, not
   that spelling.
6. The held descriptor is `fstat`-ed: regular file, `st_nlink == 1`, `st_dev`
   equal to the root directory's. `fstat` on the fd, never `lstat` on a path —
   same reason as the relativity above.

   **`st_nlink == 1` is belt-and-braces, and the spec must not pretend
   otherwise.** Step 5 creates the file with `O_CREAT|O_EXCL`, so a pre-planted
   hardlink at that path returns `EEXIST` and step 6 is never reached; every file
   that gets here has `st_nlink == 1` **by construction**. Keep the check — it is
   the one that still refuses if step 5's flags are ever loosened — but do not
   attribute a catch to it (see the test list, where an earlier draft did exactly
   that). The hardlink window that *is* real is different and uncovered: between
   step 5 and step 7, anything with write access in the project directory can
   `link()` the temporary elsewhere, keeping a handle that can read and write the
   artifact after the descriptor is published. Nothing here detects that; §9 owns
   it.
7. **On clean exit only:** confirm `bytes_written == expected_size`, flush,
   `fsync` the file, then rename the temporary → `name` relative to the project
   descriptor with **`RENAME_NOREPLACE`**, then `fsync` the project directory.

   `RENAME_NOREPLACE` is the control, not a pre-existence check — checking first
   and renaming second is a TOCTOU window of exactly the kind this walk exists to
   close. A plain `renameat` silently replaces its destination, which is what
   makes both of the following reachable: a second dump quietly destroying a
   completed one (see the timeout interaction below), and a call whose `name`
   collides with an in-flight temporary clobbering it. `renameat2` is Linux-only,
   which A7 has already accepted as the cost of this function.

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
- A pre-existing temporary fails `O_EXCL`; the message names the file and says
  to remove it. **Left in place deliberately** — §5.5 wants the orphan visible to
  `ls` at the bench, and that is worth more than a convenient retry. The message
  must **not** assume the file is abandoned: nothing here distinguishes an orphan
  from a live writer, and the most likely way to meet one is the timeout case
  below, where an operator told to "remove it" would unlink a running dump's
  target mid-write.
- `EEXIST` from the step 7 rename means `name` already exists. Distinct message
  from the one above: a completed artifact is in the way, not a temporary, and
  the operator's choice is different.
- `ENOENT` at step 7 means the temporary vanished while it was being written —
  the other half of the case above, seen from the writer's side.
- `bytes_written != expected_size` on a clean exit: the temporary is kept, and the
  message states both numbers.

Orphans accumulate, and multi-gigabyte ones are not cheap on a 117 GiB drive.
`scripts/bench_doctor.sh` reports free space but has no `.partial`-specific
check, so an accumulation looks like ordinary shrinkage rather than a named
problem. It gets one, alongside the artifact-dispatch probe in §8.

**What the tests must discriminate.** Each must be verified *failing* against an
`artifact_path`-only implementation. A test that passes there is exercising a
path that was already correct, and the fix belongs somewhere else.

- A symlink swapped in at the project-directory component *between* a lexical
  check and the open. This is the one case `artifact_path` cannot catch and the
  reason this function exists.
- A pre-existing hardlink at the temporary's path. **Assert that `O_EXCL`
  refuses it** — not that `st_nlink` does. An earlier draft of this list
  justified this test as *"only `st_nlink` sees it"*, which is false for the
  reason in step 6. The test would still have gone green against the new code and
  red against the old, satisfying the "verified failing" rule while crediting the
  wrong mechanism — the precise trap that rule exists to catch, sprung by the
  rule's own author.
- **An absent sentinel**, which is the likeliest real bench failure and was in no
  list at all: `/mnt/bench-store` is fstab-mounted `nofail`, so a Pi that boots
  with the drive unplugged has an ordinary empty directory there. The error must
  say *"no `.bench-store-id` at `{root}` — is the drive mounted?"*, distinct from
  a mismatch, and a bare `FileNotFoundError` is not acceptable. (The
  wrong-filesystem sentinel test an earlier draft asked for is **dropped**: the
  sentinel is read relative to the root fd, so it is in root's filesystem by
  construction except via a file bind mount needing `CAP_SYS_ADMIN`. The test was
  either unconstructible or vacuous — it would have compared two values the walk
  makes equal by construction, and passed forever.)
- **A short read that exits cleanly** — `bytes_written < expected_size` with no
  exception. This is the ordinary hardware failure, and step 7's size check is
  the only thing that catches it.
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

1. **Resolve `produces` first — by calling the accessor that already exists.**
   An earlier draft of this spec instructed the implementer to build "a
   `_tool_produces` cache with the ratchet `_tier_highwater` gets". **That work
   is already done**, and building it again would create a second cache that
   `list_tools` never feeds, i.e. one that is not ratcheted at all.
   `_produces_highwater` is declared at `risk_pool.py:132` — with the comment
   *"a reload must not become a channel for turning descriptor validation
   off"*, the exact property §5.3 asks for — populated from wire meta at
   `:365-368`, and exposed as `produces(worker, tool)` at `:206-214`, defaulting
   to `PRODUCES_RESULT`. It has **zero callers**: built, populated, never
   consulted. `call_tool` calls `self.produces(worker, tool)`.

   One limit worth one sentence, since §5.3's argument is about reloads: the
   ratchet is in-memory, so it survives a `/worker reload` and **not** a daemon
   restart. That is the same property `_tier_highwater` and `_floor_highwater`
   already have, so it is a pre-existing characteristic rather than something
   this wiring introduces — but the reload hole is what it closes, not the
   restart one.
2. **If `artifact`, validate the spec and refuse before the gate.** No
   `artifact_root` → refused (§5.4, fail closed). No `artifact_drive_id` → refused
   (A5). No `artifact_host` → refused (A2). No slug → refused.

   **And raise the effective tier to at least `high`.** `risk_pool.py:413` —
   `if effective in ("high", "critical")` — is the *only* path to
   `_await_operator`, and nothing anywhere couples `produces=artifact` to a tier
   that reaches it. Without this, a tool declaring `produces: artifact` and
   `risk_tier: low` dispatches with no prompt, and step 3 below spends its whole
   argument protecting an approval surface that never renders. This is the same
   reasoning that put `hardware`'s `risk_default` at `high` in `workers.yaml` —
   a floor, so the worst case is a prompt — applied to the property that actually
   predicts a write rather than to the worker that happens to host it.

   All of these refuse **before `_await_operator`**, because prompting an
   operator to approve a dispatch that cannot succeed teaches them to approve
   without reading.

   **The slug is validated here, before injection, not by the worker.** §5.4's
   *"the daemon supplies a slug, never a path (D7): validated `re.fullmatch`"* has
   an implementation waiting — `validate_slug` (`artifacts.py:205`), which like
   `validate_descriptor` has no non-test consumer. This is its consumer. Relying
   on the worker's `artifact_path` to reject a bad slug would check it *after* the
   operator approved it, and the reserved argument is injected before the snapshot
   at `:399`, so an unvalidated slug lands verbatim in the approval prompt
   (`:466`) and the audit row (`:567`). `validate_descriptor` already reasons this
   way about `path` (`artifacts.py:88-90`): *"an escape sequence rewrites what the
   operator SEES while confirming the path."* A slug carrying `\x1b[2K` or a
   newline does the same to the prompt and to any line-oriented reader of the
   audit log.

   **Every refusal here emits an audit row.** Each existing early return in
   `call_tool`/`_await_operator` emits before returning — `risk_pool.py:454`,
   `:472`, `:485`, `:510` — and a security refusal that leaves no trace is the one
   event an audit log exists for. `Outcome` (`types.py:222-233`) has no member for
   this; the plan picks one and states it rather than leaving the implementer to
   invent it.
3. **Inject the reserved arguments, then snapshot.** `snapshot` at `:399` is what
   reaches the approval prompt. Injecting first means the operator sees the slug
   and drive the call will actually use; injecting after would show the operator
   the model's values and send different ones — an approval surface displaying
   something other than what it approves.

   **The reserved names are wire vocabulary and get the same treatment as every
   other shared constant.** They are agreed between two independently installed
   packages that never share a Python environment — precisely the condition §7
   states a constant and a bidirectional guard for — so they are named constants
   on both sides, guarded, not string literals in two files.

   **The object dispatched at `:517` must be the object snapshotted at `:399`.**
   Not a deepcopy-equal one. Stated as an invariant because the pre-existing
   non-dict-`arguments` defect (§1) sits exactly here, and an injection written as
   `arguments = dict(arguments or {})` would silently make a list-of-pairs succeed
   and keep the divergence, where in-place mutation raises and fails loudly.
4. **Gate and dispatch unchanged.**
5. **Validate — and "after `:517`" is not precise enough.** It goes inside
   `_execute_and_audit` after the dispatch and **before** the `_emit` at `:559`,
   so the audit row can carry the refusal, and it must **not** run on the paths
   that already returned `_ErrorResult` (`:542`, `:513`). Placed instead in
   `call_tool` after `_execute_and_audit` returns, it would receive
   `_ErrorResult` (`risk_pool.py:57-67`) — one text block that does not parse as
   JSON — and every transport failure, reload race and genuine worker error from
   an artifact tool would be re-reported as *"declared produces=artifact but
   returned a non-JSON object"*, masking the real cause.

   `validate_descriptor(payload, spec=spec, tool=tool)` performs containment and
   compares the reported `drive_id` to `spec.artifact_drive_id`.

   **Containment is against `{spec.artifact_root}/{slug}`, not against
   `spec.artifact_root` alone.** An earlier draft said the root, which leaves the
   injected slug binding nothing: the daemon injects `project_slug=alpha`, shows
   it to the operator, records it — and then accepts a descriptor at
   `/mnt/bench-store/beta/dump.bin`. That is §10's Risk 1 scenario exactly, and
   containment against the root would not catch it.

   **One ordering detail:** the reported `host` is checked for well-formedness
   here, but `artifact_host` was already validated at step 2, before the gate — so
   a malformed operator declaration refuses before a 70-second dump rather than
   after it.

**How the descriptor is extracted, and why strictly.** Nothing in any of the
three repos uses `structuredContent` — `_stringify_result`
(`tool_factory.py:19`) reads only `content`, and every worker returns text
blocks. So a `produces=artifact` result must be **exactly one text content block
parsing as a JSON object**. Not "the first block that parses": that is a guess
dressed as a rule, and it silently tolerates a tool that logs a line before its
descriptor. Exactly-one is refusable and therefore checkable.

**Where that rule can be enforced is not where an earlier draft of this spec put
it.** That draft said it "becomes a conformance assertion beside
`_assert_valid_produces_meta` (`conformance.py:63`), so a worker that gets it
wrong fails at build time". **The conformance suites never call a tool.**
`assert_streamable_http_conformance` (`:190-203`) and `assert_stdio_conformance`
(`:296-352`) both do `connect` → `initialize` → `list_tools` → `close`, and
inspect `tool.name`, `tool.inputSchema` and `tool.meta`. There is no `tools/call`
anywhere in the file — verified by grep, which returns nothing.

An assertion about a *result* therefore requires invoking the tool, which for a
real hardware worker means dumping a chip in CI. Two things follow, and the plan
must treat them as work rather than as a line added beside `:63`:

- **What conformance *can* assert today, it should**: that an artifact-declaring
  tool's `inputSchema` declares the reserved argument names. That is a `_meta`/
  schema property, available from `list_tools`, and it is the real mitigation for
  §10's Risk 1 — an author who does not know the name is reserved fails the build.
- **The result-shape rule needs an invocation harness** — a conformance mode that
  calls the tool, which in turn needs artifact tools to support a declared
  no-op/dry-run. That is a discrete piece of design, and until it exists the
  result-shape rule is enforced at dispatch only, at runtime, which is the thing
  the draft claimed to avoid. Say so rather than implying a build-time guarantee
  that does not exist.

**Timeouts, and the silent overwrite they cause.** An earlier draft of this spec
did not contain the word "timeout". It needs to, because the bench's numbers make
the failure routine rather than theoretical: a 2 GB dump at the measured
~28 MB/s takes about 70 s, and the only `read_timeout` precedent an operator has
to copy is `frida`'s `60` at `workers.yaml:63`.

**What a read timeout does was read from the installed SDK, not assumed.**
`mcp/shared/session.py:290-303` wraps the wait in `anyio.fail_after` and, on
expiry, raises `McpError` **locally**; no `notifications/cancelled` is sent, and
the `finally` at `:307` drops the response stream. Nothing in `agent_core`
cancels the server side either. So the worker keeps writing, completes, renames a
perfectly good artifact into place, and answers on a stream nobody is reading —
while the daemon has already reported failure to the model.

The retry is where the data is lost: the temporary is gone (renamed away), so
`O_EXCL` passes cleanly, a second 70-second write runs, and **a plain `renameat`
would silently replace the good file**. §5 step 7's `RENAME_NOREPLACE` is what
makes that second rename fail loudly instead — the two findings share one fix,
which is why they are in one round.

Two requirements follow. An artifact-producing worker's `read_timeout` must
**exceed its worst-case dump time with real margin**, declared in `workers.yaml`
beside `artifact_root`; and because a bound can always be wrong, the
`RENAME_NOREPLACE` refusal is the backstop that keeps being wrong survivable. The
plan must state the floor as a rule tied to the measured rate, not as a literal
number that rots the moment a faster enclosure arrives.

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

Without it the failure is the quiet one: the two sides disagree by one field, and
every dump is refused with a message about *that field being missing* rather than
about the drift that caused it. Stated as a disagreement rather than as "five
versus six" on purpose — the field count changed once already while this spec was
being reviewed, and a literal here would have rotted inside a day.

**Order. An earlier draft of this spec got this wrong, and the CI step it cited
as verified is the thing that refutes it.** That draft had the kit merge first
and said its guard would *skip* agent_core's not-yet-present constant — "honest
and visible". Two facts kill it:

- **A skip is a hard failure here, by design.**
  `pare-worker-kit/.github/workflows/test.yml:53-71` runs the guards and then
  `sys.exit`s if `skipped` is non-zero: *"A skipped cross-package guard proves
  nothing."* §1 cites this step approvingly and the draft then designed an
  ordering that trips it.
- **It would not even skip.** `pytest.importorskip` skips on
  `ModuleNotFoundError`. `agent_core.workers.artifacts` exists on main; a missing
  *attribute* raises `AttributeError`, which is a failure, not a skip. Every
  existing guard in both repos accesses attributes directly, with no `hasattr`
  fallback — so a guard written like its neighbours fails, and one written
  defensively skips, which the CI step then fails anyway.

The mirrored order is red for the same reason, so no ordering of the two PRs as
drafted works. **The constants must land as pure declarations before the guard
that compares them exists:**

1. **Declaration PRs, either order, in both repos.** Each adds the descriptor
   field-set constant and nothing that reads the sibling's. No cross-package
   guard is added or changed, so the existing guards keep passing against the
   existing constants.
2. **Guard PR, in either repo, after both of step 1 are on main.** Now the
   attribute exists on both sides, the guard compares two real values, and it
   neither skips nor raises.
3. **The behavioural PRs** — `open_artifact` in the kit, `_REQUIRED` plus
   `validate_descriptor`'s signature in agent_core, dispatch wiring.
4. **Tag `pare-worker-kit v0.2.0` and `agent_core v1.11.0`.** Tags exist for
   consumer pins, not for CI — CI installs siblings from `main`.
5. **PARE bumps its pin** from `agent_core@v1.10.0`.

The three worker repos are insulated from steps 1-3: all three pin the kit at the
**tag** `v0.1.2` and none installs `agent_core` at all, so nothing in their CI
moves until step 4.

**Two version literals, not one.** `pare-worker-kit` states its version in
`pyproject.toml` *and* at `src/pare_worker_kit/__init__.py:18`, and `stamp_version`
feeds the latter into `serverInfo` — which the networked-workers spec made the
worker-swap detection signal. Step 4 bumps both, or the wire starts lying about
which build is running.

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

## 7a. Sequencing, and why this is not one implementation plan

Counting the discrete units of work this spec requires: roughly nine in
`agent_core`, four in `pare-worker-kit`, five in PARE, plus pin bumps and suite
runs in three worker repos and two tag releases. **Around twenty items across six
repos**, including two breaking changes to a released library and one security
invariant with a race in it. That is far past the four-item round this project
caps changes at, and the cap is not arbitrary — larger rounds are where a fix
introduces a regression worse than the bug.

It also mixes phases with very different stakes, which is the more important half:
running them at one review weight would over-process the mechanical work and
under-process the one part where a silent failure is severe and hard to detect.

- **P1 — wire vocabulary.** Mechanical, fully specified, single-file edits:
  **include code in the plan.** The descriptor field-set and reserved-argument
  constants land in both repos as pure declarations, *then* the bidirectional
  guard (§7), then `_REQUIRED` and `validate_descriptor`'s new signature with
  containment and the drive-id comparison.
- **P2 — `open_artifact`.** Heaviest review, most adversarial framing, **no code
  in the plan** (§5 says why), loopback rig required. The walk, the Linux
  refusal, the failure modes, the tests that discriminate.
- **P3 — dispatch wiring.** Route on the existing `pool.produces()`, injection
  and its ordering against `snapshot`/`_await_operator`, the tier floor, slug
  validation, extraction, the refusals and their audit rows.
- **P4 — release, consumers, acceptance.** Tags, four pin bumps, consuming suites,
  the probe and its tripwire, publication and the retrieval command, the bench
  run.

Each phase gets its own plan. P2 gets the most capable reviewer and an explicit
attack-sequence brief; P1 and P4 do not need one.

## 8. Acceptance

**The wiring's end-to-end property does not need a Tigard.** §14 phrases
acceptance as "a dump on `pare-bench`", which could be read as blocking this work
— there is no Tigard attached. It does not. Every check in §4–§7 concerns a file
on a drive, and the drive exists, is mounted, carries its sentinel and has been
write-verified. A probe that writes bytes exercises the path a flash dump would.

**Level 1 — local, on `agenthost`.** A stdio probe worker (with an explicit
`artifact_host`, per A2). Exercises injection ordering, the `produces` ratchet
across a reload, containment, the drive-id comparison, every refusal path, and
both races from §5. This is where the adversarial tests live.

**The rig is a loopback filesystem, not a temp directory**, and an earlier draft
of this section got that wrong in a way that would have hollowed out the most
important tests. The parent's §14 specifies it — `truncate -s 64M`, `mkfs.ext4`,
mount — because you cannot produce `ENOSPC`, `EROFS`, or an unmounted root on a
tmpdir. With a tmpdir rig the `ENOSPC` and `EROFS` tests get mocked, and a mocked
`ENOSPC` proves nothing about whether the temporary survives, which is the entire
assertion. Every drive state gets forced on the loopback: full mid-write,
read-only, unmounted, wrong id, absent sentinel.

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

**And that is two pages, not one.** The kiosk's home URL is `http://127.0.0.1:8080/`
— the Pi's *own* status server (`bench/status_server.py`), never one served by
`agenthost`, per the parent's §8.1. It reaches the workbench through
`handoff_url` (`build_status`, `:308`), which is only offered when the probes are
green **and** `probe_workbench_exists` (`:238`) confirms the workbench is there,
specifically so a handoff cannot land on the SPA's not-found page. So acceptance
walks that path: status page green, handoff offered, workbench reached, finding
read. Anything that changes `bench/` also needs a `scripts/bench_deploy.sh` run
before it is true at the bench — the unit serving the screen is the installed
copy at `/etc/systemd/system/`, not the file in the repo.

**Two operational additions, both small and both worth doing here.** The status
page has four probes (`PROBE_ORDER`, `:46`) and `scripts/bench_doctor.sh` has
five, and **none of them can answer "did the last artifact dispatch fail, and
why"** — which by design publishes nothing, so the screen stays silent. An
operator at the bench, whose only other route to that answer is a live `pare-cli`
session against `agenthost`, should get it locally. And the probe worker's
`workers.yaml` entry needs a **tripwire, not a sentence**: "leaving it declared is
a finding" is enforced by nobody. `tests/test_workers_yaml_format_comment.py`
reads only lines above the `workers:` key, so it is structurally blind to it, and
`tests/test_risk_overrides_coverage.py` would at most emit a `UserWarning`. A
test that fails on a declared `probe` worker is the enforcement.

**Test discipline.** Every regression test is verified failing against the
pre-change code. No assertion on a count of fields or of tests: assert the
relationship — the kit's constant equals agent_core's; a descriptor the kit
produces validates against the daemon's validator — because a literal breaks on
the next legitimate change and a relationship survives it.

## 9. What this cannot establish

**Containment under `artifact_root` against a *compromised* worker.** This must be
said plainly, because §5.4, §6 and both docstrings describe containment as a
control and a reader finishes them believing `artifact_root` is an enforced
boundary. It is not, against the threat the trust boundary actually names:

- `open_artifact` is a function the worker *chooses to call*. A compromised worker
  calls nothing, runs `ln -s /etc/shadow {root}/{slug}/dump.bin`, and hand-writes
  a descriptor. Daemon-side, every check passes — the path string genuinely is
  under the root, the drive id genuinely matches. The operator's `scp` follows the
  remote symlink.
- So the daemon's lexical containment constrains a **buggy** worker and a hostile
  *client*; against a compromised worker it buys nothing, because `commonpath`
  compares strings and the mapping from string to inode belongs to the attacker.
  §6 calls it defence in depth; there is no first depth for this threat.
- **The retrieval window is hours, not milliseconds.** `open_artifact` closes a
  race at write time; the operator retrieves by path, by hand, long afterwards.
  Anything with write access inside the root can swap the file in between. The
  only detector is the operator comparing the sha256 by hand — and per the
  paragraph below, that comparison cannot establish authenticity either.

What bounds this is not a check in this codebase: it is that `workers.yaml`, which
the worker cannot touch, declares where the producer may write, and that a
compromised bench worker is a compromise of the bench.

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
  not know the name is reserved writes a tool whose argument silently never
  arrives. An earlier draft said this was "mitigated by the conformance assertion
  in §6, not by documentation" — and §6's only proposed assertion was about the
  *result* shape, which is a different thing and, as §6 now records, cannot live
  in the conformance suites at all. The real mitigation is the **input-schema**
  assertion §6 specifies: an artifact-declaring tool must declare the reserved
  names in its `inputSchema`, which `list_tools` already exposes and conformance
  can therefore check at build time.
- **`open_artifact` is Linux-only and the classifier enforces nothing** (A7). The
  runtime refusal is the control, and it is only a control once a test has
  watched it refuse.
- **Two breaking changes to `agent_core` ride together** (§4). Free today
  because nothing consumes the function; the cost of being wrong about that is
  the whole point of §1 being read rather than recalled.
- **The `.partial` left behind is a deliberate ergonomic cost.** If it proves
  intolerable in practice the fix is a named recovery command, never silently
  overwriting on `O_EXCL`.
- **The result-shape rule has no build-time enforcement** until an invocation
  harness exists (§6). Until then it is a runtime refusal, and a worker can ship
  a wrong result shape that only fails at the bench.
- **A `read_timeout` is a guess about hardware that will change.** §6 states it
  as a rule tied to the measured rate rather than a number, and
  `RENAME_NOREPLACE` is the backstop for the guess being wrong — but a dump
  slower than anyone predicted still costs a wasted 70 seconds and a confusing
  failure.
- **This spec was substantially wrong on first draft, in ways a four-reviewer
  panel caught and its author did not.** Recorded as a risk because the same
  failure mode — asserting from the shape of an API rather than from the file —
  is available to whoever writes the implementation plans. The phases in §7a that
  carry security invariants get an attack-sequence brief for that reason, not as
  ceremony.

## 11. Provenance

**Revision note.** v1 of this document was reviewed on 2026-09-12 by four
independent reviewers reading the source rather than the spec. About thirty
findings survived verification; they landed in four commits on
`docs/artifact-wiring-design`, grouped by kind rather than by count. The larger
reversals are narrated in place — A2's host decision, §6's already-existing
ratchet, §7's release order, §8's rig — rather than quietly corrected, because
the reasoning that produced the wrong version is the useful part.

Every file:line in this document was read on 2026-09-12. Machine state — the
daemon active, ArcticBase 200 on api and ui, `agent_core` 1.10.0 in PARE's venv,
`/mnt/bench-store` mounted with 116 GiB free and its sentinel readable, the
JMicron enclosure present, no Tigard — was re-read on the machines the same day
rather than carried over from `2026-09-11-bench-state.md`.
