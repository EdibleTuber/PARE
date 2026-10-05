# Artifact wiring — P2 (`open_artifact`) — implementation plan

**Date:** 2026-10-02
**Spec:** PARE `docs/superpowers/specs/2026-09-12-artifact-wiring-design.md` — §5 (lines 277–462, primary),
§7a (P2 line), §8 (the rig), §4 (descriptor fields), A7 (Linux)
**Parent spec (context only):** PARE `docs/superpowers/specs/2026-09-06-arcticbase-artifacts-design.md` — §5.5
**Status:** planned, **not started**. Runs in a FRESH session via `superpowers:executing-plans`; the ledger lives at
`PARE/.superpowers/sdd/2026-10-02-artifact-wiring-p2/progress.md`.
**There is no implementation code in this plan, on purpose.** §7a: P2 gets *"heaviest review, most adversarial
framing, no code in the plan (§5 says why), loopback rig required."* §5 says why: the walk is a security invariant
with an ordering requirement and a race in it, and code written into prose is unexecuted and gets transcribed
faithfully. This plan specifies signatures, invariants, the exact failure-mode taxonomy, what each test
discriminates, and verification commands. The implementer writes the code against the actual codebase.

Nothing in this plan touches `agent_core`, PARE's code, or the bench drive. The hardware console-capture plan stays
halted at its Task 1 gate until P1–P4 have landed.

## What P2 is, and is not

P2 (this plan), in the spec's own order (§5, §7a, §8):

1. **`open_artifact`** in `pare_worker_kit.artifacts`, signature exactly §5:
   `open_artifact(root, slug, name, *, media_type, expect_drive_id, expected_size)` — a context manager
   yielding a writer. The writer exposes `write(b)`, hashing inline, and, after a clean exit, `.descriptor`:
   the worker-reported fields of §4 — the seven fields P1 pinned as `ARTIFACT_DESCRIPTOR_FIELDS`.
2. **The seven-step walk** of §5, with the ordering as a security invariant: every step after the first is
   relative to a directory *descriptor*, never re-derived from a path.
3. **The four invariants**, in priority order, verbatim from §5: *No component after `root` is ever resolved
   through a symlink. No path string is re-opened after being checked. Drive identity is established before the
   first byte is written. A descriptor exists only for a file that reached step 7.*
4. **The failure-mode taxonomy** — each mode a distinct named error (§5's list, plus the absent-sentinel and
   A7 refusals). R9 pins the names and the required message content.
5. **The loopback rig** (§8): real `truncate` → `losetup` → `mkfs.ext4` → mount. *"Every drive state gets forced
   on the loopback: full mid-write, read-only, unmounted, wrong id, absent sentinel."* **No skip mode — a rig
   that cannot be built is a loud failure, not a skip.** A mocked ENOSPC proves nothing about whether the
   temporary survives; that is the entire assertion.
6. **The discriminating tests.** §5: *"Each must be verified failing against an `artifact_path`-only
   implementation. A test that passes there is exercising a path that was already correct, and the fix belongs
   somewhere else."* Task 2 builds that baseline on purpose, runs the suite red against it, and records why
   each red is the right red.

Not P2: `agent_core` dispatch wiring (P3), PARE's probe worker / bench run / `bench_doctor` temp check (P4),
version bumps / tags / pin bumps (P4 — the kit stays `0.2.0` in *both* version locations), `hash_artifact`
(deferred), anything in `pare-hardware-mcp`.

## Repos, environments, commands

| | |
|---|---|
| Kit repo | `/mnt/secondary/projects/pare-worker-kit` — `main`, `origin/main` = `ca36885` at planning time |
| Kit venv | `.venv`, Python 3.12, plain pip (created in P1). agent_core is importable in it (P1 fixture), so the cross-package guards **run** locally, not skip |
| Test run (local) | `cd /mnt/secondary/projects/pare-worker-kit && sg disk -c '.venv/bin/python -m pytest'` |
| Never | `uv run` — this venv is plain pip |
| Rig privilege | `losetup` / `mkfs.ext4` via `sg disk` (in-session; no logout). `mount` / `remount` / `umount` / `chown` via `sudo -n`, NOPASSWD-scoped by `/etc/sudoers.d/pare-rig` (five exact commands, installed and probed on agenthost before this plan was written) |
| CI | `ubuntu-latest` kit jobs (3.10 + 3.12 legs). Passwordless sudo means the rig's `sudo -n` leg covers everything there; no sudoers file needed |
| Reviewer | Claude Code headless (`claude -p --model opus`), verified in preflight; R6 fallback if unavailable |
| Ledger | `PARE/.superpowers/sdd/2026-10-02-artifact-wiring-p2/progress.md` |

## Global constraints

1. **Kit only.** No edits to `agent_core`, PARE code, or any pin.
2. **No implementation code in this plan** (§5). Steps state signatures, invariants, test discrimination, and
   verification commands. The executor re-reads the actual files at use time — nothing here is typed from
   memory at execution time.
3. **Nothing version-shaped moves.** Kit `0.2.0` in `pyproject.toml` *and* `__init__.py` (`stamp_version` feeds
   the latter into `serverInfo` — §7). No tags. PARE's pin is P4.
4. **Never `uv run`.**
5. **The rig fails loud.** Rig unavailable → a named rig error, a test failure. No skip marker, no `xfail`, no
   mocked `ENOSPC`/`EROFS`, no tmpdir stand-in for the drive (§8 says exactly why).
6. **TDD with a naive baseline.** The suite is verified RED against a deliberately naive `open_artifact`
   (the `artifact_path` lexical check + a plain `open` writing in place) before the real walk exists. A test
   that goes green against the baseline is *not* a discriminator: it is labelled a contract pin (P), or it is
   rewritten until it fails for the right reason (D) — §5's own rule, sprung on §5's own author by the
   `st_nlink` misattribution this plan must not repeat.
7. **Linux only (A7).** A non-Linux platform is a named refusal before any filesystem operation.
8. **House style.** Long honest docstrings (`artifact_path` is the model). Wire literals pinned by local tests.
   One named error per failure mode — a caller must never catch a bare `OSError` out of this function.
