# Artifact contract — what step 1 left behind

Step 1 of the ArcticBase/artifacts spec is complete: `agent_core` PR #35 and
`pare-worker-kit` PR #5. This is the list of things those PRs deliberately did not do,
written down so none of it has to be rediscovered.

**The honest summary of step 1:** the contract is declared, ratcheted and validated, but
**nothing consumes it yet**. `validate_descriptor`, `validate_slug`, `produces()` and
`artifact_root` have no non-test consumers in either repo. That was the intended scope —
it keeps the wire contract reviewable on its own — but it means the end-to-end property is
not demonstrated anywhere. The wiring step is what proves it.

## Blocking the wiring step

**1. `risk_pool.py:345` still has the `or {}` defect that was fixed in `conformance.py`.**

`meta = getattr(tool, "meta", None) or {}` turns a falsy non-dict (`[]`) into an empty
dict, which then reads as "absent". The defect predates this branch, but fixing one
instance made the two **diverge**: conformance now rejects `meta=[]` as malformed, while
`risk_pool` records it as an honest non-advertiser. Two validators disagreeing about
whether an input is hostile is where an attacker lives. Fix the sibling.

**2. CI must install the other package *from source* — an install line is not enough.**

The cross-package guard tests have two failure modes, and the second is worse:

- *Green-by-skip.* The `produces` and slug guards `importorskip` the other package, which
  is absent, so they skip. Honest and visible in the output.
- *Green-against-stale.* The kit's risk-tier guard does **not** skip. It passes — against
  whatever `agent_core` is installed. Demonstrated by mutating the working tree's
  `RISK_TIER_META_KEY` to `agent_core/DRIFTED`:

  ```
  kit suite, routine:             107 passed, 2 skipped   <- reports AGREEMENT
  kit suite, PYTHONPATH=worktree:   1 failed, 108 passed   <- catches it
  ```

  It was comparing against a frozen v1.9.0 in site-packages. A skip is visible; a pass
  against stale code is not.

The `produces` guards escape this only by accident — v1.9.0 predates
`workers/artifacts.py`, so they get a `ModuleNotFoundError` and skip.

**3. Two checks the daemon *can* do, deferred because they need the `WorkerSpec`.**

Both are named in the docstrings so the wiring task inherits them:

- **Containment** of a descriptor's `path` under the worker's declared `artifact_root`.
  `validate_descriptor(payload, *, worker, tool)` takes no root, so it is not expressible
  in the current signature.
- **`host` is never checked against the WorkerSpec endpoint.** A descriptor can name any
  well-formed hostname, so a compromised worker can point the operator's retrieval at a
  machine of its choosing.

**4. Open the artifact with `O_NOFOLLOW|O_CREAT|O_EXCL`.**

`artifact_path()` returns a path and never holds a file descriptor, so it cannot close the
TOCTOU window or detect a hardlink — both are documented limits, not oversights. The
wiring step is the first place that actually opens the file, and it is where the guarantee
can finally be made.

**5. The retrieval command must not be a legacy `scp -O`.**

`scp(1)` CAVEATS: legacy SCP mode "requires execution of the remote user's shell", so the
path is re-parsed remotely and an argv list or `shlex.quote` protects only the operator's
*local* shell. Use `scp` without `-O`, `sftp`, or `rsync -s`. OpenSSH ≥9.0 defaults to
SFTP, so the default is safe — this is about not overriding it.

## Not blocking, worth sweeping

- `pare_worker_kit/risk.py:9` and `README.md:104` claim `agent_core` re-exports the risk
  key "so there is still exactly one definition". False — `agent_core` states its own
  literal, which is the no-cross-import constraint working as intended.
- `VALID_RISK_TIERS` (kit) and `_WIRE_VALID_TIERS` (`conformance.py:44`) are unguarded in
  both directions. The newer `VALID_PRODUCES` is guarded both ways.
- `registry.py:43`: with `extra="forbid"`, one unknown key now aborts the whole file — no
  workers *and* no `risk_overrides`. Defensible fail-closed, but `workers.yaml`'s format
  comment is now load-bearing and already omits `autoload`, `connect_timeout`,
  `read_timeout`.
- `WorkerSpec.artifact_root_is_absolute` accepts roots the kit refuses (`/a/../b`,
  `//mnt/store`). Fails closed — the worker is the enforcer — but the operator learns at
  hardware-run time rather than at config load.
- The kit README still describes the package as "the risk-tier metadata key, and
  `run_worker`". There are now eight public names, one of them a security control.

## Two claims that were wrong, and how they were caught

Both were security rationales written with more confidence than the mechanism earned, and
both were mine. Neither was caught by a test — tests check what code does, not whether a
docstring's reasoning is true.

- **"sha256 is the control that survives an untrusted producer."** The producer computes
  both the file and its digest. The spec already said so thirty lines below where it said
  the opposite.
- **"Argument-injection safety is bought completely at the call site by `shlex.quote`."**
  True of the local shell only; `scp -O` re-parses remotely.

What caught them was asking a reviewer to reason from the mechanism and check the claim
against `man scp` and against the spec's own later text — not asking whether the tests
passed.
