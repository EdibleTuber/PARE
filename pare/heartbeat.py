"""The daemon heartbeat — liveness the bench can see.

Spec: docs/superpowers/specs/2026-09-06-arcticbase-artifacts-design.md §8.2.

The Pi at the bench needs to distinguish a daemon that is working from one that
is wedged, and those look identical from a page that can only see its own
failure to fetch. A beat that stops is the signal.

Three constraints follow from what the beat is FOR:

* **It rides the sweep.** A beat on its own timer keeps ticking through a wedged
  daemon, which is the state it exists to reveal. `beat()` is therefore called by
  the task that has just completed a worker sweep, and not called at all when
  that sweep failed -- see `agent.py`. This module deliberately owns no timer.
* **It goes in its own workbench.** `put_object_content` appends an `audit.jsonl`
  row on every call, there is no rotation anywhere, and `read_audit` reads the
  whole file. Measured at 206 bytes a row, a 60s beat writes ~289 KiB/day. In a
  project's workbench that would bury the audit trail you actually want after a
  bricked target under a day's worth of "still alive".
* **It carries a boot id.** A restarted daemon that reused the same object would
  look continuous. The boot id changes when the process does, so "it has been up
  the whole time" is a checkable claim rather than an assumption.

`beat()` is synchronous because the client is. The caller runs it off the event
loop (`asyncio.to_thread`) -- a blocking urllib call in the daemon's loop would
stall every channel for the request timeout on each beat.
"""
from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

# Never a project's workbench. See the module docstring.
HEARTBEAT_WORKBENCH = "pare-daemon-status"
HEARTBEAT_TITLE = "heartbeat"

# §8.2: beat at 60s, stale at 3 minutes. The staleness bound is published in the
# payload rather than hard-coded into the reader, so the Pi's status page and the
# daemon cannot disagree about what "stale" means.
BEAT_INTERVAL_SECONDS = 60.0
STALE_AFTER_SECONDS = 180.0


class Heartbeat:
    def __init__(self, client: Any, *, boot_id: str | None = None,
                 workbench: str = HEARTBEAT_WORKBENCH) -> None:
        self._client = client
        self._workbench = workbench
        self.boot_id = boot_id or uuid.uuid4().hex[:12]
        self.last_error: str | None = None
        self.last_beat_at: str | None = None
        self._ensured = False

    def beat(self, *, active_slug: str | None,
             workers: dict[str, bool] | None = None) -> bool:
        """Write one beat. Returns whether it landed; never raises.

        Never raises because this runs inside the worker sweep loop: an
        exception here would take worker liveness down with it, so the
        heartbeat would have broken the daemon it exists to report on.
        """
        now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        payload = {
            "boot_id": self.boot_id,
            "active_slug": active_slug,
            "ts": now,
            "workers": workers or {},
            "beat_interval_seconds": BEAT_INTERVAL_SECONDS,
            "stale_after_seconds": STALE_AFTER_SECONDS,
        }
        try:
            if not self._ensured:
                self._client.ensure_workbench(self._workbench, "PARE daemon status")
                self._ensured = True
            self._client.upsert(self._workbench, kind="file",
                                title=HEARTBEAT_TITLE,
                                content=json.dumps(payload, indent=2, sort_keys=True))
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            # Re-ensure next time: the workbench may be gone (a restarted
            # ArcticBase with a fresh data dir), not merely unreachable.
            self._ensured = False
            return False
        self.last_error = None
        self.last_beat_at = now
        return True
