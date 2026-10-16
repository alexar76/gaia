"""Where an oracle keeps durable rows: SQLite by default, Postgres when a URL says so.

    ORACLE_DATABASE_URL=postgresql://user:pass@host:5432/db   Postgres (psycopg 3, extra "postgres")
    ORACLE_DATABASE_URL=sqlite:///path/oracle.sqlite          SQLite at that path
    (unset)                                                    SQLite at <data dir>/oracle.sqlite

One connection, serialised by a lock: an oracle writes a few rows a second at most, from one
background writer, and reads them for an operator's dashboard. SQL is written with ``?``
placeholders and translated for Postgres. A lost Postgres connection is reopened once per call.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable, Sequence
from urllib.parse import urlsplit

log = logging.getLogger("oracle_core.storage")


class StorageError(RuntimeError):
    pass


def _redact(url: str) -> str:
    parts = urlsplit(url)
    if parts.password:
        netloc = parts.netloc.replace(f":{parts.password}@", ":***@")
        return parts._replace(netloc=netloc).geturl()
    return url


class Store:
    """A small, thread-safe SQL store. ``dialect`` is ``sqlite`` or ``postgres``."""

    def __init__(self, url: str):
        self.url = url
        self._lock = threading.RLock()
        scheme = urlsplit(url).scheme
        if scheme in ("postgres", "postgresql"):
            self.dialect = "postgres"
        elif scheme == "sqlite":
            self.dialect = "sqlite"
        else:
            raise StorageError(f"unsupported database URL scheme {scheme!r} "
                               "(use postgresql://… or sqlite:///path)")
        self._conn: Any = None
        self._open()

    # ── connection ──────────────────────────────────────────────────────────

    def _open(self) -> None:
        if self.dialect == "sqlite":
            # SQLAlchemy's rule: what follows sqlite:/// is the path, so sqlite:///rel/x.db
            # is relative and sqlite:////abs/x.db is absolute.
            if not self.url.startswith("sqlite:///"):
                raise StorageError("a SQLite URL is sqlite:///relative/path or sqlite:////absolute/path")
            path = self.url[len("sqlite:///"):]
            if not path:
                raise StorageError("the SQLite URL names no file")
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(path, check_same_thread=False, timeout=30, isolation_level=None)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._conn = conn
            self.path = path
        else:
            try:
                import psycopg
            except ImportError as exc:  # pragma: no cover - depends on the install
                raise StorageError("Postgres needs psycopg: pip install 'aimarket-oracle-core[postgres]'") from exc
            self._conn = psycopg.connect(self.url, autocommit=True, connect_timeout=10)
            self.path = _redact(self.url)

    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.dialect == "postgres" else sql

    def _run(self, fn):
        with self._lock:
            try:
                return fn()
            except Exception as exc:
                if self.dialect != "postgres" or not _is_connection_error(exc):
                    raise
                log.warning("database connection lost (%s); reconnecting", type(exc).__name__)
                try:
                    self._conn.close()
                except Exception:  # noqa: BLE001
                    pass
                self._open()
                return fn()

    # ── statements ──────────────────────────────────────────────────────────

    def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        def go():
            cur = self._conn.cursor()
            cur.execute(self._sql(sql), tuple(params))
            cur.close()
        self._run(go)

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        rows = [tuple(r) for r in rows]
        if not rows:
            return

        def go():
            cur = self._conn.cursor()
            if self.dialect == "sqlite":
                cur.execute("BEGIN")
                try:
                    cur.executemany(self._sql(sql), rows)
                    cur.execute("COMMIT")
                except Exception:
                    cur.execute("ROLLBACK")
                    raise
            else:
                with self._conn.transaction():
                    cur.executemany(self._sql(sql), rows)
            cur.close()
        self._run(go)

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[tuple]:
        def go():
            cur = self._conn.cursor()
            cur.execute(self._sql(sql), tuple(params))
            rows = cur.fetchall()
            cur.close()
            return [tuple(r) for r in rows]
        return self._run(go)

    def script(self, statements: Iterable[str]) -> None:
        """Run DDL one statement at a time (``CREATE … IF NOT EXISTS``, so it is repeatable)."""
        for stmt in statements:
            self.execute(stmt)

    @property
    def autoinc(self) -> str:
        return "BIGSERIAL PRIMARY KEY" if self.dialect == "postgres" else "INTEGER PRIMARY KEY AUTOINCREMENT"

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001
                pass


def _is_connection_error(exc: Exception) -> bool:
    name = type(exc).__name__
    return name in ("OperationalError", "InterfaceError") or "closed" in str(exc).lower()


def default_url(data_dir: str | Path | None = None) -> str:
    """ORACLE_DATABASE_URL, else SQLite in the data directory (ORACLE_DATA_DIR, else ./data)."""
    url = (os.environ.get("ORACLE_DATABASE_URL") or "").strip()
    if url:
        return url
    base = Path(data_dir or os.environ.get("ORACLE_DATA_DIR") or "data")
    return f"sqlite:///{(base / 'oracle.sqlite').resolve()}"


def open_store(url: str | None = None, data_dir: str | Path | None = None) -> Store:
    return Store(url or default_url(data_dir))


__all__ = ["Store", "StorageError", "default_url", "open_store"]