9. **Pre-existing dirty state is left alone** (PARE: `M .gitignore`, `?? uv.lock`, unpushed `8a15664`).

## Rulings (plan-level decisions; recorded here, not made in secret)

**R1 — Rig location and shape.** A private test-support module `tests/loopback_rig.py` plus a smoke test
`tests/test_loopback_rig.py`, in the kit's `tests/` (which is not shipped: the wheel packages
`src/pare_worker_kit` only). Not a package dependency — the kit is one dependency *deliberately*, and a rig is
test scaffolding, not a consumer. A context manager yields the drive root (the fixed mountpoint) in the
requested state.

**R2 — Privileged-command policy.** Every privileged operation is tried **as-is first**, retried under
`sudo -n` on failure, and if both legs fail raises a named `RigUnavailable` whose message names the operation
and both errors. Local runs wrap the whole pytest invocation in `sg disk`, so the as-is leg carries
`losetup`/`mkfs.ext4` (disk group) while the `sudo -n` leg carries `mount`/`chown` (the scoped entries). On CI
the as-is leg fails and `sudo -n` carries everything (passwordless). One helper code path, both environments,
no environment sniffing.

**R3 — Fixed rig paths.** Image `/tmp/opencode/rig/disk.img`; mountpoint `/tmp/opencode/rig/mnt`. The
mountpoint path is pinned by agenthost's sudoers entry; CI's passwordless sudo accepts the same path. Image
size is a parameter, default 64 MiB (the parent's §14). Small images (8 MiB) are legitimate: an 8 MiB ext4
image yields ~3.5 MiB usable, which is plenty for the "expected size exceeds free space" state.

**R4 — `chown` the mount root to the running uid:gid after every mount** (computed dynamically — `1000:1000`
on agenthost, matching the scoped sudoers entry; the runner's own ids on CI). A fresh ext4 root is
`root:root` 0755; without the chown the test process gets `EACCES` everywhere, *including on read-only
mounts*, where the EROFS test would then see "permission denied" instead of read-only — the exact misnaming
§5 forbids, and the discrimination would be void.

**R5 — RED strategy.** Task 2 commits the full suite plus a deliberately naive `open_artifact` and records the
red per test, with each red's reason. Task 3 replaces the body with the real walk. Branch history shows
red→green; the reviewer sees both halves.

**R6 — Review bar.** Claude Code headless, run from the kit repo dir:
`claude -p --model opus --output-format text --allowedTools "Read Grep Glob"` — read-only, and it reads the
actual code, not just the diff. Input: the branch diff, spec §5, and the attack-sequence brief (Task 4), with
P1's verdict schema (Critical / Important / Minor / declined-to-judge, each with cost-if-wrong). Subscription-
backed — zero metered tokens. **Fallback-of-the-fallback**, recorded as a ruling *if triggered at review time*
(preflight decides whether it will be needed): two local passes — Qwen3.8-27B (main slot), then an
independent gemma-4-26B pass with the same brief that has *not* seen pass 1's findings; findings merged and
re-graded.

**R7 — Branch and landing.** One feature branch, `feat/open-artifact`; tasks commit onto it in sequence; one PR
for the whole branch (the red→green history is part of the review); `gh pr merge --merge`; verify
`origin/main` afterwards. Mirrors P1's landing.

**R8 — Sentinel.** Module constant `SENTINEL_NAME = ".bench-store-id"` in `artifacts.py` with a docstring (the
name `bench_deploy` writes; the parent's §5.5), exported from the package root, value pinned by a local test.
The comparison strips surrounding whitespace (the deployed file ends with a newline). A sentinel that is not a
regular file (e.g. a symlink) is treated as **absent** → `DriveNotMountedError`: a symlinked sentinel is a
redirection, `O_NOFOLLOW` refuses it, and the honest diagnosis is "there is no real sentinel here."

**R9 — Error taxonomy.** One base class, a distinct subclass per mode. Path-shaped input errors keep the
existing `ArtifactPathError` (`ValueError`, unchanged, outside the new hierarchy) — it is the established
refusal for anything wrong with root/slug/name, and two error families for one input class would force
callers to catch both:

- base: **`ArtifactWriteError`**
- **`NotLinuxError`** — the A7 platform gate; raised before any filesystem operation
- **`DriveNotMountedError`** — sentinel absent (or non-regular). Message is §5's exact string: `no `.bench-
  store-id` at `{root}` — is the drive mounted?` — the spec's wording verbatim (backticks and em-dash as in
  the spec), `{root}` replaced by the declared root
- **`DriveIdMismatchError`** — sentinel value ≠ `expect_drive_id`; the message names both
- **`DriveReadOnlyError`** — `EROFS`; says read-only, never "permission denied"
- **`DriveFullError`** — `ENOSPC` (step-3 precheck, mid-write, or at fsync: one class, messages may differ);
- **`FileTooLargeError`** — `EFBIG`; names FAT32's 4 GiB ceiling (the store is ext4; a replacement stick might
  not be). Not constructible on the ext4 rig (the 4 GiB ceiling needs FAT32 or a multi-GiB image), so it is
  pinned by taxonomy and message content, not by a rig test — the same "impossible to construct or vacuous"
  standard that dropped the spec's wrong-filesystem test
- **`SizeMismatchError`** — `bytes_written != expected_size` on clean exit; the message states both numbers;
  the temporary is kept
- **`TempAlreadyExistsError`** — `O_EXCL` refusal at step 5; the message names the file and says to remove
  it, and must **not** assume it is abandoned (nothing distinguishes an orphan from a live writer — the
  timeout case)
- **`ArtifactExistsError`** — `EEXIST` at publish (step 7); distinct message from the one above: a completed
  artifact is in the way, not a temporary, and the operator's choice is different
- **`TempVanishedError`** — `ENOENT` at step 7: the temporary vanished while it was being written
- **`ProjectDirOpenError`** — step-4 bounded-retry exhaustion: a symlink is parked at the project directory
- **`InvalidArtifactInputError`** — `media_type` not a str, or `expected_size` not a non-negative int (bools
  rejected)
- Internal invariant violations (step-6 `fstat` failure, inode mismatch at publish) raise **the base
  `ArtifactWriteError` itself**, with a message stating which invariant broke. §5's distinct-error list covers
  drive failure modes; these are "should be unreachable" refusals, and the base class is the honest one.

**R10 — No lexical re-check of post-root components.** The walk does **not** re-run `artifact_path`'s
`lexists`/`islink` pair — `O_NOFOLLOW` is its race-free counterpart (§5 step 4 says so). What the walk
validates *before any filesystem operation*: the root's rules (absolute, normalized, no ambiguity — reuse
`artifact_path`'s root normalisation, which may be factored into a private shared helper; `artifact_path`'s
behaviour and its tests must not move), `slug` against `SLUG_RE`, `name` against `_NAME_RE` — violations raise
`ArtifactPathError`. Consequence, test-pinned (D1): a persistent symlink at the project directory yields
`ProjectDirOpenError` — the walk's own refusal — *not* `ArtifactPathError`.

