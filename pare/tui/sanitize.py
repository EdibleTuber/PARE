"""Untrusted bytes → text safe to put on screen.

Target output is arbitrary bytes from a board under test
(pare-hardware-mcp contract.py:79-80), and tool arguments reaching the approval
modal are equally untrusted. Neither may carry terminal escapes into the
renderer.
"""
from __future__ import annotations

import re

# Everything in C0 except \n and \t, plus DEL and the C1 range. \r is included
# deliberately: a carriage-return run overwrites a line in place, which is a
# display-integrity problem even though \r is not an escape introducer.
_UNSAFE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")

_MAX_ARG = 200
_MAX_TOTAL = 1000


def for_display(data: bytes) -> str:
    """Decode with replacement (a board emits arbitrary bytes, not valid
    UTF-8) and strip anything that could move the cursor or repaint."""
    text = data.decode("utf-8", errors="replace")
    return _UNSAFE.sub("�", text)


def clip_args(arguments: dict) -> str:
    """Render tool arguments for the approval modal: sanitised, per-argument
    clipped, then total-clipped. Mirrors the CLI's _sanitize_args
    (agent_core/adapters/cli.py:31-46)."""
    rendered = []
    for key, value in (arguments or {}).items():
        s = _UNSAFE.sub(" ", str(value))
        if len(s) > _MAX_ARG:
            s = s[:_MAX_ARG] + f"...(+{len(s) - _MAX_ARG} chars)"
        rendered.append(f"{key}={s}")
    out = ", ".join(rendered)
    if len(out) > _MAX_TOTAL:
        out = out[:_MAX_TOTAL] + "...(truncated)"
    return out
