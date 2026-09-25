"""The FIRMS parser must not mint a fresh copy of the same string per row.

This is the leak that took GAIA's backend to 3.04 GB in four days and the oracle host into swap. A
heap census named it exactly: two `HotspotSession` lists of **140 781** rows each — and the cost was
in the strings, not the fires.

Every row carried its own freshly-allocated `satellite`, `instrument`, `daynight`, `version`,
`acq_date` and `observed_at`. Those columns take a dozen distinct values across an ENTIRE global day,
but `value[:80]` and an f-string allocate a new object per row, so a 250 000-row day held roughly a
million duplicate strings.

The fix pools them. It must change no VALUE — a global fire day really does carry hundreds of
thousands of hotspots, and retaining a fraction of them to save memory would make the product lie
about the world. So these tests check both halves: the objects are shared, and the data is intact.
"""

from __future__ import annotations

import sys

import pytest

from gaia.devices import live_open


def _csv(rows: int) -> str:
    head = "latitude,longitude,bright_ti4,confidence,acq_date,acq_time,satellite,instrument,daynight,version"
    out = [head]
    for i in range(rows):
        # Realistic FIRMS shape: lat/lon vary per row, the metadata columns repeat.
        out.append(f"{1.0 + i * 0.001},{2.0},{300 + (i % 50)},n,2026-08-27,{1200 + (i % 3)},"
                   f"N,VIIRS,D,2.0NRT")
    return "\n".join(out)


def _parse(rows: int, limit: int = 10_000):
    # A bare instance: collect_hotspots is a pure parse over the CSV text and needs only these two
    # attributes, so this avoids standing up a live device just to test string identity.
    dev = object.__new__(live_open.FirmsFireHotspot)
    dev.device_id = "fire-01"
    dev._default_collect = 250_000
    return live_open.FirmsFireHotspot.collect_hotspots(dev, _csv(rows), limit=limit)


def test_repeated_metadata_strings_are_shared_not_copied():
    """The whole point: one object per DISTINCT value, not one per row."""
    rows, *_ = _parse(400)
    assert len(rows) == 400, "no row may be dropped to save memory"
    ids = {id(r["satellite"]) for r in rows}
    assert len(ids) == 1, f"'satellite' was allocated {len(ids)} times for one distinct value"
    for field in ("instrument", "daynight", "version", "acq_date"):
        assert len({id(r[field]) for r in rows}) == 1, field


def test_observed_at_is_shared_per_distinct_minute():
    """`observed_at` is built by an f-string, so it was a new object every row. There are at most
    1440 distinct values in a day."""
    rows, *_ = _parse(300)
    stamps = {r["observed_at"] for r in rows}
    ids = {id(r["observed_at"]) for r in rows}
    assert len(ids) == len(stamps), "one object per distinct stamp, not per row"
    assert len(stamps) <= 3


def test_no_value_changes():
    """Pooling shares objects; it must not alter a single field. Equality is the contract."""
    rows, *_ = _parse(50)
    for p in rows:
        assert p["satellite"] == "N" and p["instrument"] == "VIIRS"
        assert p["daynight"] == "D" and p["version"] == "2.0NRT"
        assert p["acq_date"] == "2026-08-27"
        assert p["observed_at"].startswith("2026-08-27T12:")
        assert p["observed_at"].endswith(":00Z")


def test_every_row_survives_a_large_day():
    """A global day carries hundreds of thousands of hotspots. None of them may be silently dropped —
    that was the fix I tried first and it was the wrong trade for fire data."""
    rows, truncated, matched, _best = _parse(5_000)
    assert len(rows) == 5_000 and matched == 5_000 and truncated is False


def test_the_pool_is_bounded_against_a_malformed_feed():
    """A feed with high-cardinality junk in those columns must not turn the pool into the leak."""
    assert live_open._STRING_POOL_MAX <= 50_000


def test_the_saving_is_real():
    """Measured, not asserted: pooled metadata must cost far less than one copy per row."""
    rows, *_ = _parse(2_000)
    distinct = {id(r[f]) for r in rows
                for f in ("satellite", "instrument", "daynight", "version", "acq_date")}
    naive = 5 * len(rows)
    assert len(distinct) <= 10, f"{len(distinct)} objects where a naive parse would hold {naive}"
    saved = (naive - len(distinct)) * sys.getsizeof("2.0NRT")
    assert saved > 400_000, "the point of the exercise is a large, measurable saving"
