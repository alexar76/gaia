"""Resumable hotspot packet delivery for large FIRMS (and similar) clusters.

One upstream fetch → ranked list cached under a short-lived session → client
pulls fixed-size pages via opaque ``cursor``. Retrying the **same** cursor is
idempotent (safe after timeout/429); advancing requires ``next_cursor``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

# Packet size: Hub buyers default 500; map clients may request up to 2000.
DEFAULT_PAGE_SIZE = 500
MAX_PAGE_SIZE = 2000
# Keep every ranked row we scan from the FIRMS day CSV (see ``_CSV_SCAN_CAP``).
DEFAULT_COLLECT_MAX = 250_000
MAX_COLLECT_TOTAL = 250_000
SESSION_TTL_S = 600.0
MAX_SESSIONS = 64
# Global row budget across all sessions — 64 × 250k-row sessions is multiple GB
# of dicts for ~$0.13 of invokes; evict LRU sessions past this ceiling.
MAX_TOTAL_ROWS = 500_000


def clamp_page_size(raw: Any, default: int = DEFAULT_PAGE_SIZE) -> int:
    try:
        n = int(raw) if raw is not None else default
    except (TypeError, ValueError):
        n = default
    return max(1, min(n, MAX_PAGE_SIZE))


def clamp_collect_total(raw: Any, default: int = DEFAULT_COLLECT_MAX) -> int:
    try:
        n = int(raw) if raw is not None else default
    except (TypeError, ValueError):
        n = default
    return max(1, min(n, MAX_COLLECT_TOTAL))


class CursorError(ValueError):
    """Invalid, forged, or expired continuation cursor."""


@dataclass
class HotspotSession:
    fetch_id: str
    device_id: str
    # Rows carry numeric measurements plus a small allowlisted provenance
    # envelope (observation time, satellite/event id, source status, ...).
    hotspots: list[dict[str, Any]]
    values: dict[str, float]
    created_at: float = field(default_factory=time.monotonic)
    bbox: tuple[float, float, float, float] | None = None
    truncated: bool = False
    # Rows matching filters before collect-cap (honest total when truncated).
    hotspot_matched: int = 0
    source: str = ""
    model: str = ""
    site: str = ""
    firmware: str = "1.0.0"
    # Page size the session was opened with — resumes default to it.
    page_size: int = DEFAULT_PAGE_SIZE
    # Refreshed on every page access so an active drain never expires mid-way.
    last_access: float = field(default_factory=time.monotonic)


class HotspotPageStore:
    """Process-local TTL cache of ranked hotspot lists for packetized delivery."""

    def __init__(
        self,
        *,
        ttl_s: float = SESSION_TTL_S,
        max_sessions: int = MAX_SESSIONS,
        secret: bytes | None = None,
    ) -> None:
        self.ttl_s = float(ttl_s)
        self.max_sessions = int(max_sessions)
        env_secret = (os.environ.get("GAIA_HOTSPOT_CURSOR_SECRET") or "").strip()
        if secret is not None:
            self._secret = secret
        elif env_secret:
            self._secret = env_secret.encode("utf-8")
        else:
            self._secret = secrets.token_bytes(32)
        self._lock = threading.Lock()
        self._sessions: dict[str, HotspotSession] = {}

    def _purge_locked(self, now: float) -> None:
        dead = [
            fid
            for fid, s in self._sessions.items()
            if (now - s.last_access) > self.ttl_s
        ]
        for fid in dead:
            self._sessions.pop(fid, None)
        while len(self._sessions) > self.max_sessions:
            oldest = min(self._sessions.items(), key=lambda kv: kv[1].last_access)
            self._sessions.pop(oldest[0], None)
        # Row budget: cheap invokes must not pin gigabytes of parsed CSV.
        total_rows = sum(len(s.hotspots) for s in self._sessions.values())
        while total_rows > MAX_TOTAL_ROWS and len(self._sessions) > 1:
            fid, oldest = min(self._sessions.items(), key=lambda kv: kv[1].last_access)
            total_rows -= len(oldest.hotspots)
            self._sessions.pop(fid, None)

    def put(
        self,
        *,
        device_id: str,
        hotspots: list[dict[str, Any]],
        values: dict[str, float],
        bbox: tuple[float, float, float, float] | None = None,
        truncated: bool = False,
        hotspot_matched: int = 0,
        source: str = "",
        model: str = "",
        site: str = "",
        firmware: str = "1.0.0",
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> str:
        now = time.monotonic()
        fetch_id = uuid.uuid4().hex
        matched = int(hotspot_matched) if hotspot_matched else len(hotspots)
        session = HotspotSession(
            fetch_id=fetch_id,
            device_id=device_id,
            hotspots=list(hotspots),
            values=dict(values),
            created_at=now,
            bbox=bbox,
            truncated=truncated,
            hotspot_matched=matched,
            source=source,
            model=model,
            site=site,
            firmware=firmware,
            page_size=clamp_page_size(page_size),
            last_access=now,
        )
        with self._lock:
            self._purge_locked(now)
            self._sessions[fetch_id] = session
        return fetch_id

    def get(self, fetch_id: str) -> HotspotSession:
        now = time.monotonic()
        with self._lock:
            self._purge_locked(now)
            session = self._sessions.get(fetch_id)
            if session is None:
                raise CursorError("cursor expired or unknown — restart without cursor")
            session.last_access = now
            return session

    def sign_cursor(self, fetch_id: str, offset: int) -> str:
        payload = {"v": 1, "id": fetch_id, "off": int(offset)}
        raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        body = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        sig = hmac.new(self._secret, body.encode("ascii"), hashlib.sha256).hexdigest()[:22]
        return f"{body}.{sig}"

    def parse_cursor(self, cursor: str) -> tuple[str, int]:
        text = str(cursor or "").strip()
        if not text or "." not in text:
            raise CursorError("malformed cursor")
        body, sig = text.rsplit(".", 1)
        expect = hmac.new(self._secret, body.encode("ascii"), hashlib.sha256).hexdigest()[:22]
        if not hmac.compare_digest(expect, sig):
            raise CursorError("cursor signature invalid")
        pad = "=" * (-len(body) % 4)
        try:
            raw = base64.urlsafe_b64decode(body + pad)
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise CursorError("malformed cursor payload") from exc
        if not isinstance(payload, dict) or payload.get("v") != 1:
            raise CursorError("unsupported cursor version")
        fetch_id = str(payload.get("id") or "")
        try:
            offset = int(payload.get("off"))
        except (TypeError, ValueError) as exc:
            raise CursorError("cursor offset invalid") from exc
        if not fetch_id or offset < 0:
            raise CursorError("cursor fields invalid")
        return fetch_id, offset

    def page(
        self,
        *,
        cursor: str | None = None,
        fetch_id: str | None = None,
        offset: int = 0,
        page_size: int | None = None,
        device_id: str | None = None,
    ) -> dict[str, Any]:
        """Return one packet. Pass either ``cursor`` or ``fetch_id``+``offset``.

        ``page_size=None`` keeps the page size the session was opened with.
        """
        if cursor:
            fetch_id, offset = self.parse_cursor(cursor)
        if not fetch_id:
            raise CursorError("fetch_id or cursor required")
        session = self.get(fetch_id)
        page_size = clamp_page_size(
            page_size if page_size is not None else session.page_size
        )
        if device_id and session.device_id != device_id:
            raise CursorError("cursor device_id mismatch")
        total = len(session.hotspots)
        if offset > total:
            raise CursorError("cursor offset past end — restart without cursor")
        chunk = session.hotspots[offset : offset + page_size]
        next_off = offset + len(chunk)
        next_cursor = (
            self.sign_cursor(fetch_id, next_off) if next_off < total and chunk else None
        )
        return {
            "fetch_id": fetch_id,
            "device_id": session.device_id,
            "values": dict(session.values),
            "hotspots": chunk,
            "hotspot_count": len(chunk),
            "hotspot_total": total,
            "hotspot_offset": offset,
            "hotspot_page_size": page_size,
            "next_cursor": next_cursor,
            "truncated": session.truncated,
            "hotspot_matched": session.hotspot_matched,
            "model": session.model,
            "site": session.site,
            "firmware": session.firmware,
            "source": session.source,
            "bbox": session.bbox,
        }


# Process singleton — cursor secret stable for the process lifetime.
STORE = HotspotPageStore()
