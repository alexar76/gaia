"""Path wiring: gaia + oracle-core (+ hub for e2e when present)."""

from __future__ import annotations

import sys
from pathlib import Path

_GAIA_ROOT = Path(__file__).resolve().parents[1]
_REPO = Path(__file__).resolve().parents[2]

_candidates = [
    _GAIA_ROOT,
    _GAIA_ROOT / "vendor" / "oracle-core",
    _REPO / "gaia",
    _REPO / "oracles" / "core",
    _REPO / "aimarket-hub",
]
for p in _candidates:
    if p.is_dir():
        s = str(p)
        if s not in sys.path:
            sys.path.insert(0, s)
