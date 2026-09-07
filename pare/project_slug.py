"""The project slug — derived once, then stored.

Spec: docs/superpowers/specs/2026-09-06-arcticbase-artifacts-design.md §5.2.

One project identity has to span three places: the capture store on the daemon
host, the workbench on the inference server, and the artifact directory on the
bench drive. PARE resolves a project by a `.pare/` walk-up, but that yields a
*path*, not a name, so the name is derived here.

Two properties do the real work:

* **Derived once, then read.** The slug is written into `.pare/project` on first
  use and never re-derived from the directory name. Re-deriving would mean a
  `mv` silently orphans both the workbench and the artifacts.
* **No project, no slug.** `resolve_capture_db` falls back to a per-CLI-launch
  path outside a project. A slug derived from that would create a new workbench
  on every launch, so the absence of a `.pare/` is an error naming the cwd, not
  a default.
"""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

# ArcticBase's own rule (backend/.../storage/filesystem.py:54), which is the
# strictest of the three consumers, so it is the one all three use.
#
# Anchored with \A and \Z rather than relying on the caller reaching for
# fullmatch: §5.2 records that an unanchored `re.match` accepts "proj/../../etc",
# and an anchored pattern cannot be misused that way. \Z rather than $, because
# $ also matches before a trailing newline -- "evil\n" would pass.
#
# No re.IGNORECASE, deliberately: it would make [a-z] match U+212A KELVIN SIGN
# and U+0130, so the pattern would stop agreeing with the byte-level rule
# ArcticBase actually applies.
ARCTIC_BASE_SLUG_RE = re.compile(r"\A[a-z0-9][a-z0-9_-]{0,63}\Z")

_SLUG_MAX = 64
_HASH_LEN = 8
_SLUG_FILE = "project"


class ProjectSlugError(Exception):
    """Base class, so a caller can catch every slug failure in one clause."""


class NoProjectError(ProjectSlugError):
    """The cwd is not inside a project. Never resolved to a default."""


class InvalidStoredSlugError(ProjectSlugError):
    """`.pare/project` holds something ArcticBase would reject."""


def _sanitize(name: str) -> str:
    """Reduce a directory name to the slug alphabet, or to empty."""
    out = re.sub(r"[^a-z0-9_-]+", "-", name.lower())
    out = re.sub(r"-{2,}", "-", out)
    out = out.strip("-_")
    return re.sub(r"\A[^a-z0-9]+", "", out)


def derive_slug(project_root: Path | str) -> str:
    """Derive a slug from a project root. Pure: no filesystem writes.

    The hash suffix is not decoration. `/work/a/target` and `/work/b/target` are
    two different projects sharing one basename, and without it they would share
    one workbench and one artifact directory.
    """
    resolved = Path(project_root).resolve()
    # os.fsencode, not .encode(): a path is bytes, and need not be valid UTF-8.
    digest = hashlib.sha256(os.fsencode(str(resolved))).hexdigest()[:_HASH_LEN]
    base = _sanitize(resolved.name)[: _SLUG_MAX - _HASH_LEN - 1].rstrip("-_")
    slug = f"{base}-{digest}" if base else digest
    if not ARCTIC_BASE_SLUG_RE.fullmatch(slug):
        # Unreachable by construction; loud rather than shipping a slug that
        # ArcticBase will reject at publish time, far from the cause.
        raise ProjectSlugError(
            f"derived slug {slug!r} for {resolved} is not one ArcticBase accepts")
    return slug


def read_or_create_slug(pare_dir: Path | str) -> str:
    """Return the project's slug, deriving and storing it on first use."""
    pare_dir = Path(pare_dir)
    slug_path = pare_dir / _SLUG_FILE

    stored = _read_stored(slug_path)
    if stored is not None:
        return stored

    slug = derive_slug(pare_dir.parent)
    try:
        # O_EXCL so a concurrent daemon cannot half-write this underneath us.
        # Two daemons on one project derive the SAME slug, so losing the race is
        # harmless -- but a hand-chosen slug written between our read and our
        # write is not, and that is what the re-read below catches.
        fd = os.open(slug_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        existing = _read_stored(slug_path)
        if existing is None:
            raise InvalidStoredSlugError(
                f"{slug_path} appeared while deriving a slug and is unreadable")
        return existing
    with os.fdopen(fd, "w") as fh:
        fh.write(slug + "\n")
    return slug


def _read_stored(slug_path: Path) -> str | None:
    """The stored slug, or None if the file is absent. Raises if it is invalid."""
    try:
        raw = slug_path.read_text()
    except FileNotFoundError:
        return None
    stored = raw.strip()
    if not ARCTIC_BASE_SLUG_RE.fullmatch(stored):
        # Deliberately not repaired. A repaired slug names a DIFFERENT workbench
        # from the one the operator has been reading, so silently continuing
        # would publish findings somewhere nobody is looking.
        raise InvalidStoredSlugError(
            f"{slug_path} holds {stored!r}, which ArcticBase would reject. "
            f"Fix the file or delete it to re-derive.")
    return stored


def resolve_project_slug(cwd: Path | str, *, marker: str = ".pare",
                         home: Path | str) -> str:
    """Walk up from `cwd` for `marker` and return that project's slug.

    The walk mirrors agent_core's `resolve_capture_db` -- same $HOME ceiling,
    same filesystem-root stop -- so the slug and the capture store always resolve
    to the same project. If they disagreed, findings would be published against
    one project while captures were written to another.
    """
    start = Path(cwd).resolve()
    home_resolved = Path(home).resolve()
    for d in [start, *start.parents]:
        if d == home_resolved or d == d.parent:
            break
        if (d / marker).is_dir():
            return read_or_create_slug(d / marker)
    raise NoProjectError(
        f"no {marker}/ project found walking up from {start} (stopping at "
        f"{home_resolved}). A gated call needs a project: run from inside one, "
        f"or create {start / marker}/.")
