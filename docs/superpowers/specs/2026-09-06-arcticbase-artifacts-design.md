# ArcticBase adoption and the artifact layer — design

**Date:** 2026-09-06
**Status:** v2 — rewritten after a four-lens review panel
**Repos:** `PARE`, `agent_core`, `pare-worker-kit`, `ArcticBase` (consumed, not modified)
**Follows:** [`2026-09-05-networked-workers-design.md`](2026-09-05-networked-workers-design.md)
**Partially defers:** [`../2026-09-05-approval-channel-decision.md`](../2026-09-05-approval-channel-decision.md) — see §6
**Unblocks:** `pare-hardware-mcp`

> **v2 changes.** A panel (fact-check, security, framework API, operations) reviewed v1
> and found **two independent blockers** on its central proposal — putting the bench
> approval channel on ArcticBase. Both are fatal and both point the same way, so that
> proposal is withdrawn and replaced (§6). The artifact layer survived with real
> corrections. v1 also contained several factual errors, corrected in place and listed
> in §13.
>
> Every claim about existing behaviour here was read out of the source before being
> written down. Where something was not measured, it says so.

## 1. Why

**Hardware produces files, not results.** Every PARE worker to date returns text. A
firmware dump is multi-gigabyte binary. The networked-workers spec §7.3 recorded this as
unsolved and warned it must not be assumed solved: `CaptureLayer` runs daemon-side and
substitutes a stub only *after* the bytes have crossed the network.

**The bench has no view.** `pare-bench` (the Pi) has a screen. The operator standing at
it, wired to a target, currently has no way to see what the agent found — the capture
store and its `/snapshot` renderer are on the inference server, reachable only through
`pare-cli` over a Unix socket.

ArcticBase — already in this ecosystem — answers the second and constrains the first: a
self-hosted workbench host with file objects, themed rendering and an agent API.

## 2. Goals

1. A hardware artifact never crosses the network unless someone asks for it.
2. One project identity spans the capture store, the workbench and the bench drive.
3. What a tool produces — a result or an artifact — is declared in its contract, not
   decided by the model at call time.
4. The operator at the bench can **see** the agent's findings and the artifact index.
5. The operator at the bench can **approve** a gated call without walking to a desk.
6. The Pi's screen distinguishes its failure modes, including the one that looks healthy.

## 3. Non-goals

- **Modifying ArcticBase.** It is consumed through its HTTP API.
- **Running ArcticBase on the Pi.** D1.
- **Approvals submitted through ArcticBase.** Withdrawn from v1 with reasons — §6.
- **Cross-machine artifact transfer.** Deferred; §11.
- **Adopting the `runbook` object kind** (it already ships in ArcticBase; what is
  deferred is *use* of it) and the bounded-capability-grant autonomous mode that would
  build on it. Both wait until `pare-hardware-mcp` has tools to grant.
- **Automatic publishing of every capture.** D5.
- **Changing the approval timeout.** §12.

## 4. Decisions

**D1 — One ArcticBase instance, on the inference server. The Pi is a browser.**
Its storage is filesystem-backed with `fcntl` advisory locks
(`ArcticBase/backend/src/arctic_base/storage/filesystem.py`), so two instances over a
network filesystem would corrupt each other. The Pi runs no ArcticBase code.

**D2 — Artifacts stay on the machine that produced them.** Three layers reference one
identity; only descriptors travel. §5.1.

**D3 — What a tool produces is declared in its contract.** A `_meta` key, so the daemon
routes on the contract rather than on the model's choice of tool. §5.3.

**D4 — Facts publish automatically; interpretation is the model's editorial act.** A
descriptor reaches the workbench because the tool declared one. The *finding* is written
and published by the model. The capture store already holds everything and is
FTS-searchable, so publishing every capture would bury the one finding that matters in a
surface whose value is that it is curated.

