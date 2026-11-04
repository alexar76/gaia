"""Contract tests for four open station feeds and signed GeoNet camera evidence."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from gaia.attestation import verify_reading
from gaia.clock import SimClock
from gaia.devices import live_wave_stations as live
from gaia.devices.base import DeviceOffline

_NOW = datetime(2026, 11, 4, 12, 43, tzinfo=UTC)


class _Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return _NOW.astimezone(tz)


@pytest.fixture(autouse=True)
def clean_snapshot(monkeypatch):
    monkeypatch.setattr(live, "datetime", _Clock)
    monkeypatch.setattr(live, "_IRISH_CACHE", None)


def _device(device_cls, tmp_path, code=None):
    kwargs = {} if code is None else {"station_code": code}
    return device_cls(f"test-{code or 'volcano'}", SimClock(), key_dir=tmp_path, **kwargs)


def _pollen(*rows):
    return (
        "station_abbr;reference_timestamp;kabetuh0;khpoach0;kaalnuh0;kacoryh0;kafaguh0;kafraxh0;kaquerh0\n"
        + "\n".join(rows)
    ).encode()


def test_pollen_accepts_zero_retains_partial_columns_and_skips_future(monkeypatch, tmp_path):
    sensor = _device(live.MeteoSwissPollen, tmp_path, "pbe")
    csv = _pollen(
        "PBE;04.11.2026 13:00;99;99;99;99;99;99;99",
        "PBE;04.11.2026 12:00;0;;1;2;3;4;5",
        "PBE;04.11.2026 11:00;3;3;3;3;3;3;3",
    )
    monkeypatch.setattr(sensor, "_fetch_bytes", lambda *_args, **_kwargs: csv)
    result = sensor.read()
    assert verify_reading(result["reading"], result["attestation"])
    values = result["reading"]["values"]
    assert values["birch_pollen_grains_m3"] == 0
    assert "grass_pollen_grains_m3" not in values
    assert values["ash_pollen_grains_m3"] == 4
    assert values["observation_unix"] == _NOW.replace(minute=0).timestamp()
    assert "Source: MeteoSwiss" in sensor.source


@pytest.mark.parametrize(("rows", "message"), [
    (["PBE;04.11.2026 10:00;0;0;0;0;0;0;0"], "past 2 hours"),
    (["PBE;04.11.2026 12:00;;;;;;;"], "past 2 hours"),
    (["PLS;04.11.2026 12:00;0;0;0;0;0;0;0"], "different MeteoSwiss"),
    (["PBE;04.11.2026 12:00;0;0;0;0;0;0;0", "PBE;04.11.2026 12:00;1;0;0;0;0;0;0"], "conflicting"),
])
def test_pollen_rejects_unusable_rows(monkeypatch, tmp_path, rows, message):
    sensor = _device(live.MeteoSwissPollen, tmp_path, "pbe")
    monkeypatch.setattr(sensor, "_fetch_bytes", lambda *_args, **_kwargs: _pollen(*rows))
    with pytest.raises(DeviceOffline, match=message):
        sensor.sample()


def _irish_csv(*rows):
    return ("\n".join([",".join(live._IRISH_COLUMNS), ",".join(live._IRISH_UNITS), *rows]) + "\n").encode()


def test_irish_buoys_share_bounded_snapshot_and_keep_quality_meaning(monkeypatch, tmp_path):
    m2 = _device(live.IrishWeatherBuoy, tmp_path, "M2")
    m3 = _device(live.IrishWeatherBuoy, tmp_path, "M3")
    calls = []

    def fetch(url, *, max_bytes):
        calls.append((url, max_bytes))
        return _irish_csv(
            "M2,-5.4302,53.4836,2026-11-04T12:00:00Z,1.758,4.57,82,13.938,18.671,67,0",
            "M2,-5.4302,53.4836,2026-11-04T12:20:00Z,1.9,4.57,82,13.938,18.671,67,9",
            "M2,-5.4302,53.4836,2026-11-04T13:00:00Z,1.95,4.57,82,13.938,18.671,67,1",
            "M3,-10.548261,51.215956,2026-11-04T12:00:00Z,2.109,5.859,80,13.641,17.305,60,1",
        )

    monkeypatch.setattr(m2, "_fetch_bytes", fetch)
    result = m2.read()
    assert verify_reading(result["reading"], result["attestation"])
    values = result["reading"]["values"]
    assert values["wave_height_m"] == 1.758 and values["wave_qc_flag"] == 0
    assert values["wind_mps"] == pytest.approx(18.671 * 1852 / 3600, abs=0.0001)
    assert m3.sample()["wave_qc_flag"] == 1
    assert len(calls) == 1 and calls[0][1] <= 32_000
    assert "station_id%3D~%22M2%7CM3%7CM5%7CM6%22" in calls[0][0]


def test_irish_rejects_missing_station_and_unexpected_units(monkeypatch, tmp_path):
    sensor = _device(live.IrishWeatherBuoy, tmp_path, "M2")
    other = "M3,-10.548261,51.215956,2026-11-04T12:00:00Z,2.109,5.859,80,13.641,17.305,60,1"
    monkeypatch.setattr(sensor, "_snapshot", lambda _now: _irish_csv(other).decode())
    with pytest.raises(DeviceOffline, match="past 2 hours"):
        sensor.sample()
    monkeypatch.setattr(sensor, "_snapshot", lambda _now: _irish_csv(other).decode().replace("knots", "m/s"))
    with pytest.raises(DeviceOffline, match="columns or units"):
        sensor.sample()


def _aodn_ascii(station, month, rows):
    path = f"{station}/{month:%Y}/IMOS_COASTAL-WAVE-BUOYS_{month:%Y%m}01_{station}_RT_WAVE-PARAMETERS_monthly.nc"
    headings = f"Dataset {{}} IMOS/COASTAL-WAVE-BUOYS/WAVE-BUOYS/REALTIME/WAVE-PARAMETERS/{path};"
    days = [((r[0] - live._AODN_EPOCH).total_seconds() / 86400,) + r[1:] for r in rows]
    blocks = [
        f"{field}[{len(days)}]\n" + ", ".join(str(row[i]) for row in days)
        for i, field in enumerate(live._AODN_COLUMNS)
    ]
    return (headings + "\n" + "-" * 45 + "\n" + "\n\n".join(blocks)).encode()


def test_aodn_filters_quality_freshness_and_position(monkeypatch, tmp_path):
    sensor = _device(live.AodnWaveBuoy, tmp_path, "APOLLO-BAY")
    data = _aodn_ascii("APOLLO-BAY", _NOW, [
        (_NOW - timedelta(minutes=48), -38.754, 143.724, 1.687, 9.44, 193, 1),
        (_NOW - timedelta(minutes=3), -38.754, 143.724, 1.9, 9, 190, 2),
        (_NOW - timedelta(minutes=4), -40.754, 143.724, 1.9, 9, 190, 1),
        (_NOW + timedelta(minutes=4), -38.754, 143.724, 1.9, 9, 190, 1),
    ])
    seen = []

    def fetch(url, *, max_bytes):
        seen.append(url)
        return data

    monkeypatch.setattr(sensor, "_fetch_bytes", fetch)
    result = sensor.read()
    assert verify_reading(result["reading"], result["attestation"])
    values = result["reading"]["values"]
    assert values["wave_height_m"] == 1.687
    assert values["wave_period_s"] == 9.44
    assert values["wave_direction_deg"] == 193
    assert values["observation_unix"] == (_NOW - timedelta(minutes=48)).timestamp()
    assert seen[0].endswith("20261101_APOLLO-BAY_RT_WAVE-PARAMETERS_monthly.nc.ascii?TIME,LATITUDE,LONGITUDE,WSSH,WPPE,WPDI,WAVE_quality_control")


def test_aodn_reads_previous_month_only_inside_freshness_window(monkeypatch, tmp_path):
    current = datetime(2026, 1, 1, 0, 15, tzinfo=UTC)

    class Clock(_Clock):
        @classmethod
        def now(cls, tz=None):
            return current.astimezone(tz)

    monkeypatch.setattr(live, "datetime", Clock)
    sensor = _device(live.AodnWaveBuoy, tmp_path, "WOOLI")
    previous = current - timedelta(minutes=45)
    calls = []

    def fetch(url, *, max_bytes):
        calls.append(url)
        if "20260101" in url:
            raise DeviceOffline("current file not published")
        return _aodn_ascii("WOOLI", previous, [(previous, -29.857, 153.314, 0.914, 7, 185, 1)])

    monkeypatch.setattr(sensor, "_fetch_bytes", fetch)
    assert sensor.sample()["wave_height_m"] == 0.914
    assert len(calls) == 2 and "20251201" in calls[-1]


def _volcano(vid, title, level, coords, color="Green"):
    return {
        "type": "Feature", "geometry": {"type": "Point", "coordinates": coords},
        "properties": {"volcanoID": vid, "volcanoTitle": title, "level": level, "acc": color},
    }


def _camera(cid, stamp, *, relative=None):
    return {
        "id": cid, "volcano-id": ["ruapehu"],
        "geometry": {"type": "Point", "coordinates": [-39.16732, 175.48055]},
        "properties": {
            "title": "Ruapehu from North", "latest-timestamp": stamp,
            "latest-image-thumb": relative or f"latest/t-{cid}.jpg",
        },
    }


def test_geonet_signs_status_and_independently_validated_camera_metadata(monkeypatch, tmp_path):
    sensor = _device(live.GeonetVolcano, tmp_path)
    features = [
        _volcano("taupo", "Taupo", 0, [175.896, -38.784]),
        _volcano("ruapehu", "Ruapehu", 1, [175.563, -39.281]),
    ]
    valid = _camera("ruapehunorth", " 1:30 am (NZDT) 05 Nov 2026")
    stale = _camera("ruapehusouth", " 1:00 am (NZDT) 05 Nov 2026")
    foreign = _camera("ruapehueast", " 1:30 am (NZDT) 05 Nov 2026", relative="https://other.test/x.jpg")
    calls = []

    def fetch(url, *, max_bytes):
        calls.append(url)
        payload = (
            {"type": "FeatureCollection", "features": features} if url == sensor.url
            else [{"features": [valid, stale, foreign]}, {"features": [valid]}]
        )
        return json.dumps(payload).encode()

    monkeypatch.setattr(sensor, "_fetch_bytes", fetch)
    result = sensor.read()
    assert verify_reading(result["reading"], result["attestation"])
    reading = result["reading"]
    assert calls == [sensor.url, live._GEONET_CAMERAS_URL]
    assert reading["hotspot_count"] == 2
    first, second = reading["hotspots"]
    assert first["volcano_id"] == "ruapehu" and second["volcanic_alert_level"] == 0
    assert first["source_retrieved_at"] == _NOW.isoformat()
    assert first["valid_until"] == (_NOW + timedelta(minutes=30)).isoformat()
    camera, = first["cameras"]
    assert camera["latitude"] == -39.16732
    assert camera["longitude"] == 175.48055
    assert camera["observed_at"] == "2026-11-04T12:30:00+00:00"
    assert camera["url"].startswith("https://images.geonet.org.nz/volcano/cameras/latest/t-ruapehunorth.jpg?v=")
    camera["caption"] = "tampered"
    assert not verify_reading(reading, result["attestation"])


def test_geonet_keeps_status_when_camera_feed_fails(monkeypatch, tmp_path):
    sensor = _device(live.GeonetVolcano, tmp_path)

    def fetch(url, *, max_bytes):
        if url == live._GEONET_CAMERAS_URL:
            raise DeviceOffline("camera service down")
        return json.dumps({
            "type": "FeatureCollection",
            "features": [_volcano("whiteisland", "White Island", 2, [177.183, -37.521], "Yellow")],
        }).encode()

    monkeypatch.setattr(sensor, "_fetch_bytes", fetch)
    assert sensor.read()["reading"]["hotspots"][0]["cameras"] == []
