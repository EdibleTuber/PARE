"""RepeatGuard: no-progress tool-call loop detection.

Keys on the actual pathology observed in a live OMTG-SQLite run — the model
re-issued the *same* tool call and got the *same* (often empty) result dozens
of times, burning the whole tool-call budget. Legit re-polls (same call,
*different* result) must NOT be penalized.
"""
from pare.repeat_guard import RepeatGuard


def test_distinct_calls_are_never_flagged():
    g = RepeatGuard()
    assert g.should_run("static_grep_smali", {"pattern": "A"}) is True
    assert g.record("static_grep_smali", {"pattern": "A"}, "1 match") == "1 match"
    assert g.should_run("static_grep_smali", {"pattern": "B"}) is True
    # different args → a fresh call, returned verbatim, no note
    assert g.record("static_grep_smali", {"pattern": "B"}, "0 matches") == "0 matches"


def test_identical_call_and_result_gets_a_soft_note():
    g = RepeatGuard(soft_after=1, hard_after=3)
    g.record("static_grep_smali", {"pattern": "X"}, "0 matches")
    # second identical (call, result) is the first repeat → note appended
    annotated = g.record("static_grep_smali", {"pattern": "X"}, "0 matches")
    assert annotated.startswith("0 matches")
    assert "repeat-guard" in annotated
    assert len(annotated) > len("0 matches")


def test_hard_limit_short_circuits_without_running():
    g = RepeatGuard(soft_after=1, hard_after=3)
    for _ in range(3):
        assert g.should_run("static_grep_smali", {"pattern": "X"}) is True
        g.record("static_grep_smali", {"pattern": "X"}, "0 matches")
    # count has reached hard_after → further identical calls must not execute
    assert g.should_run("static_grep_smali", {"pattern": "X"}) is False
    blocked = g.blocked("static_grep_smali", {"pattern": "X"})
    assert "repeat-guard" in blocked


def test_changed_result_resets_baseline_poll_case():
    """read_hook_events-style poll: same call, but once data arrives the result
    changes. The changed result resets the guard — no note, never blocked."""
    g = RepeatGuard(soft_after=1, hard_after=3)
    g.record("read_hook_events", {"session_id": "s"}, "0 events")
    g.record("read_hook_events", {"session_id": "s"}, "0 events")  # repeat → would soft-note
    # now the poll finally returns data — different result resets to a clean baseline
    fresh = g.record("read_hook_events", {"session_id": "s"}, "1 event: plaintext=hunter2")
    assert fresh == "1 event: plaintext=hunter2"
    assert "repeat-guard" not in fresh
    assert g.should_run("read_hook_events", {"session_id": "s"}) is True


def test_call_count_trigger_blocks_wobbling_results():
    """The exact same call whose result flips between a couple of values (the
    real 6x grep flipped 0<->1 rows) evades the result-aware streak but must
    still be hard-blocked once it has been issued call_ceiling times."""
    g = RepeatGuard(soft_after=1, hard_after=3, call_ceiling=5)
    wobble = ["0 matches", "1 match"]
    for i in range(5):
        assert g.should_run("static_grep_smali", {"pattern": "X"}) is True
        g.record("static_grep_smali", {"pattern": "X"}, wobble[i % 2])
    # 5 identical calls issued this turn — no result-streak ever reached 3, but
    # the call-count ceiling fires anyway.
    assert g.should_run("static_grep_smali", {"pattern": "X"}) is False
    assert "5 times" in g.blocked("static_grep_smali", {"pattern": "X"})


def test_poll_under_ceiling_is_never_blocked():
    """A re-poll that stays under the call ceiling and eventually returns data
    is never blocked (poll-safety preserved after adding the call-count trigger)."""
    g = RepeatGuard(soft_after=1, hard_after=3, call_ceiling=5)
    for _ in range(3):  # 3 empty polls, under the ceiling of 5
        assert g.should_run("read_hook_events", {"session_id": "s"}) is True
        g.record("read_hook_events", {"session_id": "s"}, "0 events")
    fresh = g.record("read_hook_events", {"session_id": "s"}, "1 event: secret")
    assert "repeat-guard" not in fresh
    assert g.should_run("read_hook_events", {"session_id": "s"}) is True


def test_signature_is_argument_order_independent():
    g = RepeatGuard(soft_after=1, hard_after=3)
    g.record("t", {"a": 1, "b": 2}, "r")
    annotated = g.record("t", {"b": 2, "a": 1}, "r")  # same args, different key order
    assert "repeat-guard" in annotated


def test_tripped_fires_once_per_signature():
    g = RepeatGuard(soft_after=1, hard_after=3, call_ceiling=5)
    for _ in range(3):
        g.record("static_grep_smali", {"pattern": "X"}, "0 matches")
    # now hard-blocked
    assert g.should_run("static_grep_smali", {"pattern": "X"}) is False
    assert g.tripped("static_grep_smali", {"pattern": "X"}) is True   # first time
    assert g.tripped("static_grep_smali", {"pattern": "X"}) is False  # only once
    # a different, non-blocked signature never trips
    assert g.tripped("static_grep_smali", {"pattern": "Y"}) is False
