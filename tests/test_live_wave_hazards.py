"""Provider parsing, map provenance and signed coverage for the hazard relays."""

from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import pytest
from gaia.attestation import verify_reading
from gaia.clock import SimClock
from gaia.devices.base import DeviceOffline
from gaia.devices.live_wave_hazards import DeaHotspots, SpcStormReports, WashingtonVaacAsh


class CaptureTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 11, 4, 12, 41, tzinfo=UTC).astimezone(tz)


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr("gaia.devices.live_wave_hazards.datetime", CaptureTime)


def _device(cls, tmp_path):
    return cls("hazard-test", SimClock(realtime=True), key_dir=tmp_path)


_TORNADO = "Time,F_Scale,Location,County,State,Lat,Lon,Comments"
_WIND = "Time,Speed,Location,County,State,Lat,Lon,Comments"
_HAIL = "Time,Size,Location,County,State,Lat,Lon,Comments"
_EMPTY_SPC = "\n".join((_TORNADO, _WIND, _HAIL))


def test_spc_uses_reporting_day_and_omits_free_text(tmp_path, monkeypatch):
    device = _device(SpcStormReports, tmp_path)
    archived = "\n".join((
        _TORNADO, "1240,UNK,Town,Example,MO,38.1,-92.4,Expired",
        _WIND, "1610,UNK,4 S Renick,Randolph,MO,39.29,-92.4,Private narrative,with comma",
        _HAIL, "0030,150,Town,Example,MO,38.1,-92.4,Another private narrative",
    ))
    today = "\n".join((
        _TORNADO, _WIND,
        "1225,70,North of Town,Example,MO,38.1,-92.4,Do not relay",
        "1400,70,South of Town,Example,MO,38.1,-92.4,Future",
        _HAIL,
    ))
    responses = {"261103_rpts.csv": archived, "today.csv": today}
    monkeypatch.setattr(device, "_text", lambda url, **kw: responses[url.rsplit("/", 1)[1]])
    result = device.read()
    assert verify_reading(result["reading"], result["attestation"])
    reading = result["reading"]
    assert reading["values"]["storm_report_count"] == 3
    assert [row["observed_at"] for row in reading["hotspots"]] == [
        "2026-11-04T12:25:00+00:00", "2026-11-04T00:30:00+00:00", "2026-11-03T16:10:00+00:00",
    ]
    assert reading["hotspots"][0]["wind_mps"] == 31.2928
    assert reading["hotspots"][1]["hail_size_mm"] == 38.1
    assert reading["hotspots"][2]["valid_until"] == "2026-11-04T16:10:00+00:00"
    assert "wind_mps" not in reading["hotspots"][2]
    assert "narrative" not in str(reading).lower()


def test_spc_empty_is_signed_but_unrecognized_schema_is_offline(tmp_path, monkeypatch):
    device = _device(SpcStormReports, tmp_path)
    monkeypatch.setattr(device, "_text", lambda *a, **kw: _EMPTY_SPC)
    result = device.read()
    assert verify_reading(result["reading"], result["attestation"])
    assert result["reading"]["hotspots"] == []
    assert result["reading"]["values"]["storm_report_count"] == 0
    monkeypatch.setattr(device, "_text", lambda *a, **kw: _WIND)
    with pytest.raises(DeviceOffline, match="malformed SPC"):
        device.read()


_VAAC_SHAPE = """
<ashCloud><VolcanicAshCloudForecast><ashCloudExtent><aixm:AirspaceVolume>
<aixm:upperLimit uom="FL">220</aixm:upperLimit><aixm:upperLimitReference>STD</aixm:upperLimitReference>
<aixm:lowerLimit>GND</aixm:lowerLimit><aixm:lowerLimitReference>SFC</aixm:lowerLimitReference>
<aixm:horizontalProjection><aixm:Surface axisLabels="Lat Long" srsDimension="2"
 srsName="http://www.opengis.net/def/crs/EPSG/0/4326">
<gml:patches><gml:PolygonPatch><gml:exterior><gml:LinearRing>
<gml:posList count="4">-2 -78 -2 -79 -3 -79 -2 -78</gml:posList>
</gml:LinearRing></gml:exterior></gml:PolygonPatch></gml:patches>
</aixm:Surface></aixm:horizontalProjection>
</aixm:AirspaceVolume></ashCloudExtent></VolcanicAshCloudForecast></ashCloud>"""