**D5 — Model-authored reports are `md`, never `html`.** ArcticBase renders `html`
objects in an iframe with **no `sandbox` attribute** (`frontend/src/lib/themed/
HtmlViewer.svelte:32-36`; no `sandbox` appears anywhere in `frontend/src`), from a
same-origin URL, and its postMessage bridge takes its target workbench and object from
the *message payload* with no origin check (`frontend/src/lib/bridge.ts:48`, `:77`).
Model-authored HTML would therefore execute with full access to the ArcticBase API.
`mini-md.ts:3` states markdown rendering *"HTML escapes everything else; no
`<script>`/`<iframe>`/raw HTML pass-through"*, which removes the class. **Confirm the
server-side md render path escapes as the inline renderer does before relying on this.**

**D6 — The artifact root is operator-declared in `workers.yaml`,** not in the worker's
environment. The trust anchor is the file the worker cannot touch — the same reasoning
as the risk pins. §5.4.

**D7 — Containment is enforced on the worker; the daemon's check is lexical only.**
Corrected from v1, which put symlink resolution on the daemon. §5.4.

**D8 — Approvals happen through `pare-cli` running at the bench.** §6.

## 5. Architecture

### 5.1 Three layers, one identity

```
descriptor = { host, path, size, sha256, hashed_at, media_type, produced_by, drive_id }
```

| Layer | Machine | Holds |
|---|---|---|
| **Artifact store** | producer — `pare-bench`'s drive | the bytes |
| **Capture store** | daemon host, `.pare/` | every tool *result*, including descriptors |
| **Workbench** | inference server | reports and descriptors — never bytes |

**Why bytes must not enter the workbench.** `filesystem.py` tars the entire workbench
directory in `archive_workbench` (`:275`), `snapshot_workbench` (`:313`),
`export_workbench` (`:470`) and `export_bulk` (`:507`, `:510`). A 2 GB image makes every
labelled checkpoint a 2 GB archive, and `get_snapshot_bytes` (`:338-342`) reads a whole
tarball into memory to serve it.

**This must be enforced, not merely stated.** `ARCTIC_BASE_MAX_UPLOAD_BYTES` defaults to
2 GB — precisely the failure above. PARE posts only markdown and JSON descriptors here,
so set it to **8 MiB**. One environment line converts a rule into an enforcement.

**Why hardware needs no new snapshot architecture.** A hardware tool *result* is a
result and flows to the capture store at the wire layer like every other worker's. That
is the pattern that already replaced the per-worker store —
`pare/commands/snapshot.py:4-5`: *"The frida worker store is gone; captures are written
to the project store at the wire layer."* Only artifacts are special, and only because
they are files.

### 5.2 One project slug, three places — derived once, then stored

PARE resolves a project by a `.pare/` walk-up from the CLI's cwd
(`pare/capture_store.py:1-8`). That resolution yields a *path*, not a name; nothing in
PARE produces a slug today.

**The slug is derived once, on first use, and written into `.pare/project`.** Never
re-derived from the directory name — otherwise a `mv` silently orphans both the
workbench and the artifacts.

**One alphabet, ArcticBase's, because it is the strictest consumer.**
`filesystem.py:54`: `SLUG_RE = ^[a-z0-9][a-z0-9_-]{0,63}$`. v1 proposed `[a-z0-9-]+`,
which is wrong three ways: unanchored (so `proj/../../etc` passes `re.match`), admits a
leading `-` (argument injection into the `scp`/`tar` commands §11 says the workbench will
display), and disagrees with the rule ArcticBase enforces. Use `re.fullmatch` and
require a leading alphanumeric.

**Collisions are real:** `/work/a/target` and `/work/b/target` are two projects with one
basename. Append a short hash of the resolved path.

**No project, no publish.** `resolve_capture_db` falls back to
`xdg_state/captures/{channel_id}.db` when there is no `.pare/` marker — keyed per CLI
launch. A slug derived from that would create a new workbench on every launch. A gated
call from a cwd with no project **fails loudly, naming the cwd**, rather than inventing
a workbench.

### 5.3 Declaring what a tool produces

A second `_meta` key beside the risk tier. Default `result`.

