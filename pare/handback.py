"""Pure helpers for operator-handback checkpoints (see the 2026-07-18 spec).

No agent_core / worker changes: candidate classes are parsed from a grep result
(or its capture body), scanning L...; type tokens across the whole row so a
variant is found whether it is the enclosing class or a referenced type."""
from __future__ import annotations

import json
import os
import re

_LTOKEN = re.compile(r"L[\w/$]+;")

# Worker-prefixed tool names (agent_core prefixes by worker). Any new class-scoped
# dig-in / instrumentation tool that could commit to the wrong class goes here.
COMMIT_TOOLS = frozenset({"static_list_methods", "static_decompile_method", "frida_java_hook"})
NAME_SEARCH_TOOLS = frozenset({"static_grep_smali"})
POLL_TOOLS = frozenset({"frida_read_hook_events", "frida_list_sessions"})


def normalize_class(name: str) -> str:
    """smali `Lsg/vp/Foo$Bar;` -> dotted `sg.vp.Foo$Bar`; dotted passes through."""
    if not name:
        return name
    n = name.strip()
    if n.startswith("L") and n.endswith(";") and "/" in n:
        n = n[1:-1].replace("/", ".")
    return n


def _simple_name(dotted: str) -> str:
    return dotted.rsplit(".", 1)[-1]


def _pattern_stem(pattern: str) -> str:
    """The bare class-name portion of a grep pattern, so a qualified pattern
    compares against a class simple name. gemma greps in either form — bare
    (`OMTG_DATAST_001_SQLite`) or fully-qualified smali/dotted
    (`Lsg/vp/.../OMTG_DATAST_001_SQLite`, `...SQLite;`, `sg....SQLite`). Without
    this, the qualified form never matched a simple name and disambiguation
    silently didn't fire (smoke test 1)."""
    p = (pattern or "").strip().rstrip(";")
    return p.rsplit("/", 1)[-1].rsplit(".", 1)[-1]


def _rows_from(result: str, capture_store) -> list:
    try:
        d = json.loads(result)
    except (TypeError, ValueError):
        return []
    if isinstance(d, dict) and isinstance(d.get("rows"), list):
        return d["rows"]
    ref = None
    if isinstance(d, dict):
        ref = (d.get("captured") or {}).get("ref") or d.get("ref")
    if ref and capture_store is not None:
        rec = capture_store.get(ref)
        if rec and rec.get("body"):
            try:
                inner = json.loads(rec["body"])
                if isinstance(inner, dict) and isinstance(inner.get("rows"), list):
                    return inner["rows"]
            except (TypeError, ValueError):
                return []
    return []


def candidate_classes(result: str, pattern: str, *, capture_store=None) -> set[str]:
    """Distinct dotted class names referenced in a grep result whose simple name
    contains `pattern`. Scans L...; tokens across each row (class/insn/match)."""
    out: set[str] = set()
    stem = _pattern_stem(pattern)
    if not stem:            # empty pattern must not match everything
        return out
    for row in _rows_from(result, capture_store):
        blob = json.dumps(row) if not isinstance(row, str) else row
        for tok in _LTOKEN.findall(blob):
            dotted = normalize_class(tok)
            # Pure "contains" extraction, per the documented interface. NOTE: the
            # brief's sample used the same `pattern in simple` test but its
            # broad-grep test expected set() for a lone framework class — a self-
            # contradiction. Framework-noise filtering is NOT this function's job;
            # it belongs at the downstream near_duplicate >=2 gate (Task 3), where a
            # lone framework class (1 candidate) never arms disambiguation. Keeping
            # this a dumb extractor avoids silently dropping exact-name app searches
            # (e.g. `grep MainActivity` must still yield the MainActivity class).
            if stem in _simple_name(dotted):
                out.add(dotted)
    return out


def _common_prefix(names: list[str]) -> str:
    return os.path.commonprefix(names)


def near_duplicate(candidates: set[str], pattern: str) -> bool:
    """≥2 candidates that are variants of EACH OTHER: a shared stem that contains
    the pattern and is most of every simple name (guards against unrelated classes
    that merely share a short token, e.g. User*)."""
    simples = [_simple_name(c) for c in candidates]
    if len(set(simples)) < 2:
        return False
    stem = _common_prefix(simples)
    if _pattern_stem(pattern) not in stem:
        return False
    return all(len(stem) >= 0.6 * len(s) for s in simples)


def disambig_question(cls: str, candidates: set[str]) -> str:
    listed = ", ".join(f"`{_simple_name(c)}`" for c in sorted(candidates))
    return (f"I'm about to dig into `{_simple_name(cls)}`, but the search referenced "
            f"{len(candidates)} near-identical classes: {listed}. Which is the target?")


def spin_question(name: str, arguments: dict, repeats: int, last_result: str,
                  candidates: set[str]) -> str:
    base = (f"I've re-run `{name}({_fmt_args(arguments)})` {repeats}× with the same "
            f"result (`{last_result[:80]}`) and I'm stuck.")
    if candidates:
        listed = ", ".join(f"`{_simple_name(c)}`" for c in sorted(candidates))
        base += f" That search referenced: {listed}."
    return base + " Which should I dig into, or how would you like me to proceed?"


def _fmt_args(arguments: dict) -> str:
    return ", ".join(f'{k}="{v}"' for k, v in (arguments or {}).items())
