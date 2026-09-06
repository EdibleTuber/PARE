# ArcticBase adoption and the artifact layer — design

**Date:** 2026-09-06
**Status:** v1 — pending review
**Repos:** `PARE`, `agent_core`, `pare-worker-kit`, `ArcticBase` (consumed, not modified)
**Follows:** [`2026-09-05-networked-workers-design.md`](2026-09-05-networked-workers-design.md)
**Implements:** [`../2026-09-05-approval-channel-decision.md`](../2026-09-05-approval-channel-decision.md) (D8)
**Unblocks:** `pare-hardware-mcp`

> Every claim about existing behaviour in this document was read out of the source
> before being written down, and cited. Where something was not measured, it says so.

## 1. Why

Two problems arrive together, and solving them separately would be worse.

**The bench has no surface.** `pare-bench` (the Raspberry Pi) has a screen. The operator
standing at it, wired to a target, is the person best placed to approve a flash write —
that is the decision already recorded in the approval-channel decision doc. But PARE's
only approval surface is `pare-cli`, which speaks over a **Unix domain socket** and is
therefore local to the daemon host. There is currently no way for the person at the
bench to answer anything.

**Hardware produces files, not results.** Every PARE worker to date returns text: rows,
JSON, smali. A firmware dump is a multi-gigabyte binary. The networked-workers spec
§7.3 recorded that this was unsolved and must not be assumed solved — `CaptureLayer`
runs daemon-side and substitutes a stub only *after* the bytes have crossed the network.

ArcticBase answers the first and constrains the second. It is a self-hosted workbench
host already in this ecosystem, with approval forms, versioned responses, file objects
and a documented agent API — and it states the same trust posture this deployment
already assumes: single-user, no auth, LAN/Tailscale.

## 2. Goals

1. The operator at `pare-bench` can approve a gated call from the Pi's screen, with the
   same authority as the CLI.
2. A hardware artifact never crosses the network unless someone asks for it.
3. One project identity spans the capture store, the workbench, and the bench drive.
4. What a tool produces — a result or an artifact — is declared in its contract, not
   decided by the model at call time.
5. The Pi's screen distinguishes its three failure modes.

## 3. Non-goals

- **Modifying ArcticBase.** It is consumed through its HTTP API.
- **Running ArcticBase on the Pi.** See D2.
- **Cross-machine artifact transfer.** Deferred; see §9.
- **The `runbook` object kind / Shape-2 autonomous mode.** Deferred until
  `pare-hardware-mcp` has tools to grant. Designing an autonomous surface before the
  worker exists is the speculative half of this work.
- **Automatic publishing of every capture.** See D5.
- **Changing the approval timeout.** See §8.

## 4. Decisions

**D1 — One ArcticBase instance, on the inference server. The Pi is a browser.**
Not a preference: `ArcticBase/backend/src/arctic_base/storage/filesystem.py` is
filesystem-backed and uses `fcntl` advisory locks (`.lock` files) for manifest writes
and response monotonicity. Two instances over a network filesystem would corrupt each
other. The Pi runs a browser in kiosk mode against the server over the tailnet, and
runs no ArcticBase code.

**D2 — In a partition, nothing works, and the screen says why.** When the tailnet or
the server is unreachable there is no daemon to dispatch anything, so an approval has
nothing to approve. A local cache or a second instance would have to reconcile
approvals made while partitioned, which is exactly the class that cannot be safely
merged afterwards. See §7 for what the screen must show.

**D3 — Artifacts stay on the machine that produced them.** The bytes live on
`pare-bench`'s external drive. Three layers reference one identity; only descriptors
travel. §5.1.

**D4 — What a tool produces is declared in its contract.** A `_meta` key, the same
mechanism as `RISK_TIER_META_KEY`, so the daemon routes on the contract rather than on
the model's choice of tool. §5.3.

**D5 — Facts publish automatically; interpretation is the model's editorial act.**
A descriptor reaches the workbench because the tool declared it. The *finding* — "this
partition holds a hardcoded key, here is the dump it came from" — is written and
published by the model. The capture store already holds everything and is
FTS-searchable, so publishing every capture would bury the one finding that matters in
a surface whose entire value is that it is curated.

