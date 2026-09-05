"""Finding a slow leak in a live service — an exact store census, arrived at by elimination.

GAIA's backend reached **3.04 GB** on the oracle host after four days and fell to 95 MB on restart:
about 750 MB a day, half a megabyte a minute. Too slow to see in a short sample, and not findable by
reading the code, because every obvious store here is already bounded (``Fleet._history`` is a
``deque(maxlen=512)``, ``VerifierService._traces`` is capped and evicted, the hotspot sessions are
capped by count and by row total).

Two earlier attempts are recorded here because each failed for a reason worth not repeating:

1. **``tracemalloc``.** Answers the ideal question — which line allocated what — but on a warm process
   of this size ``take_snapshot()`` walks millions of blocks holding the GIL. From an HTTP handler it
   never returned inside 75 seconds, at three frames as well as at eight, and while enabled it taxes
   *every* allocation by roughly 1.5–2× on a service that is simultaneously answering paid invokes.
2. **A ``gc.get_objects()`` type histogram.** Cheap, but **blind to this leak**: CPython untracks
   dicts and tuples that contain only atomic values, so ``gc.is_tracked({"leak": 1})`` is ``False``.
   Creating 5 000 such dicts moved the reported ``dict`` count by four. Sensor readings are dicts of
   scalars, so the one shape most likely to be accumulating is precisely the shape this cannot see —
   it would have reported "no growth" on a service leaking half a gigabyte a day.

What is left is exact rather than clever, and it is enough:

* ``sys.getallocatedblocks()`` sees **everything** CPython allocates, tracked or not. Growth there
  proves the leak is Python-level; flat while RSS climbs points at a C extension or fragmentation.
* a **store census** walks GAIA's own runtime and reports the real length of every container it
  holds — the fleet, each device's caches, the verifier's maps. If one of them is growing, this names
  it exactly. If none is, that is equally informative and saves a day of grepping.

Nothing is instrumented, so there is no idle cost and nothing to enable.
"""

from __future__ import annotations

import gc
import os
import sys
import time
from typing import Any

#: Retained samples. Each is a few hundred integers; the useful comparison is against the oldest one
#: still held, because half a megabyte a minute is noise between adjacent samples.
_SERIES_MAX = max(2, int(os.environ.get("GAIA_HEAP_SERIES", "24") or 24))
#: A tracked container this big is a suspect wherever it lives. Set from the first real measurement:
#: GAIA's own stores accounted for ~1 300 objects while 4 350 000 Python allocations appeared in an
#: hour, so the accumulation is held by references the attribute walk never reaches.
_BIG_CONTAINER = max(1000, int(os.environ.get("GAIA_HEAP_BIG_CONTAINER", "10000") or 10000))
#: Containers smaller than this are omitted from the census — a fleet has hundreds of tiny dicts and
#: listing them buries the one that matters.
_MIN_INTERESTING = max(0, int(os.environ.get("GAIA_HEAP_MIN_LEN", "16") or 16))

_series: list[dict[str, Any]] = []


def _rss_mb() -> float | None:
    try:
        with open("/proc/self/statm", encoding="ascii") as fh:
            pages = int(fh.read().split()[1])
        return round(pages * os.sysconf("SC_PAGE_SIZE") / 1_048_576, 1)
    except (OSError, IndexError, ValueError):
        return None


def _sized(value: Any) -> int | None:
    """``len`` if this is a container we can measure, else None. Never iterates."""
    if isinstance(value, (str, bytes, bytearray)):
        return None                       # a long string is not an accumulating container
    try:
        return len(value)
    except TypeError:
        return None


def _census_object(label: str, obj: Any, out: dict[str, int]) -> None:
    """Record every measurable container held directly by ``obj``.

    Attribute walk rather than a hardcoded list, so a device type added later is covered without
    anyone remembering to update this file."""
    for name in dir(obj):
        if name.startswith("__"):
            continue
        try:
            value = getattr(obj, name)
        except Exception:  # noqa: BLE001 - a property that raises must not break the census
            continue
        if callable(value):
            continue
        size = _sized(value)
        if size is not None and size >= _MIN_INTERESTING:
            out[f"{label}.{name}"] = size


def stores(runtime: Any) -> dict[str, int]:
    """Exact lengths of GAIA's own containers: the fleet, each device, the verifier."""
    out: dict[str, int] = {}
    if runtime is None:
        return out
    fleet = getattr(runtime, "fleet", None)
    if fleet is not None:
        _census_object("fleet", fleet, out)
        history = getattr(fleet, "_history", None)
        if isinstance(history, dict):
            lengths = [len(v) for v in history.values()]
            out["fleet._history.devices"] = len(lengths)
            out["fleet._history.readings_total"] = sum(lengths)
            out["fleet._history.readings_max"] = max(lengths) if lengths else 0
        try:
            for device in fleet.devices():
                _census_object(f"device:{getattr(device, 'device_id', '?')}", device, out)
        except Exception:  # noqa: BLE001
            pass
    service = getattr(runtime, "service", None)
    if service is not None:
        _census_object("verifier_service", service, out)
        verifier = getattr(service, "verifier", None)
        if verifier is not None:
            _census_object("verifier", verifier, out)
    return out


