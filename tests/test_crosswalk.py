"""Integrity + golden-set tests for the NBA-id <-> BBRef-slug crosswalk.

The crosswalk is the single runtime chokepoint for joining the two id systems,
so these tests guard it hard: no duplicate ids or slugs, no many-to-one
collisions, every entry round-trips, the loader's guards actually fire on a
corrupt file, and a hand-picked golden set of edge-case names (accents,
suffixes, spelling divergence, common surnames) resolves to the exact id+slug.
"""

from __future__ import annotations

import json

import pytest

from nba_trade_analyzer.data.crosswalk import (
    Crosswalk,
    CrosswalkEntry,
    CrosswalkError,
    crosswalk_from_dict,
    dump_crosswalk,
    load_crosswalk,
)


@pytest.fixture(scope="module")
def committed() -> Crosswalk:
    """The committed data/player_crosswalk.json, loaded + validated."""
    return load_crosswalk()


def _entry(nba_id: int, slug: str) -> CrosswalkEntry:
    return CrosswalkEntry(nba_id, f"Name {nba_id}", slug, f"Name {slug}")


# ---------------------------------------------------------------------------
# Integrity of the committed crosswalk
# ---------------------------------------------------------------------------


def test_committed_loads_and_is_nonempty(committed: Crosswalk):
    assert len(committed) > 300  # ~450+ NBA players under contract


def test_no_duplicate_nba_ids(committed: Crosswalk):
    ids = [e.nba_id for e in committed.entries]
    assert len(ids) == len(set(ids))


def test_no_duplicate_bbref_slugs(committed: Crosswalk):
    slugs = [e.bbref_slug for e in committed.entries]
    assert len(slugs) == len(set(slugs))


def test_no_many_to_one_collisions(committed: Crosswalk):
    # Both maps are bijective: id->slug and slug->id are each 1:1.
    by_id = {e.nba_id: e.bbref_slug for e in committed.entries}
    by_slug = {e.bbref_slug: e.nba_id for e in committed.entries}
    assert len(by_id) == len(committed)
    assert len(by_slug) == len(committed)


def test_every_entry_round_trips(committed: Crosswalk):
    for e in committed.entries:
        assert (
            committed.nba_id_for_slug(committed.slug_for_nba_id(e.nba_id)) == e.nba_id
        )
        assert committed.slug_for_nba_id(committed.nba_id_for_slug(e.bbref_slug)) == (
            e.bbref_slug
        )


def test_committed_file_is_valid_json_and_schema():
    from nba_trade_analyzer.data.crosswalk import DEFAULT_CROSSWALK_PATH

    data = json.loads(DEFAULT_CROSSWALK_PATH.read_text(encoding="utf-8"))
    assert data["schema_version"] == 1
    assert isinstance(data["entries"], list)
    for raw in data["entries"]:
        assert set(raw) >= {"nba_id", "nba_name", "bbref_slug", "bbref_name"}


# ---------------------------------------------------------------------------
# The guards actually fire (not just the happy path)
# ---------------------------------------------------------------------------


def test_constructor_raises_on_duplicate_nba_id():
    with pytest.raises(CrosswalkError, match="duplicate nba_id"):
        Crosswalk([_entry(1, "aaa01"), _entry(1, "bbb01")])


def test_constructor_raises_on_duplicate_slug():
    with pytest.raises(CrosswalkError, match="duplicate bbref_slug"):
        Crosswalk([_entry(1, "aaa01"), _entry(2, "aaa01")])


def test_load_raises_on_injected_duplicate_id(tmp_path):
    payload = dump_crosswalk([_entry(1, "aaa01"), _entry(1, "bbb01")], season="2025-26")
    path = tmp_path / "cw.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(CrosswalkError):
        load_crosswalk(path)


def test_load_raises_on_injected_duplicate_slug(tmp_path):
    payload = dump_crosswalk([_entry(1, "aaa01"), _entry(2, "aaa01")], season="2025-26")
    path = tmp_path / "cw.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(CrosswalkError):
        load_crosswalk(path)


