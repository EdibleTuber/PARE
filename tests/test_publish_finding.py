"""publish_finding — the model's editorial act (D4), and its guard rails.

Everything here runs without a server. The live round-trip lives in
tests/test_publish_finding_live.py.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from pare.tools.publish_finding import PublishFinding

UNREACHABLE = "http://127.0.0.1:59999"


def _ctx(cwd, url=UNREACHABLE):
    cfg = SimpleNamespace(arcticbase_url=url, project_marker=".pare")
    return SimpleNamespace(cwd=str(cwd), agent=SimpleNamespace(config=cfg))


def _project(tmp_path):
    (tmp_path / ".pare").mkdir()
    return tmp_path


async def _run(tool, args, ctx):
    return json.loads(await tool.run(args, ctx))


# --- D5, structurally --------------------------------------------------------

def test_the_tool_exposes_no_way_to_choose_the_object_kind():
    """A `kind` parameter would put D5 back in the model's hands."""
    props = PublishFinding.parameters["properties"]
    assert "kind" not in props
    assert set(props) == {"title", "markdown"}


# --- no project, no publish --------------------------------------------------

@pytest.mark.asyncio
async def test_publishing_outside_a_project_fails_and_names_the_cwd(tmp_path):
    cwd = tmp_path / "nowhere"
    cwd.mkdir()
    out = await _run(PublishFinding(), {"title": "t", "markdown": "x"}, _ctx(cwd))
    assert out["status"] == "error"
    assert str(cwd) in out["reason"]


# --- not configured ----------------------------------------------------------

@pytest.mark.asyncio
async def test_an_unconfigured_workbench_host_is_named_not_guessed(tmp_path):
    root = _project(tmp_path)
    out = await _run(PublishFinding(), {"title": "t", "markdown": "x"},
                     _ctx(root, url=""))
    assert out["status"] == "error"
    assert "PARE_ARCTICBASE_URL" in out["reason"]


# --- argument handling -------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("args", [
    {"markdown": "x"}, {"title": "t"}, {"title": "", "markdown": "x"},
    {"title": "t", "markdown": "   "},
])
async def test_missing_or_blank_arguments_are_refused(tmp_path, args):
    out = await _run(PublishFinding(), args, _ctx(_project(tmp_path)))
    assert out["status"] == "error"


# --- the service is down -----------------------------------------------------

@pytest.mark.asyncio
async def test_an_unreachable_workbench_host_reports_rather_than_raises(tmp_path):
    root = _project(tmp_path)
    out = await _run(PublishFinding(), {"title": "t", "markdown": "x"}, _ctx(root))
    assert out["status"] == "error"
    # The operator needs to know WHICH of the five candidates failed (§10).
    assert "59999" in out["reason"]
