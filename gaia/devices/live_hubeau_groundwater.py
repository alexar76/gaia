"""Raw, near-real-time Hub'Eau piezometer observations for vetted pilot wells."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from math import isfinite
from typing import Any
from urllib.parse import urlencode

from gaia.devices._live_base import LiveDevice, _num
from gaia.devices.base import DeviceOffline
from gaia.devices.live_p0 import signed_cluster_read
from gaia.source_policy import require_approved_source


# Coordinates checked against the official Hub'Eau station registry and
# chroniques_tr telemetry. Only mainland France stations are in this pilot.
_WELLS = {
    "BSS000ACKJ": (50.9710871049055, 2.16959880905396),
    "BSS000DWEC": (49.9553093957476, 1.99421770506152),
    "BSS000YVBN": (48.0797994706409, 6.91755623642851),
    "BSS000ZJKX": (48.0076764988087, -3.03240756095195),
    "BSS001VZGZ": (45.2297273883712, -0.749057576154961),
    "BSS001XSHT": (45.1161801997773, 6.00553876761217),
    "BSS002CXKD": (43.9823757670165, -0.155560344404009),
    "BSS003CGKO": (43.412577864045, 3.66910628988542),
}
_MAX_AGE = timedelta(hours=24)
_PAGE_SIZE = 1000


class HubEauGroundwater(LiveDevice):
    """Depth relative to the measuring reference and elevation on the NGF datum."""

    model = "GAIA-GROUNDWATER (Hub'Eau Piézométrie)"
    policy_id = "hubeau_groundwater"
    fields = {
        "groundwater_reference_depth_m": "m",
        "groundwater_level_ngf_m": "m",
        "observation_unix": "s",
        "latitude": "deg",
        "longitude": "deg",
    }
    url = "https://hubeau.eaufrance.fr/api/v1/niveaux_nappes/chroniques_tr"
    source = (
        f"{url} (Hub'Eau / BRGM Piézométrie; Etalab Open Licence 2.0. "
        "Raw, unvalidated telemetry from a mainland France pilot.)"
    )
    attribution = "BRGM / Hub'Eau Piézométrie · Licence Ouverte 2.0"

    def read(self) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        since = now - _MAX_AGE
        url = f"{self.url}?" + urlencode({
            "bss_id": ",".join(_WELLS),
            "date_debut_mesure": since.date().isoformat(),
            "size": _PAGE_SIZE,
        })
        require_approved_source(self.policy_id).require_endpoint(url)
        payload = self._fetch(url)
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise DeviceOffline(f"{self.device_id}: invalid Hub'Eau observations")
        if payload.get("next") or payload.get("count") != len(rows) or len(rows) >= _PAGE_SIZE:
            raise DeviceOffline(f"{self.device_id}: incomplete Hub'Eau observations")

        by_station: dict[str, dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            station_id = str(row.get("bss_id") or "")
            if station_id not in _WELLS:
                continue
            latitude, longitude = _WELLS[station_id]
            upstream_lat = _num(row.get("latitude"))
            upstream_lon = _num(row.get("longitude"))
            if (upstream_lat is None or upstream_lon is None
                    or not abs(latitude - upstream_lat) < 0.0001
                    or not abs(longitude - upstream_lon) < 0.0001):
                continue
            depth = _num(row.get("profondeur_nappe"))
            level = _num(row.get("niveau_eau_ngf"))
            if (depth is None or level is None or not isfinite(depth) or not isfinite(level)
                    or not -500 <= depth <= 12_000 or not -500 <= level <= 9_000):
                continue
            try:
                observed = datetime.fromisoformat(str(row.get("date_mesure") or "").replace("Z", "+00:00"))
            except ValueError:
                continue
            if observed.tzinfo is None or not since <= observed <= now:
                continue
            observed_unix = observed.timestamp()
            if station_id in by_station and by_station[station_id]["observation_unix"] >= observed_unix:
                continue
            date_updated = str(row.get("date_maj") or "")
            try:
                updated = datetime.strptime(date_updated, "%Y-%m-%d").date()
            except ValueError:
                continue
            if updated < observed.astimezone(timezone.utc).date():
                continue
            by_station[station_id] = {
                "station_id": station_id,
                "groundwater_reference_depth_m": depth,
                "groundwater_level_ngf_m": level,
                "observation_unix": observed_unix,
                "observed_at": observed.astimezone(timezone.utc).isoformat(),
                "source_updated_date": date_updated,
                "latitude": latitude,
                "longitude": longitude,
                "reference": "Depth from station measuring reference (positive downward); water elevation on NGF datum",
                "approval_status": "Raw telemetry",
            }
        if not by_station:
            raise DeviceOffline(f"{self.device_id}: no Hub'Eau pilot well measurements from the last 24 hours")
        return signed_cluster_read(
            self,
            [by_station[station_id] for station_id in sorted(by_station)],
            numeric_keys=tuple(self.fields),
            meta_keys=("station_id", "observed_at", "source_updated_date", "reference", "approval_status"),
        )
