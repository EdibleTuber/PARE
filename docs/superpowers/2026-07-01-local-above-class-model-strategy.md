# Strategy: local-model-above-its-class via the research/RAG pipeline

**Date:** 2026-07-01
**Status:** Strategy / roadmap — starts **after** the capture-layer fold (Plan 2: PARE wiring + pare-frida-mcp teardown) lands.
**Owner:** PARE (RE agent), fed by PAL (research agent) + the agent_core capture store.

## The hypothesis

A **local** model, backed by the research/RAG pipeline (PAL's corpus + the agent_core
capture store with `search_capture`/`read_capture`), can perform **above its parameter class**
on reverse-engineering work — without ever calling out to a remote/bigger model.

Constraint: **stay local.** No remote escalation tier. Hardware is the Tesla P40 (23 GB,
Pascal, weak compute), so the practical model class is **low-active-parameter MoE**
(the `a4b`/A3B tier — e.g. the current `gemma-4-26b-a4b` fires ~4B active params/token,
which is exactly why it runs acceptably on the P40). Don't pick a model for context length —
the capture layer means the model works from ~512 B stubs + targeted retrieval, never whole
artifacts. Pick for **tool-calling reliability** and **code/assembly reasoning**.

## Why it's only half true (the framing that drives the whole plan)

The hypothesis splits along two axes, and RAG only wins one:

