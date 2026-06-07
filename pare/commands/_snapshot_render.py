"""Deterministic, width-clipped table rendering for /snapshot.

Pure functions: the command runs in the daemon and yields a string over a
socket, so it cannot see the user's TTY or spawn a pager — columns are clipped
to a conservative fixed width instead of allowed to hard-wrap.
"""
from __future__ import annotations


def _clip(value: str, width: int) -> str:
    return value if len(value) <= width else value[: max(1, width - 1)] + "…"


def render_table(rows: list[dict], max_width: int = 100) -> str:
    if not rows:
        return "(no rows)"
    cols = list(rows[0].keys())
    cell = {c: [str(r.get(c, "")) for r in rows] for c in cols}
    # Natural width per column, then shrink the widest until the line fits.
    widths = {c: max(len(c), *(len(v) for v in cell[c])) for c in cols}
    sep = 2  # spaces between columns
    def line_len() -> int:
        return sum(widths.values()) + sep * (len(cols) - 1)
    while line_len() > max_width and any(w > 6 for w in widths.values()):
        widest = max(widths, key=lambda c: widths[c])
        widths[widest] -= 1
    def fmt(vals: list[str]) -> str:
        return (" " * sep).join(_clip(v, widths[c]).ljust(widths[c])
                                for c, v in zip(cols, vals)).rstrip()
    out = [fmt(cols), fmt(["-" * widths[c] for c in cols])]
    out += [fmt([cell[c][i] for c in cols]) for i in range(len(rows))]
    return "\n".join(line[:max_width] for line in out)


def render_catalog(sources: list[dict]) -> str:
    if not sources:
        return "(no snapshots captured yet)"
    return "\n".join(f"{s['count']:>6}  {s['source']}" for s in sources)
