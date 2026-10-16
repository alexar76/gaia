"""Every invoke an oracle answers, written down: the call log behind its statistics.

One row per ``POST /ai-market/v2/invoke``, whatever the outcome — served, refused by the price
gate, over a limit, failed — with the capability, the HTTP status, a short reason, who called
(a hub that signed the call, an operator-nominated paid caller, or an anonymous client), the
price of a served priced call, the latency, and the SHA-256 of the input (never the input).

* Durable: oracle_core.storage — SQLite in the data directory by default, Postgres when
  ``ORACLE_DATABASE_URL`` says so. Rows are written by a background thread in batches, so a
  slow database never slows an answer; a full queue drops rows and counts them
  (``oracle_calls_dropped_total``) instead of blocking.
* Private: the client address is kept only as a salted SHA-256 prefix (the salt lives in the
  data directory, 0600), so callers can be counted and rate abuse traced without storing IPs.
* Bounded: rows older than ``ORACLE_CALLS_RETENTION_DAYS`` (default 90) are purged daily.
* Read by the operator: ``/ops/v1/stats``, ``/ops/v1/calls`` and ``/ops/v1/metrics``
  (Prometheus text) behind ``ORACLE_OPS_TOKEN`` — see oracle_core.app.

On with ``ORACLE_CALLS=1`` (our images set it, with ``ORACLE_DATA_DIR=/data``); off by
default, so a library user or a test suite gets no files and no threads it did not ask for.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import secrets
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from oracle_core.storage import Store, open_store

log = logging.getLogger("oracle_core.calls")

OUTCOMES = ("ok", "failed", "refused", "payment_required", "unauthorized", "rate_limited",
            "invalid", "error")
CALLER_KINDS = ("hub", "paid_tier", "anonymous")
#: Latency buckets for the Prometheus histogram, in seconds.
_BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)

_DDL_SQLITE = (
    """CREATE TABLE IF NOT EXISTS oracle_calls (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts DOUBLE PRECISION NOT NULL,
        service TEXT NOT NULL,
        capability_id TEXT NOT NULL,
        outcome TEXT NOT NULL,
        http_status INTEGER NOT NULL,
        reason TEXT NOT NULL DEFAULT '',
        caller_kind TEXT NOT NULL,
        caller_hub TEXT NOT NULL DEFAULT '',
        client_hash TEXT NOT NULL DEFAULT '',
        price_usd DOUBLE PRECISION NOT NULL DEFAULT 0,
        latency_ms DOUBLE PRECISION NOT NULL DEFAULT 0,
        input_sha256 TEXT NOT NULL DEFAULT '',
        subject TEXT NOT NULL DEFAULT '')""",
)
_DDL_POSTGRES = (_DDL_SQLITE[0].replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY"),)
_DDL_INDEX = (
    "CREATE INDEX IF NOT EXISTS oracle_calls_ts ON oracle_calls (ts)",
    "CREATE INDEX IF NOT EXISTS oracle_calls_cap_ts ON oracle_calls (capability_id, ts)",
)
_COLUMNS = ("ts", "service", "capability_id", "outcome", "http_status", "reason", "caller_kind",
            "caller_hub", "client_hash", "price_usd", "latency_ms", "input_sha256", "subject")


def outcome_for(status: int, body: Any = None) -> str:
    """The outcome of an invoke from its HTTP status and, for a 200, its ``ok`` field."""
    if status == 200:
        return "ok" if not isinstance(body, dict) or body.get("ok", True) else "failed"
    return {402: "payment_required", 401: "unauthorized", 403: "refused", 429: "rate_limited",
            400: "invalid", 404: "invalid", 422: "invalid"}.get(status, "error")


def input_sha256(payload: Any) -> str:
    try:
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError):
        raw = repr(payload)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _strings(payload: Any, depth: int = 0) -> list[str]:
    if depth > 4:
        return []
    if isinstance(payload, str):
        return [payload]
    if isinstance(payload, dict):
        return [s for v in list(payload.values())[:100] for s in _strings(v, depth + 1)]
    if isinstance(payload, (list, tuple)):
        return [s for v in list(payload)[:100] for s in _strings(v, depth + 1)]
    return []


def scrub_reason(reason: str, payload: Any) -> str:
    """The reason without any text the caller sent: an error message may quote the input (a VAT
    number, an address), and the log promises never to keep the input. A quote cut short by the
    handler (``value[:60]``) is caught by its first 8 characters and removed as far as it matches."""
    out = reason or ""
    for value in sorted(set(_strings(payload)), key=len, reverse=True):
        if len(value) < 4:
            continue
        head = value[:8]
        start = out.find(head)
        while start != -1:
            end = start
            while end < len(out) and end - start < len(value) and out[end] == value[end - start]:
                end += 1
            out = out[:start] + "‹input›" + out[end:]
            start = out.find(head, start + len("‹input›"))
    return out


class CallLog:
    def __init__(self, store: Store, service: str, *, retention_days: int = 90,
                 salt: bytes | None = None, queue_max: int = 10_000, flush_s: float = 1.0):
        self.store = store
        self.service = service
        self.retention_days = retention_days
        self._salt = salt or secrets.token_bytes(16)
        self._q: queue.Queue[tuple] = queue.Queue(maxsize=queue_max)
        self._flush_s = flush_s
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.dropped = 0
        self.written = 0
        self.write_errors = 0
        # In-memory counters for /metrics, since this process started.
        self._count: dict[tuple[str, str], int] = defaultdict(int)
        self._lat_sum: dict[str, float] = defaultdict(float)
        self._lat_n: dict[str, int] = defaultdict(int)
        self._lat_hist: dict[str, list[int]] = defaultdict(lambda: [0] * (len(_BUCKETS) + 1))
        self._revenue: dict[str, float] = defaultdict(float)
        ddl = _DDL_POSTGRES if store.dialect == "postgres" else _DDL_SQLITE
        store.script(ddl + _DDL_INDEX)
        self._last_purge = 0.0
        self._thread = threading.Thread(target=self._writer, name="oracle-calls", daemon=True)
        self._thread.start()

    # ── recording ───────────────────────────────────────────────────────────

    def client_hash(self, client: str) -> str:
        if not client:
            return ""
        return hashlib.sha256(self._salt + client.encode("utf-8")).hexdigest()[:16]

    def record(self, *, capability_id: str, http_status: int, outcome: str, latency_ms: float,
               caller_kind: str = "anonymous", caller_hub: str = "", client: str = "",
               price_usd: float = 0.0, reason: str = "", input_digest: str = "",
               subject: str = "") -> None:
        cap = (capability_id or "?")[:120]
        with self._lock:
            self._count[(cap, outcome)] += 1
            self._lat_sum[cap] += latency_ms / 1000.0
            self._lat_n[cap] += 1
            hist = self._lat_hist[cap]
            for i, edge in enumerate(_BUCKETS):
                if latency_ms / 1000.0 <= edge:
                    hist[i] += 1
            hist[-1] += 1
            if outcome == "ok" and price_usd > 0:
                self._revenue[cap] += price_usd
        row = (time.time(), self.service, cap, outcome, int(http_status), (reason or "")[:240],
               caller_kind if caller_kind in CALLER_KINDS else "anonymous", (caller_hub or "")[:200],
               self.client_hash(client), float(price_usd or 0.0), round(float(latency_ms), 2),
               input_digest, (subject or "")[:120])
        try:
            self._q.put_nowait(row)
        except queue.Full:
            with self._lock:
                self.dropped += 1

    def _writer(self) -> None:
        sql = (f"INSERT INTO oracle_calls ({', '.join(_COLUMNS)}) "
               f"VALUES ({', '.join('?' for _ in _COLUMNS)})")
        while not self._stop.is_set():
            batch: list[tuple] = []
            try:
                batch.append(self._q.get(timeout=self._flush_s))
            except queue.Empty:
                pass
            while len(batch) < 500:
                try:
                    batch.append(self._q.get_nowait())
                except queue.Empty:
                    break
            if batch:
                try:
                    self.store.executemany(sql, batch)
                    self.written += len(batch)
                except Exception as exc:  # noqa: BLE001 — the log must never take the oracle down
                    self.write_errors += 1
                    with self._lock:
                        self.dropped += len(batch)
                    log.warning("call log write failed (%d rows dropped): %s", len(batch), exc)
            if time.time() - self._last_purge > 86_400:
                self._last_purge = time.time()
                try:
                    self.purge()
                except Exception as exc:  # noqa: BLE001
                    log.warning("call log purge failed: %s", exc)

    def purge(self, now: float | None = None) -> None:
        """Delete rows older than the retention (run daily by the writer)."""
        cutoff = (time.time() if now is None else now) - self.retention_days * 86_400
        self.store.execute("DELETE FROM oracle_calls WHERE ts < ?", (cutoff,))

    def flush(self, timeout_s: float = 5.0) -> None:
        """Wait until queued rows are written (tests, shutdown)."""
        deadline = time.time() + timeout_s
        while not self._q.empty() and time.time() < deadline:
            time.sleep(0.02)
        time.sleep(self._flush_s * 0.1 + 0.05)

    def close(self) -> None:
        self.flush()
        self._stop.set()

    # ── reading ─────────────────────────────────────────────────────────────

    def calls(self, *, since: float, until: float, capability: str = "", outcome: str = "",
              limit: int = 100) -> list[dict[str, Any]]:
        where, args = ["ts >= ?", "ts < ?"], [since, until]
        if capability:
            where.append("capability_id = ?")
            args.append(capability)
        if outcome:
            where.append("outcome = ?")
            args.append(outcome)
        rows = self.store.query(
            f"SELECT {', '.join(_COLUMNS)} FROM oracle_calls WHERE {' AND '.join(where)} "
            f"ORDER BY ts DESC LIMIT {max(1, min(int(limit), 1000))}", args)
        out = []
        for r in rows:
            d = dict(zip(_COLUMNS, r))
            d["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(d["ts"])))
            out.append(d)
        return out

    def stats(self, *, since: float, until: float) -> dict[str, Any]:
        rows = self.store.query(
            "SELECT capability_id, outcome, caller_kind, COUNT(*), SUM(price_usd), "
            "SUM(latency_ms), MAX(latency_ms), COUNT(DISTINCT client_hash) FROM oracle_calls "
            "WHERE ts >= ? AND ts < ? GROUP BY capability_id, outcome, caller_kind",
            (since, until))
        per: dict[str, dict[str, Any]] = {}
        kinds: dict[str, int] = defaultdict(int)
        total = 0
        for cap, outcome, kind, n, price, lat_sum, lat_max, _clients in rows:
            n = int(n)
            total += n
            kinds[kind] += n
            c = per.setdefault(cap, {"calls": 0, "outcomes": defaultdict(int), "revenue_usd": 0.0,
                                     "latency_ms_mean": 0.0, "latency_ms_max": 0.0, "_lat": 0.0})
            c["calls"] += n
            c["outcomes"][outcome] += n
            if outcome == "ok":
                c["revenue_usd"] += float(price or 0.0)
            c["_lat"] += float(lat_sum or 0.0)
            c["latency_ms_max"] = max(c["latency_ms_max"], float(lat_max or 0.0))
        for c in per.values():
            c["latency_ms_mean"] = round(c.pop("_lat") / c["calls"], 2) if c["calls"] else 0.0
            c["outcomes"] = dict(c["outcomes"])
            c["revenue_usd"] = round(c["revenue_usd"], 6)
        clients = self.store.query(
            "SELECT COUNT(DISTINCT client_hash) FROM oracle_calls WHERE ts >= ? AND ts < ? "
            "AND client_hash <> ''", (since, until))
        return {
            "service": self.service,
            "from": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(since)),
            "to": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(until)),
            "calls": total, "distinct_clients": int(clients[0][0]) if clients else 0,
            "by_caller": dict(kinds),
            "revenue_usd": round(sum(c["revenue_usd"] for c in per.values()), 6),
            "capabilities": dict(sorted(per.items(), key=lambda kv: -kv[1]["calls"])),
        }

    def prometheus(self) -> str:
        """Process counters in the Prometheus text exposition format."""
        lines = ["# HELP oracle_calls_total Invokes answered, by capability and outcome.",
                 "# TYPE oracle_calls_total counter"]
        with self._lock:
            count = dict(self._count)
            lat_sum, lat_n = dict(self._lat_sum), dict(self._lat_n)
            hist = {k: list(v) for k, v in self._lat_hist.items()}
            revenue = dict(self._revenue)
            dropped = self.dropped
        svc = _label(self.service)
        for (cap, outcome), n in sorted(count.items()):
            lines.append(f'oracle_calls_total{{service="{svc}",capability="{_label(cap)}",'
                         f'outcome="{outcome}"}} {n}')
        lines += ["# HELP oracle_call_duration_seconds Invoke latency.",
                  "# TYPE oracle_call_duration_seconds histogram"]
        for cap, h in sorted(hist.items()):
            for edge, n in zip(_BUCKETS, h):
                lines.append(f'oracle_call_duration_seconds_bucket{{service="{svc}",'
                             f'capability="{_label(cap)}",le="{edge}"}} {n}')
            lines.append(f'oracle_call_duration_seconds_bucket{{service="{svc}",'
                         f'capability="{_label(cap)}",le="+Inf"}} {h[-1]}')
            lines.append(f'oracle_call_duration_seconds_sum{{service="{svc}",'
                         f'capability="{_label(cap)}"}} {lat_sum.get(cap, 0.0):.6f}')
            lines.append(f'oracle_call_duration_seconds_count{{service="{svc}",'
                         f'capability="{_label(cap)}"}} {lat_n.get(cap, 0)}')
        lines += ["# HELP oracle_revenue_usd_total Catalogue price of served priced calls.",
                  "# TYPE oracle_revenue_usd_total counter"]
        for cap, usd in sorted(revenue.items()):
            lines.append(f'oracle_revenue_usd_total{{service="{svc}",capability="{_label(cap)}"}} {usd:.6f}')
        lines += ["# HELP oracle_calls_dropped_total Call-log rows not written (queue full or a write error).",
                  "# TYPE oracle_calls_dropped_total counter",
                  f'oracle_calls_dropped_total{{service="{svc}"}} {dropped}']
        return "\n".join(lines) + "\n"


def _label(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _salt_from(data_dir: Path) -> bytes:
    path = data_dir / "calls.salt"
    try:
        if path.is_file():
            return path.read_bytes()
        data_dir.mkdir(parents=True, exist_ok=True)
        salt = secrets.token_bytes(16)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(salt)
        return salt
    except OSError:
        return secrets.token_bytes(16)


def from_env(service: str, *, force: bool = False) -> CallLog | None:
    """The call log this process should keep: None unless ORACLE_CALLS=1 (or ``force``), or if
    it cannot open."""
    wanted = os.environ.get("ORACLE_CALLS", "").strip().lower() in ("1", "true", "yes", "on")
    if not (wanted or force):
        return None
    data_dir = Path(os.environ.get("ORACLE_DATA_DIR") or "data")
    try:
        store = open_store(data_dir=data_dir)
    except Exception as exc:  # noqa: BLE001 — an oracle without its log still serves
        log.warning("call log disabled: %s", exc)
        return None
    return CallLog(store, service,
                   retention_days=int(os.environ.get("ORACLE_CALLS_RETENTION_DAYS", "90") or 90),
                   salt=_salt_from(data_dir))


__all__ = ["CALLER_KINDS", "CallLog", "OUTCOMES", "from_env", "input_sha256", "outcome_for",
           "scrub_reason"]