**R11 — Temporary name.** The spelling is pinned as `{name}+partial` (the spec's own suggestion; the property
binds, the spelling serves the tests and P4's `bench_doctor` glob). The property is test-pinned: for any valid
`name`, the temporary does not match `_NAME_RE` (contains ≥ 1 character outside its alphabet) and does not
begin with `.` (§5.5 wants the orphan visible to a plain `ls`). The implementation is tied to the spelling by
the `TempAlreadyExistsError` message, which names the file.

**R12 — Free-space precheck.** Step 3 refuses only when `free < expected_size` (strict). Equality is admitted
and the write itself is the arbiter — that is what makes the ENOSPC test (R13) constructible.

**R13 — The ENOSPC test construction.** Fresh default (64 MiB) image; the rig helper reports free bytes on the
mounted root as `statvfs.f_bavail * f_frsize` — **the same expression the walk uses at step 3** (the rig and
the walk must agree on "free"); the test sets `expected_size` to exactly that number. The precheck passes
(R12's equality). Guaranteed metadata overhead (inode, indirect blocks, journal commits — bounded, ≪ 1 MiB
for a file of this size) makes the write run out within the last fraction of a MiB: a genuine mid-write
`ENOSPC` with `0 < bytes_written < expected_size`. Deterministic, no concurrency, no race.

**R14 — The EROFS test pre-creates the project directory** (sentinel and project dir written while rw, then
remount-ro), so the refusal lands at step 5's `O_CREAT` — the spec's write-probe, "done at the moment it
matters" — not at step 4's mkdir.

**R15 — The unmounted state comes from the rig**: image created but not mounted — the empty mountpoint
directory, the exact state of an `fstab nofail` boot with the drive unplugged (the likeliest real bench
failure, §5). Not a `tmp_path` shortcut.

**R16 — The descriptor.** A plain dict, exactly the seven keys of `ARTIFACT_DESCRIPTOR_FIELDS`, set **only**
after a clean exit (absent before — test-pinned):

- `host` — `socket.gethostname()`
- `path` — the normalised declared root + `/` + slug + `/` + name. The operator-declared string, not the
  resolved target: it is what the operator's `scp` uses and what the daemon's containment check compares
- `size` — `bytes_written` (== `expected_size` on clean exit)
- `sha256` — the inline digest over the bytes written
- `hashed_at` — RFC 3339 UTC, `Z` form, **ASCII digits only** — the grammar P1 landed in the daemon
  validator; P1's Important finding was the Unicode-digit hole, and the kit's output must pass the daemon's
  check (cross-package pin, Task 2 P8)
- `media_type` — as supplied
- `drive_id` — `expect_drive_id`, the identity established at step 2

**R17 — The platform gate runs first**: before input validation, before the filesystem. Test-pinned by
non-Linux + a nonexistent root → `NotLinuxError` (not `FileNotFoundError`, not a drive error, no side effects).

**R18 — Input validation** (pre-walk): root's rules, `SLUG_RE`, `_NAME_RE` → `ArtifactPathError` (R10).
`media_type` must be a str (not None — §5: a tool that cannot state its media type has not decided what it is
writing). `expected_size` must be an int, not bool, ≥ 0 — else `InvalidArtifactInputError` (§5: a tool that
cannot state its size in advance cannot have the short-read check; a `None` that silently disables it is worse
than a refusal). Grammar of `media_type` beyond "a str" is the daemon's job (P1's validator checks the IANA
grammar): the worker states the fact, the daemon validates the grammar.

**R19 — The publish mechanism (step 7).** Neither `os.rename` nor `os.replace` is available: both replace the
destination, which is precisely what `RENAME_NOREPLACE` exists to prevent; and this Python (3.10 and 3.12) has
no stdlib `RENAME_NOREPLACE` (there is no `os.renameat2`; a ctypes syscall table would be arch-specific, and
the kit targets aarch64 Raspberry Pis where an x86_64 syscall number is wrong *and* untestable in CI). Probed
in this session against the kit's own venv. The pinned **requirement** — atomic publish, `EEXIST` refusal when
`name` exists, the temporary consumed, the final name left as the exact inode the walk created and
verified — is met with a pure-stdlib sequence, all `dir_fd`-relative (3.3/3.4-era APIs; the CI 3.10 leg
confirms). One signature wrinkle, probed this session: `os.link`'s `dir_fd` keyword form differs between
3.10 and this 3.12.14 build (which takes `src_dir_fd`/`dst_dir_fd`; verified by signature inspection in
the kit venv) — the implementer adds a small private helper that hides the keyword difference (both names
are in the *same* directory, so a single-directory form suffices where available), and the CI 3.10 leg
confirms the 3.10 side. `follow_symlinks` is a keyword on both:

1. hardlink the temporary to `name` relative to the project descriptor, with `follow_symlinks=False` — a
   link onto an existing `name` fails `EEXIST` without touching it (the atomic refusal); `follow_symlinks=
   False` means that if the temporary's *name* is swapped for a symlink between step 6 and publish, a copy of
   the symlink is installed (caught by 2) instead of an attacker's inode being hardlinked into the final
   name;
