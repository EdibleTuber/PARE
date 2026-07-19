"""RepeatGuard — detect no-progress tool-call loops within a single turn.

A model can get stuck re-issuing the *same* tool call and getting the *same*
result. In one observed OMTG-SQLite run, 41% of the turn's tool calls were
verbatim repeats returning identical (usually empty) results — one exact
`grep_smali` ran six times — burning the whole tool-call budget without
progress.

The guard keys on the actual pathology, not on a tool allowlist: a call whose
(name, arguments) AND result are unchanged from an earlier invocation this turn
carries no new information. It escalates — appending a nudge to the repeated
result — then, past a hard limit, short-circuits the call so the model stops
spending backend round-trips on it and is told to change approach.

Legitimate re-polls are NOT penalized. The dynamic flow deliberately re-runs
`read_hook_events` / `list_sessions` after the operator triggers an action;
those return a *different* result once data arrives, which resets the baseline.
Only a genuinely stuck repeat (same call → same result) is ever flagged.

Scope is one turn (one `handle_chat` call): construct a fresh guard per turn so
a re-poll on a *later* turn always starts clean.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

_SOFT_AFTER = 1     # identical (call, result) repeats tolerated before a nudge
_HARD_AFTER = 3     # identical (call, result) repeats after which the call is blocked
_CALL_CEILING = 5   # total identical *calls* per turn, regardless of result, before block


def _signature(name: str, arguments: object) -> str:
    """Stable key for a (tool, arguments) pair, independent of dict key order."""
    try:
        args = json.dumps(arguments, sort_keys=True, default=str)
    except (TypeError, ValueError):
        args = repr(arguments)
    return f"{name}\x00{args}"


@dataclass
class _Entry:
    result: str
    count: int   # identical (call, result) streak this turn (>= 1)
    total: int   # total times this call signature has been executed this turn (>= 1)


class RepeatGuard:
    """Two independent hard-block triggers, both poll-aware:

    - **Result-aware** (`hard_after`): the same call returning the *same* result
      too many times carries no new information. A *changed* result resets the
      streak, so a re-poll that eventually returns data is never penalized.
    - **Call-count** (`call_ceiling`): the exact same call issued too many times
      in one turn is pathological even if the result wobbles between a couple of
      values (e.g. a grep flipping 0↔1 rows). Re-issuing an identical query many
      times within a single turn — before any operator action — is itself the
      waste the doctrine warns against.
    """

    def __init__(self, soft_after: int = _SOFT_AFTER, hard_after: int = _HARD_AFTER,
                 call_ceiling: int = _CALL_CEILING):
        self.soft_after = soft_after
        self.hard_after = hard_after
        self.call_ceiling = call_ceiling
        self._seen: dict[str, _Entry] = {}
        self._handed_back: set[str] = set()

    def should_run(self, name: str, arguments: object) -> bool:
        """False once either hard-block trigger has fired for this exact call."""
        entry = self._seen.get(_signature(name, arguments))
        if entry is None:
            return True
        return entry.count < self.hard_after and entry.total < self.call_ceiling

    def record(self, name: str, arguments: object, result: str) -> str:
        """Record a real tool result and return it — appending a no-progress
        note once the identical (call, result) pair recurs past soft_after."""
        sig = _signature(name, arguments)
        entry = self._seen.get(sig)
        if entry is None:
            self._seen[sig] = _Entry(result=result, count=1, total=1)
            return result
        entry.total += 1
        if result != entry.result:
            # Progress: the call returned something new — reset the streak but
            # keep counting total invocations (the call-count trigger still bites).
            entry.result = result
            entry.count = 1
            return result
        entry.count += 1
        if entry.count > self.soft_after and isinstance(result, str):
            return result + self._note(name, entry.count, entry.total, blocked=False)
        return result

    def blocked(self, name: str, arguments: object) -> str:
        """Synthetic result returned in place of a short-circuited call (one for
        which should_run() returned False)."""
        entry = self._seen.get(_signature(name, arguments))
        count = entry.count if entry is not None else self.hard_after
        total = entry.total if entry is not None else self.call_ceiling
        return self._note(name, count, total, blocked=True)

    def tripped(self, name: str, arguments: object) -> bool:
        """True the first time this signature is hard-blocked and has not yet
        handed back this turn. Used to escalate a confirmed spin to the operator
        exactly once (not on every subsequent blocked call)."""
        if self.should_run(name, arguments):
            return False
        sig = _signature(name, arguments)
        if sig in self._handed_back:
            return False
        self._handed_back.add(sig)
        return True

    def entry(self, name: str, arguments: object) -> tuple[int, str] | None:
        """Read-only accessor for the operator-handback question builder:
        (total invocations, last-seen result) for this call signature this
        turn, or None if it was never recorded. Callers outside this module
        should use this instead of reaching into `_seen`/`_Entry`."""
        e = self._seen.get(_signature(name, arguments))
        return None if e is None else (e.total, e.result)

    def _note(self, name: str, count: int, total: int, *, blocked: bool) -> str:
        if blocked:
            lead = (f"`{name}` was NOT re-run: you have issued this identical call "
                    f"{total} times this turn.")
        else:
            lead = (f"`{name}` has now returned the same result {count} times.")
        return (f"\n\n[repeat-guard] {lead} Re-issuing an identical call yields no "
                f"new information. Change approach — try a different class, method, "
                f"or search pattern; or step back and reconsider whether you are "
                f"looking at the right target: re-read what the operator said they "
                f"observed and start from the class or activity it points to.")