def test_load_raises_on_bad_schema_version(tmp_path):
    path = tmp_path / "cw.json"
    path.write_text(json.dumps({"schema_version": 99, "entries": []}), encoding="utf-8")
    with pytest.raises(CrosswalkError, match="schema_version"):
        load_crosswalk(path)


def test_load_raises_on_missing_field(tmp_path):
    bad = {"schema_version": 1, "entries": [{"nba_id": 1, "bbref_slug": "aaa01"}]}
    path = tmp_path / "cw.json"
    path.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(CrosswalkError, match="missing required field"):
        load_crosswalk(path)


def test_load_raises_on_non_integer_id(tmp_path):
    bad = {
        "schema_version": 1,
        "entries": [
            {"nba_id": "1", "nba_name": "x", "bbref_slug": "aaa01", "bbref_name": "x"}
        ],
    }
    path = tmp_path / "cw.json"
    path.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(CrosswalkError, match="must be an integer"):
        load_crosswalk(path)


def test_load_raises_on_invalid_json(tmp_path):
    path = tmp_path / "cw.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(CrosswalkError, match="not valid JSON"):
        load_crosswalk(path)


def test_load_raises_on_missing_file(tmp_path):
    with pytest.raises(CrosswalkError, match="not found"):
        load_crosswalk(tmp_path / "nope.json")


def test_dump_round_trips_through_loader():
    entries = [_entry(10, "zzz01"), _entry(20, "aaa01")]
    cw = crosswalk_from_dict(dump_crosswalk(entries, season="2025-26"))
    assert cw.nba_id_for_slug("aaa01") == 20
    assert cw.slug_for_nba_id(10) == "zzz01"


# ---------------------------------------------------------------------------
# Golden set — hand-verified id + slug for edge-case names
# ---------------------------------------------------------------------------

# (display name, bbref_slug, nba_id). Covers: accents (Dončić, Jokić), Jr.
# suffixes (Trent, Jackson), a III suffix (Bagley), a known spelling-divergence
# alias (Nic/Nicolas Claxton), a recently-traded star (Durant), and a common
# surname where a collision is plausible (three distinct Joneses).
GOLDEN: list[tuple[str, str, int]] = [
    ("Luka Dončić", "doncilu01", 1629029),
    ("Nikola Jokić", "jokicni01", 203999),
    ("Gary Trent Jr.", "trentga02", 1629018),
    ("Jaren Jackson Jr.", "jacksja02", 1628991),
    ("Marvin Bagley III", "baglema01", 1628963),
    ("Wendell Carter Jr.", "cartewe01", 1628976),
    ("Nic Claxton", "claxtni01", 1629651),
    ("Kevin Durant", "duranke01", 201142),
    ("Jayson Tatum", "tatumja01", 1628369),
    ("Herbert Jones", "joneshe01", 1630529),
    ("Derrick Jones Jr.", "jonesde02", 1627884),
    ("Kelly Oubre Jr.", "oubreke01", 1626162),
    ("Jaime Jaquez Jr.", "jaqueja01", 1631170),
    ("Jabari Smith Jr.", "smithja05", 1631095),
    ("Giannis Antetokounmpo", "antetgi01", 203507),
]


@pytest.mark.parametrize("name, slug, nba_id", GOLDEN, ids=[g[0] for g in GOLDEN])
def test_golden_resolves_id_and_slug(committed: Crosswalk, name, slug, nba_id):
    assert committed.nba_id_for_slug(slug) == nba_id, name
    assert committed.slug_for_nba_id(nba_id) == slug, name
    # Round-trips both directions through the chokepoint.
    assert committed.slug_for_nba_id(committed.nba_id_for_slug(slug)) == slug
    assert committed.nba_id_for_slug(committed.slug_for_nba_id(nba_id)) == nba_id
