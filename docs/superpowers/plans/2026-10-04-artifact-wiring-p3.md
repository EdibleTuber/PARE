# Artifact wiring — P3 (daemon-side dispatch wiring) — implementation plan

> **For agentic workers:** this plan is written to be executed by a fresh
> session that has not seen the planning conversation. Every line reference was
> verified against the checked-out sources on 2026-10-04, but **re-read the
> cited lines before editing** — if the file moved, the file is the truth, not
> this plan. Every RED is stated with its expected baseline reason; if the
> baseline fails for a different reason than stated, the test is miswritten —
> fix the test, record the correction in the ledger, and do not proceed.

| | |
|---|---|
| Date | 2026-10-04 |
| Spec | `docs/superpowers/specs/2026-09-12-artifact-wiring-design.md` — §3 (A2 :105-162, A4 :170-176, A5 :178-182, A8 :198, A9 :203-223, descriptor field table :241), §6 daemon dispatch (:463-647), §7a P3 line (:718-744), §8 level split (:750+) |
| Parent spec | `docs/superpowers/specs/2026-09-06-arcticbase-artifacts-design.md` §5.1-5.5 |
| Epic state | P1 ✅ (agent_core `ea1130c..abe2cef`), P2 ✅ (kit PR #12, merged `9280c87`), **P3 = this plan**, P4 pending. The hardware console-capture plan stays halted until P1-P4 land. |
| Status | planned, not started — no implementation code in this plan |
| Ledger | `.superpowers/sdd/2026-10-04-artifact-wiring-p3/progress.md` |
| Runs in | a **FRESH session** |

## What P3 is, and is not

P3 makes the daemon dispatch path real: a tool that declares
`produces: artifact` (P1's wire vocabulary) is routed in
`RiskAwareToolPool.call_tool` — pre-gate refusals, the tier floor, injection of
the reserved slug/drive-id arguments, result extraction, descriptor validation
against the operator's `WorkerSpec`, host reconciliation, the refusal audit
rows, and the 8-field handoff object P4's publisher will consume. Plus the
config side of A2 (`WorkerSpec.artifact_host` and its load rules), the A5 load
rule, the list_tools-assertable conformance check for the reserved arguments,
and PARE supplying the slug through `ctx`.

P3 is **not**:

- any version movement (agent_core stays `1.11.1`, no new tags, kit stays
  `0.2.0`, PARE's pin stays `@v1.11.1` — all bumps are P4);
- any `workers.yaml` change in PARE (not values, not the header comment — R11);
- any change to pare-worker-kit;
- the probe worker, the §8 Level 1/2 runs, `publish_descriptor`'s call site,
  the retrieval command, `bench_doctor`'s check, or the four pin bumps (P4).

P3 consumes P1's validated vocabulary (`agent_core/workers/artifacts.py`) and
P2's kit-side walk; P2's P8 pin is the standing guarantee that the descriptor
the walk produces is exactly what `validate_descriptor` accepts.

## Repos, environments, commands

| Item | Value (verified 2026-10-04) |
|---|---|
| agent_core repo | `/mnt/secondary/projects/agent_core` — `main`, HEAD `abe2cef` at planning. Pre-existing dirty: `M .gitignore`, `?? uv.lock` — leave alone. |
| agent_core origin | `github.com/EdibleTuber/agent_core.git`. Tag `v1.11.1` = `a5a6129`, a **direct ancestor of main**: main = tag + P1's four commits (`ea1130c`, `41e50c4`, `01889f6`, `abe2cef`). The tag's `WorkerSpec` already has `artifact_root`/`artifact_drive_id` (pre-P1); it does NOT have P1's or P3's work. |
| agent_core venv | `.venv`, Python 3.12. Run: `.venv/bin/python -m pytest`. Baseline at planning: **1079 passed, 2 skipped** (49.7 s). |
| PARE repo | `/mnt/secondary/projects/PARE` — `main`, `1cb2dab` at planning. Pre-existing dirty: `M .gitignore`, `?? uv.lock`, unpushed commits — leave alone. |
| PARE venv | `.venv`, Python 3.12. Installed agent_core is **byte-identical to tag `v1.11.1`** (verified by diff at planning) — PARE's local suite and PARE's CI see the same agent_core. **pytest is absent** from this venv — preflight installs the dev deps (plain pip). |
| PARE CI | `.github/workflows/test.yml` installs `agent_core @ git+...@v1.11.1`. **Already RED on main at planning** — a pre-existing flaky TUI timing test (`test_tui_approval.py::test_pane_poller_keeps_making_progress_while_modal_is_open`, run `37174909026`), unrelated to artifacts. Record; do not fix in P3. |
| kit | `/mnt/secondary/projects/pare-worker-kit` — untouched in P3. |
| PARE pin | `pyproject.toml:11` — `agent_core @ git+https://github.com/EdibleTuber/agent_core.git@v1.11.1` — unchanged in P3. |
| Reviewer | R6 primary (`claude -p --model opus`); two-pass local fallback. Probe in preflight (R15). |
| Ledger | `.superpowers/sdd/2026-10-04-artifact-wiring-p3/progress.md` — created in preflight. |

## Global constraints

1. **No version movement.** agent_core `1.11.1` (pyproject and tags untouched),
   kit `0.2.0`, PARE pin `@v1.11.1`. P4 owns every bump.
2. **Never `uv run`.** Every venv in this project is plain pip; keep it that
   way (including the preflight pip install into PARE's venv).
3. **PARE's venv stays on the tag for all of P3.** No refresh from local
   agent_core source — that lands in P4 with the pin bump. Consequently every
   PARE-side line of P3 (code and tests) must be green against the **tag's**
   agent_core, because that is what PARE's CI installs.
4. **PARE's `workers.yaml` is byte-identical throughout P3** — no values, no
   header comment line (R11).
5. **The kit is untouched.**
6. **A skip is not a pass.** Every new test must RUN. A P-pin (a test that
   must be green at baseline and stay green) that fails at baseline is
   miswritten — fix the test, record the correction (P2 lesson).
7. **Every RED must be the right RED.** Record each red with its reason in the
   ledger before going green (P2 lesson). If the baseline fails for a
   different reason than this plan states, stop and treat it as a
   miswritten test.
8. **One branch, one PR per repo; HARD STOP before merge.** Push the branch,
   open the PR, stop. Merging is the orchestrator's job (P2 lesson).
9. **Leave pre-existing dirty state alone** (both repos, listed in the table).
10. **House style:** long, honest docstrings; wire literals pinned by local
    tests; no NEW cross-package constants in P3 — the reserved-argument names
    and the descriptor field tuple already exist on both sides with P1/P2
    guard tests, and P3 reuses them.

## Rulings (plan-level decisions; recorded here, not made in secret)

**R1 — The slug channel.** `HandlerContext` (agent_core/agent.py:25-45) gains
`project_slug: str | None = None`. The pool reads
`getattr(ctx, "project_slug", None)`. PARE stamps it per message at the top of
`handle_chat`; no other ctx path (Frida `_frida.py:48`, mitm `mitm.py:77`)
stamps it. agent_core "asks, never derives" (spec §6:463-472). *Why:* ctx is
the only per-request object that crosses the boundary, and the `getattr`
default makes an old PARE (no stamp) fail closed — an artifact dispatch
against an unstamped daemon is refused, naming the cwd, which is today's safe
behavior. *Cost if wrong:* a future ctx path that needs the slug stamps it
there too; the reader side is unchanged.

**R2 — Outcome for every artifact refusal.** The existing `validation_failed`
outcome (Outcome literal, types.py:226). Verified at planning: it has **zero**
code/test uses anywhere — it exists in the literal and nowhere else, so there
is no exhaustive-match risk. The refusal message goes in the audit row's
`detail`. Spec §6:530-532 delegates this choice to the plan. *Cost if wrong:*
if refusals later deserve a distinct outcome, that is an enum addition plus an
audit-query migration — cheap now, expensive later.

**R3 — The tier floor.** `produces == "artifact"` ⇒ effective tier ≥ `high`,
applied to `effective` **after** `self._gate.evaluate(...)` (risk_pool.py:408-410)
and **before** `if effective in ("high", "critical")` (:413) — the sole path
to `_await_operator`. The reason is appended to `gate_override`, which flows
into the audit row's `override_reason`. *Why* (§6:500-508): a
`produces: artifact` tool at `risk_tier: low` would otherwise dispatch
unprompted; the floor mirrors why `hardware.risk_default` is `high`. The floor
raises, never lowers (a critical artifact tool stays critical). *Cost if
wrong:* the direction is conservative — at worst one extra prompt, never a
missing one.

**R4 — Refusals are all pre-gate, before `_await_operator`.** Spec §6:510-512:
"prompting an operator to approve a dispatch that cannot succeed teaches them
to approve without reading." The idiom is the existing `_await_operator`
refusal (risk_pool.py:452-457): `self._emit(...)` an audit row, then
`return _ErrorResult(msg)`. Four refusals, in dependency order: (1) no
`spec.artifact_root`; (2) no `spec.artifact_drive_id` (A5); (3) no
`spec.artifact_host` (A2 — defense in depth: after Task 1 this is
unreachable via config load, reachable via programmatic construction);
(4) slug is `None` or fails `validate_slug` (artifacts.py:334) — the message
names the operator's **cwd** (`ctx.cwd`), not the ctx object (§6:514-525), and
does **not** echo the raw invalid slug. *Why slug is validated before
injection:* an unvalidated slug would land verbatim in the approval prompt
(:466) and the audit row. *Cost if wrong:* refusal order only changes which
single refusal an operator sees when several conditions are broken; the order
is the most-operator-actionable-first order.

**R5 — Injection.** In-place mutation of the `arguments` dict, after the
pre-gate refusals, before the dispatch snapshot; the snapshot line moves from
risk_pool.py:399 to after the injection (a second `deepcopy`; the first
stays at :399 and serves the refusal rows — see Task 3). `dict(arguments or
{})` or any rebuild is forbidden: §1 defect #2 and §6:533-549 require the
dispatched object to BE the object the snapshot was copied from.
`arguments[RESERVED_SLUG_ARG] = slug; arguments[RESERVED_DRIVE_ID_ARG] =
spec.artifact_drive_id` — overwriting whatever the model supplied (A4).
Non-dict `arguments` ⇒ `TypeError` at the subscript ⇒ propagates out of
`call_tool` ⇒ caught by `tool_factory._run` (tool_factory.py:68-75) ⇒ the
model sees "`{worker}_{tool} call failed: ...`". No audit row on that path
(the pool never reached a decision) — "fails loudly" is spec-endorsed; do not
"fix" it. *Cost if wrong:* none for dict arguments (the MCP contract);
non-dict arguments reaching a pool method is already a contract violation
elsewhere.

**R6 — Extraction.** `result.content` must be **exactly one** text block, and
its text must parse (`json.loads`) to a JSON object. Any other shape is a
refusal naming the actual problem ("expected exactly one text content block;
got N" / "the single text block is not a JSON object"). §6:576-582:
`structuredContent` exists in none of the three repos; "exactly-one is
refusable and therefore checkable." Prose around the descriptor is a contract
violation — the descriptor is the protocol. *Cost if wrong:* a worker that
logs a human line before its descriptor is refused — that is the spec's
intent.

**R7 — Descriptor validation.** Inside `_execute_and_audit` (risk_pool.py:497),
after the dispatch and the in-band error determination, before `_emit` (:559).
Skipped entirely when the result is an in-band error/`isError`, and never on
the `_ErrorResult` paths (generation recheck ~:513, dispatch exception ~:542).
On refusal: a `validation_failed` audit row (detail = the
extraction/validator message), the **verbatim** worker result still goes
through the capture layer (A9: capture stores the worker's verbatim text —
see the existing block at call_tool :429-437, "The layer stores
unconditionally and never stubs an error"), and the model gets
`_ErrorResult(refusal)`. The call is `validate_descriptor(payload, spec=spec,
tool=tool, slug=slug)` (P1 signature, artifacts.py:120; raises
`DescriptorError` :116; returns a dict copy on success :316) — containment is
against `{spec.artifact_root}/{slug}`, not the root alone (§6:564-569; §10
Risk 1), and the descriptor's `drive_id` is compared to
`spec.artifact_drive_id`. *Cost if wrong:* if capturing a refused descriptor
were ever deemed wrong, only the `call_tool` tail ordering changes — A9
mandates the capture stays.

**R8 — The handoff object (for P4).** Only on successful validation, the pool
sets `ctx.artifact_descriptor` to the seven validated fields in
`ARTIFACT_DESCRIPTOR_FIELDS` order with `host` replaced by the reconciled
`spec.artifact_host`, plus `produced_by = f"{worker}.{tool}"` — eight fields,
matching the note at artifacts.py:27-40 ("The eighth field of the object that
gets published, produced_by, is added by the daemon AFTER validation and never
travels"). On refusal, `ctx.artifact_descriptor` is left untouched. No new
cross-package constants (the names are Python-side only; P4 consumes them).
*Cost if wrong:* if P4's publisher needs more context, extend the object then;
there is no consumer yet.

**R9 — `WorkerSpec.artifact_host` (A2, load side).** New field
`artifact_host: str | None = None` beside `artifact_root`/`artifact_drive_id`
(types.py:112+). A `model_validator(mode="after")` enforces: (a) root set,
host unset, endpoint transport (`streamable_http`/`http_job_api` — the
transports `validate_transport_fields` already requires an endpoint for) ⇒
default to `urlsplit(endpoint).hostname`; (b) root set, `transport: stdio` ⇒
host must be declared (load error — a stdio worker's reachable address is not
derivable); (c) a declared host (any transport) is validated against
`_HOST_RE` (artifacts.py:63) at load — IPv6 literals and dash-leading values
are refused, fail-closed (spec :160-162); (d) root unset + host declared ⇒
inert, no load error (no descriptor can validate without a root, so the field
cannot be used for retrieval); (e) root set, endpoint with no/empty hostname,
host unset ⇒ load error — cannot be defaulted; declare explicitly.
Reconciliation semantics (A2 :141-157): the worker's reported `host` is what
it calls itself; the operator's `artifact_host` is the reachable address; they
legitimately differ, so a **disagreement is recorded, not refused** (audit row
detail carries both values) — a refusal that fires on every correct bench
dispatch gets disabled by the operator. Retrieval (P4) always uses
`spec.artifact_host`. The operator declaration is validated pre-gate "so a
malformed operator declaration refuses before a 70-second dump"
(§6:571-574). `_HOST_RE` is imported from `agent_core.workers.artifacts` at
module level in types.py — **no import cycle**: artifacts.py imports
`WorkerSpec` only under `TYPE_CHECKING` (verified 2026-10-04); if a cycle
surfaces anyway, fall back to a lazy import inside the validator (precedent:
risk_pool.py:450) and record it. *Cost if wrong:* if an endpoint is later
served through a proxy under a different hostname, the operator declares
`artifact_host` explicitly — the field exists for exactly that.

**R10 — A5 at load.** Root set ⇒ `artifact_drive_id` must be set (load error,
same validator or a sibling). A5: "required whenever `artifact_root` is set" —
"an optional security input means a branch in a security path whose absent
case is the untested one." The pre-gate refusal (R4 #2) stays as defense in
depth for programmatically constructed specs. Consequence: existing test
fixtures that set `artifact_root` without `artifact_drive_id` are now invalid
configs — the executor adds a valid drive id to each and records every one;
if an existing test pins "root without drive id loads", that pin is
superseded by A5 and is updated to pin the refusal, with the supersession
recorded explicitly. *Cost if wrong:* a third-party config with root but no
drive id fails to load at the P4 bump — but A5 declares exactly such configs
invalid, and the error names the missing field.

**R11 — The PARE-side boundary.** PARE's P3 diff is the `handle_chat` stamp
plus its tests — nothing else. No `workers.yaml` change (comment line or
values), no PARE venv refresh. The stamp sets a plain instance attribute on
ctx; it must be green against the **tag's** `HandlerContext` (which has no
`project_slug` field) because PARE's CI installs the tag. The comment line +
venv refresh + real `workers.yaml` values all land in P4 with the pin bump —
a comment advertising a field the tag's `WorkerSpec` lacks would flip
`tests/test_workers_yaml_format_comment.py` red in CI (it compares the
comment against the *installed* `WorkerSpec.model_fields`). The stamp reads
`self.config.project_marker or ".pare"` **only after** its
`isinstance(cwd, str) and cwd` guard, so config is touched only when the ctx
carries a real string cwd. `Agent.config` is framework-populated and absent on
a bare `PareAgent()`; the two handback test files **already** stub it
(`agent.config = MagicMock(...)` at test_poll_failure_handback.py:40 and
test_unloaded_worker_handback.py:36 — verified 2026-10-04) and their tests pin
`cwd=None`, so the stamp skips them before any config read — **no change
needed there**. Only `test_handle_chat.py`'s `_make_agent` (:17-31) has no
config stub, and only the new D tests give the ctx a `str` cwd — the executor
adds the one-line `agent.config` stub there. The
stamp accepts only `str` cwd (the documented `HandlerContext` type); a
`Path`-typed cwd is treated as absent and refuses downstream. Do **not**
reuse `_active_slug` (pare/agent.py:321-335) — global last-served state, not
per-request. *Cost if wrong:* PARE's stamp is validated only against the tag —
but the stamp touches no agent_core API, so there is nothing to validate
against; the pool-side read is tested in the agent_core suite.

**R12 — `read_timeout` is a rule, not a rotting literal** (spec §6:608+).
Measured ~28 MB/s ⇒ a 2 GB dump ≈ 70 s. P3 records the rule; P4 applies the
value: when P4 fills the hardware entry, its declared `read_timeout` must be
≥ (expected_max_size / measured_rate) + headroom — for 2 GB at ~28 MB/s that
is ≈70 s of floor, so declare at least ~2× that. (The field's current default
`None` leaves a 300 s transport read, types.py:96-100 — but P4 declares it
explicitly beside `artifact_root`, per the spec.) *Cost if wrong:* none — the
rule survives the rate drifting; only the declared value needs updating.

**R13 — Conformance.** New helper `_assert_artifact_reserved_args(tool)`
beside `_assert_valid_produces_meta` (conformance.py:63). For a tool whose
`_meta` declares `produces: artifact` (`PRODUCES_META_KEY`/`PRODUCES_ARTIFACT`),
assert `inputSchema.properties` contains **both** `RESERVED_SLUG_ARG` and
`RESERVED_DRIVE_ID_ARG`; the assertion names the missing one. Non-artifact
tools: no-op. Non-dict meta: fail closed, identical to the sibling
(unreachable in the live suite — `_assert_valid_produces_meta` runs first in
the same loop and already fails closed on a non-dict meta; the unit test pins
the sibling behavior anyway). Called from **both** live-suite loops at the
same sites as the sibling (the streamable-http loop's sibling call sits at
conformance.py:274 in `assert_streamable_http_conformance`, the stdio loop's
at :352 in `assert_stdio_conformance` — verified 2026-10-04; re-read before
editing). The helper's docstring states that the
**result-shape rule is runtime-only until an invocation harness exists**
(§6:593-606) — stated, not implied. Conformance suites never invoke tools
(verified by grep; A4, spec :587-591) — this is list_tools-assertable only.
*Cost if wrong:* if a wire name changes, P1/P2's guard tests catch the drift
on both sides; the helper reuses the constants, so it cannot drift alone.

**R14 — Landing.** Two PRs, agent_core first (`feat/artifact-dispatch`), then
PARE (`feat/ctx-slug-supply`). The order is safe either way (the pool's
`getattr` default is fail-closed; PARE's stamp is inert against the tag), but
agent_core-first is the dependency order for P4's bump. HARD STOP before each
merge (R8 / P2 lesson).

**R15 — Review.** R6 primary (`claude -p --model opus`). The P2 ledger
(2026-10-02) recorded the Opus OAuth as dead — preflight probes; if dead, the
two-pass local fallback (two fresh sessions, merge findings, re-grade, record
the rationale).

## Preflight (run in the execution session, before Task 1)

Record every result in the ledger as you go.

1. **agent_core repo:** `git fetch`; on `main`; HEAD == origin/main (planning
   value `abe2cef`; record actual). Record the dirty state (`M .gitignore`,
   `?? uv.lock`) — leave alone.
2. **agent_core baseline:** `.venv/bin/python -m pytest -q` → planning value
   1079 passed, 2 skipped. Record the count **and the two skipped test
   names** (confirm pre-existing — they are import-or-skip gates, not ours).
3. **PARE repo:** on `main`, `1cb2dab` at planning (record actual). Record the
   dirty state — leave alone.
4. **PARE venv:** confirm the installed agent_core == tag:
   `diff <(git -C /mnt/secondary/projects/agent_core show
   v1.11.1:agent_core/workers/types.py)
   /mnt/secondary/projects/PARE/.venv/lib/python3.12/site-packages/agent_core/workers/types.py`
   → planning value: identical. Install the dev deps (plain pip, venv-local,
   matches the `pyproject.toml` dev extra):
   `.venv/bin/pip install "pytest>=8.0.0" "pytest-asyncio>=0.23.0"
   "pytest-httpx>=0.30"`.
5. **PARE baseline:** `.venv/bin/python -m pytest -q` → record count + skipped
   test names (the `*_live.py` tests self-skip without their env vars).
6. **PARE CI state:** `gh run list --limit 3` → record. Planning value: RED on
   main from the pre-existing flaky TUI timing test (run `37174909026`). P3's
   PARE acceptance is "local suite green + P3's commit adds no new CI
   failures", **not** "CI green".
7. **Reviewer probe:** `claude --version` and a minimal `claude -p` round
   trip → record whether R6 is usable.
8. **Landing:** `gh auth status` → record.
9. `mkdir -p .superpowers/sdd/2026-10-04-artifact-wiring-p3` and start
   `progress.md` with the preflight results.

## File map

**agent_core** — branch `feat/artifact-dispatch` from `main` (`abe2cef`):

| File | Change | Task |
|---|---|---|
| `agent_core/workers/types.py` | + `artifact_host` field + docstring; + `model_validator` (R9 host rules, R10 A5 rule); import `_HOST_RE` from artifacts | 1 |
| `agent_core/agent.py` | `HandlerContext` + `project_slug`, + `artifact_descriptor` (both `= None`) | 2 |
| `agent_core/workers/risk_pool.py` | `call_tool`: produces routing, pre-gate refusals, injection, snapshot move, tier floor, refusal-tuple tail; `_execute_and_audit`: + `ctx`/`spec`/`slug` params, extraction, validation, reconciliation, handoff; module-level `_extract_descriptor` | 2, 3 |
| `agent_core/workers/conformance.py` | + `_assert_artifact_reserved_args`; called from both live-suite loops | 4 |
| `tests/workers/test_types.py` | + load-rule tests (D1-D11) | 1 |
| other `tests/workers/` files | A5 fixture fixes only (Task 1's executor note — record each) | 1 |
| `tests/workers/test_risk_pool_artifacts.py` (new) | routing/refusal/floor (D12-D20), injection/extraction/validation (D21-D35) | 2, 3 |
| `tests/workers/test_conformance_reserved_args.py` (new) | D36-D40 | 4 |

**PARE** — branch `feat/ctx-slug-supply` from `main` (`1cb2dab`):

| File | Change | Task |
|---|---|---|
| `pare/agent.py` | `handle_chat` slug stamp at the top of the `_bind_store` block; import `resolve_project_slug`/`ProjectSlugError` | 6 |
| `tests/test_handle_chat.py` | + stamp tests (D41-D47); + one-line `agent.config` stub in `_make_agent` (:17-31) | 6 |
| `tests/test_poll_failure_handback.py`, `tests/test_unloaded_worker_handback.py` | **unchanged** — `agent.config` already stubbed (:40 / :36); their `cwd=None` makes the stamp skip before any config read (R11) | — |
| `docs/superpowers/plans/2026-10-04-artifact-wiring-p3.md` | this file, committed | 7 |

Shared fixtures to **re-read first** (patterns, not copies):
`tests/workers/test_risk_pool.py` (`_InnerPool` :14, `_pool` :40, `_spec` :51,
`_audit_lines` :55), `tests/workers/test_produces_ratchet.py` (an
`_InnerPool` whose `list_tools` returns a tool carrying
`_meta = {PRODUCES_META_KEY: PRODUCES_ARTIFACT}` — the only way a tool reads
as `produces: artifact` at dispatch, via the registration code at
risk_pool.py:365-368), `tests/workers/test_conformance_produces.py` (how P1
wired-tested `_assert_valid_produces_meta`), `tests/test_handle_chat.py`
(`_make_agent` :17, `_ctx` :34, `_Stream` :42,
`test_streaming_text_turn` :54 as the cheapest turn).

## Task 1 — `WorkerSpec.artifact_host` + the A5 load rule

Spec: A2 (:105-162), A5 (:178-182). Rulings: R9, R10.
Files: `agent_core/workers/types.py`; `tests/workers/test_types.py`.

**Red first** (write, run, record each red + reason):

- **D1** — endpoint transport (`streamable_http`), `artifact_root` set, no
  `artifact_host` ⇒ loads, and `spec.artifact_host ==
  urlsplit(endpoint).hostname` (fixture endpoint
  `http://100.97.133.126:9101/mcp` ⇒ `"100.97.133.126"`). *Red at baseline:*
  `AttributeError` — the field does not exist. Right red.
- **D2** — `transport: stdio`, root set, no host ⇒ `ValidationError` naming
  `artifact_host` (and that stdio cannot default it). *Red at baseline:* the
  spec loads fine (no field, no rule). Right red.
- **D3** — stdio + root + `artifact_host="pare-bench"` ⇒ loads,
  `spec.artifact_host == "pare-bench"`. *Red at baseline:* `ValidationError` —
  extra fields not permitted (the field is unknown). Right outcome, wrong
  mechanism (extra-forbid, not the rule) — record per the P2 D1 pattern.
- **D4** — endpoint + root + explicit host ⇒ loads; explicit wins over the
  default. *Red at baseline:* extra-forbid.
- **D5** — `artifact_host="::1"` (IPv6 literal) ⇒ `ValidationError` (`_HOST_RE`
  admits no colons/brackets). *Red at baseline:* extra-forbid.
- **D6** — `artifact_host="-evil"` (dash-leading — an ssh/scp argument, not a
  destination) ⇒ `ValidationError`. *Red at baseline:* extra-forbid.
- **D7** — root set, endpoint with no hostname (e.g. `http://:9101/mcp`), no
  host ⇒ `ValidationError` — cannot be defaulted; declare explicitly. *Red at
  baseline:* the spec loads. Right red.
- **D8** (A5) — root set, `artifact_drive_id` `None` ⇒ `ValidationError`
  naming `artifact_drive_id`. *Red at baseline:* the spec loads. Right red.
- **D9** (P-pin) — root set + a valid `artifact_drive_id` ⇒ loads. *Green at
  baseline; must stay green.*
- **D10** (P-pin) — no root, no host, no drive id ⇒ loads, and
  `getattr(spec, "artifact_host", None) is None` (written `getattr`-style so
  it is green both pre- and post-field). *Green at baseline; must stay green.*
- **D11** — no root + declared `artifact_host="pare-bench"` ⇒ loads, inert
  (R9d). *Red at baseline:* extra-forbid. Right red.
- **P-pin:** the existing `artifact_root` validator tests in
  `test_types.py` stay green (relative root refused, traversal refused, ...).

**Executor note (A5 in the same task):** the A5 rule (R10) lands in this same
task, so every root-set fixture that is meant to **load** must also carry a
valid `artifact_drive_id`, or the A5 rule fires before the host rule under
test: this applies to D1, D3, D4, D7 (and to D2, whose error must name
`artifact_host`, not `artifact_drive_id`).

**Executor note (R10 consequence):** the A5 rule (D8) may invalidate existing
fixtures that set `artifact_root` without `artifact_drive_id` (search
`tests/workers/` — `test_types.py` for sure, possibly `test_artifacts.py` and
conformance tests). For each: add a valid `artifact_drive_id` to the fixture
and record it in the ledger — that is A5's intent, not a test "fix". If an
existing test pins "root without drive id loads", that pin is superseded by
A5: update it to pin the refusal and record the supersession explicitly.

**Green:** implement the field (with a long docstring: operator-declared
reachable address; defaults from the endpoint for endpoint transports; stdio
must declare it; inert without a root; IPv6 refused at load, fail-closed) and
the validator (named for what it checks, e.g.
`artifact_declaration_is_complete`; behavior is pinned by the tests, the name
is not). Import `_HOST_RE` from `agent_core.workers.artifacts` at module
level — no cycle (artifacts.py imports `WorkerSpec` under `TYPE_CHECKING`
only; verified); fall back to a lazy import inside the validator if a cycle
surfaces, and record it.

**Task acceptance:** `test_types.py` green; `tests/workers/` green; ledger
carries every red with its reason and every fixture fix.

## Task 2 — The ctx channel + pre-gate refusals + the tier floor

Spec: §6 :463-472 (channel), :490-512 (refusals), :500-508 (floor),
:514-525 (slug validation, cwd naming). Rulings: R1, R2, R3, R4.
Files: `agent_core/agent.py`, `agent_core/workers/risk_pool.py`,
`tests/workers/test_risk_pool_artifacts.py` (new).

**Implementation shape** (re-read `call_tool` :397-437 before editing):

- `HandlerContext` gains two fields, both defaulted, with docstrings:
  `project_slug: str | None = None` ("supplied by the host agent — PARE stamps
  it per message from its own project resolution; agent_core asks, never
  derives; `None` refuses artifact dispatch, naming the cwd — fail closed")
  and `artifact_descriptor: dict | None = None` ("set by the pool only after a
  successfully validated artifact dispatch; the 8-field object the publisher
  consumes; never travels the wire" — matching artifacts.py:27-40).
- In `call_tool`, the first line stays where it is today (:399):
  `snapshot = copy.deepcopy(arguments) if isinstance(arguments, dict) else {}`
  — the **refusal rows use this snapshot** (the model's raw arguments; nothing
  has been injected yet at refusal time). Then, between the `gen` line (:407)
  and the gate (:408):
  ```
  produces = self.produces(worker, tool)          # :206-214, ratcheted; "result" when absent
  spec = slug = None
  if produces == PRODUCES_ARTIFACT:
      spec = self.spec_for(worker)                # :145
      slug = getattr(ctx, "project_slug", None)
      # four refusals, each: self._emit(worker, tool, snapshot, declared,
      # effective-at-floor?, 0, "validation_failed", None, msg) then
      # return _ErrorResult(msg)
  ```
  (The refusal rows record the **declared** tier and `None` override — the
  gate never ran; the executor re-reads `_emit` :563 for the exact signature.)
- The four refusals, in order: (1) `spec.artifact_root` falsy → msg names the
  missing declaration and `workers.yaml`; (2) `spec.artifact_drive_id` falsy
  → same shape (A5); (3) `spec.artifact_host` falsy → same shape (A2,
  defense in depth); (4) `slug is None` or `validate_slug(slug)` raises
  → msg names `ctx.cwd` and says the project slug is unavailable/invalid;
  the msg must **not** contain the raw invalid slug.
- The tier floor, after :408-410:
  ```
  if produces == PRODUCES_ARTIFACT and _TIER_ORDER.get(effective, -1) < _TIER_ORDER["high"]:
      effective = "high"
      gate_override = (gate_override + "; " if gate_override else "") + \
          "produces=artifact tier floor (high)"
  ```
  (`_TIER_ORDER` exists at risk_pool.py:41. The message must contain
  `produces=artifact` and `high` — the tests pin those substrings in the audit
  row's `override_reason`; exact surrounding wording is the executor's.)

**Fixtures** (in the new test file): `_InnerPool`-style fake whose
`list_tools` returns one tool with `_meta` carrying
`{RISK_TIER_META_KEY: "low", PRODUCES_META_KEY: PRODUCES_ARTIFACT}` (follow
`test_produces_ratchet.py`); spec fixture
`WorkerSpec(name="hw", transport="streamable_http", endpoint="http://100.97.133.126:9101/mcp", risk_default="low", artifact_root="/mnt/bench-store", artifact_drive_id=<uuid>)`
(its `artifact_host` defaults after Task 1); ctx is a **real**
`HandlerContext(conversation=Conversation(history_depth=1), channel_id="t",
writer=object(), project_slug="bench-slug-abc123", cwd="/mnt/secondary/projects/PARE")`;
approval auto-approve via a `send` stub that resolves the registry (the
pattern at test_risk_pool.py:75-86); capture stub recording whatever
`maybe_substitute` is handed.

**Red first** (record each red + reason):

- **D12** — low-tier artifact tool, valid slug ⇒ an approval request **is**
  sent (floored to high); after approval the inner is called once; the audit
  row: `effective_tier == "high"`, `override_reason` contains
  `produces=artifact` and `high`, outcome `hitl_approved`. *Red at baseline:*
  no routing at all — auto-executes at `low`, no approval, outcome `ok`.
  Right red.
- **D13** — ctx without `project_slug` (real `HandlerContext`, field unset) ⇒
  `_ErrorResult`; the message names the cwd; inner not called; no approval
  sent; audit row `validation_failed`, `detail` names the cwd. *Red at
  baseline:* the call dispatches (`ok` row). Right red.
- **D14** — `project_slug="../evil"` ⇒ refused; the message names the cwd and
  does **not** contain the raw slug; inner not called; no approval;
  `validation_failed` row. *Red at baseline:* dispatches. Right red.
- **D15** — spec with `artifact_root=None` (legal config) + artifact tool ⇒
  refused, naming the missing declaration; inner not called; no approval.
  *Red at baseline:* dispatches. Right red.
- **D16** — root set, `artifact_drive_id=None` — an in-memory spec only
  (deliberately bypassing the Task 1 load rule; this pins the dispatch-time
  guard, R4 #2). Build the task's valid spec fixture, then
  `spec.model_copy(update={"artifact_drive_id": None})` — `model_copy` skips
  validation (verified on the venv's pydantic 2.13.5). `model_construct` with
  partial kwargs also works but silently defaults missing fields — use
  `model_copy`, it cannot be misread. ⇒ refused. *Red at baseline:* dispatches.
  Right red.
- **D17** — root set, `artifact_host=None` (`model_copy(update=...)` from the
  valid fixture) ⇒ refused, naming `artifact_host`. *Red at baseline:*
  dispatches. Right red.
- **D18** (P-pin) — low-tier **non-artifact** tool ⇒ unchanged: auto-executes,
  no approval, `effective_tier == "low"`, no floor in `override_reason`,
  outcome `ok`. *Green at baseline; must stay green* (guards "only artifact
  tools are floored").
- **D19** (P-pin) — high-tier artifact tool (`_meta` tier `high`) ⇒ approval
  sent exactly once; audit row `effective_tier == "high"`; `override_reason`
  does **not** mention the floor. *Green at baseline; must stay green.*
- **D20** (P-pin) — critical artifact tool ⇒ stays `critical` (the floor never
  lowers); approval + rationale path unchanged. *Green at baseline; must stay
  green.*

**Green:** implement (fields, refusal block, floor). Ledger: every red with
its reason.

**Task acceptance:** new file green; existing `tests/workers/` green; ledger
complete.

## Task 3 — Injection, extraction, validation, reconciliation, handoff

Spec: §6 :466 (the approval prompt shows what dispatches), :533-549 (the
injection invariant), :564-569 (containment), :571-574 (reconciliation),
:576-582 (extraction); A4, A9. Rulings: R5-R8.
Files: `agent_core/workers/risk_pool.py` (only); `tests/workers/test_risk_pool_artifacts.py`
(continued).

**Implementation shape:**

1. **`call_tool` reordering** — the produces block from Task 2 grows: after
   the four refusals (which fire *before* any mutation), the injection, in
   place, on the live dict:
   ```
   arguments[RESERVED_SLUG_ARG] = slug
   arguments[RESERVED_DRIVE_ID_ARG] = spec.artifact_drive_id
   snapshot = copy.deepcopy(arguments)    # rebind: the approval/audit surface
                                          # is now exactly what will dispatch
   ```
   The first snapshot (Task 2's, at :399) already served the refusal rows;
   this second deepcopy is the one that flows to `_await_operator` and
   `_execute_and_audit`. Non-dict `arguments` raises `TypeError` at the
   subscript — do not guard it (R5).
2. **`_execute_and_audit`** — extend the signature (currently :497-498) with
   `ctx=None, spec=None, slug=None` (keyword); call it from `call_tool`
   (:425-427) with `ctx=ctx, spec=spec, slug=slug` (`spec`/`slug` are `None`
   for non-artifact tools). After the dispatch and the in-band error
   determination, before `_emit` (:559):
   ```
   refusal = None
   descriptor = None
   host_note = None
   if spec is not None and slug is not None and not <in-band error>:
       payload, problem = _extract_descriptor(result)
       if problem is not None:
           refusal = f"{worker}.{tool}: {problem}"
       else:
           try:
               descriptor = validate_descriptor(payload, spec=spec, tool=tool, slug=slug)
           except DescriptorError as exc:
               refusal = str(exc)
   if descriptor is not None:
       if descriptor["host"] != spec.artifact_host:
           host_note = (f"host mismatch: worker reported {descriptor['host']!r}, "
                        f"operator artifact_host is {spec.artifact_host!r}")
       if ctx is not None:
           desc = {}
           for f in ARTIFACT_DESCRIPTOR_FIELDS:
               desc[f] = spec.artifact_host if f == "host" else descriptor[f]
           desc["produced_by"] = f"{worker}.{tool}"
           ctx.artifact_descriptor = desc
   ```
   then `_emit` with outcome `"validation_failed"` and `detail=refusal` when
   `refusal` is set (otherwise the existing outcome), and `host_note` visible
   in the success row's `detail` (append, never clobber, if the success path
   already sets a detail — re-read `_emit` :563 and its current call sites to
   place it correctly). Return `(result, refusal)` when `refusal` is set,
   else `result`.
3. **The `call_tool` tail** (the existing capture block :429-437 — re-read it;
   it `return`s the `maybe_substitute` result directly):
   ```
   result = await self._execute_and_audit(...)
   refusal = None
   if isinstance(result, tuple):
       result, refusal = result
   if self._capture is not None:
       session_id = arguments.get("session_id") if isinstance(arguments, dict) else None
       captured = await self._capture.maybe_substitute(worker, tool, result,
                                                       substitute=capture, session_id=session_id)
       return _ErrorResult(refusal) if refusal is not None else captured
   return _ErrorResult(refusal) if refusal is not None else result
   ```
   The capture receives the **verbatim** worker result on refusal too (A9 —
   "The layer stores unconditionally and never stubs an error" is the
   existing block's own comment); the model gets the refusal, never the raw
   text. The `_ErrorResult` paths (gen recheck, dispatch exception) return
   non-tuples and are untouched.
4. **`_extract_descriptor(result) -> (dict|None, str|None)`** — module-level
   in risk_pool.py (add `import json` if absent). Rule (R6): `result.content`
   must be exactly one block of type `"text"`, and `json.loads` of its `text`
   must yield a dict. Return `(payload, None)` on success; `(None, problem)`
   otherwise, `problem` naming the actual problem. Docstring cites
   §6:576-582 and the "exactly-one is refusable and therefore checkable"
   rationale; states that `structuredContent` exists in none of the three
   repos.

**Red first** (record each red + reason; continue the same test file):

- **D21** — injection: the model's arguments carry forged
  `{"project_slug": "model-forged", "expected_drive_id": "model-forged",
  "size": "2g"}`; ctx slug `bench-slug-abc123`; spec drive id `U` ⇒ the inner
  receives `project_slug == "bench-slug-abc123"` and
  `expected_drive_id == U` (forged values overwritten); the approval
  request's arguments (the snapshot) contain the injected values, not the
  forged ones. *Red at baseline:* no injection — the inner receives the
  model's values. Right red.
- **D22** (P-pin, identity) — the inner receives the **same dict object**
  (`id(...)`) that was passed to `call_tool` — in-place mutation, no rebuild
  (the invariant §6:533-549 requires: the dispatched object is the object the
  snapshot was copied from). *Green at baseline; must stay green.*
- **D23** — non-dict `arguments` (a list) on an artifact tool ⇒
  `pytest.raises(TypeError)` from `call_tool`. *Red at baseline:* no raise —
  baseline dispatches the list. Right red.
- **D24** — happy path: the inner returns exactly one text block holding a
  fully valid descriptor (path under `/mnt/bench-store/bench-slug-abc123/`,
  drive id `U`, host `"100.97.133.126"` == `artifact_host`, valid
  sha256/hashed_at/media_type/size) ⇒ the result passes through to the
  caller **verbatim** (not an error); the audit row is `ok`/`hitl_approved`
  (no `validation_failed`); `ctx.artifact_descriptor` is set: 8 keys in
  `ARTIFACT_DESCRIPTOR_FIELDS` order + `produced_by`, `host`
  == `"100.97.133.126"`, `produced_by == "hw.dump"`, the other six fields
  equal the descriptor's. *Red at baseline:* the result passes through but
  `ctx.artifact_descriptor` stays `None` (real `HandlerContext`). Right red —
  the handoff object does not exist yet.
- **D25** — two text blocks (the descriptor + one prose line) ⇒ refused; the
  model-facing error names "exactly one text content block" and `2`; the
  capture stub received the **verbatim** worker result (not the refusal, not
  an error stub); audit row `validation_failed`. *Red at baseline:* the
  result passes through, no refusal. Right red.
- **D26** — one text block that is not JSON (prose) ⇒ refused, detail says it
  is not a JSON object; capture verbatim. *Red at baseline:* passes through.
  Right red.
- **D27** — one text block that is a JSON **array** ⇒ refused, "not a JSON
  object" (`json.loads` succeeds; the `isinstance dict` check fails). *Red at
  baseline:* passes through. Right red.
- **D28** — invalid descriptor: `path` outside the slug directory
  (`/mnt/bench-store/other-slug/x.iso`) ⇒ `validate_descriptor`'s
  `DescriptorError` → refused with the validator's message (it names the
  worker+tool); capture verbatim; audit row `validation_failed`. *Red at
  baseline:* passes through. Right red.
- **D29** — `drive_id` in the descriptor ≠ `spec.artifact_drive_id` ⇒ refused.
  *Red at baseline:* passes through. Right red.
- **D30** — host mismatch: descriptor `host "pare-bench"`,
  `artifact_host "100.97.133.126"` — both well-formed ⇒ **not** refused (R9):
  the result passes through; `ctx.artifact_descriptor["host"] ==
  "100.97.133.126"` (reconciled); the audit row's `detail` contains **both**
  `"pare-bench"` and `"100.97.133.126"`. *Red at baseline:* the descriptor is
  unset at all (no reconciliation). Right red.
- **D31** (P-pin) — in-band worker error (inner returns `isError=True`, text
  `"boom"`) ⇒ no validation attempted (the text is not a JSON object and
  must not be refused for it); the result passes through; the existing error
  outcome; capture verbatim. *Green at baseline; must stay green.*
- **D32** (P-pin) — dispatch exception (inner raises) ⇒ `_ErrorResult`
  (existing behavior); no validation; `ctx.artifact_descriptor` stays `None`.
  *Green at baseline; must stay green.*
- **D33** (P-pin) — generation-recheck path (bump the worker's generation
  between `call_tool` entry and the recheck — follow the existing
  gen-recheck test in `test_risk_pool.py`) ⇒ refused before dispatch, no
  validation, no handoff object. *Green at baseline; must stay green.*
- **D34** (P-pin) — non-artifact tool whose result happens to be a JSON-object
  text block ⇒ passes through; `ctx.artifact_descriptor` stays `None`; no
  host handling; no validation row. *Green at baseline; must stay green.*
- **D35** (P-pin) — non-artifact tool with list `arguments` ⇒ no `TypeError`
  (the injection is guarded on `produces`). *Green at baseline; must stay
  green.*

**Green:** implement. Ledger: every red with its reason.

**Task acceptance:** new file green; `tests/workers/` green; ledger complete.

## Task 4 — Conformance: the reserved-argument assertion

Spec: A4 (:170-176), :585-606 (list_tools-assertable; the invocation-harness
note). Ruling: R13.
Files: `agent_core/workers/conformance.py`;
`tests/workers/test_conformance_reserved_args.py` (new).

**Red first** (record each red + reason):

- **D36** — a tool with `_meta = {PRODUCES_META_KEY: PRODUCES_ARTIFACT}` and
  an `inputSchema.properties` missing `"expected_drive_id"` ⇒ the helper
  raises `AssertionError` naming the missing argument. *Red at baseline:*
  `ImportError` — the helper does not exist. Legitimate import-red (the P2
  Task 1 pattern); record it as such.
- **D37** — artifact tool with both reserved arguments ⇒ no raise. *Red at
  baseline:* `ImportError`.
- **D38** — non-artifact tool (no produces meta, or `PRODUCES_RESULT`) with no
  reserved arguments ⇒ no raise (no-op). *Red at baseline:* `ImportError`.
- **D39** — non-dict `_meta` ⇒ fails closed, identical to the sibling
  (`_assert_valid_produces_meta`'s behavior on the same input). *Red at
  baseline:* `ImportError`.
- **D40** (wiring) — the helper is invoked by **both** live-suite functions,
  once per artifact tool. Monkeypatch it with a counting wrapper and drive
  `assert_streamable_http_conformance` / `assert_stdio_conformance` the same
  way P1 wired-tested `_assert_valid_produces_meta` — re-read
  `tests/workers/test_conformance_produces.py` and apply the same shape
  (fake server / counter / whatever it is). If P1 unit-tested the helper only,
  D40 pins the wiring with a monkeypatched counter against a minimal fake and
  records the gap. *Red at baseline:* `ImportError` (nothing to patch).

**Green:** implement `_assert_artifact_reserved_args` beside
`_assert_valid_produces_meta` (:63) and add the call at both live-suite sites
immediately beside the sibling call — conformance.py:274
(`assert_streamable_http_conformance`) and :352 (`assert_stdio_conformance`);
re-read both loops before editing. The helper's docstring: what it
asserts and why (A4 — the reserved arguments must be visible in the tool's
`inputSchema`; they land in the capture store with the rest of the call, and a
worker that declares `produces: artifact` but omits them cannot receive the
injected slug, so the violation is discoverable at conformance time instead of
at the 70-second mark); and an explicit statement that the **result-shape
rule is runtime-only until an invocation harness exists** (§6:593-606) — this
helper does not assert result shapes.

**Task acceptance:** new file green; `tests/workers/` green; ledger complete.

## Task 5 — agent_core acceptance, review, landing

1. **Local acceptance.** `.venv/bin/python -m pytest -q` → green. Record the
   count (baseline 1079 + the new tests). The two pre-existing skips must be
   exactly the two named in preflight — **zero new skips**; the new test
   modules contribute zero skips.
2. **Nothing version-shaped moved:** `pyproject.toml` still `1.11.1`; `git
   tag` unchanged (no new tags); `git diff --stat origin/main...HEAD` shows
   only the intended files.
3. **Review package:** `git diff origin/main...HEAD > /tmp/opencode/p3-review.diff`.
4. **Review** — R6 if preflight's probe succeeded, else the two-pass local
   fallback (R15). The prompt (or the fallback passes) carries the **review
   focus** — five items, each pinned to its tests:
   1. The approval surface shows the injected values, and the dispatched
      object is the mutated original (injection before the snapshot; no
      rebuild) — D21, D22.
   2. Every pre-gate refusal emits a `validation_failed` audit row, sends no
      approval prompt, and dispatches nothing — D13-D17.
   3. Post-dispatch, a `validation_failed` row exists exactly on
      extraction failure and descriptor refusal — never on the
      `_ErrorResult` paths, never on in-band errors, never on
      non-artifact tools (the pre-gate refusals are item 2) — D25-D35.
   4. The capture layer receives the worker's verbatim result on every
      executed artifact path, including refusals (A9) — D24-D30 via the
      capture stub.
   5. The 8-field handoff object is set only on successful validation,
      carries the reconciled host + `produced_by`, and never reaches the
      capture (D24, D30, D34); host disagreement is recorded, not refused
      (D30) — the A2 semantics.
   Plus the **attack-sequence brief** (prose, for the reviewer): a malicious
   worker reporting a different host (lateral targeting — retrieval must come
   from the operator's file, never the worker's claim); a slug smuggled via
   the ctx channel (PARE stamps it; the pool re-validates shape before
   injection, so an invalid slug never reaches the approval prompt or the
   audit row); a tool that logs a line before its descriptor (exactly-one
   refusal); list-of-pairs arguments (loud `TypeError`, no silent divergence);
   an operator declaring an IPv6 `artifact_host` (refused at config load,
   before the 70-second dump); an old PARE daemon without the stamp (every
   artifact dispatch refuses, naming the cwd — fail closed; no artifact
   lands); a reload race (the floor reads the ratcheted
   `produces` high-water; the generation recheck is untouched).
5. **Finding disposition:** every finding is fixed (commit, re-run the suite,
   re-record) or rejected with the rationale in the ledger (P2 cadence).
6. **Landing:** `git push -u origin feat/artifact-dispatch`; open ONE PR
   (agent_core) with the plan summary + the review verdict; **HARD STOP** — do
   not merge.

## Task 6 — PARE: the ctx slug supply

Spec: §6 :463-472. Rulings: R1, R11.
Files: `pare/agent.py`; `tests/test_handle_chat.py`. (The two handback test
files are **unchanged** — see R11: their `agent.config` stubs pre-exist at
:36/:40 and their `cwd=None` skips the stamp before any config read.)

**The stamp** — top of the `with self._bind_store(ctx):` block in
`handle_chat` (pare/agent.py:575), before the inline
`from agent_core.inference import StreamEnd` line (:576):

```python
cwd = getattr(ctx, "cwd", None)
if isinstance(cwd, str) and cwd:
    try:
        ctx.project_slug = resolve_project_slug(
            cwd, marker=self.config.project_marker or ".pare",
            home=Path.home())
    except ProjectSlugError:
        # Outside a project, or an invalid stored slug: the slug channel
        # stays empty for this message. An artifact dispatch against it
        # refuses downstream, naming the cwd (fail closed); non-artifact
        # turns never see this.
        ctx.project_slug = None
```

Executor notes:

- Imports: `resolve_project_slug` and `ProjectSlugError` (base class;
  subclasses `NoProjectError`, `InvalidStoredSlugError` — pare/project_slug.py
  :45-55) go with the module-level `pare` imports (PARE's own module — no
  cycle, no reason to inline them). `Path` is already imported at
  pare/agent.py:23 — no new stdlib import.
- `resolve_project_slug(cwd, *, marker=".pare", home)` — `home` is a required
  keyword (pare/project_slug.py:129); the walk mirrors
  `resolve_capture_db` ($HOME ceiling, filesystem-root stop), so slug and
  capture store always resolve to the same project.
- The stamp may **create** `.pare/project` via `read_or_create_slug` when the
  project has a marker dir but no stored slug — the documented, idempotent
  behavior of the project-slug module (D44 pins it).
- `str`-only cwd (R11): a `Path`-typed `ctx.cwd` is treated as absent and
  refuses downstream. `self.config.project_marker or ".pare"`: `config` is
  framework-populated (absent on a bare `PareAgent()` — the test stubs cover
  it); `None`/empty marker falls back to the config's own default.
- Do **not** reuse `_active_slug` (pare/agent.py:321-335) — global
  last-served state, not per-request.
- No other ctx path stamps (Frida `_frida.py:48`, mitm `mitm.py:77`); a future
  miss fails closed with the cwd-naming refusal.

**Fixture stubs (R11):** `agent.config = SimpleNamespace(project_marker=".pare")`
(plus a one-line comment: "the framework populates config in run_daemon;
handle_chat's slug stamp reads project_marker") added **only** to
`_make_agent` (test_handle_chat.py:17-31). The two handback files need
nothing: they already stub `agent.config` (MagicMock at :40 / :36) and their
tests pin `cwd=None`, so the stamp's `isinstance(cwd, str)` guard skips before
any config read. D47 overrides the stub per test (`project_marker=".projx"`).
Record the stub in the ledger.

**Red first** (in `tests/test_handle_chat.py`; each turn is the cheapest
shape — mode `off`, `_Stream` ending in `StreamEnd`, following
`test_streaming_text_turn` :54):

- **D41** — cwd = a `tmp_path` with `.pare/project` holding a valid stored
  slug (take a slug that matches `ARCTIC_BASE_SLUG_RE` from
  `tests/test_project_slug.py`'s fixtures) ⇒ after one turn,
  `ctx.project_slug == <stored slug>`. Use the `_ctx()` MagicMock with
  `ctx.cwd = str(tmp)`. *Red at baseline:* no stamp — the MagicMock's
  auto-created `ctx.project_slug` is a `MagicMock`, not the string. Right red.
- **D42** — cwd = a bare `tmp_path` (no marker anywhere up the tree) ⇒
  `NoProjectError` caught ⇒ `ctx.project_slug is None`. *Red at baseline:*
  `MagicMock is not None`. Right red.
- **D43** — `.pare/project` holds an invalid slug ⇒ `InvalidStoredSlugError`
  ⇒ `None`. *Red at baseline:* as D42. Right red.
- **D44** — cwd = a `tmp_path` with `.pare/` present but **no** `project`
  file ⇒ the stamp creates the file; `ctx.project_slug` equals the derived
  slug; the file's content is one line matching `ARCTIC_BASE_SLUG_RE`. *Red
  at baseline:* no stamp, no file. Right red.
- **D45** (P-pin) — a **real** `HandlerContext` (the tag's — no
  `project_slug` field) with `cwd=None` ⇒ after a turn,
  `getattr(ctx, "project_slug", "SENTINEL") == "SENTINEL"` (the attribute is
  never touched for a `None` cwd). *Green at baseline; must stay green.*
- **D46** (P-pin) — real `HandlerContext`, `cwd = Path(<a real tmp project
  with a valid slug>)` ⇒ the attribute is untouched (`SENTINEL`) — `str`-only
  (R11). *Green at baseline; must stay green.* If a real `HandlerContext`
  trips on a framework attribute the MagicMock used to auto-serve (a later
  `ctx.emit`/`ctx.agent` dereference inside `handle_chat`), give that one
  attribute a real stand-in (a collecting writer, a stub agent) — do **not**
  fall back to a bare `MagicMock`, whose auto-attributes break the SENTINEL
  assertion. (Verified at planning: `handle_chat`'s body touches only
  `ctx.conversation` and `ctx.channel_id` in its first ~80 lines.)
- **D47** — the agent's config stub has `project_marker=".projx"`; cwd = a
  `tmp_path` with `.projx/project` holding a valid slug ⇒ the stamp resolves
  via `.projx`. *Red at baseline:* no stamp. Right red.

(The two handback files get no new tests and no edits — their pre-existing
config stubs plus `cwd=None` keep the existing suite green once the stamp
reads `self.config`.)

**Green:** implement the stamp.

**Task acceptance:** `tests/test_handle_chat.py` green; the two handback
files green; ledger carries the reds + every fixture stub.

## Task 7 — PARE acceptance, review, landing

1. **Local acceptance.** `.venv/bin/python -m pytest -q` → green **against
   the tag venv** (constraint 3). Record the count (preflight baseline + the
   new tests) and the pre-existing skips (the `*_live.py` tests).
2. **Nothing version-shaped moved:** `git diff pyproject.toml` empty (pin
   still `@v1.11.1`); `git diff workers.yaml` empty (byte-identical,
   constraint 4).
3. **Commit this plan file** to PARE (`docs/superpowers/plans/...`).
4. **Review** — R6 or the two-pass fallback (R15), lighter brief. Focus:
   1. The stamp runs once per message at the top of `handle_chat`'s
      `_bind_store` block; no other ctx path (Frida/mitm) stamps.
   2. Every `ProjectSlugError` ⇒ `None` (fail closed; never re-raised into
      the chat loop).
   3. `None`/non-`str` cwd leaves the attribute untouched (D45, D46).
   4. `workers.yaml` byte-identical; the pin untouched.
   5. Test hermeticity: `tmp` cwds; the walk stops at the filesystem root or
      `$HOME` (none of the `tmp` paths are under `$HOME`); D44's file
      creation stays inside the `tmp` project; nothing writes outside `tmp`.
5. **Landing:** `git push -u origin feat/ctx-slug-supply`; open ONE PR
   (PARE); **HARD STOP** — do not merge.

## Acceptance — P3 is done when

1. **The agent_core suite is green locally** (record the count; exactly the
   two pre-existing skips from preflight, zero new skips; the new modules
   contribute zero skips), and nothing version-shaped moved in agent_core.
2. **The PARE suite is green locally against the tag venv** (record the count
   + pre-existing skips); the pin is `@v1.11.1`; `workers.yaml` is
   byte-identical.
3. **The reviews are on record:** each verdict, each finding's disposition
   with probe evidence, and the R6-or-fallback rationale.
4. **Both PRs are open at the HARD STOP** (agent_core first, PARE second).
   Merging is the orchestrator's; after the orchestrator merges, verify
   `origin/main` and record the landing hashes in the ledger.
5. **The ledger carries everything:** the preflight (including the PARE-CI-red
   fact and the pip install into PARE's venv), every red with its reason,
   every green, every A5 fixture fix, every config stub, the CI run IDs of
   any runs P3's commits triggered, the landing hashes.
6. **This plan file is committed to PARE.**
7. **The handoff below is accurate** against what actually landed.

## Handoff (what P4 consumes)

- **agent_core release:** after the PR merges, bump `pyproject.toml` to
  `1.12.0` and tag `v1.12.0`. The tag must contain P1 (`ea1130c..abe2cef`)
  **and** P3.
- **The four pin bumps** per the P2 handoff — including PARE's
  `pyproject.toml:11` `@v1.11.1` → `@v1.12.0`, and the **PARE venv refresh**
  (from the new tag, or from source per the P1/P2 precedent) — only after the
  bump.
- **PARE's `workers.yaml`** (post-bump — the `extra="forbid"` ordering is why
  the comment moves with the bump, R11): the header comment's
  `artifact_host` line (a continuation line must **not** match the offered-key
  regex `^#\s{2,}([a-z_]{3,})\s{2,}\S` — test_workers_yaml_format_comment.py
  :46 forbids advertising nonexistent fields); the hardware entry's real
  values (`artifact_root` on the bench, `artifact_drive_id` from
  `.bench-store-id`, `artifact_host` — explicit if stdio, defaulted from the
  endpoint otherwise); and a declared `read_timeout` per **R12** (rule: ≥
  expected_max_size / measured_rate + headroom; 2 GB at ~28 MB/s ≈ 70 s).
- **§8's probe worker** (lives in PARE's `scripts/`): Level 1 loopback rig
  (exercises both §5 races), Level 2 bench run; `bench_doctor`'s temporary
  check (it looks for `*+partial`).
- **`publish_descriptor`'s call site** — consumes `ctx.artifact_descriptor`
  (P3's 8-field object; R8) — **and the retrieval command**, which always
  uses `spec.artifact_host`, never the worker-reported host (R9).
- **The kit tag** (the next version — **both** version locations:
  `pyproject.toml` and `src/pare_worker_kit/__init__.py`).
- **The hardware console-capture plan** remains halted until P1-P4 land.