2. `lstat` the final name via the project descriptor (no follow): regular file, inode equal to the
   temporary's step-6 `fstat` inode, same device — else the base `ArtifactWriteError` naming the broken
   invariant; if our own link installed a symlink (the swap case), remove the name *we just created*, then
   raise;
3. unlink the temporary via the project descriptor;
4. `fsync` the project directory.

No arch-specific code anywhere in the walk.

**R20 — Writer contract.** `write(b)` accepts bytes-like (`bytes`/`bytearray`/`memoryview`), writes it all
(the `os.write` loop handles partial writes and `EINTR` internally), returns the count written, updates the
inline SHA-256 and the byte count. `ENOSPC` mid-write raises `DriveFullError` — §5: it surfaces at write or
fsync, never at a buffered `write()`, and is not swallowed; the writer holds the fd and calls `os.write`,
so there is no buffered layer to swallow into. Non-bytes input raises `TypeError` (a programming error,
consistent with the built-in file API — this is the one place a bare builtin is right). The `.descriptor`
attribute is set only on clean exit.

**R21 — Step-4 retry bound.** 3 attempts. A parked symlink surfaces as `ELOOP` or `ENOTDIR` (probed this
session: `openat` with `O_NOFOLLOW|O_DIRECTORY` on a symlink-to-directory → `ENOTDIR`, errno 20; a chain →
`ELOOP`) — either counts as a symlink attempt. Exhaustion → `ProjectDirOpenError`, message naming the slug
and stating that a symlink was detected (fail-safe refusal — §5: anything with write access inside the root
can otherwise drive the loop indefinitely).

**R22 — Descriptor hygiene.** `O_CLOEXEC` on every open (the worker forks tools). Every fd closed on every
path (`try`/`finally`). The temporary is `fsync`-ed before publish; the project directory after. **The walk
never unlinks the temporary**: it is consumed only by a successful publish; every post-step-5 failure leaves
it on the drive, visible to `ls` and to P4's `bench_doctor` check. That is deliberate (§5.5): the orphan is
worth more than a convenient retry.

**R23 — CI.** The rig tests must **run** (not skip) in the kit's CI on `ubuntu-latest`; Task 4 verifies by run
output. The 3.10 matrix leg confirms the `dir_fd` signatures used by R19.

## Preflight (run in the execution session, before Task 1)

1. **Kit repo state.** `git fetch`; on `main`; worktree clean (leave any pre-existing dirty state alone and
   record it); in sync with `origin/main` (planning-time hash `ca36885` — record the actual).
2. **Versions.** `0.2.0` in both `pyproject.toml` and `src/pare_worker_kit/__init__.py` (record both).
3. **Venv.** `.venv/bin/python --version` → 3.12.x; agent_core importable in the venv (the P1 fixture — the
   cross-package guards will RUN locally, not skip).
