# PARE

**P**ersonal **A**gentic **R**everse **E**ngineer — a conversational mobile-RE
operator.

PARE holds a tool-using conversation about reverse-engineering work. It reasons
over a chat, **consults PAL's research vault** by semantic search (so it answers
from curated knowledge, not just training data), and **drives RE workers** —
static APK analysis, Frida instrumentation, HTTPS interception — through a
risk-gated dispatch layer that pauses dangerous actions for operator approval and
audits every call. Built on
[`agent_core`](https://github.com/EdibleTuber/agent_core).

**→ [Get it running](QUICKSTART.md)** (a few minutes, needs an inference server
on your LAN).

## The RE loop

Reverse engineering in PARE is a loop. Static and dynamic analysis are *tools
that serve the loop's beats* — the structure is the methodology, not any one
tool:

1. **Orient** — from the behaviour the operator describes, find the region of
   code or traffic it corresponds to. The operator's description is a lead to
   corroborate, not ground truth.
2. **Enumerate** — build the *candidate set* before committing: every site that
   could produce the symptom, not the first idiom that matches.
3. **Hypothesize** — pick one candidate and pin what you expect to observe at
   runtime.
4. **Verify** — confirm, don't re-discover. An empty capture means "not
   triggered yet"; a *contradicting* value means the target is wrong.
5. **Re-orient** — on a dead end, advance to the next candidate; on a runtime
   surprise, go back to static to explain it.

Each worker serves different beats: static analysis is the front of the loop,
Frida is the Verify beat, and traffic interception can serve either.

The loop as the model actually runs it lives in
[`pare/prompts/system.md`](pare/prompts/system.md).

### Two guards, because prose alone won't hold the model to it

- **Repeat-guard** (`pare/repeat_guard.py`) — a no-progress floor. A tool call
  that repeats with the same result is short-circuited instead of spinning to
  the round cap.
- **Operator-handback checkpoints** (`pare/handback.py`) — PARE ends its turn
  with a question when it is stuck, or when it is about to commit to one of
  several near-duplicate candidates. That turns a silent wrong commit into a
  one-line correction.

## The capture store: context spent on what the model asks for

RE tools produce large output — a process list, every class in an APK, a
module's exports, an hour of HTTPS flows. Pasting all of it into the
conversation would fill a local model's context window in a few calls. Even a
model with a 1M-token window would be paying, on every later turn, for rows it
never needed.

So PARE stores every worker result and only lets small ones through. A large
result is replaced in the conversation by a stub of at most 512 bytes: what was
captured, how many rows, which columns, and the `ref` to read it by. The model
then **asks for just what it is interested in**. It searches across everything
captured with `search_capture`, and reads one result a window at a time with
`read_capture`. A 500-class `enumerate_classes` result therefore costs a few
hundred bytes, not the whole class list. Only the matches for the class the
investigation is about ever reach the model.

Because nothing is thrown away, findings outlive the call that produced them.
They can be searched later in the investigation, and after the worker itself is
unloaded. `/snapshot` shows any capture to the operator in full, read straight
from the store, with no model in the path.

How it works, where the store lives and when a result is stubbed:
[`ARCHITECTURE.md`](ARCHITECTURE.md#the-capture-store).

## Where everything else lives

| | |
|---|---|
| [`QUICKSTART.md`](QUICKSTART.md) | install, configure, run, first conversation, troubleshooting |
| [`WORKERS.md`](WORKERS.md) | the worker registry, risk tiers and operator approval, `/worker`, adding one |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | daemon + client, a chat turn end to end, RAG, risk gating, where the code lives |
| [`DEPLOY.md`](DEPLOY.md) | the daemon as a service, the bench Pi, ArcticBase |
| [`docs/tui-quickstart.md`](docs/tui-quickstart.md) | the TUI operator surface: chat + live UART pane, layout, daemon auto-spawn |
| [`docs/frida-quickstart.md`](docs/frida-quickstart.md) | the worker on the device's laptop, `frida-server`, attach, Java hooks, operator commands |
| [`docs/mitm-quickstart.md`](docs/mitm-quickstart.md) | HTTPS interception end to end |
| [`docs/superpowers/`](docs/superpowers/) | designs, implementation plans and build records |
| [`bench/INSTALL.md`](bench/INSTALL.md) | the Raspberry Pi at the workbench |

## Status

PARE is one of two agents on `agent_core` — PAL curates the research vault, PARE
reads it and drives RE workers. Active work and open threads are in
[`docs/superpowers/`](docs/superpowers/); the most recent state of the physical
bench is the newest `*-bench-state.md` there.
