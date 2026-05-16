"""Phase 1 end-to-end smoke: PARE → apk_re_agents → findings ref.

Requires apk_re_agents to be running (docker compose up) and a fixture
APK reachable by the coordinator's shared volume.

Enable with:
    PARE_PHASE1_SMOKE_APK_PATH=/work/input/sample.apk \\
    PARE_PHASE1_SMOKE_AGENTS_URL=http://127.0.0.1:8000 \\
    pytest tests/test_phase1_smoke.py -v
"""
import os

import pytest

from pare.tools._http import ApkReAgentsClient


APK_PATH = os.getenv("PARE_PHASE1_SMOKE_APK_PATH")
AGENTS_URL = os.getenv("PARE_PHASE1_SMOKE_AGENTS_URL")


pytestmark = pytest.mark.skipif(
    not (APK_PATH and AGENTS_URL),
    reason="set PARE_PHASE1_SMOKE_APK_PATH and PARE_PHASE1_SMOKE_AGENTS_URL to run",
)


@pytest.mark.asyncio
async def test_static_analyze_against_real_apk_re_agents():
    """Submit a real APK; wait up to 30 min for completion; verify findings
    paths come back."""
    client = ApkReAgentsClient(AGENTS_URL)
    try:
        result = await client.run_to_completion(
            apk_path=APK_PATH,
            poll_interval_s=5.0,
            timeout_s=1800.0,
        )
        assert result.state == "completed"
        assert result.results, "expected at least one analyser to produce findings"
        # Spot-check that manifest analyser ran (it's deterministic, no LLM
        # variance in whether it reports findings).
        assert "manifest_analyzer" in result.results
    finally:
        await client.close()