4. **Baseline suite green.** `sg disk -c '.venv/bin/python -m pytest -q'` → all green (record the count).
5. **The rig probe chain** — one-liners; **any failure stops P2 loudly and names what is missing**:
   1. `sg disk -c 'losetup -a'` → runs (note free loop nodes; loop0–5 are held by snaps)
   2. `mkdir -p /tmp/opencode/rig`
   3. `truncate -s 8M /tmp/opencode/rig/disk.img`
   4. `sg disk -c 'losetup -f --show /tmp/opencode/rig/disk.img'` → `/dev/loopN` (record N)
   5. `sg disk -c 'mkfs.ext4 -q /dev/loopN'` → rc 0
   6. `sudo -n /usr/bin/mount -t ext4 /dev/loopN /tmp/opencode/rig/mnt` → rc 0
   7. `sudo -n /usr/bin/chown $(id -u):$(id -g) /tmp/opencode/rig/mnt` → rc 0
   8. `touch /tmp/opencode/rig/mnt/w && rm /tmp/opencode/rig/mnt/w` → rc 0 (rw write works)
   9. `sudo -n /usr/bin/mount -o remount -o ro /tmp/opencode/rig/mnt` → rc 0
   10. `touch /tmp/opencode/rig/mnt/w` → **must fail** with "Read-only file system" (genuine `EROFS`, not
       `EACCES` — a permission error here means the chown leg is broken, and the EROFS test would be void)
   11. `sudo -n /usr/bin/mount -o remount -o rw /tmp/opencode/rig/mnt` → rc 0
   12. `sudo -n /usr/bin/umount /tmp/opencode/rig/mnt` → rc 0
   13. `sg disk -c 'losetup -d /dev/loopN'` → rc 0
   14. `losetup -a | grep disk.img` → empty (no leftovers)
   If a `sudo -n` leg fails, the sudoers file is missing or stale: the operator installs it (the installer
   one-liner is recorded in the ledger from this session's work) and the chain re-runs. The two-`-o` remount
   form and the dynamic uid:gid must match the installed entry (locally `1000:1000`).
6. **Reviewer.** `claude --version` and `claude -p "Reply with exactly: ok" --model opus --output-format
   text` → `ok`. If unavailable: record that R6's fallback will be used at review time, and continue.
7. **Landing.** `gh auth status` → authenticated; otherwise the operator merges the PR and records it.
8. **Create the ledger** and record all of the above.

## File map

| File | Task | Change |
|---|---|---|
| kit `src/pare_worker_kit/artifacts.py` | 2, 3 | + `open_artifact` (Task 2: naive baseline → Task 3: the real walk), + the R9 error classes, + `SENTINEL_NAME` (R8). Possible refactor: factor `artifact_path`'s root normalisation into a private shared helper — `artifact_path`'s behaviour and its existing tests must not move |
| kit `src/pare_worker_kit/__init__.py` | 2, 3 | exports: `open_artifact`, `SENTINEL_NAME`, the R9 classes |
| kit `tests/loopback_rig.py` | 1 | new: the loopback rig (test support, private to `tests/`) |
| kit `tests/test_loopback_rig.py` | 1 | new: the rig smoke test (fails loud) |
| kit `tests/test_open_artifact.py` | 2, 3 | new: the discriminating suite (D) + contract pins (P) |
| PARE `docs/superpowers/plans/2026-10-02-artifact-wiring-p2.md` | — | this file |

No `pyproject.toml` change (no new dependencies: the rig and the walk are stdlib plus shell commands). No
version movement, no tags, no pin bumps.

## Task 1 — the loopback rig (test support)

**Files:** kit `tests/loopback_rig.py` (new), `tests/test_loopback_rig.py` (new).
**Spec:** §8. **Rulings:** R1–R4.

**Step 1 — RED.** Write the smoke test first. The rig module does not exist, so the collection error is the
red (an import-red is a legitimate discrimination when the test targets the module's existence; record it as
such). The smoke test — and nothing in `tests/` carries a skip marker for it; that absence is the fail-loud
contract:

- **S1 — mounted rw state.** The rig yields the root; a file can be written, read back, and removed.
- **S2 — read-only state.** After remount-ro, a write raises `OSError` with `errno == EROFS`. Assert the
  errno, not the message — the EACCES confusion is exactly what R4's chown exists to prevent.
- **S3 — unmounted state.** The root is an empty ordinary directory (no mount, no sentinel).
- **S4 — teardown.** On exit (clean, on exception, and on double-close) no loop device is attached to the
  rig image (`losetup -a` before/after diff) and the image file is removed.

**Step 2 — GREEN.** Implement the rig per R1–R4: a context manager that truncates the image (size parameter,
default 64 MiB) → `losetup -f --show` → `mkfs.ext4 -q` → mount → chown the mount root to the running uid:gid
(dynamically), yields the mountpoint path, and on exit umounts → detaches → removes the image, under
`try`/`finally`, idempotent. Privileged commands through the R2 helper (as-is → `sudo -n` → `RigUnavailable`
naming the operation and both errors). The suite needs three state operations: remount-ro, remount-rw, and
`free_bytes()` on the mounted root (R13 — the same expression the walk's step 3 will use).

**Step 3 — Run.** `sg disk -c '.venv/bin/python -m pytest tests/test_loopback_rig.py -v'` → all green; then the
full suite (no regression). Record both outputs in the ledger.

**Step 4 — Commit:** `test(artifacts): loopback rig for the open_artifact suite (spec §8)`

**Step 5 — Ledger:** S1–S4 red → green, full-suite output.

**Task 1 is done when** the smoke is green, teardown leaves zero loop devices, the full suite is still green,
and it is committed.

## Task 2 — the discriminating suite + naive baseline (RED)

**Files:** kit `tests/test_open_artifact.py` (new); `src/pare_worker_kit/artifacts.py` (naive baseline +
error-class declarations + `SENTINEL_NAME`); `__init__.py` (exports).
**Spec:** §5 — signature, walk, invariants, failure modes, test list. **Rulings:** R5, R8, R9, R11, R16, R20.

**Step 1 — The suite.** Every test below is labelled **D** (discriminator — *must fail* against the baseline,
and the ledger must record *why* it failed: the wrong reason is the trap) or **P** (contract pin — may pass
against the baseline; it protects the walk's ordering and mechanics from regression and is not a §5
"verified failing" item). Non-rig tests use a `tmp_path` root with a hand-written sentinel (a canonical UUID
plus trailing newline) — only the drive-state tests need the rig.

**Discriminators (must be red against the baseline):**

- **D1 — the project-directory symlink.** Persistent stand-in for the racing symlink (the spec's own
  formulation; a single-threaded test cannot interleave, so it parks the redirect and credits the
  *mechanism*). Setup: valid sentinel; a symlink at `root/slug` pointing to a real directory **outside**
  `root`. Assert: `ProjectDirOpenError` — the walk's own refusal, **not** `ArtifactPathError` (a lexical
  check cannot bind what the open sees; crediting it would be the `st_nlink` trap) — and nothing is created
  at the symlink's target. *Red:* the baseline's `artifact_path` raises `ArtifactPathError` (right result,
  wrong mechanism).
- **D2 — a pre-existing hardlink at the temporary's path.** Seed a file at `root/slug/{name}+partial`,
  hardlinked to a file elsewhere (nlink 2), with known content. Assert: `TempAlreadyExistsError`; the
  message names the file and says to remove it and does not claim it is abandoned (no "orphan"/"safe"
  wording); the seeded file is untouched (content and nlink still 2). *Assert that `O_EXCL` refuses it —
  not that `st_nlink` does* (§5 is explicit). *Red:* the baseline has no temporary; it opens the final path
  and succeeds.
- **D3 — an absent sentinel (the likeliest real bench failure).** A bare root directory, no sentinel.
  Assert: `DriveNotMountedError` with §5's exact message, `{root}` interpolated (R9). A bare
  `FileNotFoundError` is not acceptable. *Red:* the baseline has no sentinel check and succeeds.
- **D4 — a sentinel mismatch.** Sentinel carries UUID A; `expect_drive_id` is UUID B. Assert:
  `DriveIdMismatchError`, message names both. *Red:* the baseline succeeds.
- **D5 — a symlinked sentinel.** `root/.bench-store-id` is a symlink to a file containing the *correct*
  UUID. Assert: `DriveNotMountedError` (R8 — a non-regular sentinel is no sentinel at all). *Red:* the
  baseline succeeds (and would have accepted the redirect).
- **D6 — a short read that exits cleanly.** `expected_size` 100, write 50, clean exit. Assert:
  `SizeMismatchError`; the message states both numbers; the temporary remains (50 bytes); the final name
  does not exist; no descriptor. Parametrised: also overwriting (write 150 of 100 → same class). **No
  hash-difference assertion** — §5: a truncated dump is not distinguishable by hash; the assertion is that
  the size check caught it and the temporary survived. *Red:* the baseline exits clean, having written in
  place.
- **D7 — `ENOSPC` mid-write (rig, R13).** Fresh 64 MiB image; `expected_size` = the rig's reported free
  bytes. Assert: `DriveFullError`; **no descriptor is produced and the temporary remains** with
  `0 < size < expected_size` — "asserting only the raise is vacuous against a design whose claim is the
  `.partial`" (§5). *Red:* the baseline's raw `os.write` raises a bare `OSError(ENOSPC)` (unnamed) and its
  partial file sits at the final name, not a temporary.
- **D8 — `EROFS` (rig, R14).** Sentinel and project directory written while rw; remount-ro; call. Assert:
  `DriveReadOnlyError`; the message says read-only (case-insensitive) and does **not** say "permission
  denied"; no temporary created. *Red:* the baseline raises a bare `OSError(EROFS)`.
- **D9 — the unmounted state (rig, R15).** The rig's unmounted root. Assert: `DriveNotMountedError`, §5's
  exact message — the real-world shape of D3 (`fstab nofail`, drive unplugged). *Red:* the baseline
  succeeds.
- **D10 — a completed artifact already at the final name.** Seed the final name with content X and a valid
  sentinel. Assert: `ArtifactExistsError` — a distinct message from D2's (a completed artifact is in the
  way, not a temporary); X is byte-identical afterwards; the temporary remains (R22). *Red:* the baseline's
  plain write-mode open truncates and clobbers X.
