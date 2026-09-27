"""pare-tui's own preferences file: `$XDG_CONFIG_HOME/pare/tui.json`.

Holds only UI state the operator chose (today: the theme). Every failure
here -- missing, unreadable, corrupt, unwritable -- is a logged warning and
a fallback to defaults, never an exception: losing a preference must not
stop the TUI from launching or keep running.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Mapping

logger = logging.getLogger(__name__)


def default_tui_config_path(env: Mapping[str, str]) -> Path:
    """`$XDG_CONFIG_HOME/pare/tui.json`, else `~/.config/pare/tui.json`.

    Per the XDG base-directory spec, an empty or relative XDG_CONFIG_HOME
    is ignored."""
    xdg = env.get("XDG_CONFIG_HOME", "")
    base = Path(xdg) if xdg and Path(xdg).is_absolute() else Path.home() / ".config"
    return base / "pare" / "tui.json"


def _read(path: Path) -> dict | None:
    """The file's JSON object; None when it is absent (silently) or bad
    (with a warning)."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("could not read pare-tui preferences %s: %s", path, exc)
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("ignoring corrupt pare-tui preferences %s: %s", path, exc)
        return None
    if not isinstance(data, dict):
        logger.warning(
            "ignoring pare-tui preferences %s: expected a JSON object, got %s",
            path, type(data).__name__,
        )
        return None
    return data


def load_theme(path: Path) -> str | None:
    """The saved theme name, or None. Whether the name is a theme that
    exists is the app's call (it owns the registry)."""
    data = _read(path)
    if data is None or "theme" not in data:
        return None
    theme = data["theme"]
    if not isinstance(theme, str):
        logger.warning("ignoring pare-tui preferences %s: 'theme' is not a string", path)
        return None
    return theme


def save_theme(path: Path, theme: str) -> bool:
    """Write `theme` into the file, keeping any other keys. Atomic (temp
    file + rename), so a crash mid-write cannot leave a corrupt file.
    Returns False, with a warning, on any failure."""
    data = _read(path) or {}
    data["theme"] = theme
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tui.", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
                fh.write("\n")
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError as exc:
        logger.warning("could not save pare-tui preferences %s: %s", path, exc)
        return False
    return True
