"""Validated hourly ambient gamma dose observations from Germany's BfS."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import re
from typing import Any
from urllib.parse import urlencode

from gaia.devices._live_base import LiveDevice, _num
from gaia.devices.base import DeviceOffline
from gaia.devices.live_p0 import signed_cluster_read
from gaia.devices.live_transport_events import _utc
from gaia.source_policy import require_approved_source

_MAX_AGE = timedelta(hours=2)
_MAX_FEATURES = 2500
_MAX_RESPONSE_BYTES = 2_000_000
_STATION_ID = re.compile(r"[0-9]{9}")
_USV_TO_NSV = 1000


class BfsGammaOdl(LiveDevice):
    """One signed cluster of fresh, validated BfS monitoring stations."""

    model = "GAIA-RADIATION (BfS Gamma ODL)"
    retain_hotspots_in_history = False
    policy_id = "bfs_gamma_odl"
    fields = {
        "dose_equivalent_nsv_h": "nSv/h",
        "observation_unix": "s",
        "latitude": "deg",
        "longitude": "deg",
    }
    url = "https://www.imis.bfs.de/ogc/opendata/ows?" + urlencode({
        "service": "WFS", "version": "2.0.0", "request": "GetFeature",
        "typeNames": "opendata:odlinfo_odl_1h_latest",
        "outputFormat": "application/json", "srsName": "EPSG:4326",
    })
    source = (
        f"{url} (Bundesamt für Strahlenschutz, BfS Gamma ODL hourly validated observations; "
        "Datenlizenz Deutschland – Namensnennung 2.0. Dose rate converted from µSv/h to nSv/h; "
        "coordinates rounded. Licence: https://www.govdata.de/dl-de/by-2-0.)"
    )

    def read(self) -> dict[str, Any]:
        require_approved_source(self.policy_id).require_endpoint(self.url)
        raw = self._fetch_bytes(self.url, max_bytes=_MAX_RESPONSE_BYTES)
        try:
            payload = json.loads(raw)
        except (UnicodeError, ValueError) as exc:
            raise DeviceOffline(f"{self.device_id}: invalid BfS WFS JSON") from exc
        features = payload.get("features") if isinstance(payload, dict) else None
        if (not isinstance(features, list) or payload.get("type") != "FeatureCollection"
                or payload.get("totalFeatures") != len(features) or len(features) > _MAX_FEATURES):
            raise DeviceOffline(f"{self.device_id}: invalid or incomplete BfS feature collection")

        now = datetime.now(UTC)
        by_station: dict[str, dict[str, Any]] = {}
        for feature in features:
            row = self._observation(feature, now)
            if row is None:
                continue
            station = row["station_id"]
            previous = by_station.get(station)
            if previous is not None:
                if row["observation_unix"] < previous["observation_unix"]:
                    continue
                if row["observation_unix"] == previous["observation_unix"] and row != previous:
                    raise DeviceOffline(f"{self.device_id}: conflicting BfS observations for station {station}")
            by_station[station] = row
        if not by_station:
            raise DeviceOffline(f"{self.device_id}: no validated BfS observations from the last 2 hours")

        return signed_cluster_read(
            self, sorted(by_station.values(), key=lambda row: (-row["observation_unix"], row["station_id"])),
            numeric_keys=tuple(self.fields), meta_keys=("station_id", "station_name", "observed_at"),
        )

    @staticmethod
    def _observation(feature: Any, now: datetime) -> dict[str, Any] | None:
        if not isinstance(feature, dict) or feature.get("type") != "Feature":
            return None
        props, geom = feature.get("properties"), feature.get("geometry")
        if not isinstance(props, dict) or not isinstance(geom, dict) or geom.get("type") != "Point":
            return None
        station = props.get("kenn")
        status, validated = props.get("site_status"), props.get("validated")
        if (not isinstance(station, str) or not _STATION_ID.fullmatch(station)
                or type(status) is not int or status != 1 or type(validated) is not int or validated != 1
                or props.get("duration") != "1h" or props.get("nuclide") != "Gamma-ODL-Brutto"
                or props.get("unit") != "µSv/h"):
            return None

        coords = geom.get("coordinates")
        if not isinstance(coords, list) or len(coords) != 2:
            return None
        lon, lat = map(_num, coords)
        dose = _num(props.get("value"))
        if (lon is None or not -180 <= lon <= 180 or lat is None or not -90 <= lat <= 90
                or dose is None or not 0 <= dose <= 10_000):
            return None

        start, end = _utc(props.get("start_measure")), _utc(props.get("end_measure"))
        if (start is None or end is None or end - start != timedelta(hours=1)
                or not timedelta(0) <= now - end <= _MAX_AGE):
            return None
        label = props.get("name")
        return {
            "station_id": station,
            "station_name": label[:120] if isinstance(label, str) else None,
            "dose_equivalent_nsv_h": dose * _USV_TO_NSV,
            "observation_unix": end.timestamp(),
            "observed_at": end.isoformat(),
            "latitude": lat,
            "longitude": lon,
        }
