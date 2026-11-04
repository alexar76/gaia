"""Measured Baltic wave conditions from the FMI open-data buoy network."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs
from xml.etree import ElementTree

from gaia.devices._live_base import LiveDevice, _num
from gaia.devices.base import DeviceOffline
from gaia.source_policy import require_approved_source

if TYPE_CHECKING:
    from gaia.clock import SimClock

FMI_MARINE_BUOYS = {
    "103976": (60.123333, 24.972833, "Helsinki Suomenlinna"),
    "108126": (59.715833, 21.368333, "Utö Svartbådarna"),
    "108499": (65.488750, 24.346467, "Kemi"),
    "134220": (59.248167, 20.998333, "Northern Baltic Proper"),
    "134221": (59.965000, 25.235000, "Gulf of Finland"),
    "134246": (61.800100, 20.232670, "Bothnian Sea"),
}

_NS = {
    "wfs": "http://www.opengis.net/wfs/2.0",
    "omso": "http://inspire.ec.europa.eu/schemas/omso/3.0",
    "om": "http://www.opengis.net/om/2.0",
    "gml": "http://www.opengis.net/gml/3.2",
    "wml": "http://www.opengis.net/waterml/2.0",
}
_HREF = "{http://www.w3.org/1999/xlink}href"
_FMISID = "http://xml.fmi.fi/namespace/stationcode/fmisid"
_LAT_LON_CRS = "http://www.opengis.net/def/crs/EPSG/0/4258"
_MAX_AGE = timedelta(hours=2)
# FMI publishes the units in the linked metadata, separately from the series.
_PARAMETERS = {
    "WaveHs": ("wave_height_m", 0.0, 30.0),
    "WTP": ("wave_period_s", 0.0, 40.0),
    "ModalWDi": ("wave_direction_deg", 0.0, 360.0),
    "TWATER": ("sst_c", -2.0, 40.0),
}


class FmiMarine(LiveDevice):
    """Sign the latest measured Hs and any colocated, same-time parameters."""

    model = "GAIA-MARINE (Finnish Meteorological Institute)"
    policy_id = "fmi_marine_waves"
    fields = {
        "wave_height_m": "m",
        "wave_period_s": "s",
        "wave_direction_deg": "deg",
        "sst_c": "cel",
        "observation_unix": "s",
        "latitude": "deg",
        "longitude": "deg",
    }

    def __init__(self, device_id: str, clock: SimClock, *, station_id: str, **kw: Any):
        if station_id not in FMI_MARINE_BUOYS:
            raise ValueError("unsupported FMI pilot buoy")
        super().__init__(device_id, clock, **kw)
        self.station_id = station_id
        self.latitude, self.longitude, _ = FMI_MARINE_BUOYS[station_id]
        self.url = (
            "https://opendata.fmi.fi/wfs?service=WFS&version=2.0.0"
            "&request=getFeature&storedquery_id=fmi::observations::wave::timevaluepair"
            f"&fmisid={station_id}&parameters={','.join(_PARAMETERS)}&timestep=30"
        )
        self.source = (
            f"{self.url} (Finnish Meteorological Institute wave buoy; CC BY 4.0. "
            "Measured significant wave height, peak period, direction waves come from and "
            "near-surface water temperature when available. Wave timestamp is the start of sampling.)"
        )
        require_approved_source(self.policy_id).require_endpoint(self.url)

    def sample(self) -> dict[str, float]:
        return self.map(self._fetch_text(self.url, max_chars=120_000))

    def map(self, payload: Any) -> dict[str, float]:
        try:
            root = ElementTree.fromstring(payload)
        except (ElementTree.ParseError, TypeError, ValueError) as exc:
            raise DeviceOffline(f"{self.device_id}: invalid FMI wave XML") from exc
        if root.tag != f"{{{_NS['wfs']}}}FeatureCollection":
            raise DeviceOffline(f"{self.device_id}: FMI wave feature collection unavailable")

        now = datetime.now(UTC)
        earliest = now - _MAX_AGE
        series: dict[str, dict[datetime, float]] = {}
        for member in root.findall("wfs:member/omso:PointTimeSeriesObservation", _NS):
            prop = member.find("om:observedProperty", _NS)
            if prop is None:
                continue
            metadata, _, raw_query = prop.get(_HREF, "").partition("?")
            if metadata != "https://opendata.fmi.fi/meta":
                continue
            query = parse_qs(raw_query)
            params = query.get("param", [])
            if query.get("observableProperty") != ["observation"] or len(params) != 1 or params[0] not in _PARAMETERS:
                continue

            feature = member.find("om:featureOfInterest", _NS)
            if feature is None:
                continue
            station = feature.find(f".//gml:identifier[@codeSpace='{_FMISID}']", _NS)
            point = feature.find(f".//gml:Point[@srsName='{_LAT_LON_CRS}']", _NS)
            if station is None or station.text != self.station_id or point is None:
                continue
            position = point.findtext("gml:pos", default="", namespaces=_NS).split()
            if len(position) != 2:
                continue
            lat, lon = map(_num, position)
            # Accommodate the precision of the WFS position and small mooring movement.
            if (lat is None or lon is None or not abs(lat - self.latitude) < 0.01
                    or not abs(lon - self.longitude) < 0.01):
                continue

            field, minimum, maximum = _PARAMETERS[params[0]]
            readings = series.setdefault(field, {})
            for sample in member.findall("om:result/wml:MeasurementTimeseries/wml:point/wml:MeasurementTVP", _NS):
                value = _num(sample.findtext("wml:value", namespaces=_NS))
                if value is None or not minimum <= value <= maximum:
                    continue
                try:
                    observed = datetime.fromisoformat(sample.findtext("wml:time", default="", namespaces=_NS))
                except ValueError:
                    continue
                if observed.tzinfo is None or not earliest <= observed <= now:
                    continue
                if readings.setdefault(observed, value) != value:
                    raise DeviceOffline(f"{self.device_id}: conflicting FMI wave observations")

        heights = series.get("wave_height_m")
        if not heights:
            raise DeviceOffline(f"{self.device_id}: no fresh FMI wave height from the configured buoy")
        observed = max(heights)
        return {
            **{field: readings[observed] for field, readings in series.items() if observed in readings},
            "observation_unix": observed.timestamp(),
            "latitude": self.latitude,
            "longitude": self.longitude,
        }
