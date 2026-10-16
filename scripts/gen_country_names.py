#!/usr/bin/env python3
"""Write gaia/gaia/country_names.json: every ISO 3166 country's name in 16 languages.

The gazetteer matches the part after a comma ("Kraków, Польша", "Lyon, Francia") against
these names, so a qualifier works in the languages GAIA's callers write in, not only in
English. The names come from the Unicode CLDR through Babel (Unicode License, permissive;
attribution in the JSON). Run it on a machine that has Babel when CLDR moves:

    python3 gaia/scripts/gen_country_names.py

The runtime never imports Babel; it reads the committed JSON.
"""

from __future__ import annotations

import json
from pathlib import Path

import babel
from babel import Locale

LANGUAGES = ("en", "ru", "es", "fr", "zh_Hans", "zh_Hant", "de", "it", "pt", "ja",
             "ko", "ar", "tr", "pl", "uk", "nl")
OUT = Path(__file__).resolve().parents[1] / "gaia" / "country_names.json"


def main() -> None:
    english = Locale("en").territories
    # Two-letter alphabetic codes only: CLDR also names regions ("150" = Europe) and
    # pseudo-territories ("ZZ" = Unknown Region, "EZ" = Eurozone, "UN"), none of which
    # is a country a city can be in.
    skip = {"ZZ", "EU", "EZ", "UN", "QO", "XA", "XB", "XK"}
    codes = sorted(c for c in english if len(c) == 2 and c.isalpha() and c not in skip)
    names: dict[str, list[str]] = {}
    for code in codes:
        seen: list[str] = []
        for lang in LANGUAGES:
            name = Locale.parse(lang).territories.get(code)
            if name and name not in seen:
                seen.append(name)
        names[code] = seen
    OUT.write_text(json.dumps({
        "source": f"Unicode CLDR via Babel {babel.__version__} (Unicode License)",
        "languages": list(LANGUAGES),
        "names": names,
    }, ensure_ascii=False, indent=0, sort_keys=True) + "\n", encoding="utf-8")
    print(f"{OUT}: {len(names)} countries")


if __name__ == "__main__":
    main()
