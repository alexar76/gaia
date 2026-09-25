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


def main() -> None:
    import uvicorn

    uvicorn.run("gaia.main:app", host="0.0.0.0",
                port=int(os.environ.get("GAIA_PORT", "9320")), reload=False)


if __name__ == "__main__":
    main()