def _time_element(tag, when):
    return f"<{tag}><gml:TimeInstant><gml:timePosition>{when}</gml:timePosition></gml:TimeInstant></{tag}>"


def _advisory(issue, *, number="2026/758", name="SANGAY 352090", centre="WASHINGTON", final=False):
    obs = _time_element("phenomenonTime", "2026-11-04T09:00:00Z")
    fcst = _time_element("phenomenonTime", "2026-11-04T15:00:00Z")
    next_time = _time_element("nextAdvisoryTime", "2026-11-04T15:30:00Z") if not final else "<nextAdvisoryTime/>"
    return f"""<MeteorologicalBulletin xmlns="http://def.wmo.int/collect/2014"
      xmlns:gml="http://www.opengis.net/gml/3.2" xmlns:aixm="http://www.aixm.aero/schema/5.1.1"
      xmlns:metce="http://def.wmo.int/metce/2013"><meteorologicalInformation>
      <VolcanicAshAdvisory xmlns="http://icao.int/iwxxm/3.0" permissibleUsage="OPERATIONAL" reportStatus="NORMAL">
      {_time_element("issueTime", issue)}
      <issuingVolcanicAshAdvisoryCentre><aixm:Unit><aixm:name>{centre}</aixm:name></aixm:Unit></issuingVolcanicAshAdvisoryCentre>
      <volcano><metce:EruptingVolcano><metce:name>{name}</metce:name></metce:EruptingVolcano></volcano>
      <advisoryNumber>{number}</advisoryNumber>
      <observation><VolcanicAshObservedOrEstimatedConditions status="IDENTIFIABLE" isEstimated="true">
      {obs}{_VAAC_SHAPE}</VolcanicAshObservedOrEstimatedConditions></observation>
      <forecast><VolcanicAshForecastConditions status="PROVIDED">
      {fcst}{_VAAC_SHAPE}</VolcanicAshForecastConditions></forecast>
      {next_time}
      </VolcanicAshAdvisory></meteorologicalInformation>
      <bulletinIdentifier>A_LUXX24KNES040930_C_KNES_20261104093123.xml</bulletinIdentifier>
      </MeteorologicalBulletin>"""


def _vaac_index(*names):
    links = "".join(
        f'<a href="/products/atmosphere/vaac/volcanoes/xml_files/{name}">XML</a>'
        for name in names
    )
    return f"<dt>Advisories from the past 24 hours</dt>{links}"


def test_vaac_latest_advisory_preserves_observation_and_forecast(tmp_path, monkeypatch):
    device = _device(WashingtonVaacAsh, tmp_path)
    responses = {
        "messages.html": _vaac_index("FVXX24_20261104_0930.xml", "FVXX24_20261104_1000.xml"),
        "FVXX24_20261104_0930.xml": _advisory("2026-11-04T09:30:00Z"),
        "FVXX24_20261104_1000.xml": _advisory("2026-11-04T10:00:00Z", number="2026/759"),
    }
    monkeypatch.setattr(device, "_text", lambda url, **kw: responses[url.rsplit("/", 1)[1]])
    result = device.read()
    reading = result["reading"]
    assert verify_reading(reading, result["attestation"])
    assert reading["values"]["ash_shape_count"] == 2
    observed, forecast = reading["hotspots"]
    assert {row["advisory_number"] for row in (observed, forecast)} == {"2026/759"}
    assert observed["claim_class"] == "estimated_ash"
    assert observed["geometry"]["coordinates"] == [[[-78, -2], [-79, -2], [-79, -3], [-78, -2]]]
    assert observed["event_bbox"] == [-79, -3, -78, -2]
    assert observed["base_ft"] == 0 and observed["top_ft"] == 22000
    assert observed["valid_until"] == forecast["valid_until"] == "2026-11-04T15:30:00+00:00"
    assert forecast["claim_class"] == "ash_forecast"
    assert forecast["valid_at"] == "2026-11-04T15:00:00+00:00"
    assert "observation_unix" not in forecast


