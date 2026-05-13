# PARE v1 Design — Round-2 Panel Findings (Addendum)

**Date:** 2026-05-12 (review); preamble updated 2026-05-13
**Status:** Historical addendum. The main spec was recalibrated to a benign threat model (IoT/app-store apps) on 2026-05-13 after this round-2 review, which means most of the findings below are no longer applicable to v1 — they were assessing an adversarial-tier design that has since been reverted. The file is retained so the analysis isn't lost; if PARE's threat profile ever shifts to include malware, this is the catalog of issues to address in that pass.

## Context

After the consolidated spec rewrite (project-directory restructure, §9.2 defense-in-depth, evidence base, boundary tag mechanism, guardrail model), the five-expert panel was re-engaged. Each reviewer received their round-1 findings, a change summary, and the current design, with two tasks: (a) verify whether their prior concerns were addressed, (b) flag new issues introduced by the changes.

Overall: most round-1 findings landed as RESOLVED. A meaningful set carried over as PARTIAL or UNRESOLVED, and the panel surfaced new issues — many of which converge across multiple reviewers.

**Caveat on triage:** the session got security-heavy by the time round-2 ran, so the new findings skew toward the security and framework lenses. Some of these are real-and-load-bearing; others are paranoia-tier or premature optimization for v1's primary use case (app-store apps, not malware-as-default). Treat this addendum as material for a v1.0.1 pass, not as blockers to the v1 spec itself unless something here looks like it would break the design.

## Carryover findings (round-1 items still PARTIAL or UNRESOLVED)

### Security architect
- **C2 PARTIAL** — composition gate default budget for primary use not stated; per-chain taint escalation rule not explicit.
- **C4 PARTIAL** — §9.1 has no-privileged + scoped USB, but no explicit seccomp profile, `no-new-privileges`, or cap_drop list documented inline.
- **C5 PARTIAL** — pinning is integrity, not isolation. Ghidra/Hopper MCP still runs in PARE's trust zone.
- **I1 PARTIAL** — `O_NOFOLLOW` / `openat2(RESOLVE_BENEATH)` on findings writes not mandated; symlink races implementation-defined.
- **I3 UNRESOLVED** — auto-reattach on `-32002` does not explicitly re-prompt or invalidate carried-over HITL consent.
- **I4 UNRESOLVED** — audit log still reachable from the worker container; no host-side WORM sink described.
- **I5 PARTIAL** — vault poisoning via LLM-generated summary text has no structural mitigation (diff cap, dual review, etc.).
- **I6 PARTIAL** — pairing material lifetime/storage during a session not specified.
- **N3 UNRESOLVED** — size cap pre-Pydantic-parse not explicit; parser-DoS path remains.

### MCP / agent framework engineer
- **5 PARTIAL** — `register_tools()` hook designed but integration with PAL's existing inference loop unverified until Phase 0.
- **7 PARTIAL** — external MCP name-pattern override is heuristic over untrusted strings; servers publishing `persist_state` etc. evade.
- **10 UNRESOLVED** — stub worker fixture doesn't catch FastMCP-specific bugs (anyio.to_thread.run_sync, lifespan ordering).

### Mobile RE practitioner
- **8 PARTIAL** — reattach semantics across frida-server OOM, device reboot, USB renumber, app force-stop undefined.
- **9 UNRESOLVED** — objection-style helpers explicitly deferred to v1.1.
- **10 UNRESOLVED** — decompile-then-pick-target loop deferred to v1.1.

### DevOps / SRE
- **1 PARTIAL** — warnings surface problems but don't block; missing-worker still manifests mid-session.
- **2 PARTIAL** — recovered job state machine on daemon restart unspecified.
- **3 PARTIAL** — disk-growth warnings are not quotas.
- **5 UNRESOLVED** — no periodic worker liveness ping.
- **9 PARTIAL** — `/snapshot` is operator-pull only; no off-host backup story.
- **10 UNRESOLVED** — inference SPOF graceful degradation deferred.
- **11 PARTIAL** — `systemd or docker-compose` named as external-MCP supervisor without unit files / restart policy.
- **14 UNRESOLVED** — `.env` permissions and missing-file behavior unspecified.

### Software architect
- **6 UNRESOLVED** — flat namespace persists; capability-tag-driven selection deferred.
- **11 PARTIAL** — Pydantic validation error path direction stated, but field-level hint shape not specced.
- **12 UNRESOLVED** — monorepo split trigger still vague.
- **13 PARTIAL** — direct vault writes to PAL acknowledged with §13 open question; no concrete cutover trigger.

## New issues — load-bearing (recurring across reviewers)