**This is more work than v1 implied.** `ToolSpec` is **not** in `pare-worker-kit` — it is
a separate private dataclass in each worker repo, and they are not the same shape
(`pare-mitm-mcp` and `pare-static-mcp` have five fields; `pare-frida-mcp` has six). Each
`server.py` builds a one-key meta dict literal. And `pare-mitm-mcp/tests/test_server.py`
asserts `tool.meta == {RISK_TIER_META_KEY: spec.risk_tier}` — an exact-equality check
that any second key breaks, in a repo v1 did not list as modified.

Two questions v1 left open, both answered here:

- **Monotonicity.** The wire tier is cached at discovery in `_tool_tiers` and ratcheted
  in `_tier_highwater`, which `_bump()` deliberately never clears, because a reload
  would otherwise be a downgrade channel. `produces` gets the same treatment: **once a
  tool has been seen to produce artifacts, it may not later be treated as producing
  results.** Otherwise a reload disables descriptor validation.
- **Enforcement point.** The risk tier is checked at *build* time by
  `_assert_valid_risk_tier_meta` in the conformance suites, which is described there as
  the compensating control for having no runtime fail-safe. `produces` gets the
  equivalent conformance assertion. An unrecognised value is a conformance failure, not
  a silent default.

The bidirectional guard test pattern applies: both packages state the constant, and each
suite asserts the other's matches when it is installed.

### 5.4 The path is the security boundary

The descriptor is self-reported. Lying about a hash is a data-integrity problem. Lying
about a **path** is different: the operator will act on it, and a descriptor naming
`/etc/shadow` would make the worker a file-exfiltration primitive against its own host.

```yaml
  hardware:
    artifact_root: /mnt/bench-store        # operator-declared
    artifact_drive_id: 7c9f-…              # sentinel UUID; see §5.5
```

**Containment is enforced on the worker.** v1 put symlink resolution on the daemon. It
cannot go there: the artifact is on `pare-bench` and the daemon has no view of that
filesystem, so `Path.resolve()` daemon-side resolves against the *wrong* namespace —
worse than not checking. The worker opens under `{root}/{slug}` with `O_NOFOLLOW`,
`lstat`s to confirm a regular file, and checks `st_dev` matches the mounted root. The
daemon then does the lexical check it *can* do — absolute, normalised, contained, no
`..` — as defence in depth.

**Be honest about what this buys.** A worker-side check defends against a *buggy* worker
and a confused path, not against a hostile one. The control that survives an untrusted
producer is content-addressing: the operator's retrieval verifies `sha256` after
transfer and refuses on mismatch.

**A missing root fails closed.** `WorkerSpec` sets no `model_config`, so pydantic's
`extra="ignore"` applies — the class documents this hazard in situ for `autoload`. An
older `agent_core` would silently drop `artifact_root`. Because this one is a security
control rather than an ergonomic default: **a `produces="artifact"` dispatch against a
worker with no resolved `artifact_root` is refused**, and `WorkerSpec` gains
`model_config = ConfigDict(extra="forbid")` so a typo is an error rather than an absent
control.

**The daemon supplies a slug, never a path** (D7): validated `re.fullmatch`, joined by
the worker under its own root.

### 5.5 Artifact integrity

**Hashing during the write is nearly free** — the producing tool already has every byte
in hand, so sha256 costs CPU but no additional I/O, and for an SPI flash dump the read
is the bottleneck. Re-reading afterwards is the expensive operation, so it is a separate
`hash_artifact` tool.

| | Cost | Answers |
|---|---|---|
| hash during the dump | ~free | *this is what came off the chip* |
| `hash_artifact` | I/O-bound, intentional | *this is what is on disk now* |

**`hashed_at` is recorded** so a late hash cannot masquerade as a creation-time one. The
gap is exactly what a chain of custody exists to make visible.

**A truncated dump must not produce a valid-looking descriptor.** Hashing inline means a
dump killed by `ENOSPC` at 1.4 GB of 2 GB yields a descriptor with a correct `size`, a
correct `sha256` *of the truncated bytes*, and a correct `hashed_at` — indistinguishable
from a good dump. On a bench drive, ext4's default `errors=remount-ro` makes this
routine. Therefore:

- **Preflight:** `os.path.ismount(root)`, a write-probe (`ismount` is true for a
  read-only mount), and `disk_usage(root).free` against a declared expected size.