def test_vaac_final_replaces_previous_polygon_and_rejects_foreign_centre(tmp_path, monkeypatch):
    device = _device(WashingtonVaacAsh, tmp_path)
    responses = {
        "messages.html": _vaac_index("FVXX24_20261104_0930.xml", "FVXX24_20261104_1000.xml"),
        "FVXX24_20261104_0930.xml": _advisory("2026-11-04T09:30:00Z"),
        "FVXX24_20261104_1000.xml": _advisory("2026-11-04T10:00:00Z", number="2026/759", final=True),
    }
    monkeypatch.setattr(device, "_text", lambda url, **kw: responses[url.rsplit("/", 1)[1]])
    result = device.read()
    assert verify_reading(result["reading"], result["attestation"])
    assert result["reading"]["hotspots"] == []
    responses["FVXX24_20261104_1000.xml"] = _advisory("2026-11-04T10:00:00Z", centre="TOKYO")
    with pytest.raises(DeviceOffline, match="malformed Washington VAAC"):
        device.read()


def _dea_feature(id, *, observed="2026-11-04T12:27:22Z", coords=(133.7012, -18.6104)):
    return {
        "type": "Feature", "id": "ephemeral-wfs-fid",
        "geometry": {"type": "Point", "coordinates": list(coords)},
        "properties": {
            "id": id, "satellite": "HIMAWARI-9", "sensor": "AHI",
            "temp_kelvin": 364, "datetime": observed, "confidence": 100,
        },
    }


def test_dea_paginates_stable_source_ids_and_signs_query_scope(tmp_path, monkeypatch):
    device = _device(DeaHotspots, tmp_path)
    monkeypatch.setattr("gaia.devices.live_wave_hazards._DEA_PAGE", 2)
    calls = []

    def page(bbox, now, offset, size):
        calls.append((bbox, now.isoformat(), offset, size))
        rows = [_dea_feature(i) for i in range(1, 4)]
        return {"numberMatched": 3, "features": rows[offset:offset + size]}

    monkeypatch.setattr(device, "_page", page)
    result = device.read_bbox(130, -19, 149, -11)
    reading = result["reading"]
    assert verify_reading(reading, result["attestation"])
    assert [row["event_id"] for row in reading["hotspots"]] == ["1", "2", "3"]
    assert reading["values"]["coverage_complete"] == 1
    assert reading["values"]["hotspot_matched"] == 3
    assert reading["values"]["bbox_west_deg"] == 130
    assert reading["hotspots"][0]["valid_until"] == "2026-11-04T15:27:22+00:00"
    assert calls == [
        ((130, -19, 149, -11), "2026-11-04T12:41:00+00:00", 0, 2),
        ((130, -19, 149, -11), "2026-11-04T12:41:00+00:00", 2, 2),
    ]
    reading["values"]["coverage_complete"] = 0
    assert not verify_reading(reading, result["attestation"])


def test_dea_scoped_limit_outside_area_and_invalid_rows(tmp_path, monkeypatch):
    device = _device(DeaHotspots, tmp_path)

    def page(bbox, now, offset, size):
        assert size == 2
        return {"numberMatched": 6, "features": [_dea_feature(1), _dea_feature(2, coords=(40, 1))]}

    monkeypatch.setattr(device, "_page", page)
    reading = device.read_bbox(130, -19, 149, -11, limit=2)["reading"]
    assert reading["values"]["coverage_complete"] == 0
    assert reading["values"]["hotspot_matched"] == 6
    assert [row["event_id"] for row in reading["hotspots"]] == ["1"]
    assert device.read_bbox(15, -19, 40, -11)["reading"]["values"]["coverage_complete"] == 1
    with pytest.raises(ValueError, match="bbox"):
        device.read_bbox(149, -19, 130, -11)


def test_dea_wfs_filters_time_and_freezes_the_load_cutoff(tmp_path, monkeypatch):
    device = _device(DeaHotspots, tmp_path)

    def text(url, **kw):
        args = parse_qs(urlsplit(url).query)
        assert args["sortBy"] == ["datetime D,id A"]
        assert args["count"] == ["200"]
        assert args["startIndex"] == ["0"]
        assert "BBOX(geometry,130.0000,-19.0000,149.0000,-11.0000,'EPSG:4326')" in args["CQL_FILTER"][0]
        assert "datetime AFTER 2026-11-04T09:41:00Z" in args["CQL_FILTER"][0]
        assert "load_dt BEFORE 2026-11-04T12:41:00Z" in args["CQL_FILTER"][0]
        return '{"type":"FeatureCollection","numberMatched":0,"features":[]}'

    monkeypatch.setattr(device, "_text", text)
    assert device.read_bbox(130, -19, 149, -11)["reading"]["values"]["coverage_complete"] == 1
