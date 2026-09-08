"""A small ArcticBase client — the workbench half of the artifact layer.

Spec: docs/superpowers/specs/2026-09-06-arcticbase-artifacts-design.md §5.1, D5.

ArcticBase is consumed through its HTTP API and never modified, so two of this
design's safety properties have to live on this side of the wire. Both are
enforced here rather than documented for callers to remember:

**Nothing executable is publishable.** D5 decides that model-authored reports are
`md`, never `html`. The reason is thinner than it first appears: `MdViewer` and
`HtmlViewer` both render into an iframe with no `sandbox` attribute from a
same-origin URL, so the two kinds share a container and differ only in what
reaches it. `md` is safe solely because ArcticBase renders it with markdown-it
configured `html: False`. There is therefore no code path from here that
publishes `html`, `approval-html` or `qa-form` -- see PUBLISHABLE_KINDS.

**Content goes up the path that is actually capped.** §5.1 assumed setting
ARCTIC_BASE_MAX_UPLOAD_BYTES was enough. It is not: the cap is applied to the
multipart part (objects.py:151) and to PUT .../content (objects.py:313), but the
JSON `POST /objects` branch stores inline `content` with no size check at all
(objects.py:170-184) -- measured, 9 MiB accepted with HTTP 201. So publishing is
two calls: POST the metadata with no content, then PUT the bytes to the endpoint
that enforces. The client checks the size too, but that is for a clear error;
the PUT is the control.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

# `image`, `file` and `runbook` are not executable, but only the two kinds PARE
# actually produces are allowed through -- an allowlist that happens to exclude
# the dangerous kinds is weaker than one that names what we publish.
PUBLISHABLE_KINDS = frozenset({"md", "file"})

# Matches the ARCTIC_BASE_MAX_UPLOAD_BYTES the deployment is expected to set.
# §5.1: a workbench holds reports and descriptors, never artifact bytes.
DEFAULT_MAX_BYTES = 8 * 1024 * 1024

_URL_ENV = "PARE_ARCTICBASE_URL"


class ArcticBaseError(Exception):
    """Base class, so a caller can catch every publish failure in one clause."""


class NotConfigured(ArcticBaseError):
    """No base URL. Never defaulted to localhost -- see the docstring below."""


class Unreachable(ArcticBaseError):
    """The service did not answer."""


class PublishFailed(ArcticBaseError):
    """The service answered, and refused."""


class ContentTooLarge(ArcticBaseError):
    """Refused locally, before anything was created server-side."""


class UnpublishableKind(ArcticBaseError):
    """A kind this client will not publish. See PUBLISHABLE_KINDS."""


class ArcticBaseClient:
    def __init__(self, base_url: str, *, max_bytes: int = DEFAULT_MAX_BYTES,
                 timeout: float = 5.0) -> None:
        self._base = (base_url or "").rstrip("/")
        self._max_bytes = max_bytes
        self._timeout = timeout

    # -- plumbing ------------------------------------------------------------

    def _api(self, path: str) -> str:
        if not self._base:
            # Deliberately not defaulted to http://127.0.0.1:2929. A default
            # that is wrong for every deployment but one publishes findings
            # into a void that looks like success.
            raise NotConfigured(
                f"no ArcticBase URL configured; set {_URL_ENV} "
                f"(the daemon publishes nothing until it is set)")
        return f"{self._base}/api{path}"

    def _request(self, method: str, path: str, *, body: bytes | None = None,
                 content_type: str | None = None) -> tuple[int, bytes]:
        req = urllib.request.Request(self._api(path), data=body, method=method)
        if content_type:
            req.add_header("Content-Type", content_type)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            # An HTTP error is an answer, not a failure to reach.
            return e.code, e.read()
        except (urllib.error.URLError, OSError) as e:
            raise Unreachable(f"{self._base} did not answer: {e}") from e

    # -- api -----------------------------------------------------------------

    def health(self) -> dict[str, Any]:
        status, raw = self._request("GET", "/health")
        if status != 200:
            raise PublishFailed(f"health returned {status}")
        return json.loads(raw)

    def ensure_workbench(self, slug: str, title: str) -> bool:
        """Create the workbench if absent. Returns True if this call created it.

        A 409 counts as success: two daemons racing on the same project both
        want the same workbench to exist, and it does.
        """
        status, _ = self._request("GET", f"/workbenches/{slug}")
        if status == 200:
            return False
        body = json.dumps({"slug": slug, "title": title}).encode()
        status, raw = self._request(
            "POST", "/workbenches", body=body, content_type="application/json")
        if status == 201:
            return True
        if status == 409:
            return False
        raise PublishFailed(
            f"could not create workbench {slug!r}: {status} {raw[:200]!r}")

    def _check_publishable(self, kind: str, content: str) -> bytes:
        """Both write paths go through here, so neither can drift from the other."""
        if kind not in PUBLISHABLE_KINDS:
            raise UnpublishableKind(
                f"refusing to publish kind {kind!r}; this client publishes only "
                f"{sorted(PUBLISHABLE_KINDS)}. ArcticBase renders html and "
                f"approval-html into an iframe with no sandbox attribute from a "
                f"same-origin URL, so model-authored content is never published "
                f"as one.")
        payload = content.encode("utf-8")
        if len(payload) > self._max_bytes:
            # Checked BEFORE any write. Doing it after would leave a
            # metadata-only object behind when the PUT is rejected.
            raise ContentTooLarge(
                f"content is {len(payload)} bytes, over the {self._max_bytes} "
                f"byte limit for a workbench object; artifacts stay on the "
                f"machine that produced them and only a descriptor travels")
        return payload

    def publish(self, slug: str, *, kind: str, title: str, content: str,
                description: str = "") -> str:
        """Publish one object and return its id. Two calls; see the module docstring."""
        payload = self._check_publishable(kind, content)

        meta = json.dumps(
            {"kind": kind, "title": title, "description": description}).encode()
        status, raw = self._request(
            "POST", f"/workbenches/{slug}/objects", body=meta,
            content_type="application/json")
        if status != 201:
            raise PublishFailed(
                f"could not create object in {slug!r}: {status} {raw[:200]!r}")
        oid = json.loads(raw)["id"]

        status, raw = self._request(
            "PUT", f"/workbenches/{slug}/objects/{oid}/content",
            body=payload, content_type="text/markdown")
        if status != 200:
            raise PublishFailed(
                f"created object {oid} in {slug!r} but its content was refused: "
                f"{status} {raw[:200]!r}")
        return oid

    def upsert(self, slug: str, *, kind: str, title: str, content: str) -> str:
        """Create the object named `title`, or replace the content of the one
        that already exists. Returns its id.

        `publish` mints a new object per call, which is right for a finding and
        wrong for anything written repeatedly: the heartbeat beats 1440 times a
        day, and a workbench accumulating 1440 objects a day is not a status
        page, it is a landfill.

        Matching is by title within the workbench. That is ArcticBase's only
        stable handle short of storing the object id ourselves, and storing it
        would not survive a daemon restart -- which is exactly when the beat
        matters most.
        """
        self._check_publishable(kind, content)
        status, raw = self._request("GET", f"/workbenches/{slug}/objects")
        if status != 200:
            raise PublishFailed(
                f"could not list objects in {slug!r}: {status} {raw[:200]!r}")
        existing = next(
            (o["id"] for o in json.loads(raw) if o.get("title") == title), None)
        if existing is None:
            return self.publish(slug, kind=kind, title=title, content=content)
        status, raw = self._request(
            "PUT", f"/workbenches/{slug}/objects/{existing}/content",
            body=content.encode("utf-8"), content_type="text/markdown")
        if status != 200:
            raise PublishFailed(
                f"could not replace content of {existing} in {slug!r}: "
                f"{status} {raw[:200]!r}")
        return existing

    def publish_report(self, slug: str, title: str, markdown: str) -> str:
        """A model-authored finding. Always `md` -- D5, enforced by having no
        parameter that could make it anything else."""
        return self.publish(slug, kind="md", title=title, content=markdown)

    def publish_descriptor(self, slug: str, title: str,
                           descriptor: dict[str, Any]) -> str:
        """An artifact descriptor: the reference that travels, never the bytes."""
        return self.publish(slug, kind="file", title=title,
                            content=json.dumps(descriptor, indent=2, sort_keys=True))
