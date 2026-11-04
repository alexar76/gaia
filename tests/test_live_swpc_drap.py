"""Check D-RAP geometry, modeled maxima, and fail-closed freshness."""

from datetime import UTC, datetime, timedelta

import pytest

from gaia.attestation import verify_reading
from gaia.clock import SimClock
from gaia.devices import live_swpc_drap
from gaia.devices.live_swpc_drap import SwpcDRap, parse_drap
from gaia.fleet import Fleet

NOW = datetime(2026, 11, 4, 8, 10, tzinfo=UTC)


def _bulletin() -> str:
    lines = [
        "# DRAP Tabular Values",
        "# Product Valid At : 2026-11-04 08:01 UTC",
        " ".join(map(str, range(-178, 180, 4))),
        "------",
    ]
    for i, lat in enumerate(range(89, -90, -2)):
        cells = ["0.0"] * 90
        if i == 2:
            cells[2] = "7.2"
        lines.append(f"{lat} | " + " ".join(cells))
    return "\n".join(lines)


def test_full_grid_keeps_native_values_and_quiet_cells():
    points = parse_drap(_bulletin(), NOW)
    assert len(points) == 8100
    assert len({(p["latitude"], p["longitude"]) for p in points}) == 8100
    first = points[0]
    assert (first["latitude"], first["longitude"], first["drap_frequency_mhz"]) == (85, -170, 7.2)
    assert first["forecast_valid_unix"] == (NOW - timedelta(minutes=9)).timestamp()
    assert points[-1]["drap_frequency_mhz"] == 0


def test_rejects_stale_partial_and_nonfinite_grid():
    raw = _bulletin()
    with pytest.raises(ValueError, match="not current"):
        parse_drap(raw, NOW + timedelta(minutes=6))
    with pytest.raises(ValueError, match="grid"):
        parse_drap(raw.rsplit("\n", 1)[0], NOW)
    with pytest.raises(ValueError, match="grid row"):
        parse_drap(raw.replace("7.2", "nan"), NOW)


def test_full_signed_grid_is_returned_without_retaining_history_copies(monkeypatch, tmp_path):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(live_swpc_drap, "datetime", FixedDatetime)
    relay = SwpcDRap("swpc-drap-01", SimClock(NOW.timestamp()), key_dir=tmp_path)
    monkeypatch.setattr(relay, "_fetch_text", lambda *args, **kwargs: _bulletin())
    fleet = Fleet()
    fleet.add(relay)

    result = fleet.read(relay.device_id)
    reading = result["reading"]
    assert len(reading["hotspots"]) == 8100
    assert verify_reading(reading, result["attestation"], relay.signer.public_key_b64)
    [history] = fleet.history(relay.device_id)
    assert "hotspots" not in history
    assert history["seq"] == reading["seq"] and history["values"] == reading["values"]
