"""Tests for B2: capture layer wired into PareAgent.

Verifies:
- SearchCapture and ReadCapture are in PareAgent.tools ClassVar
- PAREConfig.project_marker is ".pare"
- capture_store property reads _current_store ContextVar (isolates per-turn)
"""
import pytest
from pare.agent import PareAgent, _current_store
from agent_core.capture import CaptureStore, SearchCapture, ReadCapture


def test_retrieval_tools_registered_and_marker_set():
    assert SearchCapture in PareAgent.tools
    assert ReadCapture in PareAgent.tools
    from pare.config import PAREConfig
    assert PAREConfig().project_marker == ".pare"


def test_capture_store_property_reads_contextvar():
    agent = PareAgent.__new__(PareAgent)   # bypass full setup; only the property under test
    store = CaptureStore.open_memory()
    token = _current_store.set(store)
    try:
        assert agent.capture_store is store
    finally:
        _current_store.reset(token)
    assert agent.capture_store is None   # unset -> None (layer treats None as "don't capture")
