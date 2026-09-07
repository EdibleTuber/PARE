"""The project slug: derived once, then stored.

Spec: docs/superpowers/specs/2026-09-06-arcticbase-artifacts-design.md §5.2.
One identity spans the capture store, the workbench and the bench drive, so the
rules here are ArcticBase's -- it is the strictest consumer.
"""
from __future__ import annotations

import re

import pytest

from pare.project_slug import (
    ARCTIC_BASE_SLUG_RE,
    NoProjectError,
    InvalidStoredSlugError,
    derive_slug,
    read_or_create_slug,
    resolve_project_slug,
)


def _make_project(tmp_path, name):
    root = tmp_path / name
    (root / ".pare").mkdir(parents=True)
    return root


# --- the alphabet is ArcticBase's -------------------------------------------

def test_the_slug_pattern_is_anchored_and_requires_a_leading_alphanumeric():
    # An unanchored `match` would accept "proj/../../etc"; §5.2 says fullmatch.
    assert ARCTIC_BASE_SLUG_RE.fullmatch("ok-slug")
    assert not ARCTIC_BASE_SLUG_RE.fullmatch("-leading")
    assert not ARCTIC_BASE_SLUG_RE.fullmatch("has space")
    assert not ARCTIC_BASE_SLUG_RE.fullmatch("UPPER")
    assert not ARCTIC_BASE_SLUG_RE.fullmatch("a/../b")
    assert ARCTIC_BASE_SLUG_RE.fullmatch("x" * 64)
    assert not ARCTIC_BASE_SLUG_RE.fullmatch("x" * 65)


@pytest.mark.parametrize("name", [
    "target", "My Project", "___", "...", "-dash-lead", "UPPER_CASE",
    "a" * 200, "ünïcodé", "proj.v2", "9lives",
])
def test_every_derived_slug_is_one_arcticbase_would_accept(tmp_path, name):
    root = tmp_path / name
    root.mkdir()
    slug = derive_slug(root)
    assert ARCTIC_BASE_SLUG_RE.fullmatch(slug), f"{name!r} produced {slug!r}"


# --- collisions --------------------------------------------------------------

def test_same_basename_at_different_paths_gets_different_slugs(tmp_path):
    a = tmp_path / "work" / "a" / "target"
    b = tmp_path / "work" / "b" / "target"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    assert derive_slug(a) != derive_slug(b)


def test_the_same_path_always_derives_the_same_slug(tmp_path):
    root = tmp_path / "target"
    root.mkdir()
    assert derive_slug(root) == derive_slug(root)


# --- derived once, then stored ----------------------------------------------

def test_first_call_writes_the_slug_into_dot_pare_project(tmp_path):
    root = _make_project(tmp_path, "target")
    slug = read_or_create_slug(root / ".pare")
    assert (root / ".pare" / "project").read_text().strip() == slug


def test_a_stored_slug_is_read_back_rather_than_rederived(tmp_path):
    root = _make_project(tmp_path, "target")
    (root / ".pare" / "project").write_text("hand-chosen-name\n")
    assert read_or_create_slug(root / ".pare") == "hand-chosen-name"


def test_a_renamed_project_keeps_its_slug(tmp_path):
    """The whole reason the slug is stored: `mv` must not orphan the workbench."""
    root = _make_project(tmp_path, "before")
    original = read_or_create_slug(root / ".pare")
    moved = tmp_path / "after"
    root.rename(moved)
    assert read_or_create_slug(moved / ".pare") == original


def test_a_hand_edited_invalid_slug_is_refused_not_silently_repaired(tmp_path):
    # Repairing it would point at a different workbench than the one the
    # operator has been reading, which is worse than stopping.
    root = _make_project(tmp_path, "target")
    (root / ".pare" / "project").write_text("Not A Valid Slug\n")
    with pytest.raises(InvalidStoredSlugError) as e:
        read_or_create_slug(root / ".pare")
    assert "Not A Valid Slug" in str(e.value)


# --- no project, no publish --------------------------------------------------

def test_no_dot_pare_fails_loudly_and_names_the_cwd(tmp_path):
    """§5.2: never invent a workbench from the per-launch XDG fallback."""
    cwd = tmp_path / "not" / "a" / "project"
    cwd.mkdir(parents=True)
    with pytest.raises(NoProjectError) as e:
        resolve_project_slug(cwd, marker=".pare", home=tmp_path)
    assert str(cwd) in str(e.value)


def test_resolve_walks_up_to_the_project_root(tmp_path):
    root = _make_project(tmp_path, "target")
    deep = root / "src" / "nested"
    deep.mkdir(parents=True)
    assert resolve_project_slug(deep, marker=".pare", home=tmp_path) == \
        read_or_create_slug(root / ".pare")


def test_a_marker_at_home_is_not_a_project(tmp_path):
    """Mirrors resolve_capture_db's $HOME ceiling, so both resolve alike."""
    home = tmp_path / "home"
    (home / ".pare").mkdir(parents=True)
    with pytest.raises(NoProjectError):
        resolve_project_slug(home, marker=".pare", home=home)
