"""The GeoNames gazetteer: names in the spellings people write, honest about ambiguity.

Built from tests/fixtures/geonames — an excerpt of the real dump (CC BY 4.0) holding
the cases that broke naive matching: ten Parises, thirteen Springfields, two Moscows,
Hong Kong's old name "Victoria", Berlin's "Mitte" section, Tokyo's two-character name.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from gaia import gazetteer as gz
from gaia.gazetteer import Gazetteer, GazetteerError, norm

FIXTURE = Path(__file__).parent / "fixtures" / "geonames"


@pytest.fixture(scope="module")
def db_path(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("gaz") / "gazetteer.sqlite"
    counts = gz.build_from_dir(FIXTURE, out)
    assert counts["places"] == 65
    return out


@pytest.fixture(scope="module")
def g(db_path) -> Gazetteer:
    return Gazetteer(db_path)


@pytest.mark.parametrize("raw, key", [
    ("São Paulo", "sao paulo"),
    ("SAO-PAULO", "sao paulo"),
    ("  St.  Petersburg ", "st petersburg"),
    ("Нью-Йорк", "нью иорк"),
    ("Köln", "koln"),
    ("東京", "東京"),
    ("O’Fallon", "o fallon"),
])
def test_norm_folds_case_accents_and_punctuation(raw, key):
    assert norm(raw) == key


@pytest.mark.parametrize("query, label", [
    ("Paris", "Paris, Île-de-France, FR"),
    ("Paris, Texas", "Paris, Texas, US"),
    ("Paris, TX", "Paris, Texas, US"),
    ("paris , tx , usa", "Paris, Texas, US"),
    ("Paris, Ontario", "Paris, Ontario, CA"),
    ("Springfield, Illinois", "Springfield, Illinois, US"),
    ("Springfield, MO", "Springfield, Missouri, US"),
    ("Москва", "Moscow, RU"),
    ("Moscow, Россия", "Moscow, RU"),
    ("Moscow", "Moscow, RU"),
    ("東京", "Tokyo, JP"),
    ("Токио", "Tokyo, JP"),
    ("Kiev", "Kyiv, Kyiv City, UA"),
    ("Київ", "Kyiv, Kyiv City, UA"),
    ("Cologne", "Köln, North Rhine-Westphalia, DE"),
    ("Krakow, Польша", "Kraków, Lesser Poland, PL"),
    ("Kraków, Polonia", "Kraków, Lesser Poland, PL"),
    ("St. Petersburg", "Saint Petersburg, St.-Petersburg, RU"),
    ("Санкт-Петербург", "Saint Petersburg, St.-Petersburg, RU"),
    ("St. Petersburg, FL", "St. Petersburg, Florida, US"),
    ("Toronto, ON", "Toronto, Ontario, CA"),
    ("London", "London, England, GB"),
    ("London, England", "London, England, GB"),
    ("London, Ontario", "London, Ontario, CA"),
    ("Sydney, NSW", "Sydney, New South Wales, AU"),
    ("Victoria, BC", "Victoria, British Columbia, CA"),
    ("Berlin, NH", "Berlin, New Hampshire, US"),
    ("Berlin", "Berlin, State of Berlin, DE"),
])
def test_names_resolve_to_the_place_meant(g, query, label):
    res = g.resolve(query)
    assert res.chosen is not None, res.reason
    assert res.chosen.label == label
    assert not res.ambiguous


@pytest.mark.parametrize("query, among", [
    ("Springfield", {"Springfield, Missouri, US", "Springfield, Massachusetts, US",
                     "Springfield, Illinois, US"}),
    ("Cambridge", {"Cambridge, England, GB", "Cambridge, Ontario, CA",
                   "Cambridge, Massachusetts, US"}),
    # Hong Kong's old name: the city really called Victoria there is as big as the
    # Canadian one, so the honest answer is "which one?", never "Hong Kong".
    ("Victoria", {"Victoria, Central and Western, HK", "Victoria, British Columbia, CA"}),
])
def test_a_name_of_comparable_places_is_ambiguous_not_guessed(g, query, among):
    res = g.resolve(query)
    assert res.ambiguous and res.chosen is None
    assert among <= {p.label for p in res.candidates}
    with pytest.raises(GazetteerError, match="ambiguous.*state or country"):
        g.require(query)


def test_an_alternate_name_does_not_outrank_a_real_one(g):
    labels = [p.label for p in g.resolve("Victoria").candidates]
    assert "Hong Kong, HK" not in labels[:2]


def test_a_section_listed_twice_is_one_place(g):
    # Berlin and its "Mitte" are different names; but the two Berlin, DE rows a dump
    # can carry must not make "Berlin" ambiguous with itself.
    res = g.resolve("Berlin")
    assert sum(p.country_code == "DE" for p in res.candidates) == 1


def test_an_unknown_qualifier_is_an_error_not_ignored(g):
    with pytest.raises(GazetteerError, match="Narnia.*names no country or state"):
        g.resolve("Paris, Narnia")


def test_a_qualifier_that_excludes_every_candidate_finds_nothing(g):
    res = g.resolve("Paris, Japan")
    assert res.chosen is None and not res.ambiguous
    assert "no populated place called 'Paris' in Japan" in res.reason


def test_an_unknown_name_finds_nothing(g):
    res = g.resolve("Atlantisburg")
    assert res.chosen is None and not res.ambiguous and "latitude and longitude" in res.reason
    with pytest.raises(GazetteerError, match="no populated place"):
        g.require("Atlantisburg")


def test_an_empty_name_is_refused(g):
    with pytest.raises(GazetteerError, match="empty"):
        g.resolve("  ,  ")


def test_the_country_argument_narrows_like_a_qualifier(g):
    assert g.resolve("Paris", "US").ambiguous  # nine of them
    assert g.resolve("Springfield", "Missouri").chosen.label == "Springfield, Missouri, US"


def test_reverse_names_the_city_not_its_section(g):
    near = g.nearest(52.52, 13.40)
    assert near[0][0].name == "Berlin" and near[0][1] < 2.0
    with_sections = g.nearest(52.52, 13.40, include_sections=True)
    assert any(p.name == "Mitte" for p, _ in with_sections)


def test_reverse_in_open_sea_finds_nothing(g):
    assert g.nearest(0.0, -160.0) == []


def test_reverse_across_the_antimeridian_and_poles_does_not_break(g):
    assert g.nearest(89.9, 179.9) == []
    assert g.nearest(-89.9, -179.9) == []


def test_reverse_refuses_an_impossible_point(g):
    with pytest.raises(GazetteerError, match="latitude must be"):
        g.nearest(91.0, 0.0)


def test_provenance_names_the_build_and_the_licence(g):
    prov = g.provenance()
    assert prov["attribution"] == "GeoNames (geonames.org) · CC BY 4.0"
    assert len(prov["sha256_cities1000"]) == 64
    assert prov["places"] == 65 and prov["built_at"].endswith("Z")
    assert "CLDR" in prov["country_names"]


def test_the_file_is_opened_read_only(g, db_path):
    with pytest.raises(sqlite3.OperationalError):
        g._db().execute("DELETE FROM place")


def test_build_never_leaves_a_half_written_file(tmp_path):
    out = tmp_path / "g.sqlite"
    gz.build_from_dir(FIXTURE, out)
    assert out.exists() and not (tmp_path / "g.sqlite.tmp").exists()


def test_load_default_is_off_without_the_env(monkeypatch):
    monkeypatch.delenv("GAIA_GAZETTEER_PATH", raising=False)
    assert gz.load_default() is None


def test_load_default_survives_a_missing_file(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("GAIA_GAZETTEER_PATH", str(tmp_path / "absent.sqlite"))
    assert gz.load_default() is None
    assert "resolving relayed cities only" in capsys.readouterr().err


def test_country_names_cover_the_project_languages():
    import json

    names = json.loads((Path(gz.__file__).with_name("country_names.json")).read_text("utf-8"))
    de = names["names"]["DE"]
    for spelling in ("Germany", "Германия", "Alemania", "Allemagne", "德国"):
        assert spelling in de
    assert len(names["names"]) >= 245


def test_the_download_host_is_the_approved_one():
    from gaia.source_policy import require_approved_source

    policy = require_approved_source("geonames")
    for name in gz.GEONAMES_FILES:
        policy.require_endpoint(gz.GEONAMES_BASE + name)
    assert policy.attribution == gz.ATTRIBUTION