- **D11 — the temporary vanishes under the writer.** Enter, write the full bytes, remove the sole
  unexpected entry in the project directory (the temporary — *discovered*, not named), exit. Assert:
  `TempVanishedError`; the final name does not exist. Against the baseline the discovery step finds no
  temporary — the test asserts its presence explicitly, with the message "no temporary: implementation
  writes in place", so the red is a clean assertion, not a setup crash. *Red:* as stated.
- **D12 — the platform gate (R17).** Monkeypatched non-Linux platform; root path nonexistent. Assert:
  `NotLinuxError` — not `FileNotFoundError`, not a drive error — and no filesystem side effect anywhere.
  *Red:* the baseline has no gate.
- **D13 — two writers, one name.** Writer 1 enters (its temporary exists); writer 2 enters with the same
  `(root, slug, name)`. Assert: `TempAlreadyExistsError` for writer 2; writer 1's temporary is untouched
  (content); the message does not assume abandonment — §5's timeout case: an operator told "remove it"
  would unlink a running dump's target mid-write. *Red:* the baseline's second open truncates writer 1's
  in-flight file.
- **D14 — abnormal exit.** Enter, write partway, raise inside the `with` body. Assert: the original
  exception propagates unwrapped (the exit raises nothing of its own); the temporary remains; the final
  name does not exist; no descriptor. *Red:* the baseline's partial bytes sit at the final name.
- **D15 — `expected_size` exceeds free space (rig, R12).** Small image; `expected_size` far above free.
  Assert: `DriveFullError` **before any byte**: no project directory created, no temporary created (the
  sentinel check still runs first — the sentinel is present). *Red:* the baseline has no free-space check —
  bare `OSError(ENOSPC)` and a partial file at the final name.
- **D16 — the tool-supplied facts.** `media_type=None`, `expected_size=None`, `expected_size=-1`,
  `expected_size="100"`, `expected_size=True` — each: `InvalidArtifactInputError`. *Red:* the baseline
  ignores these parameters and succeeds.

**Contract pins (may be green against the baseline):**

- **P1 — the happy path.** Tmp root, hand-written sentinel; write exactly `expected_size` in two chunks
  (60+40). Assert: the final exists with exactly the concatenated content; the temporary is gone;
  `writer.descriptor` has exactly the seven keys of `ARTIFACT_DESCRIPTOR_FIELDS` with: `sha256` == the
  digest of the bytes written; `size` == `expected_size`; `path` == normalised root + `/` + slug + `/` +
  name; `drive_id` == `expect_drive_id`; `media_type` as supplied; `host` == `socket.gethostname()`;
  `hashed_at` matches the daemon's P1 grammar (ASCII digits, RFC 3339 `Z`).
- **P2 — the zero-byte artifact.** `expected_size` 0, no writes, clean exit. Assert: the final exists,
  length 0; descriptor `size` 0 and `sha256` == the empty digest.
- **P3 — path-shaped inputs before the filesystem.** Non-str root/slug/name; slug `"../x"`; slug `"a/b"`;
  name `"a/b"`. Assert: `ArtifactPathError`, and no directory or file created under the root — validation
  precedes filesystem operations, and the walk must keep that ordering.
- **P4 — root is a symlink → followed, not refused** (the trust anchor; §5 step 1 is deliberate). Root a
  symlink to a real directory, sentinel at the target. Assert: the happy path succeeds. Guards the walk
  against over-refusal.
- **P5 — the temporary-name property.** For each of `"dump"`, `"dump.partial"`, `"dump.partial.partial"`
  (all verified to match `_NAME_RE` — the spec ran the regex): the temporary name (`name + "+partial"`,
  R11) does not match `_NAME_RE` and does not begin with `.`; and the implementation is tied to the
  spelling: pre-seed that exact file → the D2 error names exactly it.
- **P6 — descriptor timing.** `.descriptor` absent before a clean exit (`AttributeError`); present
  immediately after.
- **P7 — `write()` contract.** Returns the count written; multiple writes accumulate (P1's two chunks
  cover it); non-bytes input (`str`) → `TypeError`.