def big_containers(limit: int = 12) -> list[dict[str, Any]]:
    """The largest tracked containers on the heap, wherever they are attached.

    The attribute walk in :func:`stores` can only see what GAIA's runtime holds directly. The first
    real measurement showed why that is not enough: 4 350 000 new Python allocations in an hour while
    every GAIA container together grew by about 1 300 objects. So this asks the heap instead of the
    object graph.

    Its own blind spot, stated rather than hidden: CPython untracks dicts and tuples whose values are
    all atomic, so a dict of scalars is invisible here — but a LIST of such dicts, a deque, or any
    container holding containers is tracked, and that is the shape an accumulation almost always has.
    """
    found: list[tuple[int, Any]] = []
    for obj in gc.get_objects():
        if isinstance(obj, (str, bytes, bytearray)):
            continue
        try:
            size = len(obj)
        except Exception:  # noqa: BLE001
            # `len()` on an ARBITRARY heap object can do anything. pydantic's `_mock_val_ser`
            # raises PydanticUserError from its own __len__, which a narrow
            # `except (TypeError, AttributeError)` walked straight past — a 500 on this endpoint.
            # When introspecting objects you did not write, the only safe net is a wide one.
            continue
        if not isinstance(size, int) or size < _BIG_CONTAINER:
            continue
        found.append((size, obj))

    found.sort(key=lambda pair: -pair[0])
    out: list[dict[str, Any]] = []
    # `gc.get_referrers` walks the WHOLE heap on every call, so it is asked only about the handful
    # actually going to be reported — not once per candidate.
    for size, obj in found[:limit]:
        try:
            owners = sorted({type(r).__qualname__ for r in gc.get_referrers(obj)[:8]})[:4]
        except Exception:  # noqa: BLE001
            owners = []
        out.append({"type": type(obj).__qualname__, "len": size, "held_by": owners})
    return out


def sample(runtime: Any = None) -> dict[str, Any]:
    return {
        "ts": time.time(),
        # Sees everything CPython allocates, tracked by the GC or not. This is the number that says
        # whether a Python-level leak exists at all.
        "allocated_blocks": sys.getallocatedblocks(),
        "rss_mb": _rss_mb(),
        "stores": stores(runtime),
    }


def report(runtime: Any = None, top: int = 40) -> dict[str, Any]:
    """Current census plus growth against the OLDEST retained sample."""
    now = sample(runtime)
    _series.append(now)
    del _series[:-_SERIES_MAX]

    out: dict[str, Any] = {
        "allocated_blocks": now["allocated_blocks"],
        "rss_mb": now["rss_mb"],
        "samples_held": len(_series),
        "largest_stores": dict(sorted(now["stores"].items(), key=lambda kv: -kv[1])[:top]),
        "big_containers": big_containers(),
        "python": sys.version.split()[0],
    }
    if len(_series) < 2:
        out["growth"] = []
        out["note"] = ("baseline taken — call again after an hour. A leak of ~0.5 MB/min is noise "
                       "between adjacent samples and unmistakable across an hour.")
        return out

    first = _series[0]
    span = max(1.0, now["ts"] - first["ts"])
    per_hour = 3600.0 / span
    growth = []
    for name, size in now["stores"].items():
        diff = size - int(first["stores"].get(name, 0))
        if diff > 0:
            growth.append({"store": name, "growth": diff, "now": size,
                           "per_hour": round(diff * per_hour, 1)})
    growth.sort(key=lambda g: g["growth"], reverse=True)

    blocks_growth = now["allocated_blocks"] - int(first["allocated_blocks"])
    out.update({
        "window_s": round(span, 1),
        "growth": growth[:top],
        "allocated_blocks_growth": blocks_growth,
        "allocated_blocks_growth_per_hour": round(blocks_growth * per_hour, 1),
    })
    rss_then, rss_now = first.get("rss_mb"), now.get("rss_mb")
    if rss_then and rss_now:
        out["rss_growth_mb"] = round(rss_now - rss_then, 1)
        out["rss_growth_mb_per_hour"] = round((rss_now - rss_then) * per_hour, 1)

    # The three verdicts this tool exists to distinguish. Saying which one applies is the whole
    # point — each sends you somewhere completely different.
    store_growth = sum(g["growth"] for g in growth)
    if growth and blocks_growth < max(100_000, store_growth * 20):
        out["verdict"] = f"a GAIA container is growing: {growth[0]['store']}"
    elif blocks_growth > 100_000:
        # Reported even when a GAIA container grew a little, because "a store gained 1 257 entries"
        # is not an explanation for four million allocations, and naming it as one sends the reader
        # to the wrong place. `big_containers` is where to look next.
        biggest = (out.get("big_containers") or [{}])[0]
        out["verdict"] = (
            f"Python allocated {blocks_growth:,} more blocks while GAIA's own containers grew by "
            f"{store_growth:,} — the accumulation is held by a reference the attribute walk does "
            f"not reach. Largest tracked container: {biggest.get('type', '?')} of "
            f"{biggest.get('len', 0):,}, held by {biggest.get('held_by') or '?'}")
    elif out.get("rss_growth_mb", 0) > 5:
        out["verdict"] = ("RSS grew while Python's allocated blocks did not — this is NOT a "
                          "Python-level leak. Look at C extensions, buffers, or fragmentation")
    else:
        out["verdict"] = "no growth in this window"
    return out


def enabled() -> bool:
    """Nothing is instrumented, so there is nothing to enable; the endpoint is gated by its token.
    ``GAIA_MEMTRACE`` no longer disables anything, and saying so beats leaving a dead switch."""
    return True


def start() -> bool:
    """No-op, kept so the entrypoint's early call stays harmless."""
    return False
