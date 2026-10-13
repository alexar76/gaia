"""Retained hotspot rows are stored compactly — and must round-trip exactly.

The leak that took GAIA's backend to 3.04 GB in four days and the oracle host into swap was two
`HotspotSession` lists of ~142 000 rows each. The first attempt at a fix was to keep fewer rows; that
was the wrong trade, because a global fire day genuinely carries hundreds of thousands of hotspots and
silently retaining a fraction would make the product lie about the world.

The second attempt pooled the repeated strings. Correct, but insufficient — measured on the same
interpreter, rows still cost 520 B each, because the per-row **dict** dominates, not the strings.

So the rows are stored as a shared field tuple plus one value tuple each: 208 B/row, a 2.5x saving,
with every field preserved. These tests exist to prove the second half of that sentence — a
compaction that loses or invents a field would be worse than the leak.
"""

from __future__ import annotations

import tracemalloc

import pytest

from gaia.devices import hotspot_pages as hp


@pytest.fixture
def store():
    return hp.HotspotPageStore(ttl_s=600.0, max_sessions=8, secret=b"k" * 32)


def _put(store, rows):
    return store.put(device_id="fire-01", hotspots=rows, values={"frp": 1.0})


def _page(store, fid, size=2000):
    return store.page(fetch_id=fid, cursor=None, page_size=size)


def test_a_page_round_trips_every_field_exactly(store):
    rows = [{"latitude": 1.5, "longitude": 2.5, "brightness_k": 301.0,
             "satellite": "N", "observed_at": "2026-08-27T12:00:00Z", "frp_mw": 4.25}
            for _ in range(10)]
    fid = _put(store, rows)
    got = _page(store, fid)["hotspots"]
    assert got == rows, "compaction must be invisible to the caller"


def test_rows_with_different_keys_survive_without_gaining_nulls(store):
    """FIRMS omits fields per row (no `daynight`, no `version`, …). A compaction that filled the gaps
    with `None` would invent data the feed never sent."""
    rows = [
        {"latitude": 1.0, "satellite": "N"},
        {"latitude": 2.0, "version": "2.0NRT", "frp_mw": 3.0},
        {"longitude": 9.0},
    ]
    fid = _put(store, rows)
    got = _page(store, fid)["hotspots"]
    assert got == rows
    assert "satellite" not in got[1] and "None" not in repr(got)


def test_a_genuine_none_is_kept_and_not_confused_with_absence(store):
    """`_ABSENT` is a distinct sentinel precisely so a real `None` survives."""
    rows = [{"latitude": 1.0, "confidence": None}]
    fid = _put(store, rows)
    got = _page(store, fid)["hotspots"]
    assert got == rows and got[0]["confidence"] is None


def test_paging_still_walks_the_whole_set(store):
    rows = [{"latitude": float(i), "satellite": "N"} for i in range(1_000)]
    fid = _put(store, rows)
    seen: list[dict] = []
    cursor = None
    for _ in range(20):
        page = store.page(fetch_id=fid, cursor=cursor, page_size=250)
        seen.extend(page["hotspots"])
        cursor = page.get("next_cursor")
        if not cursor:
            break
    assert seen == rows, "every row must be reachable by paging, in order"


def test_no_row_is_dropped_on_a_large_day(store):
    rows = [{"latitude": float(i), "longitude": 2.0, "satellite": "N"} for i in range(50_000)]
    fid = _put(store, rows)
    assert len(store.get(fid).hotspots) == 50_000
    assert store.get(fid).hotspot_matched == 50_000


def test_the_saving_is_measured_not_asserted():
    """The point of the exercise. Pooled strings alone left rows at ~520 B; tuples take ~208 B."""
    n = 20_000
    shared = {"satellite": "N", "instrument": "VIIRS", "daynight": "D", "version": "2.0NRT",
              "acq_date": "2026-08-27", "observed_at": "2026-08-27T12:00:00Z"}
    rows = [{"latitude": 1.0 + i, "longitude": 2.0, "brightness_k": 300.0,
             "confidence": 50.0, "frp_mw": 1.5, **shared} for i in range(n)]

    tracemalloc.start()
    as_dicts = [dict(r) for r in rows]
    dict_cost, _ = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    del as_dicts

    tracemalloc.start()
    fields, compact = hp._compact(rows)
    tuple_cost, _ = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert len(compact) == n and len(fields) == 11
    assert tuple_cost < dict_cost * 0.6, (
        f"{tuple_cost / n:.0f} B/row compact vs {dict_cost / n:.0f} B/row as dicts — "
        "the compaction is not paying for itself")