- **Write to `{name}.partial`**, then `flush()`, `os.fsync()`, `close()`, then rename. A
  buffered `write()` does not report `ENOSPC` — it surfaces at flush or close.
- **Publish a descriptor only for a file that reached the rename.** A `.partial` left on
  the drive is visible to `ls` at the bench, which is §5.2's property doing real work.
- **Name the drive.** A sentinel `{root}/.bench-store-id` whose UUID is declared beside
  `artifact_root`. `ismount` cannot tell project A's drive from project B's, and writing
  a dump to the wrong stick is otherwise silent.
- **Distinguish `EROFS`** in the error text; "permission denied" sends the operator to
  the wrong problem. State ext4: FAT32 fails at 4 GiB with `EFBIG`.

## 6. The approval surface — and why it is not ArcticBase

The approval-channel decision holds: *the person best placed to approve a flash write is
the one looking at the wired-up target.* v1 proposed satisfying that through ArcticBase.
The panel found two independent blockers, either of which is disqualifying.

**Blocker 1 — ArcticBase has no authentication, and the surface it would replace is
kernel-gated.** Today's approval path is a Unix domain socket at
`$XDG_RUNTIME_DIR/<agent>.sock` (`agent_core/config.py:16-19`), i.e. `/run/user/<uid>`,
mode 0700: **one local uid on the inference server**. ArcticBase has no auth of any kind
and defaults to `host: "0.0.0.0"` (`config.py:12`). The principal set would go from one
uid to anything that can route to port 2929 — including every worker.

That inverts the networked-workers spec's central security argument, which is that *"the
risk gate never derived authority from the worker."* A compromised `frida` worker could
read the pending approval object, POST a `submit` response, and approve its own
`frida_execute_script` — pinned `critical` precisely so it cannot run unapproved. The
digest does not prevent this: it is a public value read from the object, and echoing a
value you can read proves nothing. `PUT /content` also replaces an object's bytes in
place with the same id and no auth, so the *displayed* form can be swapped while the
digest still matches the call underneath. And `submitted_by` is a client-supplied request
field, not an observation — it records what the poster claimed.

**Blocker 2 — the approval path cannot exist outside a live CLI turn.**
`_await_operator` fails closed at `send is None` **before** `self._registry.request(spec)`
(`risk_pool.py:419-431`, `:436`), and PARE passes `send_message=None`, relying on the
per-request `ctx.emit` of a live socket connection (`pare/agent.py:152`). No attached
`pare-cli`, no approval request, nothing to publish. And when that connection drops, the
daemon cancels the turn and `discard(proposal_id)` runs — so a dropped SSH session on the
walk to the bench would strand a live-*looking* form that can never resolve.

### D8 — `pare-cli` runs at the bench

The Pi's screen does two jobs:

| Surface | Purpose |
|---|---|
| **Browser, kiosk** | the workbench: findings, descriptors, the artifact index |
| **Terminal** | `ssh <server> -t 'tmux attach'` — where approvals happen |

This satisfies the decision's intent exactly — the prompt appears where the operator is
standing, with the *same authority* as any other CLI, because it *is* the CLI. It adds
no attack surface, needs no new code, and dissolves Blocker 2 rather than working around
it: the connection is at the bench, so there is no walk during which it can drop.

The cost is honest: a terminal, not a touch form, and `tmux` on the server is
load-bearing rather than a convenience.

**What would revive the ArcticBase form:** an authenticated approval surface — either
upstream auth in ArcticBase, or PARE serving its own form on its own port with a key the
Pi holds out of band. Both are real work. Neither belongs in this spec.

## 7. Trust boundary

Inherited from the networked-workers spec §6. What this design adds:

**A second store of project material,** on an unauthenticated port. `GET /api/archive`
returns a tar.gz of **every workbench** in one request with no parameters. On a tailnet
this is a confidentiality exposure, not an integrity one — but it is larger than v1's §6
implied, and the workbench now holds findings.