**D6 — The artifact root is operator-declared in `workers.yaml`.** Not in the worker's
environment. The trust anchor is the file the worker cannot touch — the same reasoning
as the risk pins. §5.4.

**D7 — The worker constructs artifact paths; the daemon supplies only a slug.**
Directory names on the daemon host are not trusted input. §5.4.

## 5. Architecture

### 5.1 Three layers, one identity

```
descriptor = { host, path, size, sha256, hashed_at, media_type, produced_by }
```

| Layer | Machine | Holds | Why there |
|---|---|---|---|
| **Artifact store** | producer (`pare-bench`'s drive) | firmware, APKs, dumps | the bytes are already there; moving them is the expensive thing |
| **Capture store** | daemon host, `.pare/` | every tool *result*, including descriptors | FTS-searchable; `/snapshot` renders it with the model out of the path |
| **Workbench** | inference server | approvals, reports, descriptors — never bytes | the curated, human-facing layer |

**Why bytes must not enter the workbench.** `filesystem.py` tars the *entire* workbench
directory in four places — `archive_workbench` (`:275`), `snapshot_workbench` (`:313`),
`export_workbench` (`:470`) and `export_bulk` (`:507`, `:510`) — each a
`tar.add(<workbench dir>, arcname=slug)`. A 2 GB firmware image makes every labelled
checkpoint a 2 GB archive, and a live sqlite database in that tree would be archived
mid-write, with restore-to-new-workbench restoring the tear.

**Why hardware needs no new snapshot architecture.** A hardware tool *result* is a
result, and flows to the capture store at the wire layer exactly like frida's and
mitm's. That is the pattern that already replaced the per-worker store —
`pare/commands/snapshot.py:1` records the removal: *"The frida worker store is gone;
captures are written to the project store at the wire layer."* Only artifacts are
special, and only because they are files. The external drive is for artifacts alone.

### 5.2 One project slug, three places

PARE resolves a project by a git-style `.pare/` walk-up from the CLI's cwd, with a
`$HOME` ceiling and an XDG fallback (`pare/capture_store.py:1-8`). That same resolution
already holds an advisory lock so a second daemon on one project fails loudly rather
than racing FTS writes — the same one-daemon constraint D1 states, enforced a layer
down.

That resolved project becomes **one slug** used for the capture store, the ArcticBase
workbench, and the artifact directory on the bench drive:

```
/mnt/bench-store/{slug}/fw-0001.bin
```

so `ls` at the bench shows that project's artifacts, which matters when you are standing
there with a shell rather than a browser.

### 5.3 Declaring what a tool produces

A second `_meta` key beside the risk tier, owned by `pare-worker-kit` on the server side
and stated in `agent_core` on the client side, with the existing bidirectional guard
test keeping them equal — the pattern established for `RISK_TIER_META_KEY`, and chosen
because the daemon and the worker are separately installed packages on different
machines that never share a Python environment.

```python
ToolSpec("dump_firmware", "high", "...", produces="artifact")
```

Default is `result`. So `hardware_dump_firmware` returns a descriptor and
`hardware_read_uart` returns rows, and the model cannot stream 2 GB back by calling the
wrong tool.

The daemon **validates the descriptor's shape** before treating it as one. A tool that
declares `artifact` and returns something else is a contract violation and is recorded
as an error, not silently treated as a result.

### 5.4 The path is the security boundary

The descriptor is self-reported. Lying about a hash is a data-integrity problem. Lying
about a **path** is different, because the operator will eventually act on it — `scp
pare-bench:$path` — which would turn a worker into a file-exfiltration primitive against
its own host.

Two controls, both necessary:

```yaml
  hardware:
    artifact_root: /mnt/bench-store      # operator-declared; the worker cannot change it
```

1. **The daemon supplies a slug, never a path.** Validated `[a-z0-9-]+`. The worker joins
   it under its own configured root and constructs the filename itself.
2. **The daemon rejects any descriptor whose resolved path escapes `{root}/{slug}`.**
   Resolve symlinks before comparing, or the check is decorative.

### 5.5 The approval channel

Two peers resolving one future. The enforcement core needs **no change**.

`agent_core/workers/tool_approval.py` hands out `(proposal_id, future)` from `request()`
and `resolve(proposal_id, decision)` is called by whoever answers. Today there is
exactly one caller, `agent_core/daemon.py:185`. ArcticBase becomes a second.

**First answer wins, already.** `resolve` does `self._pending.pop(proposal_id)` and its
docstring states the `KeyError` on a second call is *"EXPECTED when a response arrives
after timeout/cancellation; callers must swallow KeyError."* The race was designed for.

Three new pieces:

- **Publisher** — on `request()`, create an `approval-html` object in the project's
  workbench carrying `proposal_id` and the digest below.
- **Poller** — watch `responses?since_version=N`, map the payload to a `ToolDecision`,
  call `resolve()`, swallowing `KeyError`.
- **Cleanup** — when either peer wins, or on timeout, mark the object resolved with the
  outcome, so the bench screen never shows a live prompt for a decided call.

ArcticBase's response model suits this better than a generic form would
(`backend/src/arctic_base/models.py:100`): `ResponseEvent` carries a monotonic
lock-protected `version`, `submitted_by` (`user` | `agent`), and `kind` (`submit` |
`autosave` | `agent-edit`), each append individually audited, with `state_version`
bumped only on a real submit. A bench approval is therefore an append-only record that
already distinguishes who answered and how.

### 5.6 The digest, and what it is actually for

The daemon side is already sound: `ToolCallSpec` is frozen, its `arguments` are a
deepcopy snapshot taken by the pool, and a `ToolDecision` carries no arguments — so an
approval cannot alter the call it approves. The real risk is the **poller binding a
response to the wrong pending call**: a stale object from an earlier prompt, or a
mismatched id.

At `request()`, compute `sha256` over canonical JSON (sorted keys, stable separators) of
`(worker, tool, arguments, declared_tier, effective_tier)`. Publish it; the response
echoes it; **the poller recomputes it from the pending spec and refuses to resolve on
mismatch**, leaving the prompt live so the CLI can still answer and the discrepancy is
visible rather than silently applied.

A short prefix is shown on the form. Stated honestly: no operator compares a sha256 by
eye, and this document does not pretend otherwise. Its value is machine-side. The
visible prefix lets a human confirm two surfaces are discussing the same call, nothing
more.

### 5.7 Critical calls need a different form

Verified in `agent_core/workers/risk_pool.py`, and stricter than assumed:

- **`:387`** — `if effective != "critical" and self.is_session_approved(...)`: a critical
  call never *honours* a session approval.
- **`:463`** — `if decision.scope == "session" and effective != "critical"`: a critical
  call is never *recorded* as session-approved either. Both directions closed.
- **`:454`** — a critical approval with an empty justification is flipped to denied,
  `"justification required for critical tier"`.

Therefore, for `effective == "critical"` the form must:

- offer **no session-scope option at all**. It would be silently discarded, and an
  operator who ticked it would believe they had granted something they had not;
- require a **non-empty free-text justification**, because an empty one is a denial and
  a form that lets you submit one produces a confusing outcome.

For `high`, session scope is real and must be a visually distinct act — not a checkbox
beside *Approve*. That is the second half of the approval-channel decision: session
scope is a different kind of act from a one-time yes.

### 5.8 Hashing

Hashing **during** the write is nearly free: the producing tool already has every byte
in hand as it streams to disk, so sha256 costs CPU but no additional I/O. For an SPI
flash dump the read is slow enough that the hash will not be the bottleneck. The
expensive operation is *re-reading* the artifact afterwards.

So they are two operations answering two questions:

| | Cost | Answers |
|---|---|---|
| hash during the dump | ~free | *this is what came off the chip* |
| `hash_artifact`, a separate tool | I/O-bound, intentional | *this is what is on disk now* |

Chain of custody wants the first; verification wants the second.

**A late hash must not masquerade as a creation-time one.** The descriptor carries
`hashed_at` alongside `sha256`. The gap between "dumped" and "hashed" is precisely what
a chain of custody exists to make visible; a record that hides it is worse than no
record.

This also dissolves an interaction with the previous spec: because the dump does not pay
a separate hashing pass, its `read_timeout` only has to cover the dump itself.

## 6. Trust boundary

Inherited unchanged from the networked-workers spec §6: the tailnet plus the operator's
LAN, no application auth, no TLS between components. ArcticBase states the same posture
in its own README ("self-hosted, single-user, no auth — designed for LAN / Tailscale,
not the public internet"), which is why adopting it does not widen the boundary.

What it **does** add:

- **A second surface that can approve a dispatch.** Anything that can reach ArcticBase
  can submit a response. On this tailnet that is the same population that could already
  reach the daemon's socket host, so it does not widen the set of principals — but it
  does mean ArcticBase's port now sits inside the same blast radius as a worker's, and
  the triggers in the networked spec's §6 ("revisit if...") apply to it identically.
- **A second store of project material.** The workbench holds findings and descriptors.
  It is not sensitive in the way `mitm`'s intercepted cookies are, but it is a second
  place where project material lives, and `filesystem.py`'s trash/export tarballs make
  it trivially exfiltratable as a unit.

Not addressed here, and worth stating: ArcticBase's audit log and PARE's audit log are
separate records. A bench approval will appear in both — ArcticBase's `response.append`
and PARE's dispatch row — but nothing reconciles them. That is acceptable while one
person operates both, and is the first thing to revisit if that stops being true.

## 7. Failure modes

The three the Pi must distinguish, because they have three different fixes and two of
them look identical on a screen:

| What is wrong | What the screen must say |
|---|---|
| tailnet down | cannot reach ArcticBase — network |
| ArcticBase down, server up | workbench unreachable — service |
| **daemon down, ArcticBase up** | **stale** — page renders perfectly, nothing dispatches |

The third is the dangerous one and cannot be detected by the absence of an error. It
needs a **positive liveness signal**: PARE writes a heartbeat object to the workbench,
and the page reports *"daemon last seen 4 minutes ago"* once it goes stale. Absence of
a heartbeat is the signal; absence of an error is not.

Other failures:

- **Poller cannot reach ArcticBase while a prompt is pending.** The CLI remains a peer
  and can still answer. The prompt is not lost. Log, retry, do not fail the dispatch.
- **A response arrives for an unknown `proposal_id`.** Expected — the CLI won, or it
  timed out. Swallow the `KeyError`, mark the object resolved with the real outcome.
- **A descriptor fails root validation.** Record as a tool error naming the offending
  path. Do not publish it, and do not store it as a valid descriptor.
- **The artifact root is not mounted.** The external drive is removable. The producing
  tool must fail loudly at call time rather than writing to the mountpoint's underlying
  directory, which would silently fill the Pi's SD card.

## 8. What is deliberately not changed

**The approval timeout.** `DEFAULT_APPROVAL_TIMEOUT_SECONDS = 120.0`, shared by both
peers (one pending entry, one timer), and on expiry the decision is denied with
justification `"timeout"`. Two minutes may well be short for someone holding a probe at
a bench — but nobody has stood at that bench yet, and changing a fail-closed security
timeout on a guess is the wrong direction to guess in.

The mechanism is recorded here so the decision is cheap when there is evidence:
`risk_pool.py:436` already calls `self._registry.request(spec)`, `request()` already
accepts `timeout_seconds: float | None = None`, and `spec_for(worker)` exists at
`:140` — so a per-worker `approval_timeout` is roughly three lines plus a `WorkerSpec`
field. **The measurement that settles it is the first real flash-write approval at the
bench.**

Note also that the approval timeout is a *human* response window, not a network one.
Dispatch bounds are `connect_timeout`/`read_timeout` from the previous spec; tailnet
latency measured 8.4 ms median. A remote worker does not need a longer approval window
because it is remote — it needs one because of where the operator is standing.

**The `static` worker's name.** It was considered and rejected. Every tool description in
`pare-static-mcp/contract.py` opens with the literal word `STATIC`, and `load_apk`
instructs the model to *"corroborate with the frida (dynamic) worker"* — so static ↔
dynamic is the vocabulary the model reads on every turn to choose a worker, and the
rename's real cost is re-teaching that rather than the six files it touches. The name is
also already accurate: "APK and IPA only" is a scope fact, not a naming problem. Revisit
if `pare-hardware-mcp` grows firmware static-analysis tools someone would plausibly look
for under `static_`.

## 9. Deferred, with the reason

- **Cross-machine artifact transfer.** The IoT-Android case is real: firmware dumped on
  `pare-bench`, an APK carved from it, and `static_load_apk` takes *"an APK from a host
  path"* (`pare-static-mcp/contract.py:26`) — a path local to whichever machine `static`
  runs on. So that APK must move. But the size argument that keeps firmware on the bench
  does not transfer to an APK, and running a second `static` instance collides with
  §5.5 of the networked spec (the worker name is the tool prefix). The descriptor is
  shaped to carry this later — `host` is a field, not an assumption — and v1 leaves the
  transfer manual, with the workbench showing the exact command. Unbuilt automation
  beats wrong automation.
- **The `runbook` kind and Shape-2 autonomous mode.** Its value depends on
  `pare-hardware-mcp` having tools to grant, and that repo has zero files.
- **Pulling APKs off devices.** `pare-frida-mcp` has no such tool today; verified. Mobile
  workflow is still being designed.

## 10. Testing

- A response for a stale `proposal_id` resolves nothing and raises no error to the
  operator.
- Two peers answering the same prompt: the first wins, the second is a no-op, and the
  audit records exactly one decision.
- A digest mismatch **refuses** to resolve and leaves the prompt live — verified failing
  against a build without the check.
- A `critical` prompt's form offers no session option and rejects an empty
  justification; a `high` prompt's session control is distinct from its approve control.
- A descriptor whose path escapes `{root}/{slug}` is rejected, including via a symlink
  that resolves outside it.
- A slug containing `..`, `/`, or an absolute path never reaches the worker.
- An artifact tool whose root is unmounted fails the call rather than writing to the
  mountpoint.
- `hashed_at` is present and later than the dump when `hash_artifact` supplies the hash;
  equal to the dump time when hashed inline.
- The heartbeat goes stale when the daemon stops, and the Pi's page says so.
- End to end on real hardware: a gated call approved from the Pi's screen, dispatched,
  and audited — with the CLI never touched.

## 11. Sequencing

1. **`agent_core` + `pare-worker-kit`** — the `produces` meta key, descriptor validation,
   `artifact_root` on `WorkerSpec`, slug and path validation. The cross-repo contract,
   and it must land before `pare-hardware-mcp` is written against it.
2. **PARE — the ArcticBase client** — workbench-per-project resolution, publisher,
   poller, digest, cleanup, heartbeat. No enforcement-core changes.
3. **The approval form** — tier-aware, per §5.7.
4. **The Pi as a kiosk** — browser, the three failure states, systemd.
5. **`pare-hardware-mcp`** — its own spec, now with a transport, a liveness story, an
   artifact contract and an approval surface to be born into.

## 12. Risks

- **ArcticBase is a dependency this project does not control.** Its API is consumed, not
  vendored. A breaking change to the response model or the object API breaks the
  approval channel. Mitigation: the CLI remains a full peer, so an ArcticBase outage
  degrades to today's behaviour rather than blocking dispatch.
- **The descriptor is self-reported.** The path controls in §5.4 bound the damage but do
  not make a hostile worker honest. This is the same posture as the wire-advertised risk
  tier: the operator-declared floor is what a worker cannot touch.
- **Two audit logs, unreconciled.** §6.
- **The bench form is new attack surface for confusion, not for privilege.** The worst
  case is an operator approving something they misread — which is why the digest binds
  the display to the dispatch and why `critical` cannot be session-scoped.
