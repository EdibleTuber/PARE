"""A CI box that happens to have SOME worker contract installed, but none of
the ones any risk_overrides pin actually targets, must not read as success.

test_risk_overrides_coverage.test_every_pin_matches_at_least_one_real_tool()
already fails loudly if a pin's owner IS installed and the pin matches
nothing. But before this fix, a pin whose owner is simply not installed is
only ever "unchecked" -- reported via a UserWarning that pytest does not fail
on by default (pyproject.toml sets no filterwarnings). If every pin's owner
happens to be absent while some OTHER, pin-less worker contract is present,
`installed` is still non-empty, the existing `assert installed` says nothing,
every pin goes into `unchecked`, and the whole function returns normally: a
green, warning-only run that validated zero pins. That is exactly the
"silently no protection" failure mode this coverage file exists to catch.
"""
import warnings

import pytest

import tests.test_risk_overrides_coverage as trc


def test_zero_validated_pins_fails_even_though_some_contract_is_installed(monkeypatch):
    # Simulate a checkout where only `static` (which owns none of the real
    # risk_overrides pins -- they're all frida_*/mitm_*) is installed.
    monkeypatch.setattr(trc, "_tool_targets", lambda: (set(), {"static"}))

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with pytest.raises(AssertionError, match="validated"):
            trc.test_every_pin_matches_at_least_one_real_tool()