- **Knowledge axis — RAG wins decisively.** Much of RE is "what is this?" (an API, a protocol
  field, a constant, a vendor SDK's pinning trick, a prior teardown). That is retrievable, and a
  small model + good retrieval beats a big model with stale/no knowledge. RAG also *grounds* the
  model against real artifacts (decompiled code, captured traffic), suppressing the hallucination
  small models are most prone to.
- **Reasoning axis — RAG cannot rescue it.** Deobfuscating control flow, inferring an
  undocumented state machine, working out what an optimized loop does — retrieval injects facts,
  not reasoning depth. This is the hard ceiling where "above its class" fails.

RE blends both. **The strategy is therefore to architect so that as much work as possible rides
the knowledge axis and as little as possible depends on unaided reasoning.** This is mostly
harness design, not model choice.

## The levers (strongest first)

1. **Make the *tools* do the reasoning.** Every bit of analysis pushed into a deterministic tool
   is reasoning the model doesn't have to do: a Ghidra script that returns an already-extracted
   call graph, a decompiler pass that's already annotated, a mitmproxy addon that hands over
   already-correlated request/response pairs. The model interprets pre-digested structure instead
   of reasoning from raw bytes. Biggest capability multiplier; pure harness work, no model risk.
2. **Retrieve *playbooks*, not just facts.** Put "how to approach reversing SDK Y," worked
   examples, and *successful tool sequences* into PAL's corpus — retrieval-augmented *planning*.
   Hand the small model a retrieved plan instead of asking it to invent one.
3. **Decompose until each step is small-model-sized.** A small model can't do a 12-step
   deduction but can do twelve 1-step deductions if the harness checkpoints state between them.
   The capture store is the external working memory that makes this work — each hop stays trivial;
   the model never holds the whole chain.
4. **The capture store is a fine-tuning corpus, not just a runtime cache.** This is the
   local-only escalation path that replaces "call a bigger model." Every *successful*
   investigation is a trajectory (prompt → tool calls → retrieved context → conclusion). Distill
   those into a LoRA to specialize a small model on (a) the exact tool-calling contract and
   (b) domain patterns — precisely where stock small models fail. Staying local means the
   escalation lever is **specialization**, and the pipeline already generates the training data.

## The risk to respect

RAG can make a small model **confidently wrong** — retrieved context lends false authority, so a
bad deduction gets dressed up as grounded. Worse than "I don't know" because it's harder to
catch. Mitigate: cross-check conclusions against retrieved ground truth; where a step matters,
sample and check for self-consistency (the MoE's low active-param count makes extra samples
cheaper than the P40's raw speed implies).

## The thing that decides whether any of this is real: the eval harness

"Above its class" cannot be judged by vibes, and staying local removes the big-model safety net —
so measurement is mandatory. Build a fixed set of RE tasks, graded three ways:

- **small-model alone**
- **small-model + full pipeline** (RAG + capture store + tool-does-the-reasoning)
- **big model** (ceiling reference only — not a runtime dependency)

If small+pipeline approaches the big-model reference on the task mix, the hypothesis is proven and
you know *which task types* it holds for. If it doesn't, the harness tells you **which axis** is
failing: knowledge → improve retrieval; reasoning → push more into tools (lever 1) or specialize
(lever 4). The existing tool suite + capture store already provide the substrate to generate and
grade these runs.

## Sequencing

0. **(Prereq, in flight, other session)** Finish the capture-layer fold — Plan 2: wire
   `CaptureLayer` into `PareAgent.setup()` (compute the **window-derived** inline budget, not the
   4096 placeholder), register `search_capture`/`read_capture`, repoint `/snapshot` + `_EnumView`
   at the PARE-side store, thread `cwd`; then the lockstep pare-frida-mcp teardown.
1. **Build the eval harness first.** Fixed RE task set + the three-way grading above. Makes every
   later choice evidence-based. (Recommended starting point.)
2. **Lever 1 — push reasoning into tools.** Highest capability multiplier, no model risk; likely
   the biggest single win the eval will reward.
3. **Lever 2 — playbook retrieval** in PAL's corpus (retrieval-augmented planning).
4. **Lever 3 — step decomposition** in the PARE harness, using the capture store as working memory.
5. **Lever 4 — LoRA specialization** on distilled successful trajectories from the capture store
   (the local-only capability escalation).

## Model-selection notes (for when the eval is ready)

- Compare **within the low-active-MoE class** (A3B/A4B-active tier). Avoid dense 32B coders — they
  fit in 23 GB but prefill painfully slowly on the P40; a 3–4B-active MoE will feel far better
  per step.
- Rank candidates on **tool-calling reliability** (does it reliably emit correct worker calls and
  *remember* to call `search_capture`/`read_capture`?) and **code/asm reasoning** — not on context
  window or generic leaderboard scores.
- The dual-slot backend lets you run the orchestrator on the P40 and offload high-frequency grunt
  (triage, stub filtering, hook boilerplate) to the small iGPU slot — factor that into per-role
  model choice.

## Eval-harness build map (v1: Frida + a MASTG/DVA target)

Start dynamic (Frida) because it matches the currently-wired tool suite. Targets are the
OWASP MAS Crackmes (UnCrackable L1/L2/L3) and/or a DIVA-style vulnerable app — purpose-built,
**known solutions = ground truth**, and mostly **executably gradeable**.

**Two genuinely hard subsystems; everything else is plumbing.**

### Hard subsystem A — deterministic device environment (the schedule risk)
An existing rooted emulator with `frida-server` running clears *bring-up*, but a live dev
emulator is NOT a reproducible eval environment (it accumulates state). The eval-specific work:
- **Snapshot the current working state** (rooted, frida-server up, app installed) as the golden
  image; **restore before every run.** Capture it now, before it drifts.
- **Pin versions:** frida host+server, system image (+ x86_64 vs ARM — pin deliberately; anti-
  tamper can behave differently on x86), APK build. Version drift silently invalidates results.
- **Readiness barrier** before the agent starts: `wait-for-device` → `sys.boot_completed` →
  frida reachable → app installed. Prefer Frida **spawn over attach** for determinism.
- **Per-run isolation:** the agent's hooks/app-data mutations must not leak between runs
  (snapshot restore, or uninstall/reinstall + `pm clear`).
- Start fresh-boot+reinstall (correctness); optimize to snapshot-restore (speed) once the loop works.

### Hard subsystem B — grading oracles
- **`submit_answer(...)` tool:** grade the tool call, not free-text prose. Highest-leverage single
  decision; de-flakes value grading and doubles as a "finished" signal.
- **Value oracles** (secret extraction) → match submitted answer vs ground truth.
- **Behavioral oracles** (bypass tasks) → scripted probe asserting the protected behavior now works
  (logcat marker / returned value / reached state). Bespoke per task and must be rock-solid — a
  flaky oracle lies. Binary pass/fail for v1.

### Plumbing (straightforward once A+B hold)
- **Task spec** (declarative): APK, goal, axis label, difficulty, oracle ref, timeout,
  **max-steps/max-tool-calls** (a flailing small model loops forever — the cap is load-bearing),
  retry budget.
- **Arm controller:** one config per arm (model id/quant, RAG on/off, corpus snapshot, tool set).
- **k-trial runner:** each (task × arm) run k times at production temp. Discipline: **environment
  fixed (snapshot), agent varied (sampling)** — separate the two noise sources.
- **Reporter:** per-run rows (pass/fail, oracle output, steps, tool-call reliability, latency,
  trajectory ref via the capture store) aggregated by **arm × axis × difficulty**.

### Reproducibility ledger (stamp every run)
Emulator image + frida versions, **APK hash + mutation seed**, **corpus snapshot hash** (the
contamination control — know what RAG could see), model id/quant.

### Contamination control (deferred past v1)
MAS crackmes have public writeups and are likely in model training data → a small model may solve
them from memory. So v1 measures **tool-orchestration / procedure competence**, not novel
reasoning. To measure reasoning later: **mutate the target** (apktool rebuild + resign with a
fresh secret so procedure transfers but the answer can't be recalled) and/or add lesser-known
targets. Rebuild/resign is its own fiddly pipeline — defer it.

### Build order (walking skeleton outward)
0. One task, one arm, by hand → a single crackme driven to a graded pass/fail on the pinned
   emulator (flushes out ~80% of subsystem-A pain).
1. Harden environment reset + readiness gating (A).
2. Oracle layer + `submit_answer` tool (B).
3. Arm controller + k-trial runner + reporter (plumbing).
4. Mutation pipeline (contamination fix).
5. Scale to the full task ladder; add static (Ghidra) / network (mitmproxy) tiers as those tools
   come online — both are *more* reproducible than dynamic, so the harness gets easier, not harder.

**Bottom line:** subsystem A is the schedule risk (finicky, version-brittle); the `submit_answer`
decision in B is the highest-leverage choice. The existing rooted emulator turns A's task from
"get Frida working" into "freeze what works and make it restorable."

## Related

- Capture-layer design: `PARE/docs/superpowers/specs/2026-06-30-shared-capture-layer-design.md`
- Capture-layer plan (agent_core / Plan 1 — built; PARE-side Plan 2 pending):
  `PARE/docs/superpowers/plans/2026-06-30-capture-layer-agent-core.md`
- agent_core capture package (implemented): `agent_core/agent_core/capture/`