- **P8 — cross-package pin.** `importorskip` agent_core. Build a descriptor via the happy path with root R
  and slug S; run `agent_core`'s `validate_descriptor` against a `WorkerSpec` declaring `artifact_root=R`
  and the sentinel's UUID as `artifact_drive_id` → passes. The wire contract is pinned on both sides
  (P1's discipline), and the kit's `path`/`hashed_at`/`drive_id` shapes survive the daemon's containment
  and grammar checks. Runs wherever agent_core is installed (the local venv, CI's cross-package job);
  skips by design elsewhere — the one deliberate skip in the new suite, consistent with P1's guard pattern.

**Step 2 — The naive baseline (the red state).** Implement the `open_artifact` surface with a deliberately
naive body: the `artifact_path(root, slug, name)` lexical check; a plain write-mode `open` of the returned
path (no temporary, no sentinel, no platform gate, no free-space check, no `fsync`, no rename — the
"artifact_path-only implementation" of §5's test rule); `write()` → the file object's `write`; clean exit →
close and build the descriptor naively: same field shapes as R16 — `path` as root + `/` + slug + `/` + name,
`size` as the bytes written, the inline `sha256`, `host`, `media_type`, `drive_id` as the supplied
`expect_drive_id`, and `hashed_at` in the daemon's P1 grammar (ASCII digits, RFC 3339 `Z`) — with **no
size check**. No named errors — raw `OSError` propagates. The R9 classes and `SENTINEL_NAME` exist as declarations (the tests import them),
but the naive body raises none of them. **This is committed as-is.**

**Step 3 — The RED run.** `sg disk -c '.venv/bin/python -m pytest -v'`. Record the full failing set in the
ledger, per test, with the reason. Verify the discipline: every D fails, and for the reason stated; every P
passes (a P failing against the baseline means the P is miswritten — fix the test, not the baseline). A D
passing against the baseline is not discriminating: rewrite it until it fails for the right reason.

**Step 4 — Commit:** `test(artifacts): open_artifact suite + naive baseline (red by design)`

**Step 5 — Ledger.**

**Task 2 is done when** the red set is exactly the D set, the reds are recorded with reasons, the P set is
green, and it is committed — the branch now sits at red, on purpose.

## Task 3 — the real walk (GREEN)

**Files:** kit `src/pare_worker_kit/artifacts.py` (replace the naive body; the R9 classes gain any
message-shaping they need).
**Spec:** §5 steps 1–7, the invariants, the failure modes. **Rulings:** R10, R12, R16–R22.

**Step 1 — The walk.** Pre-walk: the R17 platform gate, then R18 input validation (root's rules via the
helper shared with `artifact_path`, `SLUG_RE`, `_NAME_RE`, the tool-supplied facts). Then the spec's seven
steps, each relative to the held descriptor — the ordering invariant: **no path string is re-opened after
being checked.**

1. Open `root` as a directory, **following symlinks** (the trust anchor). Not openable as a directory →
   `ArtifactPathError` naming the root.
2. Read `SENTINEL_NAME` relative to the root descriptor, `O_NOFOLLOW`. Absent or non-regular →
   `DriveNotMountedError` (§5's exact message). Compare the whitespace-stripped value with
   `expect_drive_id` → mismatch: `DriveIdMismatchError`, both named. **Before anything is created.**
3. `statvfs` from the same descriptor (the answer cannot come from a different filesystem). Free
   `< expected_size` (strict, R12) → `DriveFullError`.
4. Open the project directory relative to the root descriptor: `O_DIRECTORY|O_NOFOLLOW`; `ENOENT` → mkdir
   via the descriptor → retry; `ELOOP`/`ENOTDIR` (parked symlink, R21) → retry; 3 attempts; exhaustion →
   `ProjectDirOpenError` naming the slug.
5. Create the temporary (`{name}+partial`, R11) relative to the project descriptor:
   `O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW` (+`O_CLOEXEC`, R22). `EEXIST` → `TempAlreadyExistsError` (R9
   message content). `EROFS` → `DriveReadOnlyError` — the spec's write-probe, done at the moment it
   matters. `ENOSPC` → `DriveFullError`.
6. `fstat` the **held descriptor** (never `lstat` on a path): regular file, `st_nlink == 1` (belt-and-
   braces by construction — §5: keep it, do not attribute a catch to it), `st_dev` equal to the root
   directory's. Violation → the base `ArtifactWriteError` naming the broken invariant.
7. **On clean exit only:** `bytes_written != expected_size` → `SizeMismatchError` (both numbers; the
   temporary is kept — R22: never unlinked). `fsync` the file (`ENOSPC` here → `DriveFullError` — "write
   or fsync"). Publish per **R19**: hardlink onto `name` (atomic `EEXIST` refusal; `follow_symlinks=
   False`), `lstat`-verify the final name (regular, same inode and device as step 6 — else the base error;
   if our own link installed a symlink, remove the name we just created, then raise), unlink the temporary
   via the project descriptor, `fsync` the project directory. `EEXIST` → `ArtifactExistsError`;
   `ENOENT` → `TempVanishedError`. Then set `writer.descriptor` (R16).
   **Abnormal exit:** no size check, no publish, no unlink — the temporary remains and the original
   exception propagates.

The writer itself per R20. Descriptor hygiene per R22 throughout.

**Step 2 — The GREEN run.** `sg disk -c '.venv/bin/python -m pytest -v'` → all green: every D now passes
(the red→green correspondence recorded in the ledger, per test), every P still passes, Task 1's rig smoke
still passes, and `artifact_path`'s existing tests pass unchanged (the helper refactor moved no
behaviour).

**Step 3 — Commit:** `feat(artifacts): open_artifact — the openat/O_NOFOLLOW walk (spec §5)` — the walk and
the error classes in one commit: one function, one invariant set, reviewed as a unit.

**Step 4 — Ledger.**

**Task 3 is done when** the full suite is green with zero skips in the new modules (P8 runs — agent_core is
in the venv) and it is committed.

## Task 4 — acceptance, review, landing

**Step 1 — Local acceptance.** Full suite green; `-k agrees_with_agent_core -v` → 3 passed, 0 skipped (P1's
guards still run); nothing version-shaped moved (`0.2.0` × 2, no tags, PARE's pin untouched).

**Step 2 — Review package.** `git diff origin/main...HEAD` → `/tmp/opencode/p2-review.diff`, alongside the
spec file (the reviewer reads §5 from the file, not from this plan's summary).

**Step 3 — The review (R6).** From the kit repo dir, Claude Code headless:
`claude -p --model opus --output-format text --allowedTools "Read Grep Glob"`, with a prompt containing:
the role (review of a security-invariant implementation — a path walk with a race — against its spec); the
inputs (the diff file, the spec file, the kit repo readable); P1's verdict schema (each finding: severity
Critical/Important/Minor, location, claim, why it matters, cost-if-wrong; declined-to-judge items with
reasons — do not guess; a closing count line); the **review focus** — spec-implied inputs and failure modes
the reviewer must explicitly check, each already test-pinned in Task 2 (a test the suite is missing is a
finding in itself):

- traversal in slug/name is refused before any filesystem operation, with no side effects (test P3);
- root-as-symlink is **followed**, not refused — the trust anchor, and the over-refusal trap (test P4);
- the zero-byte artifact (`expected_size` 0, no writes) exits clean with a valid descriptor (test P2);
- `expected_size` far above free space is refused at the precheck — no project dir, no temporary (test
  D15, rig);
- the temporary-name property: no valid `name` collides with its temporary, and the temporary is not a
  dotfile (test P5).

and the **attack-sequence brief** — prose, verbatim from
here:

- **Walk order.** Verify every step after the first operates on the previous step's descriptor. Find any
  re-derivation from a path string after a check (invariant 2).
- **Race windows.** For each pair of adjacent steps, what can a local attacker with write access inside the
  root do: swap a symlink at the project directory (step 4 — is the bound enforced; is `ELOOP`/`ENOTDIR`
  safe, never a follow?); hardlink the temporary between steps 5 and 7 — **the §9-disclaimed hole: do not
  flag it** (a handle outliving the descriptor is §9's problem, and the spec says so); clobber the final
  name before publish (R19's link — can a completed artifact be destroyed? can a live temporary be
  clobbered?); delete the temporary under the writer (step 7 — `ENOENT` → `TempVanishedError`, no crash, no
  leak).
- **The temporary name.** Can any valid `name` (per `_NAME_RE`) collide with the temporary name? Can the
  temporary begin with `.`? Check the property, not the spelling.
- **Sentinel timing.** Is drive identity established before anything is created? Is the sentinel read
  before the project directory's mkdir? Is absent (`DriveNotMountedError`) kept distinct from mismatch
  (`DriveIdMismatchError`)?
- **Rename semantics.** Is there any path where a completed artifact is replaced, or a temporary silently
  overwritten?
- **Failure-mode confusion.** Can two distinct modes produce the same error type or an indistinguishable
  message? Is `EROFS` ever worded as "permission denied"? Is `ENOSPC` swallowed or surfaced at a buffered
  layer?
- **The invariants, in priority order:** (1) no component after `root` is resolved through a symlink — note
  that `root` itself is deliberately followed (trust anchor) and is *not* a violation; (2) no path string
  re-opened after being checked; (3) drive identity before the first byte; (4) a descriptor only for a file
  that reached step 7.
- **What this cannot establish — decline, do not flag:** the hardlink window between temporary creation and
  publish (§9); a hostile worker that calls nothing (§9).

If the reviewer is unavailable (preflight 6 failed): R6's fallback — two local passes, same brief and
schema, findings merged and re-graded; the ruling is recorded in the ledger.

**Step 4 — Findings.** Every Critical/Important is **probe-verified before fixing** (P1's discipline: make
the claimed bad behaviour happen in a scratch test, confirm it, then fix TDD-style — a failing test first,
then the fix). Minor → the ledger, deferred (not fixed in P2 unless trivially safe and confirmed by
review). Pushback on any finding follows `superpowers:receiving-code-review`: technical rigor, probe
first, no performative agreement.

**Step 5 — Landing (R7).** Push the branch; open the single PR (whole branch — the red→green history is
part of the review); `gh pr merge --merge`; `git fetch && git log origin/main -1` to verify. CI green on
all kit jobs: the rig tests **ran** on `ubuntu-latest` (not skipped — R23), the 3.10 leg green (R19's
`dir_fd` signatures).

**Step 6 — Ledger complete:** preflight (including the probe chain), every red with its reason, every
green, the review package, the verdict, each finding's disposition (fixed-with-probe / deferred /
rejected-with-probe), the CI run IDs, the landing hash.

## Acceptance — P2 is done when

1. **The full kit suite is green locally**, zero skips in the rig and open_artifact modules (agent_core is in
   the venv, so P8 runs); `-k agrees_with_agent_core -v` → 3 passed, 0 skipped.
2. **CI green** on the kit: the rig tests ran on `ubuntu-latest` (not skipped), P8 ran in the cross-package
   job, the 3.10 leg is green.
3. **Nothing version-shaped moved:** `0.2.0` in both kit locations, no new tags, PARE's pin untouched.
4. **The branch's commits** — Task 1 rig; Task 2 suite + naive baseline (red); Task 3 walk (green); plus
   any finding-fix commits — **landed on `origin/main` via the merged PR**, and this plan file is committed
   to PARE.
5. **The ledger carries everything:** the probe chain, every red with its reason, every green, the review
   verdict and each finding's disposition with probe evidence, the CI run IDs, the landing hash.
6. **The attack-sequence review is on record** — the heaviest review, the one the spec assigns to P2
   specifically.

## Handoff

- **P3 — dispatch wiring** (`agent_core` `call_tool`): route on `pool.produces()`, inject
  `RESERVED_SLUG_ARG`/`RESERVED_DRIVE_ID_ARG` before the tool snapshot, the tier floor, slug validation,
  extraction, the refusals and their audit rows, `validate_descriptor(payload, spec=, tool=, slug=)`. P3
  consumes the descriptor exactly as `open_artifact` produces it — Task 2's P8 pin is the guarantee that
  the two halves agree.
- **P4 — release, consumers, acceptance:** tag the kit (the next version — **both** version locations, §7),
  agent_core 1.12.0, the four pin bumps, §8's probe (Level 1: the local probe worker, which exercises both
  §5 races; Level 2: the bench run), `bench_doctor`'s temporary check (it looks for `*+partial` — the
  spelling this plan pinned in R11), and `hash_artifact` stays deferred.
- The hardware console-capture plan stays halted until P1–P4 have landed.
