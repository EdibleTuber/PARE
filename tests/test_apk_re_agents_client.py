"""Tests for the apk_re_agents HTTP client."""
import pytest

from pare.tools._http import ApkReAgentsClient, JobResult


@pytest.mark.asyncio
async def test_submit_returns_job_id(httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url="http://test.invalid/jobs",
        status_code=202,
        json={"job_id": "abc-123", "state": "pending"},
    )
    client = ApkReAgentsClient("http://test.invalid")
    job_id = await client.submit_job(apk_path="/work/input/sample.apk")
    assert job_id == "abc-123"
    await client.close()


@pytest.mark.asyncio
async def test_get_status_returns_state(httpx_mock):
    httpx_mock.add_response(
        method="GET",
        url="http://test.invalid/jobs/abc-123",
        json={"job_id": "abc-123", "state": "running", "current_stage": "parallel_analysis"},
    )
    client = ApkReAgentsClient("http://test.invalid")
    status = await client.get_status("abc-123")
    assert status["state"] == "running"
    assert status["current_stage"] == "parallel_analysis"
    await client.close()


@pytest.mark.asyncio
async def test_wait_for_completion_polls_until_done(httpx_mock):
    """submit + poll until state=completed."""
    httpx_mock.add_response(
        method="POST",
        url="http://test.invalid/jobs",
        status_code=202,
        json={"job_id": "j1", "state": "pending"},
    )
    # Two "running" then one "completed".
    for state in ("running", "running", "completed"):
        response = {"job_id": "j1", "state": state}
        if state == "completed":
            response["results"] = {"manifest_analyzer": "/work/findings/j1/manifest_analyzer.json"}
        httpx_mock.add_response(
            method="GET",
            url="http://test.invalid/jobs/j1",
            json=response,
        )

    client = ApkReAgentsClient("http://test.invalid")
    result = await client.run_to_completion(
        apk_path="/work/sample.apk", poll_interval_s=0.01, timeout_s=5.0
    )
    assert isinstance(result, JobResult)
    assert result.job_id == "j1"
    assert result.state == "completed"
    assert "manifest_analyzer" in result.results
    await client.close()


@pytest.mark.asyncio
async def test_wait_for_completion_raises_on_failed(httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url="http://test.invalid/jobs",
        status_code=202,
        json={"job_id": "j2", "state": "pending"},
    )
    httpx_mock.add_response(
        method="GET",
        url="http://test.invalid/jobs/j2",
        json={"job_id": "j2", "state": "failed"},
    )
    client = ApkReAgentsClient("http://test.invalid")
    with pytest.raises(RuntimeError, match="failed"):
        await client.run_to_completion(
            apk_path="/work/sample.apk", poll_interval_s=0.01, timeout_s=5.0
        )
    await client.close()
