"""Live positions of Vy trains from Entur's SIRI Lite Vehicle Monitoring feed."""

from __future__ import annotations

import hashlib
import re
import threading
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from gaia.devices._live_base import LiveDevice, _num
from gaia.devices.base import DeviceOffline
from gaia.devices.live_p0 import signed_cluster_read
from gaia.devices.live_water_ext import _utc_time
from gaia.source_policy import require_approved_source

if TYPE_CHECKING:
    from gaia.clock import SimClock

_NS = {"s": "http://www.siri.org.uk/siri"}
_POSITION_AGE = timedelta(minutes=5)
_SNAPSHOT_AGE = timedelta(minutes=2)
_FETCH_INTERVAL_S = 20.0
_VEHICLE_REF = re.compile(r"[A-Za-z0-9:_-]{1,128}")
_LINE_REF = re.compile(r"VYG:Line:[A-Za-z0-9:_-]{1,64}")
_SNAPSHOT_LOCK = threading.Lock()
_SNAPSHOT_CACHE: tuple[float, tuple[str, datetime] | None] = (float("-inf"), None)


class EnturRail(LiveDevice):
    """SIRI observed coordinates and validity for Entur's VYG rail dataset."""

    model = "GAIA-RAIL (Entur Vy)"
    policy_id = "entur_rail"
    attribution = "Inneholder data under Norsk lisens for offentlige data (NLOD) tilgjengeliggjort av Entur AS."
    url = "https://api.entur.io/realtime/v1/rest/vm?datasetId=VYG"
    source = (
        f"{url} (Entur AS, Vy rail positions; Norsk lisens for offentlige data (NLOD), "
        "https://developer.entur.no/terms-of-service. Filtered by source observation time "
        "and validity, with derived identifiers and rounded coordinates.)"
    )
    headers = {"Accept": "application/xml", "ET-Client-Name": "modelmarket-gaia"}
    fields = {
        "latitude": "deg",
        "longitude": "deg",
        "observation_unix": "s",
        "rail_position_count": "count",
    }

    def __init__(self, device_id: str, clock: SimClock, **kwargs: Any) -> None:
        super().__init__(device_id, clock, **kwargs)
        require_approved_source(self.policy_id).require_endpoint(self.url)

    def _cached_snapshot(self) -> tuple[str, datetime]:
        global _SNAPSHOT_CACHE
        with _SNAPSHOT_LOCK:
            now = time.monotonic()
            attempted_at, hit = _SNAPSHOT_CACHE
            if now - attempted_at < _FETCH_INTERVAL_S:
                if hit is None:
                    raise DeviceOffline(f"{self.device_id}: Entur snapshot retry interval")
                return hit
            # Coalesce callers and space attempts even after upstream failures.
            # The official limit is four SIRI Lite requests per minute.
            _SNAPSHOT_CACHE = (now, None)
            payload = self._fetch_text(self.url, max_chars=1_000_000)
            hit = payload, datetime.now(UTC)
            _SNAPSHOT_CACHE = (now, hit)
            return hit

    def read(self) -> dict[str, Any]:
        payload, fetched_at = self._cached_snapshot()
        if "<!DOCTYPE" in payload or "<!ENTITY" in payload:
            raise DeviceOffline(f"{self.device_id}: Entur XML declarations are unsupported")
        try:
            root = ET.fromstring(payload)
        except ET.ParseError as exc:
            raise DeviceOffline(f"{self.device_id}: invalid Entur SIRI XML") from exc
        now = datetime.now(UTC)
        service = root.find("s:ServiceDelivery", _NS)
        if (
            root.tag != f"{{{_NS['s']}}}Siri" or root.get("version") != "2.0"
            or service is None
            or service.findtext("s:ProducerRef", namespaces=_NS) != "ENT"
            or service.findtext("s:MoreData", namespaces=_NS) != "false"
            or service.findtext("s:Status", namespaces=_NS) not in (None, "true")
        ):
            raise DeviceOffline(f"{self.device_id}: unexpected or incomplete Entur delivery")
        deliveries = service.findall("s:VehicleMonitoringDelivery", _NS)
        if len(deliveries) != 1 or deliveries[0].findtext("s:Status", namespaces=_NS) not in (None, "true"):
            raise DeviceOffline(f"{self.device_id}: Entur vehicle monitoring unavailable")
        delivery = deliveries[0]
        published = _utc_time(delivery.findtext("s:ResponseTimestamp", namespaces=_NS))
        if published is None or not timedelta(0) <= now - published < _SNAPSHOT_AGE:
            raise DeviceOffline(f"{self.device_id}: Entur vehicle delivery is stale or future-dated")

        latest: dict[str, tuple[datetime, ET.Element, ET.Element]] = {}
        for activity in delivery.findall("s:VehicleActivity", _NS):
            journey = activity.find("s:MonitoredVehicleJourney", _NS)
            if journey is None or (
                journey.findtext("s:DataSource", namespaces=_NS) != "VYG"
                or journey.findtext("s:VehicleMode", namespaces=_NS) != "rail"
            ):
                continue
            ref = journey.findtext("s:VehicleRef", namespaces=_NS) or ""
            recorded = _utc_time(activity.findtext("s:RecordedAtTime", namespaces=_NS))
            if not _VEHICLE_REF.fullmatch(ref) or recorded is None:
                continue
            previous = latest.get(ref)
            if previous is None or recorded > previous[0]:
                latest[ref] = recorded, activity, journey

        rows = []
        for ref, (recorded, activity, journey) in sorted(latest.items()):
            source_end = _utc_time(activity.findtext("s:ValidUntilTime", namespaces=_NS))
            if source_end is None or recorded > published:
                continue
            end = min(source_end, recorded + _POSITION_AGE)
            if now >= end:
                continue
            line = journey.findtext("s:LineRef", namespaces=_NS) or ""
            lat = _num(journey.findtext("s:VehicleLocation/s:Latitude", namespaces=_NS))
            lon = _num(journey.findtext("s:VehicleLocation/s:Longitude", namespaces=_NS))
            if (
                not _LINE_REF.fullmatch(line)
                or lat is None or not -90 <= lat <= 90
                or lon is None or not -180 <= lon <= 180
                or (lat == 0 and lon == 0)
            ):
                continue
            identity = hashlib.sha256(ref.encode()).hexdigest()[:24]
            rows.append({
                "train_number": f"entur-{identity}",
                "line_ref": line,
                "latitude": lat,
                "longitude": lon,
                "observation_unix": recorded.timestamp(),
                "observed_at": recorded.isoformat(),
                "valid_until": end.isoformat(),
                "source_valid_until": source_end.isoformat(),
            })
        return signed_cluster_read(
            self, rows, numeric_keys=("latitude", "longitude", "observation_unix"),
            meta_keys=("train_number", "line_ref", "observed_at", "valid_until", "source_valid_until"),
            reading_meta={"source_read_at": fetched_at.isoformat(), "source_published_at": published.isoformat()},
            empty_values={"rail_position_count": 0.0},
        )
