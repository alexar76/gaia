"""UK Power Networks public near-real-time distribution fault reports."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from gaia.devices._live_base import LiveDevice, _num
from gaia.devices.base import DeviceOffline
from gaia.devices.live_p0 import signed_cluster_read
from gaia.devices.live_transport_events import _utc
from gaia.source_policy import require_approved_source

_SNAPSHOT_AGE = timedelta(minutes=30)
_MAX_RECORDS = 5_000
_INCIDENT_ID = re.compile(r"INCD-[0-9]{1,12}-[A-Z]")
_ZONE_NAME = re.compile(r"[A-Za-z][A-Za-z .&'-]{0,63}")
_LONDON = ZoneInfo("Europe/London")
_ACTIVE_STATUSES = {0: "Awaiting", 1: "In progress", 2: "Emergency", 4: "Reported off supply"}
_EXPORT_FIELDS = (
    "incidentreference", "powercuttype", "creationdatetime", "planneddate", "restoreddatetime", "statusid",
    "nocustomeraffected", "estimatedrestorationdate", "operatingzone", "geopoint",
)


def _source_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not 19 <= len(value) <= 35 or "T" not in value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.year >= 2000 else None


def _has_started(value: datetime, now: datetime) -> bool:
    if value.tzinfo is not None:
        return value <= now
    # The API stores wall times as text without an offset. During UK daylight
    # saving time only assert that it started once either convention allows it.
    return value <= min(now.replace(tzinfo=None), now.astimezone(_LONDON).replace(tzinfo=None))


class UkpnLiveFaults(LiveDevice):
    """Current published outages, with deliberately coarse postcode aggregate coordinates."""

    model = "GAIA-POWER-OUTAGES (UK Power Networks)"
    retain_hotspots_in_history = False
    policy_id = "ukpn_live_faults"
    url = "https://ukpowernetworks.opendatasoft.com/api/explore/v2.1/catalog/datasets/ukpn-live-faults"
    records_url = f"{url}/exports/json?" + urlencode({"limit": _MAX_RECORDS, "select": ",".join(_EXPORT_FIELDS)})
    attribution = "UK Power Networks · CC BY 4.0"
    source = (
        f"{url} (UK Power Networks Live Faults; CC BY 4.0, https://creativecommons.org/licenses/by/4.0/. "
        "Filtered to current published faults. Postcode aggregate coordinates rounded to 0.01 degree.)"
    )
    fields = {"latitude": "deg", "longitude": "deg", "customers_affected": "count", "power_outage_count": "count",
              "source_published_unix": "s", "source_expires_unix": "s"}

    def _snapshot(self) -> tuple[datetime, list[dict[str, Any]]]:
        policy = require_approved_source(self.policy_id)
        policy.require_endpoint(self.url)
        policy.require_endpoint(self.records_url)
        for _ in range(2):
            metadata = self._fetch(self.url)
            if not isinstance(metadata, dict) or metadata.get("dataset_id") != "ukpn-live-faults":
                raise DeviceOffline(f"{self.device_id}: unexpected UKPN dataset")
            metas = metadata.get("metas")
            default = metas.get("default") if isinstance(metas, dict) else None
            if not isinstance(default, dict):
                raise DeviceOffline(f"{self.device_id}: missing UKPN snapshot metadata")
            published = _utc(default.get("data_processed"))
            count = default.get("records_count")
            if type(count) is not int or not 0 <= count <= _MAX_RECORDS:
                raise DeviceOffline(f"{self.device_id}: missing or oversized UKPN record count")
            now = datetime.now(UTC)
            if published is None or not published <= now < published + _SNAPSHOT_AGE:
                raise DeviceOffline(f"{self.device_id}: stale or future UKPN snapshot")
            records = self._fetch(self.records_url)
            if not isinstance(records, list) or any(not isinstance(row, dict) for row in records):
                raise DeviceOffline(f"{self.device_id}: malformed UKPN export")
            if len(records) == count:
                return published, records
            # The publisher can update between the metadata and export calls.
        raise DeviceOffline(f"{self.device_id}: UKPN export count does not match snapshot metadata")

    def read(self) -> dict[str, Any]:
        published, records = self._snapshot()
        now = datetime.now(UTC)
        expires = published + _SNAPSHOT_AGE
        if not published <= now < expires:
            raise DeviceOffline(f"{self.device_id}: UKPN snapshot expired before use")
        source_times = {"source_published_at": published.isoformat(), "source_expires_at": expires.isoformat()}
        rows = []
        seen: set[str] = set()
        for record in records:
            ref = record.get("incidentreference")
            if not isinstance(ref, str) or not ref or ref in seen:
                raise DeviceOffline(f"{self.device_id}: missing or duplicate UKPN incident identity")
            seen.add(ref)
            kind = record.get("powercuttype")
            if kind not in ("Unplanned", "Planned") or record.get("restoreddatetime") not in (None, ""):
                continue
            if not _INCIDENT_ID.fullmatch(ref):
                raise DeviceOffline(f"{self.device_id}: unexpected UKPN fault identity")
            code = record.get("statusid")
            status = _ACTIVE_STATUSES.get(code) if type(code) is int else None
            if status is None or (kind == "Planned" and code not in (1, 2)):
                continue
            start = _source_datetime(record.get("planneddate" if kind == "Planned" else "creationdatetime"))
            if start is None or not _has_started(start, published):
                continue
            point = record.get("geopoint")
            if not isinstance(point, dict):
                continue
            lat, lon = _num(point.get("lat")), _num(point.get("lon"))
            if lat is None or lon is None or not (50 <= lat <= 54 and -2 <= lon <= 3):
                continue
            zone = record.get("operatingzone")
            zone = zone.strip() if isinstance(zone, str) and _ZONE_NAME.fullmatch(zone.strip()) else None
            count = record.get("nocustomeraffected")
            count = count if type(count) is int and 0 <= count <= 10_000_000 else None
            estimate = _source_datetime(record.get("estimatedrestorationdate"))
            rows.append({
                **source_times,
                "incident_id": ref, "title": f"{kind} power outage" + (f" · {zone}" if zone else ""),
                "outage_type": kind, "incident_status": status, "operating_zone": zone,
                "latitude": round(lat, 2), "longitude": round(lon, 2), "customers_affected": count,
                "estimated_restoration_at": estimate.astimezone(UTC).isoformat() if estimate and estimate.tzinfo else None,
                "estimated_restoration_local": estimate.isoformat() if estimate and estimate.tzinfo is None else None,
                "valid_until": expires.isoformat(),
            })
        rows.sort(key=lambda row: row["incident_id"])
        if len(json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode()) > 950_000:
            raise DeviceOffline(f"{self.device_id}: UKPN outages exceed the response budget")
        return signed_cluster_read(
            self, rows, numeric_keys=tuple(self.fields),
            meta_keys=("incident_id", "title", "outage_type", "incident_status", "operating_zone", "valid_until",
                       "estimated_restoration_at", "estimated_restoration_local", *source_times),
            reading_meta={**source_times, "source_read_at": now.isoformat()},
            empty_values={"power_outage_count": 0.0, "source_published_unix": published.timestamp(),
                          "source_expires_unix": expires.timestamp()},
        )
