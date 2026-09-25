"""The leak hunter's own tests — an exact store census, arrived at by elimination.

GAIA's backend grew ~750 MB/day on the oracle host: half a megabyte a minute, invisible in a short
sample and not findable by reading the code, because every obvious store here is already bounded.

Two approaches were tried and rejected first, and the second rejection is what these tests exist to
protect:

* `tracemalloc` never returned inside 75 seconds from an HTTP handler on a warm process, at three
  frames as well as eight, and taxed every allocation 1.5-2x meanwhile.
* a `gc.get_objects()` type histogram is **blind to this leak**: CPython untracks dicts of atomic
  values, so `gc.is_tracked({"leak": 1})` is False and creating 5000 such dicts moved the reported
  `dict` count by four. Sensor readings ARE dicts of scalars.

So the census measures GAIA's own containers by name, and reports which of three very different
things is happening.
"""

from __future__ import annotations

import pytest

from gaia import memtrace


class FakeDevice:
    def __init__(self, device_id: str) -> None:
        self.device_id = device_id
        self._cache: dict[str, int] = {}
        self._tiny: dict[str, int] = {"a": 1}          # below the interesting threshold
        self.name = "a plain string, not a container"

    def read(self):                                     # callable: must be skipped
        return {}

    @property
    def explodes(self):                                 # a property that raises must not break it
        raise RuntimeError("boom")


class FakeFleet:
    def __init__(self) -> None:
        self._devices = {d.device_id: d for d in (FakeDevice("dev-a"), FakeDevice("dev-b"))}
        self._history = {"dev-a": [], "dev-b": []}

    def devices(self):
        return list(self._devices.values())


class FakeRuntime:
    def __init__(self) -> None:
        self.fleet = FakeFleet()
        self.service = type("Svc", (), {"_traces": {}, "verifier": None})()


@pytest.fixture(autouse=True)
def _clean():
    memtrace._series.clear()
    yield
    memtrace._series.clear()


def test_the_first_call_is_only_a_baseline():
    """One census cannot show growth, and reporting zero would read as "healthy"."""
    out = memtrace.report(FakeRuntime())
    assert out["growth"] == [] and "baseline taken" in out["note"]
    assert out["allocated_blocks"] > 0 and out["samples_held"] == 1


def test_it_names_the_exact_container_that_grew():
    """The whole point: not "dict grew" but "THIS store grew"."""
    rt = FakeRuntime()
    memtrace.report(rt)
    dev = rt.fleet.devices()[0]
    # Dicts of scalars — the shape gc.get_objects() cannot see.
    dev._cache.update({f"k{i}": i for i in range(5000)})
    out = memtrace.report(rt)
    top = out["growth"][0]
    assert top["store"] == "device:dev-a._cache" and top["growth"] == 5000
    assert "device:dev-a._cache" in out["verdict"]
    assert "per_hour" in top


def test_history_readings_are_summed_across_devices():
    """A per-device deque can be bounded while the total is not, so both are reported."""
    rt = FakeRuntime()
    memtrace.report(rt)
    rt.fleet._history["dev-a"].extend(range(300))
    rt.fleet._history["dev-b"].extend(range(200))
    out = memtrace.report(rt)
    grown = {g["store"]: g["growth"] for g in out["growth"]}
    assert grown["fleet._history.readings_total"] == 500
    assert grown["fleet._history.readings_max"] == 300


def test_tiny_containers_and_strings_are_not_listed():
    """A fleet holds hundreds of small dicts; listing them buries the one that matters. And a long
    string is not an accumulating container."""
    out = memtrace.report(FakeRuntime())
    listed = set(out["largest_stores"])
    assert not any(k.endswith("._tiny") for k in listed)
    assert not any(k.endswith(".name") for k in listed)


def test_a_property_that_raises_does_not_break_the_census():
    out = memtrace.report(FakeRuntime())
    assert "largest_stores" in out          # FakeDevice.explodes raised and was skipped


def test_python_level_growth_with_no_store_growth_is_distinguished(monkeypatch):
    """Sends you to look inside a library rather than at GAIA's own structures."""
    rt = FakeRuntime()
    memtrace.report(rt)
    memtrace._series[0]["allocated_blocks"] -= 500_000
    out = memtrace.report(rt)
    assert out["allocated_blocks_growth"] >= 500_000
    assert "attribute walk does not reach" in out["verdict"], out["verdict"]


def test_rss_growth_without_python_growth_is_distinguished(monkeypatch):
    """The verdict that saves a day of grepping this codebase: the leak is not in Python at all."""
    rt = FakeRuntime()
    memtrace.report(rt)
    base = memtrace._series[0]
    base["rss_mb"] = 100.0
    monkeypatch.setattr(memtrace, "_rss_mb", lambda: 400.0)
    out = memtrace.report(rt)
    assert out["rss_growth_mb"] == 300.0
    assert "NOT a Python-level leak" in out["verdict"]