**v1's claim that adoption "does not widen the boundary" was wrong** and is withdrawn.
It rested on both READMEs stating the same posture, while ArcticBase ships the exact
wildcard bind `pare-worker-kit` refuses with a four-sentence explanation
(`serve.py:139`). Required deployment constraints, none of them optional:

- Publish as `-p <tailnet-ip>:2929:2929`. A bare `-p 2929:2929` writes DNAT into the
  `DOCKER` chain and **bypasses `ufw`**.
- `ARCTIC_BASE_HOST` pinned to the tailnet address.
- A Tailscale ACL denying worker-tagged nodes any access to the server's :2929. The
  previous spec recommends ACLs daemon→worker; worker→server is the direction that
  matters here.

**Two audit logs, unreconciled.** ArcticBase's `audit.jsonl` and PARE's dispatch rows are
separate records, and nothing joins them. Acceptable while one person operates both.

## 8. Failure modes

### 8.1 The Pi cannot report a failure using a page served by the thing that failed

v1 assigned "cannot reach the network" and "service unreachable" to a page hosted on the
inference server. Cold-boot the Pi with the server down and Chromium shows its own
interstitial, in kiosk mode, to someone holding two probes. And an already-loaded page
**cannot distinguish those two states**: `fetch()` surfaces both as an opaque
`TypeError: Failed to fetch`.

**Invert it.** The kiosk's home URL is a small **locally served** status page on the Pi,
with its own systemd unit. It probes in order and hands off only when all are green:

| Probe | Distinguishes |
|---|---|
| `tailscale status --json`, locally | network |
| `GET http://<server>:2929/api/health` | ArcticBase service |
| the daemon heartbeat | daemon stale |
| the daemon's active project vs the one displayed | wrong workbench |

### 8.2 Liveness

**The heartbeat is written by the poller-equivalent task itself**, not by an independent
timer — a heartbeat on its own timer keeps ticking through a wedged daemon, which is the
state it exists to reveal. It carries a **boot id** and the **active project slug**, so a
restarted or differently-scoped daemon is visible rather than continuous.

**It lives in one dedicated workbench** (`pare-daemon-status`), never a project's.
`put_object_content` writes an `audit.jsonl` row on every call (`filesystem.py:833-851`,
`audit()` at `:381-392`), there is no rotation anywhere, `read_audit` reads the whole
file into memory, and `snapshot_workbench` tars it. A 60 s beat in a project workbench
would make its audit trail — the record you want after a bricked target — mostly
heartbeat noise. Beat at 60 s, stale at 3 minutes.

**Never use the Pi's clock.** It has no RTC; a Pi off over a weekend boots believing it
is Friday. Take "now" from the `Date:` response header of the status page's own probe —
the server's clock, free with every poll, no ArcticBase change. If the Pi's own clock
differs by more than a minute, say so on screen.

### 8.3 The screen itself

Disable console blanking and DPMS explicitly in the kiosk unit, and give the page a
permanently-moving element — the heartbeat age, ticking — so *"the screen is alive"* is
answerable from three feet away. A blank screen otherwise looks identical to a crash.

Launch Chromium `--incognito`: `main.py:70-83` serves `index.html` with no
`Cache-Control`, so after an ArcticBase upgrade a cached `index.html` can reference
content-hashed assets that now 404 — a white screen on a machine nobody is logged into.

### 8.4 Other

- **A descriptor fails validation.** Record as a tool error naming the offending path.
  Do not publish it.
- **The artifact root is unmounted, full or read-only.** §5.5. Do **not** use
  `RequiresMountsFor` on the worker unit: with the drive absent the unit never starts,
  the port never listens, and `/worker list` says `UNREACHABLE` — the same string it
  prints for "the Pi is off". Start unconditionally and fail at call time with a message
  naming the mountpoint. Add a low-tier `hardware_bench_status` tool so `/health` can ask
  without dispatching a dump.
- **`/etc/fstab` must carry `nofail,x-systemd.device-timeout=10`.** Without `nofail`, a
  Pi booted at the bench with the drive unplugged drops to an emergency shell — a brick,
  on a machine with no keyboard.

## 9. Retention

v1 had none. In the order these actually fill:

| What | Where | Rule |
|---|---|---|
| Docker's `json-file` log | inference server root fs | `max-size: 10m, max-file: 3` in the compose service |
| `.pare/blobs/` | daemon host | age-based prune; no prune exists today |
| PARE's audit rows | `~/.local/share/pare/audit` | rotate; one row per dispatch, forever |
| ArcticBase `trash/`, `snapshots/` | inference server | only `restore_workbench` ever deletes one |
| the bench drive | `pare-bench` | last to fill, and the only one the design already helps |

The first is the cheapest and most urgent: when the server's root fills, ArcticBase's
`audit()` swallows the exception (`filesystem.py:391`, `except Exception: pass`) and the
approval audit trail begins **lying by omission**.

## 10. Diagnosis

When something does not arrive there are five candidates: tailnet, ArcticBase, daemon,
worker, drive.

1. **Extend `/health`** with an ArcticBase block: base URL and reachability, resolved
   project slug and workbench URL, last heartbeat write, and per-worker `artifact_root`
   mounted / free bytes / drive id.
2. **`scripts/bench_doctor.sh`, daemon-independent**, run from the Pi's own shell:
   `tailscale status`, `curl -m3 <server>:2929/api/health`, `ss -tlnp | grep <port>`,
   `mountpoint` + `df -h`. Five lines naming which of the five is at fault — and it works
   in the state you most need it, which is daemon down.
3. **Print the workbench URL in the CLI** when a gated call is made, so the operator
   knows which page to look at before walking away.

## 11. Deferred, with the reason

- **Cross-machine artifact transfer.** The IoT-Android case is real: firmware dumped on
  `pare-bench`, an APK carved from it, and `static_load_apk` takes *"an APK from a host
  path"* (`pare-static-mcp/src/pare_static_mcp/contract.py:26`) — local to whichever
  machine `static` runs on. But the size argument that keeps firmware on the bench does
  not transfer to an APK, and a second `static` instance collides with §5.5 of the
  networked spec. The descriptor carries `host` as a field rather than an assumption, so
  it can carry this later. v1 leaves transfer manual, with the workbench showing the
  command.
- **The `runbook` kind and the bounded-capability autonomous mode.** The kind already
  ships; its *use* waits for a worker with tools to grant.
- **Pulling APKs off devices.** `pare-frida-mcp` has no such tool; verified.
- **Approvals through ArcticBase.** §6.

## 12. What is deliberately not changed

**The approval timeout.** `DEFAULT_APPROVAL_TIMEOUT_SECONDS = 120.0`, and on expiry the
decision is denied with justification `"timeout"`. With D8 putting the CLI at the bench,
the walk is no longer inside the window, which removes most of the pressure to raise it.
The mechanism is recorded for when there is evidence: `risk_pool.py:436` already calls
`self._registry.request(spec)`, `request()` already accepts `timeout_seconds`, and
`spec_for(worker)` is at `risk_pool.py:140` — so a per-worker `approval_timeout` is about
three lines plus a `WorkerSpec` field. **The measurement that settles it is the first
real flash-write approval at the bench.**

**The `static` worker's name.** Every tool description in its contract opens with the
literal word `STATIC`, and `load_apk` instructs the model to *"corroborate with the frida
(dynamic) worker"* — so static ↔ dynamic is the vocabulary the model reads every turn to
choose a worker, and the rename's real cost is re-teaching that. Revisit if
`pare-hardware-mcp` grows firmware static-analysis tools someone would look for under
`static_`.

## 13. Corrections to v1

- *"'APK and IPA only' is a scope fact"* — **wrong**. `pare-static-mcp` has no IPA or iOS
  support; its README describes it as *"Static-analysis MCP worker for PARE (Android
  APK)."* That was a stated future intent recorded as present scope. (Its README also
  claims "seven read-only tools" while the worker serves ten — a stale count worth
  removing rather than correcting.)
- *"`submitted_by` (`user` | `agent`)"* — **`Submitter` has three members**: `user`,
  `agent`, `system` (`models.py:94-97`). And it is client-supplied, so it attributes
  nothing.