1. **Side-index transport + integrity** (Security NC1, MCP N3, Architect N9). Where the boundary-tag side index lives, its integrity under memory pressure, and *how tags reach PARE from worker output through MCP*. Without a normative channel, external MCP servers can forge inline markers in `content` field strings.
2. **Session terminology overload** (Architect N2). `session_epoch` means worker-reattach scope in §4.4 and daemon-process-lifetime in §4.10.1. Audit log records both ambiguously.
3. **Guardrail classifier is in-band & untested** (Security NC2). Gemma-4-E4B consumes attacker-controlled tokens; no adversarial eval; no fail-closed on classifier OOM/timeout.
4. **`PARE_SNAPSHOT_CMD` has no contract** (DevOps N1). Exit codes, timeout, output capture, blocking, partial-success, name format, sudo escalation all undefined.
5. **Project-directory has no concurrency model** (DevOps N2). Two PARE invocations vs. same project can corrupt audit log / journal.
6. **Auto-reattach + HITL approval carry** (Security I3 carryover). Silent re-granting of consent.
7. **MCP progress / cancellation lifecycle precision** (MCP N1, N2, N7). progressToken ownership, late notifications, cancellation vs reattach races.
8. **Audit-log redaction interface** (Architect N1). Redaction in PARE source but audit-log shape in agent_core — next consumer reinvents.
9. **`script_messages` pull-model loses high-rate `send()` traffic** (Mobile N1). Real Frida scripts emit thousands/sec; need bounded buffer + backpressure or progress-stream.
10. **`setup_proxy(magisk)` is persistent host-side device mutation** with no teardown contract (Mobile N2).

## New issues — worth absorbing (quality improvements)

- `/forget-taint` is an audit-evasion primitive — needs append-only audit of the command with reason (Security NI1).
- 32-char min-match brittle for short attacker-controlled artifacts (Security NI2).
- Startup warnings ≠ enforcement — operators dismiss; at minimum PARE-as-root + non-127.0.0.1 binding should be hard-fail with `PARE_I_ACCEPT_<X>=1` opt-out (Security NI3, DevOps N6).
- `supply-chain.yaml` itself unsigned — PR-bumping-pin is the attack (Security NI4).
- `request_id` uniqueness not enforced — worker-generated UUIDs collide (DevOps N4).
- Pre-Pydantic-parse size enforcement not explicit (Security N3 carryover, DevOps disk-fill).
- §9.1 hardening profile completeness — seccomp / no-new-privs / cap_drop list explicit (Security C4 carryover).
- `register_tools()` + declarative `tools=[...]` precedence unspecified (Architect N4).
- `list_classes` on large apps returns 50k+ classes; no pagination (Mobile N5).
- Periodic worker liveness ping (DevOps #5 carryover).
- `/health` endpoint schema undefined (DevOps N9).
- Audit-log size cap on daily rotation (DevOps N10).
- `journal.md` / `manifest.md` are LLM-writable — injection vector on session resume (Security NN1).
- `decrypt_binary` / `keychain_dump` need capability probe for non-JB / iOS 16+ trustcache (Mobile N3).
- `overload_signature?` ambiguity — `java_hook` needs disambiguation policy (Mobile N4).
- Script lifecycle vs session lifecycle (Mobile N6): hooks across reattach.
- `pull_app_data` semantics differ across iOS JB / non-JB (Mobile N7).
- Per-session script workspace should arguably be project-scoped (Mobile N9).
- `GuardrailClassifier` prompt in `agent_core` is PARE-specific (Architect N3): injection point needed.
- `read_findings` URI scheme governance (Architect N5).
- External-MCP conformance gap interacts with contract invariants (Architect N6).

## New issues — nits

- Reviewer-fatigue mitigations for startup warnings (DevOps N6).
- `-32005` collision risk with future SDK error codes (MCP N6).
- Worker-newer "refuses connection" should use MCP initialize-response error path, not transport reject (MCP N9).
- Per-call contract-version stamping inflates logs vs per-session (MCP N10).
- iOS platform parity illusions (Mobile N10-14): `system_log` predicate syntax, `grant_runtime_permissions` companion, frida v8 vs QuickJS runtime, `list_apps` on JB vs non-JB.
- CA fingerprint verification path (Architect N7).
- `/forget-taint` orphan markers in history (Architect N8).
- Runbooks not version-pinned to spec (DevOps N7).
- Supply-chain refresh cadence (DevOps N8).
- LUKS framing for samples-at-rest (Security NN3).
- chrony not mandated in dependencies (Security NN2).

## Triage Posture

These are notes, not blockers. The current spec is publishable, presentable, and represents a coherent v1 design. The load-bearing 10 should be addressed in a v1.0.1 pass before implementation work on Phase 2+ begins; many of the worth-absorbing items can land alongside implementation as they become relevant; nits can wait for v1.x.

A useful self-check before any future security pass: what part of the spec hasn't been stress-tested yet? Tool surface ergonomics, recipe shape, operator UX, dev-loop, Phase 2-7 details have less depth than the security architecture. Balance the next pass accordingly.
