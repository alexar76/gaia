"""Fuel prices, FAA airport programs, CBP border waits, avalanche bulletins, IMIS snow, Danish grid."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from gaia.app import _read_cap_for
from gaia.capabilities import GatewayRuntime, build_spec
from gaia.clock import SimClock
from gaia.devices import live_travel
from gaia.devices.base import DeviceOffline
from gaia.devices.live_alpine import (
    ENERGINET_AREAS,
    SLF_IMIS_STATIONS,
    SLF_REGIONS,
    VARSOM_REGIONS,
    EnerginetGrid,
    SlfAvalanche,
    SlfImisSnow,
    VarsomAvalanche,
)
from gaia.devices.live_travel import (
    CBP_CROSSINGS,
    FAA_AIRPORTS,
    FR_FUEL_CITIES,
    CbpBorderWait,
    FaaAirportStatus,
    FrFuelPrices,
    _minutes,
)
from gaia.plausibility import PHYSICS


@pytest.fixture(autouse=True)
def _no_shared_snapshots():
    live_travel._SNAPSHOTS.clear()
    yield
    live_travel._SNAPSHOTS.clear()


def _device(cls, device_id, tmp_path, **kw):
    return cls(device_id, SimClock(), site="test", key_dir=str(tmp_path), **kw)


def _iso(moment):
    return moment.isoformat()


# ── France: pump prices ───────────────────────────────────────────────────────

def test_fuel_reports_median_and_lowest_of_recent_prices_only(tmp_path):
    now = datetime.now(UTC)
    fresh, old = _iso(now - timedelta(days=1)), _iso(now - timedelta(days=30))
    payload = {"total_count": 4, "results": [
        {"id": 1, "gazole_prix": 2.10, "gazole_maj": fresh, "e10_prix": 1.90, "e10_maj": fresh},
        {"id": 2, "gazole_prix": 2.00, "gazole_maj": fresh, "sp98_prix": 2.05, "sp98_maj": fresh},
        {"id": 3, "gazole_prix": 2.30, "gazole_maj": fresh},
        {"id": 4, "gazole_prix": 1.00, "gazole_maj": old},  # a month-old price is left out
    ]}
    values = _device(FrFuelPrices, "fr-fuel-paris", tmp_path, city="paris").map(payload)
    assert values["diesel_eur_l"] == 2.10 and values["diesel_min_eur_l"] == 2.00
    assert values["petrol_e10_eur_l"] == 1.90 and values["petrol_sp98_min_eur_l"] == 2.05
    assert values["fuel_station_count"] == 3
    assert values["observation_unix"] == pytest.approx((now - timedelta(days=1)).timestamp())


def test_fuel_refuses_a_partial_page_and_a_city_without_recent_prices(tmp_path):
    device = _device(FrFuelPrices, "fr-fuel-lyon", tmp_path, city="lyon")
    with pytest.raises(DeviceOffline, match="partial median"):
        device.map({"total_count": 101, "results": []})
    stale = _iso(datetime.now(UTC) - timedelta(days=8))
    with pytest.raises(DeviceOffline, match="last 7 days"):
        device.map({"total_count": 1, "results": [{"id": 1, "gazole_prix": 2.0, "gazole_maj": stale}]})
    with pytest.raises(ValueError):
        _device(FrFuelPrices, "fr-fuel-x", tmp_path, city="atlantis")


# ── FAA ───────────────────────────────────────────────────────────────────────

def _faa(body, moment=None):
    stamp = (moment or datetime.now(UTC)).strftime("%a %b %d %H:%M:%S %Y GMT")
    return f"<AIRPORT_STATUS_INFORMATION><Update_Time>{stamp}</Update_Time>{body}</AIRPORT_STATUS_INFORMATION>"


_FAA_BODY = (
    "<Delay_type><Name>Ground Stop Programs</Name><Ground_Stop_List><Program><ARPT>LGA</ARPT>"
    "<Reason>low visibility</Reason><End_Time>7:45 am EST</End_Time></Program></Ground_Stop_List></Delay_type>"
    "<Delay_type><Name>Ground Delay Programs</Name><Ground_Delay_List><Ground_Delay><ARPT>SFO</ARPT>"
    "<Reason>low ceilings</Reason><Avg>1 hour and 26 minutes</Avg><Max>3 hours and 5 minutes</Max>"
    "</Ground_Delay></Ground_Delay_List></Delay_type>"
    "<Delay_type><Name>General Arrival/Departure Delay Info</Name><Arrival_Departure_Delay_List><Delay>"
    "<ARPT>ORD</ARPT><Reason>wind</Reason><Arrival_Departure Type=\"Departure\"><Min>16 minutes</Min>"
    "<Max>30 minutes</Max><Trend>Increasing</Trend></Arrival_Departure></Delay>"
    "</Arrival_Departure_Delay_List></Delay_type>"
    "<Delay_type><Name>Airport Closures</Name><Airport_Closure_List><Airport><ARPT>LAX</ARPT>"
    "<Reason>CLSD TO NON SKED TRANSIENT GA ACFT</Reason></Airport></Airport_Closure_List></Delay_type>"
)


def test_faa_reads_each_program_for_its_own_airport(tmp_path):
    def read(code):
        return _device(FaaAirportStatus, f"faa-status-{code.lower()}", tmp_path, airport=code).map(_faa(_FAA_BODY))

    assert read("LGA")["ground_stop"] == 1 and read("LGA")["delay_program_active"] == 1
    sfo = read("SFO")
    assert (sfo["ground_delay_avg_min"], sfo["ground_delay_max_min"], sfo["ground_stop"]) == (86, 185, 0)
    assert read("ORD")["departure_delay_max_min"] == 30 and "arrival_delay_max_min" not in read("ORD")
    # A general-aviation closure notice is not a delay program for scheduled flights.
    lax = read("LAX")
    assert lax["delay_program_active"] == 0 and lax["ground_stop"] == 0
    assert set(lax) <= set(FaaAirportStatus.fields)


def test_faa_rejects_stale_or_foreign_documents(tmp_path):
    device = _device(FaaAirportStatus, "faa-status-jfk", tmp_path, airport="JFK")
    with pytest.raises(DeviceOffline, match="not current"):
        device.map(_faa("", datetime.now(UTC) - timedelta(hours=1)))
    for text in ("<html/>", "not xml", None):
        with pytest.raises(DeviceOffline):
            device.map(text)
    assert _minutes("45 minutes") == 45 and _minutes("2 hours") == 120 and _minutes("soon") is None


# ── CBP ───────────────────────────────────────────────────────────────────────

def _cbp(port, zone, moment=None, **over):
    local = (moment or datetime.now(UTC)).astimezone(ZoneInfo(zone))
    lane = {"delay_minutes": "30", "lanes_open": "4", "operational_status": "delay"}
    blank = {"delay_minutes": "", "lanes_open": "", "operational_status": "N/A"}
    return {
        "port_number": port, "port_status": "Open",
        "date": local.strftime("%m/%d/%Y"), "time": local.strftime("%H:%M:%S"),
        "passenger_vehicle_lanes": {"standard_lanes": lane},
        "commercial_vehicle_lanes": {"standard_lanes": blank},
        "pedestrian_lanes": {"standard_lanes": {"delay_minutes": "10", "lanes_open": "16"}},
        **over,
    }


def test_cbp_reads_the_feed_time_in_the_ports_own_zone(tmp_path):
    # San Ysidro stamps Pacific time, Detroit Eastern: both rows are current.
    rows = [_cbp("250401", "America/Los_Angeles"), _cbp("380001", "America/New_York")]
    west = _device(CbpBorderWait, "cbp-wait-250401", tmp_path, port_number="250401").map(rows)
    east = _device(CbpBorderWait, "cbp-wait-380001", tmp_path, port_number="380001").map(rows)
    for values in (west, east):
        assert values["port_open"] == 1 and values["passenger_delay_min"] == 30
        assert values["pedestrian_lanes_open"] == 16 and "commercial_delay_min" not in values
        assert abs(values["source_published_unix"] - datetime.now(UTC).timestamp()) < 5


def test_cbp_refuses_stale_missing_or_unknown_rows(tmp_path):
    device = _device(CbpBorderWait, "cbp-wait-071201", tmp_path, port_number="071201")
    with pytest.raises(DeviceOffline, match="not current"):
        device.map([_cbp("071201", "America/New_York", datetime.now(UTC) - timedelta(hours=2))])
    with pytest.raises(DeviceOffline, match="missing"):
        device.map([_cbp("999999", "America/New_York")])
    with pytest.raises(DeviceOffline, match="status"):
        device.map([_cbp("071201", "America/New_York", port_status="Unknown")])
    closed = device.map([_cbp("071201", "America/New_York", port_status="Closed")])
    assert closed["port_open"] == 0


# ── Avalanche bulletins ───────────────────────────────────────────────────────

def _oslo_day(offset_days=0):
    day = (datetime.now(ZoneInfo("Europe/Oslo")) + timedelta(days=offset_days)).date()
    return f"{day}T00:00:00", f"{day}T23:59:59"


def test_varsom_reports_today_and_tomorrow_and_keeps_no_rating_as_zero(tmp_path):
    (start, end), (next_start, next_end) = _oslo_day(), _oslo_day(1)
    rows = [
        {"RegionId": 3011, "DangerLevel": "3", "ValidFrom": start, "ValidTo": end, "PublishTime": start},
        {"RegionId": 3011, "DangerLevel": "0", "ValidFrom": next_start, "ValidTo": next_end, "PublishTime": start},
        {"RegionId": 3010, "DangerLevel": "5", "ValidFrom": start, "ValidTo": end, "PublishTime": start},
    ]
    values = _device(VarsomAvalanche, "varsom-3011", tmp_path, region_id="3011").map(rows)
    assert values["avalanche_danger_level"] == 3 and values["avalanche_danger_level_tomorrow"] == 0
    assert values["source_expires_unix"] > datetime.now(UTC).timestamp() > values["source_published_unix"] - 1
    with pytest.raises(DeviceOffline, match="current day"):
        _device(VarsomAvalanche, "varsom-3003", tmp_path, region_id="3003").map(rows)


def test_slf_takes_the_highest_rating_of_the_bulletin_in_force(tmp_path):
    now = datetime.now(UTC)
    window = {"startTime": _iso(now - timedelta(hours=2)), "endTime": _iso(now + timedelta(hours=6))}
    published = _iso(now - timedelta(hours=2))
    payload = {"bulletins": [
        {"regions": [{"regionID": "CH-5123"}], "validTime": window, "publicationTime": published,
         "dangerRatings": [{"mainValue": "moderate"}, {"mainValue": "considerable"}]},
        {"regions": [{"regionID": "CH-4222"}], "validTime": window, "publicationTime": published},
        {"regions": [{"regionID": "CH-7114"}], "publicationTime": published, "dangerRatings": [{"mainValue": "high"}],
         "validTime": {"startTime": _iso(now - timedelta(days=2)), "endTime": _iso(now - timedelta(days=1))}},
    ]}

    def read(region):
        return _device(SlfAvalanche, f"slf-aval-{region.lower()}", tmp_path, region_id=region).map(payload)

    assert read("CH-5123")["avalanche_danger_level"] == 3
    assert read("CH-4222")["avalanche_danger_level"] == 0  # listed, no rating
    with pytest.raises(DeviceOffline, match="in force"):
        read("CH-7114")  # its bulletin has ended


def test_imis_uses_the_newest_measurement_within_three_hours(tmp_path):
    now = datetime.now(UTC)

    def row(age_min, depth, air=-3.0, code="WFJ2"):
        stamp = (now - timedelta(minutes=age_min)).strftime("%Y-%m-%dT%H:%M:%SZ")
        return {"station_code": code, "measure_date": stamp, "HS": depth, "TA_30MIN_MEAN": air}

    device = _device(SlfImisSnow, "slf-snow-wfj2", tmp_path, station_code="WFJ2")
    values = device.map([row(90, 30.0), row(20, -1.2, air=-4.5), row(5, 99.0, code="DAV2")])
    assert values["snow_depth_cm"] == 0.0 and values["temperature_c"] == -4.5
    with pytest.raises(DeviceOffline, match="last 3 hours"):
        device.map([row(400, 30.0)])


# ── Energinet ─────────────────────────────────────────────────────────────────

def test_energinet_picks_its_own_price_area_from_the_shared_interval(tmp_path):
    stamp = (datetime.now(UTC) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S")
    payload = {"records": [
        {"Minutes5UTC": stamp, "PriceArea": "DK1", "CO2Emission": 76.0},
        {"Minutes5UTC": stamp, "PriceArea": "DK2", "CO2Emission": 31.0},
    ]}
    dk1 = _device(EnerginetGrid, "dk-grid-dk1-01", tmp_path)
    dk2 = _device(EnerginetGrid, "dk-grid-dk2-01", tmp_path)
    assert dk1.url == dk2.url  # one upstream request serves both areas
    assert dk1.map(payload)["carbon_intensity_gco2_kwh"] == 76 and dk2.map(payload)["carbon_intensity_gco2_kwh"] == 31
    old = (datetime.now(UTC) - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S")
    with pytest.raises(DeviceOffline, match="not current"):
        dk1.map({"records": [{"Minutes5UTC": old, "PriceArea": "DK1", "CO2Emission": 76.0}]})
    with pytest.raises(DeviceOffline, match="missing"):
        dk2.map({"records": payload["records"][:1]})


def test_devices_of_one_feed_share_a_single_upstream_fetch(tmp_path, monkeypatch):
    calls = []
    stamp = (datetime.now(UTC) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S")
    payload = {"records": [{"Minutes5UTC": stamp, "PriceArea": area, "CO2Emission": 50.0} for area in ("DK1", "DK2")]}
    monkeypatch.setattr(EnerginetGrid, "_fetch", lambda self, url: calls.append(url) or payload)
    for device_id in ENERGINET_AREAS:
        _device(EnerginetGrid, device_id, tmp_path).sample()
    assert len(calls) == 1


# ── Registration ──────────────────────────────────────────────────────────────

def test_new_sources_register_and_only_use_their_own_read_capability(monkeypatch, tmp_path):
    monkeypatch.setenv("GAIA_ENABLE_LIVE", "1")
    runtime = GatewayRuntime(key_dir=str(tmp_path))
    monkeypatch.setattr(runtime, "read", lambda device_id: {"device_id": device_id})
    specs = {cap.capability_id: cap for cap in build_spec(runtime).capabilities}
    routes = {
        "gaia.fuel.price.read@v1": [f"fr-fuel-{city}" for city in FR_FUEL_CITIES],
        "gaia.airport.status.read@v1": [f"faa-status-{code.lower()}" for code in FAA_AIRPORTS],
        "gaia.border.wait.read@v1": [f"cbp-wait-{port}" for port in CBP_CROSSINGS],
        "gaia.avalanche.read@v1": [
            *(f"varsom-{region}" for region in VARSOM_REGIONS),
            *(f"slf-aval-{region.lower()}" for region in SLF_REGIONS),
        ],
        "gaia.snow.read@v1": [f"slf-snow-{code.lower()}" for code in SLF_IMIS_STATIONS],
        "gaia.grid.read@v1": list(ENERGINET_AREAS),
    }
    for cap_id, device_ids in routes.items():
        capability = specs[cap_id]
        assert capability.price_per_call_usd in (0.001, 0.002)
        for device_id in device_ids:
            device = runtime.fleet.get(device_id)
            assert device_id in capability.description
            assert _read_cap_for(device) == cap_id
            assert device.fields.keys() <= PHYSICS.keys()
            assert capability.handler({"device_id": device_id}) == {"device_id": device_id}
            with pytest.raises(ValueError, match="must be read through"):
                specs["gaia.weather.read@v1"].handler({"device_id": device_id})
    for cap_id in ("gaia.fuel.price.read@v1", "gaia.airport.status.read@v1",
                   "gaia.border.wait.read@v1", "gaia.avalanche.read@v1"):
        with pytest.raises(ValueError, match="unsupported device"):
            specs[cap_id].handler({"device_id": "ws-01"})
    with pytest.raises(ValueError, match="fixed station"):
        specs["gaia.snow.read@v1"].handler({"device_id": "slf-snow-wfj2", "latitude": 46.8, "longitude": 9.8})
    assert specs["gaia.grid.read@v1"].handler({}) == {"device_id": "uk-grid-01"}


def test_repeated_reads_of_one_observation_still_verify(monkeypatch, tmp_path):
    monkeypatch.setenv("GAIA_ENABLE_LIVE", "1")
    runtime = GatewayRuntime(key_dir=str(tmp_path))
    now = datetime.now(UTC).timestamp()
    samples = {
        "slf-snow-wfj2": {"snow_depth_cm": 37.5, "temperature_c": -3.0, "observation_unix": now - 600,
                          "latitude": 46.8296, "longitude": 9.8093},
        "faa-status-lga": {"delay_program_active": 1.0, "ground_stop": 1.0, "source_published_unix": now - 60,
                           "latitude": 40.7794, "longitude": -73.8803},
        "varsom-3011": {"avalanche_danger_level": 0.0, "source_published_unix": now - 3600,
                        "source_expires_unix": now + 3600, "latitude": 69.65, "longitude": 18.96},
    }
    for device_id, values in samples.items():
        monkeypatch.setattr(runtime.fleet.get(device_id), "sample", lambda values=values: dict(values))
        for _ in range(6):
            result = runtime.read(device_id)
            verdict = runtime.verifier.check(result["reading"], result["attestation"])
            assert verdict.verified, (device_id, verdict.summary)
