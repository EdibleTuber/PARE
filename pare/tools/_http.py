"""HTTP client for apk_re_agents — internal to the static_analyze tool.

Wraps the legacy /jobs HTTP API with async submit + poll semantics:
    submit_job(apk_path)              → job_id
    get_status(job_id)                → status dict
    run_to_completion(apk_path, …)    → JobResult (submit + poll until done)

The wrapper does no schema enrichment beyond the API's own shape; the
tool layer (static_analyze.py) is responsible for translating into the
agent_core worker contract.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class JobResult:
    """Result of a completed apk_re_agents job."""
    job_id: str
    state: str
    results: dict[str, str]  # agent_name -> findings file path


class ApkReAgentsClient:
    """Thin async client for apk_re_agents /jobs HTTP API."""

    def __init__(self, base_url: str, *, timeout_s: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=timeout_s)

    async def close(self) -> None:
        await self._client.aclose()

    async def submit_job(self, *, apk_path: str) -> str:
        """POST /jobs. Returns the job_id."""
        resp = await self._client.post(
            f"{self.base_url}/jobs",
            json={"apk_path": apk_path},
        )
        resp.raise_for_status()
        return resp.json()["job_id"]

    async def get_status(self, job_id: str) -> dict[str, Any]:
        """GET /jobs/{job_id}. Returns the raw status dict."""
        resp = await self._client.get(f"{self.base_url}/jobs/{job_id}")
        resp.raise_for_status()
        return resp.json()

    async def run_to_completion(
        self,
        *,
        apk_path: str,
        poll_interval_s: float = 5.0,
        timeout_s: float = 1800.0,
    ) -> JobResult:
        """Submit a job then poll until state ∈ {completed, failed} or timeout.

        Raises:
            RuntimeError: if the job reports state="failed" or polling times out.
        """
        job_id = await self.submit_job(apk_path=apk_path)
        deadline = asyncio.get_event_loop().time() + timeout_s
        while True:
            status = await self.get_status(job_id)
            state = status.get("state", "")
            if state == "completed":
                return JobResult(
                    job_id=job_id,
                    state=state,
                    results=status.get("results") or {},
                )
            if state == "failed":
                raise RuntimeError(
                    f"apk_re_agents job {job_id} failed: {status}"
                )
            if asyncio.get_event_loop().time() > deadline:
                raise RuntimeError(
                    f"apk_re_agents job {job_id} timed out after {timeout_s}s "
                    f"in state {state!r}"
                )
            await asyncio.sleep(poll_interval_s)
