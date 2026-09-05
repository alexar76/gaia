"""The canonical form of a reading is an interop contract. Pin it to a literal.

Every other test of attestation signs and verifies with the SAME function, so the suite is
self-consistent by construction: replace `reading_canonical` with `json.dumps(reading,
sort_keys=True)` and 39 tests still pass. Measured, not supposed — that exact substitution
was mounted over the module and the suite went green.

That blind spot is not academic. It is the shape of the bug that beat five autonomous repair
attempts on the canary: a model told to satisfy a signature contract wrote its own plausible
canonicalisation, which verified against itself and against nothing else. A round-trip test
cannot see the difference; only a pinned vector can.

If one of these literals has to change, the wire format changed with it, and every other
implementation of this contract has to change on the same day.
"""

from __future__ import annotations

from gaia.attestation import reading_canonical


#: A reading with every field the canonical form reads, and values chosen so a reordering,
#: a separator change or a dropped field all move the output.
_READING = {
    "device_id": "gaia-thermo-004",
    "model": "bmp390",
    "seq": 17,
    "ts": "2026-08-30T04:15:00+00:00",
    "values": {"temperature_c": 21.5, "pressure_hpa": 1013.2},
}


def test_the_canonical_form_is_a_pipe_delimited_field_string():
    out = reading_canonical(_READING)

    # The literal shape, not a re-derivation: a test that rebuilds the string the same way
    # the implementation does is the round-trip trap this file exists to avoid.
    assert out.startswith("device:gaia-thermo-004|model:bmp390|seq:17|ts:2026-08-30T04:15:00+00:00|")
    assert "|values_sha256:" in out
    assert out.count("|") == 4, "five fields, four separators, when there are no hotspots"


def test_it_is_not_json():
    # The single most likely wrong answer, and the one an autonomous fixer keeps reaching for.
    out = reading_canonical(_READING)
    assert not out.lstrip().startswith("{"), "the canonical form is not a JSON object"
    assert '"device_id"' not in out


def test_the_field_order_is_fixed():
    out = reading_canonical(_READING)
    order = [chunk.split(":", 1)[0] for chunk in out.split("|")]
    assert order == ["device", "model", "seq", "ts", "values_sha256"]


def test_hotspots_append_a_sixth_field_and_only_when_present():
    without = reading_canonical(_READING)
    with_hs = reading_canonical({**_READING, "hotspots": [{"x": 1, "y": 2}]})

    assert "hotspots_sha256:" not in without
    assert with_hs.startswith(without), "hotspots append; they must not reorder what came before"
    assert with_hs.count("|") == 5


def test_a_non_list_hotspots_value_is_ignored_rather_than_appended():
    # `isinstance(..., list)` is the guard in the implementation; pin the consequence so a
    # future "be helpful about the type" change is a visible decision.
    assert "hotspots_sha256:" not in reading_canonical({**_READING, "hotspots": None})
    assert "hotspots_sha256:" not in reading_canonical({**_READING, "hotspots": {"x": 1}})


def test_missing_fields_become_their_declared_defaults_not_the_word_none():
    out = reading_canonical({"values": {}})
    assert out.startswith("device:|model:|seq:0|ts:|values_sha256:")
    assert "None" not in out


def test_the_values_hash_covers_the_values_and_changes_with_them():
    a = reading_canonical(_READING)
    b = reading_canonical({**_READING, "values": {"temperature_c": 21.6, "pressure_hpa": 1013.2}})
    assert a != b, "a changed measurement must change the canonical form"


def test_reordering_the_values_dict_does_not_change_the_hash():
    # Otherwise two honest devices reporting the same reading would sign different strings.
    a = reading_canonical(_READING)
    b = reading_canonical({
        **_READING,
        "values": {"pressure_hpa": 1013.2, "temperature_c": 21.5},
    })
    assert a == b


def test_seq_is_rendered_as_a_bare_integer():
    # A float or a quoted number here would verify against itself and against no other
    # implementation — the exact failure mode this file guards.
    assert "|seq:17|" in reading_canonical(_READING)
    assert "|seq:17.0|" not in reading_canonical(_READING)
