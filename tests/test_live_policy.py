"""Live-relay SSRF policy lives in devices._policy, not in the relay catalog."""

from __future__ import annotations

from gaia.devices import live as livemod
from gaia.devices import _policy as policy
from gaia.devices.base import DeviceOffline


def test_allowlist_and_url_assert_are_the_policy_module():
    assert livemod._ALLOWED_HOSTS is policy.ALLOWED_HOSTS
    assert livemod._assert_url_allowed is policy.assert_url_allowed
    assert "api.open-meteo.com" in policy.ALLOWED_HOSTS
    assert policy._om_origin("weather").startswith("https://")


def test_policy_blocks_ssrf_the_same_way_live_did():
    try:
        policy.assert_url_allowed("https://evil.example/steal")
    except DeviceOffline:
        pass
    else:
        raise AssertionError("non-allowlisted host must be refused")
    assert policy.assert_url_allowed("https://api.open-meteo.com/v1/forecast").startswith("https://")
