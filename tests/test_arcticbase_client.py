"""The ArcticBase client — unit half. These always run, with no server.

Spec: §5.1 (what may enter the workbench) and D5 (what kind it may be published
as). Both are security properties, so they are enforced in the client rather
than left to callers, and tested here rather than only against a live service.
"""
from __future__ import annotations

import pytest

from pare.arcticbase import (
    DEFAULT_MAX_BYTES,
    PUBLISHABLE_KINDS,
    ArcticBaseClient,
    ContentTooLarge,
    NotConfigured,
    UnpublishableKind,
)

UNREACHABLE = "http://127.0.0.1:59999"


# --- D5: the client cannot publish a kind ArcticBase will execute ------------

def test_the_executable_kinds_are_not_publishable_at_all():
    # md and html both render in an unsandboxed same-origin iframe; only the
    # renderer differs. So "don't publish html" has to be a property of the
    # client, not a convention callers remember.
    assert "html" not in PUBLISHABLE_KINDS
    assert "approval-html" not in PUBLISHABLE_KINDS
    assert "qa-form" not in PUBLISHABLE_KINDS
    assert PUBLISHABLE_KINDS == {"md", "file"}


@pytest.mark.parametrize("kind", ["html", "approval-html", "qa-form", "runbook", "image"])
def test_publishing_an_executable_or_unexpected_kind_is_refused(kind):
    client = ArcticBaseClient(UNREACHABLE)
    with pytest.raises(UnpublishableKind) as e:
        client.publish("proj", kind=kind, title="t", content="x")
    assert kind in str(e.value)


def test_the_refusal_happens_before_any_network_call():
    """An unreachable URL must still raise UnpublishableKind, not a transport error."""
    client = ArcticBaseClient(UNREACHABLE, timeout=0.2)
    with pytest.raises(UnpublishableKind):
        client.publish("proj", kind="html", title="t", content="x")


# --- §5.1: the size cap the server does not apply on the POST path -----------

def test_oversized_content_is_refused_before_any_network_call():
    """The client check must precede the request.

    Otherwise a too-large publish leaves a metadata-only object behind on the
    server after the POST succeeds and the PUT is rejected.
    """
    client = ArcticBaseClient(UNREACHABLE, max_bytes=1024, timeout=0.2)
    with pytest.raises(ContentTooLarge):
        client.publish("proj", kind="md", title="t", content="x" * 2000)


def test_the_cap_counts_bytes_not_characters():
    """The server measures len(bytes). A multibyte string is bigger than it looks."""
    client = ArcticBaseClient(UNREACHABLE, max_bytes=10, timeout=0.2)
    text = "日本語です!!"
    assert len(text) < 10 < len(text.encode("utf-8")), "the premise of this test"
    with pytest.raises(ContentTooLarge) as e:
        client.publish("proj", kind="md", title="t", content=text)
    # Derived, not a literal: the point is that it reports the ENCODED length.
    assert str(len(text.encode("utf-8"))) in str(e.value)


def test_the_default_cap_matches_the_servers_configured_limit():
    assert DEFAULT_MAX_BYTES == 8 * 1024 * 1024


# --- not configured ----------------------------------------------------------

def test_an_unset_base_url_fails_loudly_rather_than_guessing_localhost():
    client = ArcticBaseClient("")
    with pytest.raises(NotConfigured) as e:
        client.publish("proj", kind="md", title="t", content="x")
    assert "PARE_ARCTICBASE_URL" in str(e.value)


def test_health_also_reports_not_configured():
    with pytest.raises(NotConfigured):
        ArcticBaseClient("").health()


# --- upsert: one object updated in place, not a new one per write -----------

def test_upsert_requires_a_publishable_kind_too():
    client = ArcticBaseClient(UNREACHABLE, timeout=0.2)
    with pytest.raises(UnpublishableKind):
        client.upsert("proj", kind="html", title="t", content="x")


def test_upsert_enforces_the_same_size_cap():
    client = ArcticBaseClient(UNREACHABLE, max_bytes=16, timeout=0.2)
    with pytest.raises(ContentTooLarge):
        client.upsert("proj", kind="file", title="t", content="x" * 100)