- *"The enforcement core needs no change"* — **wrong**, and moot now that §6 withdraws the
  poller. `ToolApprovalRegistry`'s entire public surface is `request`, `resolve`,
  `discard`, `is_pending`; the pending `ToolCallSpec` is not exposed, and `resolve()`
  retains no outcome.
- The daemon-side symlink check — **unimplementable as specified**; moved to the worker
  (§5.4).
- *"The digest binds the display to the dispatch"* — **wrong**; withdrawn with §6.
- The `runbook` kind described as deferred construction — it already ships; what is
  deferred is adoption.
- Two of five `tar.add` citations did not have the form described; all five line numbers
  are correct and the conclusion is unchanged.

## 14. Testing

- A descriptor whose path escapes `{root}/{slug}` is rejected — and the worker-side
  `O_NOFOLLOW` check rejects a symlink that resolves outside it.
- A slug containing `..`, `/`, a leading `-`, or 70 characters never reaches the worker,
  and one that ArcticBase's `SLUG_RE` would reject never reaches ArcticBase.
- A project renamed on disk keeps its slug, workbench and artifacts.
- Two projects with the same basename at different paths get different slugs.
- A gated call from a cwd with no `.pare/` fails loudly naming the cwd.
- `produces` is monotonic across a reload: a tool seen as `artifact` cannot become
  `result`.
- A dispatch against a worker with no resolved `artifact_root` is refused.
- **Every drive state, forced with a loopback filesystem** (`truncate -s 64M`, `mkfs.ext4`,
  mount): full mid-write, read-only, unmounted, wrong drive id. A dump that hits `ENOSPC`
  publishes **no** descriptor and leaves a `.partial`.
- `hashed_at` equals the dump time when hashed inline, and is later when supplied by
  `hash_artifact`.
- The heartbeat goes stale when the daemon stops — **and with the Pi's clock skewed an
  hour in both directions, the page still reports the correct age.**
- **Unattended:** the kiosk recovers from an ArcticBase restart with a rebuilt frontend
  without a human touching it, and the Pi boots with the drive absent without dropping to
  an emergency shell.
- End to end on real hardware: a dump on `pare-bench`, its descriptor in both stores, its
  finding published as `md`, read from the Pi's screen, with the approval taken at the
  bench through `pare-cli`.

## 15. Sequencing

0. **Prerequisite — the pins.** The networked-workers spec's D5 is **still unmet**:
   `workers.yaml` declares `hardware` at `risk_default: medium` with zero
   `risk_overrides`, and `risk_pool.py:386` gates only `high` and `critical`. A
   `hardware_flash_write` that fails to advertise its tier therefore dispatches **with no
   prompt at all**, bypassing every approval surface. This lands first or nothing else
   matters.
1. **`agent_core` + `pare-worker-kit`** — the `produces` meta key with its conformance
   check and ratchet, descriptor validation, `artifact_root` on `WorkerSpec` with
   `extra="forbid"`, slug validation. Cross-repo contract; must precede
   `pare-hardware-mcp`.
2. **The three existing workers** — `ToolSpec` gains the field, `server.py` builds a
   two-key meta dict, and `pare-mitm-mcp`'s exact-equality meta test becomes a subset
   assertion.
3. **PARE — the ArcticBase client** — workbench-per-project, descriptor and report
   publishing as `md`, the heartbeat, `/health` extension.
4. **The Pi** — kiosk browser, the local status page, `bench_doctor.sh`, systemd.
5. **`pare-hardware-mcp`** — its own spec, now with a transport, a liveness story, an
   artifact contract and a working bench.

## 16. Risks

- **ArcticBase is a dependency this project does not control**, consumed not vendored. A
  breaking change to its object API breaks report publishing. It no longer breaks
  approvals, because §6 removed them from that path — which is one more argument for the
  split.
- **The descriptor is self-reported.** §5.4 bounds the damage; content-addressing on
  retrieval is what survives a hostile worker.
- **The unsandboxed render iframe** remains a live issue for any `html` object. D5 keeps
  model-authored content out of that path; it does not fix ArcticBase.
- **Two audit logs, unreconciled.** §7.
