"""GAIA entrypoint — the AIMarket v2 surface comes from oracle-core."""

from __future__ import annotations

import os

# Allocation tracing starts BEFORE the app is built, or the baseline already contains the fleet,
# the station directories and every warm cache — and a diff against that baseline would show the
# steady state as growth. Off unless GAIA_MEMTRACE=1. See gaia/memtrace.py for why this exists.
from gaia import memtrace as _memtrace

_memtrace.start()

from gaia.app import build_app  # noqa: E402 - must come after tracing starts

app = build_app()


def _start_consumption_sampler() -> None:
    """In the serving process only: sample every plug's meter into its hourly buckets."""
    runtime = getattr(app.state, "runtime", None)
    store = getattr(runtime, "consumption", None)
    controller = getattr(runtime, "actuation", None)
    if store is None or controller is None or not controller.specs:
        return
    from gaia.consumption import recorder_from_env

    recorder_from_env(store, controller.fleet.read, list(controller.specs)).start()


if os.environ.get("GAIA_CONSUMPTION_SAMPLER", "1").strip().lower() not in ("0", "false", "no", "off"):
    _start_consumption_sampler()


def main() -> None:
    import uvicorn

    # The app object, not "gaia.main:app": under `python -m gaia.main` (the image's CMD) the
    # string made uvicorn import this module a second time — a second runtime, fleet and
    # consumption sampler, the idle one sampling plugs nobody switches.
    uvicorn.run(app, host="0.0.0.0",
                port=int(os.environ.get("GAIA_PORT", "9320")), reload=False)


if __name__ == "__main__":
    main()