def test_the_series_is_bounded_and_the_baseline_does_not_slide(monkeypatch):
    monkeypatch.setattr(memtrace, "_SERIES_MAX", 3)
    rt = FakeRuntime()
    memtrace.report(rt)
    first_ts = memtrace._series[0]["ts"]
    for _ in range(10):
        memtrace.report(rt)
    assert len(memtrace._series) == 3
    assert memtrace._series[0]["ts"] != first_ts, "with a bounded series the baseline must roll"


def test_no_instrumentation_is_needed():
    assert memtrace.start() is False and memtrace.enabled() is True


def test_it_works_without_a_runtime():
    """The endpoint must still answer if the runtime is unavailable — allocated blocks and RSS alone
    already say whether anything is growing."""
    out = memtrace.report(None)
    assert out["largest_stores"] == {} and out["allocated_blocks"] > 0


def test_a_tiny_store_gain_does_not_explain_a_huge_allocation_count():
    """The false positive the first real measurement produced: `fleet._history.readings_total` gained
    1 257 entries (it is bounded at 147x512) while 4 350 000 Python allocations appeared. Naming the
    bounded history as the cause sends the reader to exactly the wrong place."""
    rt = FakeRuntime()
    memtrace.report(rt)
    base = memtrace._series[0]
    base["allocated_blocks"] -= 4_350_000            # pretend four million blocks appeared
    rt.fleet._history["dev-a"].extend(range(1257))   # ...alongside a small, bounded gain
    out = memtrace.report(rt)
    assert "held by a reference the attribute walk does not reach" in out["verdict"], out["verdict"]
    # Quote the ACTUAL figure rather than a hardcoded one: real allocations happen during the test.
    assert f"{out['allocated_blocks_growth']:,}" in out["verdict"], out["verdict"]
    assert out["allocated_blocks_growth"] > 4_000_000


def test_a_store_gain_that_does_explain_the_growth_is_named():
    """The other direction still has to work: when the numbers are the same order of magnitude, the
    store IS the answer and should be named."""
    rt = FakeRuntime()
    memtrace.report(rt)
    rt.fleet.devices()[0]._cache.update({f"k{i}": i for i in range(5000)})
    out = memtrace.report(rt)
    assert "a GAIA container is growing" in out["verdict"]
    assert "device:dev-a._cache" in out["verdict"]


def test_big_containers_finds_an_accumulation_nobody_holds_a_named_reference_to():
    """The attribute walk can only see what the runtime holds directly. This asks the heap."""
    hoard = [[i] for i in range(memtrace._BIG_CONTAINER + 50)]   # a tracked list of lists
    found = memtrace.big_containers()
    assert any(c["len"] >= memtrace._BIG_CONTAINER for c in found), found[:3]
    assert all("type" in c and "held_by" in c for c in found)
    assert len(hoard) > memtrace._BIG_CONTAINER


def test_big_containers_skips_long_strings():
    """A long string is not an accumulating container, and listing one buries the real suspects."""
    blob = "x" * (memtrace._BIG_CONTAINER * 4)
    assert not any(c["type"] == "str" for c in memtrace.big_containers())
    assert len(blob) > 0


def test_a_hostile_len_does_not_break_the_scan():
    """Found live: pydantic's `_mock_val_ser` raises PydanticUserError from its own `__len__`, which
    a narrow `except (TypeError, AttributeError)` walked straight past and turned this endpoint into
    a 500. When you introspect objects you did not write, the only safe net is a wide one."""

    class Hostile:
        def __len__(self):
            raise RuntimeError("this object refuses to be measured")

    class Liar:
        def __len__(self):
            return "not an int"          # type: ignore[return-value]

    keep = [Hostile(), Liar()]
    out = memtrace.big_containers()      # must not raise
    assert isinstance(out, list)
    assert len(keep) == 2


def test_referrers_are_only_asked_about_what_gets_reported(monkeypatch):
    """`gc.get_referrers` walks the WHOLE heap per call. Asking it once per candidate instead of once
    per reported row turns a diagnostic into a stall."""
    calls = {"n": 0}
    real = memtrace.gc.get_referrers

    def counting(*args):
        calls["n"] += 1
        return real(*args)

    monkeypatch.setattr(memtrace.gc, "get_referrers", counting)
    hoard = [[[i] for i in range(memtrace._BIG_CONTAINER + 5)] for _ in range(4)]
    out = memtrace.big_containers(limit=3)
    assert calls["n"] <= 3, f"asked get_referrers {calls['n']} times for at most 3 reported rows"
    assert len(out) <= 3 and len(hoard) == 4
