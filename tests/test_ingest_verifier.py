"""Layer-1 verifier rules + crosswalk name resolution (Phase 2A)."""

from __future__ import annotations

from nba_trade_analyzer.data.crosswalk import Crosswalk, CrosswalkEntry
from nba_trade_analyzer.data.nba_salaries_csv import (
    ARTIFACT_MAX_DOLLARS,
    load_nba_salaries,
    nba_salaries_season_coverage,
)
from nba_trade_analyzer.ingest.names import NameResolver
from nba_trade_analyzer.ingest.verify import verify_salaries

_HEADER = "Player,2026-27,2027-28,2028-29,2029-30,2030-31,Team,Pos,Age,2031-32,Guaranteed,2024-25\n"


def _write(tmp_path, body: str):
    p = tmp_path / "nba_salaries.csv"
    p.write_text(_HEADER + body, encoding="utf-8")
    return p


def _crosswalk() -> Crosswalk:
    return Crosswalk(
        [
            CrosswalkEntry(1, "Kris Dunn", "dunnkr01", "Kris Dunn"),
            CrosswalkEntry(2, "Cameron Christie", "chrisca02", "Cam Christie"),
            CrosswalkEntry(3, "Damian Lillard", "lillada01", "Damian Lillard"),
            CrosswalkEntry(4, "Jayson Tatum", "tatumja01", "Jayson Tatum"),
            CrosswalkEntry(5, "Trae Young", "youngtr01", "Trae Young"),
        ]
    )


def _resolver() -> NameResolver:
    return NameResolver(_crosswalk())


# ---------------------------------------------------------------------------
# nba_salaries.csv loader rules
# ---------------------------------------------------------------------------

def test_artifact_cells_are_dropped_and_recorded(tmp_path):
    # Kris Dunn's real 2027-28 cell carries the stray "4.0" token (Phase 0 4c).
    body = "Kris Dunn,5684800.0,4.0,0.0,0.0,0.0,LAC,PG,31,0.0,5684804.0,0\n"
    rows = load_nba_salaries(_write(tmp_path, body))
    r = rows[0]
    assert r.amounts == {"2026-27": 5684800}
    assert r.artifacts == {"2027-28": 4}
    assert r.guaranteed_total == 5684804  # kept verbatim; known-contaminated


def test_zero_cells_mean_no_contract_not_artifact(tmp_path):
    body = "Jayson Tatum,58456566.0,0.0,0,0.0,0,BOS,SF,28,0.0,259806960.0,0\n"
    rows = load_nba_salaries(_write(tmp_path, body))
    assert rows[0].amounts == {"2026-27": 58456566}
    assert rows[0].artifacts == {}


def test_artifact_threshold_boundary(tmp_path):
    at = ARTIFACT_MAX_DOLLARS  # 10_000: at threshold = real, below = artifact
    body = (
        f"Kris Dunn,{at},0,0,0,0,LAC,PG,31,0,0,0\n"
        f"Jayson Tatum,{at - 1},0,0,0,0,BOS,SF,28,0,0,0\n"
    )
    rows = load_nba_salaries(_write(tmp_path, body))
    assert rows[0].amounts == {"2026-27": at}
    assert rows[1].amounts == {}
    assert rows[1].artifacts == {"2026-27": at - 1}


def test_parses_by_header_name_not_position(tmp_path):
    # Shuffle the column order; values must still land on the right seasons.
    p = tmp_path / "nba_salaries.csv"
    p.write_text(
        "Team,Player,Guaranteed,2027-28,2026-27\n"
        "LAC,Kris Dunn,5684800.0,1000000.0,5684800.0\n",
        encoding="utf-8",
    )
    rows = load_nba_salaries(p)
    assert rows[0].amounts == {"2026-27": 5684800, "2027-28": 1000000}


# ---------------------------------------------------------------------------
# Name resolution
# ---------------------------------------------------------------------------

def test_exact_flipped_and_prefix_resolution():
    r = _resolver()
    assert r.resolve("Kris Dunn") == "dunnkr01"
    assert r.resolve("Lillard Damian") == "lillada01"  # surname-first flip
    assert r.resolve("Cam Christie") == "chrisca02"  # crosswalk nba_name
    assert r.resolve("Cameron Christie") == "chrisca02"  # prefix bridge
    assert r.resolve("Totally Unknown") is None


def test_ascii_input_resolves_against_accented_crosswalk_names():
    # nba_options.csv is ASCII; the BBRef-derived crosswalk is accented.
    cw = Crosswalk(
        [
            CrosswalkEntry(10, "Nikola Jokić", "jokicni01", "Nikola Jokić"),
            CrosswalkEntry(11, "Luka Dončić", "doncilu01", "Luka Dončić"),
            CrosswalkEntry(12, "Jonas Valančiūnas", "valanjo01", "Jonas Valančiūnas"),
            CrosswalkEntry(13, "Bogdan Bogdanović", "bogdabo01", "Bogdan Bogdanović"),
        ]
    )
    r = NameResolver(cw)
    assert r.resolve("Nikola Jokic") == "jokicni01"
    assert r.resolve("Luka Doncic") == "doncilu01"
    assert r.resolve("Jonas Valanciunas") == "valanjo01"
    assert r.resolve("Bogdan Bogdanovic") == "bogdabo01"
    # Accented input against the same index also folds to the same key.
    assert r.resolve("Nikola Jokić") == "jokicni01"
    # A genuinely unknown name still lands unresolved.
    assert r.resolve("Nikola Jovic") is None  # different player, not in this crosswalk
    assert r.resolve("Totally Unknown") is None


def test_diacritics_fold_composes_with_flip_and_prefix_fallbacks():
    cw = Crosswalk(
        [
            CrosswalkEntry(20, "Luka Dončić", "doncilu01", "Luka Dončić"),
            CrosswalkEntry(21, "Sviatoslav Mykhailiuk", "mykhasv01", "Svi Mykhailiuk"),
        ]
    )
    r = NameResolver(cw)
    assert r.resolve("Doncic Luka") == "doncilu01"  # flip on folded forms
    assert r.resolve("Svi Mykhailiuk") == "mykhasv01"  # prefix bridge unaffected


def test_multiword_surname_first_names_resolve_via_last_token_flip():
    # nba_options.csv reversed-name cases: the surname-first handler covers
    # multi-word groups by also trying last-token-to-front.
    cw = Crosswalk(
        [
            CrosswalkEntry(30, "Tristan da Silva", "dasiltr01", "Tristan Da Silva"),
            CrosswalkEntry(31, "Yanic Konan Niederhauser", "konanya01", "Yanic Konan Niederhauser"),
        ]
    )
    r = NameResolver(cw)
    assert r.resolve("Da Silva Tristan") == "dasiltr01"
    assert r.resolve("Konan Niederhauser Yanic") == "konanya01"


def test_ambiguous_names_resolve_to_none():
    cw = Crosswalk(
        [
            CrosswalkEntry(1, "Jaylen Brown", "brownja02", "Jaylen Brown"),
            CrosswalkEntry(2, "Jaylin Brown", "brownja03", "Jaylin Brown"),
        ]
    )
    r = NameResolver(cw)
    # Prefix fallback bucket has two viable candidates -> refuse to guess.
    assert r.resolve("Jayl Brown") is None


# ---------------------------------------------------------------------------
# Verifier rules
# ---------------------------------------------------------------------------

def test_match_and_mismatch_are_exact_dollar(tmp_path):
    body = (
        "Kris Dunn,5684800.0,0,0,0,0,LAC,PG,31,0,5684804.0,0\n"
        "Jayson Tatum,58456566.0,0,0,0,0,BOS,SF,28,0,259806960.0,0\n"
    )
    spotrac = load_nba_salaries(_write(tmp_path, body))
    rows, summary = verify_salaries(
        ingested={
            "dunnkr01": {"2026-27": 5684800},
            "tatumja01": {"2026-27": 58456567},  # off by one dollar
        },
        bbref={
            "dunnkr01": {"2026-27": 5684800},
            "tatumja01": {"2026-27": 58456567},
        },
        spotrac_rows=spotrac,
        resolver=_resolver(),
        player_names={"dunnkr01": "Kris Dunn", "tatumja01": "Jayson Tatum"},
        cap_hold_slugs=set(),
        dead_amounts={},
    )
    by_slug = {(r.slug, r.field): r for r in rows}
    assert by_slug[("dunnkr01", "salary:2026-27")].verdict == "match"
    tatum = by_slug[("tatumja01", "salary:2026-27")]
    assert tatum.verdict == "mismatch"
    assert (tatum.our_value, tatum.spotrac_value) == ("58456567", "58456566")
    assert summary.match == 1 and summary.mismatch == 1


def test_absent_vs_present_is_a_mismatch_with_both_recorded(tmp_path):
    body = "Kris Dunn,5684800.0,1200000.0,0,0,0,LAC,PG,31,0,0,0\n"
    spotrac = load_nba_salaries(_write(tmp_path, body))
    rows, _ = verify_salaries(
        ingested={"dunnkr01": {"2026-27": 5684800}},  # we have no 2027-28
        bbref={"dunnkr01": {"2026-27": 5684800}},
        spotrac_rows=spotrac,
        resolver=_resolver(),
        player_names={"dunnkr01": "Kris Dunn"},
        cap_hold_slugs=set(),
        dead_amounts={},
    )
    row = next(r for r in rows if r.field == "salary:2027-28")
    assert row.verdict == "mismatch"
    assert row.our_value == "absent"
    assert row.spotrac_value == "1200000"


def test_unmatched_name_is_unverifiable_never_skipped(tmp_path):
    body = "Mystery Person,9990000.0,0,0,0,0,BOS,SF,28,0,0,0\n"
    spotrac = load_nba_salaries(_write(tmp_path, body))
    rows, summary = verify_salaries(
        ingested={},
        bbref={},
        spotrac_rows=spotrac,
        resolver=_resolver(),
        player_names={},
        cap_hold_slugs=set(),
        dead_amounts={},
    )
    assert summary.unverifiable == 1
    assert rows[0].verdict == "unverifiable"
    assert rows[0].player_name == "Mystery Person"


def test_clean_fa_in_cap_holds_gets_no_rows(tmp_path):
    # Trae Young: absent from salaries on BOTH sides, present in cap holds.
    spotrac = load_nba_salaries(_write(tmp_path, ""))
    rows, summary = verify_salaries(
        ingested={"youngtr01": {}},
        bbref={},
        spotrac_rows=spotrac,
        resolver=_resolver(),
        player_names={"youngtr01": "Trae Young"},
        cap_hold_slugs={"youngtr01"},
        dead_amounts={},
    )
    assert rows == []
    assert summary.match == summary.mismatch == summary.unverifiable == 0


def test_waived_spotrac_rows_compare_against_dead_money_not_contracts(tmp_path):
    body = "Lillard Damian WAIVED,22516603.0,22516603.0,0,0,0,MIL,PG,36,0,0,0\n"
    spotrac = load_nba_salaries(_write(tmp_path, body))
    rows, summary = verify_salaries(
        ingested={"lillada01": {"2026-27": 3500000}},  # his REAL contract
        bbref={"lillada01": {"2026-27": 3500000}},
        spotrac_rows=spotrac,
        resolver=_resolver(),
        player_names={"lillada01": "Damian Lillard"},
        cap_hold_slugs=set(),
        dead_amounts={"lillada01": {"2026-27": 22516603, "2027-28": 22516603}},
    )
    dead_rows = [r for r in rows if r.field.startswith("dead_money:")]
    assert {r.verdict for r in dead_rows} == {"match"}
    # The known waive-and-stretch pattern produced NO contract mismatch.
    contract_rows = [r for r in rows if r.field.startswith("salary:")]
    assert all(r.verdict == "match" for r in contract_rows)
    assert summary.mismatch == 0


# ---------------------------------------------------------------------------
# Coverage intersection (the 786-mismatch fix): whole-column absences are
# skipped and recorded once, never per-row verdicts.
# ---------------------------------------------------------------------------

def test_spotrac_missing_2025_26_column_is_skipped_not_mismatched(tmp_path):
    # Real header shape: NO 2025-26 column. Our 2025-26 salaries must produce
    # zero verdict rows for that season and one summary entry.
    body = "Kris Dunn,5684800.0,0,0,0,0,LAC,PG,31,0,0,0\n"
    path = _write(tmp_path, body)
    spotrac = load_nba_salaries(path)
    coverage = nba_salaries_season_coverage(path)
    assert "2025-26" not in coverage  # the file's actual shape
    rows, summary = verify_salaries(
        ingested={"dunnkr01": {"2025-26": 5426400, "2026-27": 5684800}},
        bbref={"dunnkr01": {"2025-26": 5426400, "2026-27": 5684800}},
        spotrac_rows=spotrac,
        resolver=_resolver(),
        player_names={"dunnkr01": "Kris Dunn"},
        cap_hold_slugs=set(),
        dead_amounts={},
        spotrac_coverage=coverage,
        our_coverage={"2025-26", "2026-27", "2027-28", "2028-29", "2029-30"},
    )
    assert all(not r.field.endswith("2025-26") for r in rows)
    assert summary.skipped_seasons["spotrac_missing"] == ["2025-26"]
    # The covered season still compares normally.
    assert [r.verdict for r in rows if r.field == "salary:2026-27"] == ["match"]
    assert summary.mismatch == 0


def test_our_window_missing_2030_31_is_skipped_mirror_case(tmp_path):
    # Spotrac carries 2030-31; the ingest window (season_keys) stops at
    # 2029-30 — a window artifact, not "no contract".
    body = "Kris Dunn,5684800.0,0,0,0,60000000.0,LAC,PG,31,0,0,0\n"
    path = _write(tmp_path, body)
    rows, summary = verify_salaries(
        ingested={"dunnkr01": {"2026-27": 5684800}},
        bbref={"dunnkr01": {"2026-27": 5684800}},
        spotrac_rows=load_nba_salaries(path),
        resolver=_resolver(),
        player_names={"dunnkr01": "Kris Dunn"},
        cap_hold_slugs=set(),
        dead_amounts={},
        spotrac_coverage=nba_salaries_season_coverage(path),
        our_coverage={"2025-26", "2026-27", "2027-28", "2028-29", "2029-30"},
    )
    assert all(not r.field.endswith("2030-31") for r in rows)
    assert summary.skipped_seasons["ours_missing"] == ["2030-31"]
    assert summary.mismatch == 0


# ---------------------------------------------------------------------------
# Dead-money known pattern #2: unmarked Spotrac dead-money salary rows
# (the anthoco01 class).
# ---------------------------------------------------------------------------

def test_unmarked_dead_money_row_matches_with_pattern_tag(tmp_path):
    # anthoco01 shape: our side absent (separated to v3_dead_money), Spotrac
    # lists the dead charge as a plain salary row, exact to the dollar.
    body = "Kris Dunn,3700000.0,0,0,0,0,MEM,PG,31,0,0,0\n"
    path = _write(tmp_path, body)
    rows, summary = verify_salaries(
        ingested={},
        bbref={},
        spotrac_rows=load_nba_salaries(path),
        resolver=_resolver(),
        player_names={"dunnkr01": "Kris Dunn"},
        cap_hold_slugs=set(),
        dead_amounts={"dunnkr01": {"2026-27": 3700000}},
        spotrac_coverage=nba_salaries_season_coverage(path),
        our_coverage={"2026-27"},
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.verdict == "match"
    assert row.field == "salary:2026-27:dead_money_pattern"  # tagged, not plain
    assert (row.our_value, row.spotrac_value) == ("absent", "3700000")
    assert summary.dead_money_pattern == 1
    assert summary.mismatch == 0


def test_one_dollar_delta_from_dead_charge_stays_mismatch(tmp_path):
    body = "Kris Dunn,3700001.0,0,0,0,0,MEM,PG,31,0,0,0\n"
    path = _write(tmp_path, body)
    rows, summary = verify_salaries(
        ingested={},
        bbref={},
        spotrac_rows=load_nba_salaries(path),
        resolver=_resolver(),
        player_names={"dunnkr01": "Kris Dunn"},
        cap_hold_slugs=set(),
        dead_amounts={"dunnkr01": {"2026-27": 3700000}},
        spotrac_coverage=nba_salaries_season_coverage(path),
        our_coverage={"2026-27"},
    )
    assert len(rows) == 1
    assert rows[0].verdict == "mismatch"
    assert rows[0].field == "salary:2026-27"  # no pattern tag
    assert summary.dead_money_pattern == 0


def test_every_compared_player_season_gets_a_row_including_matches(tmp_path):
    body = "Kris Dunn,5684800.0,1200000.0,0,0,0,LAC,PG,31,0,0,0\n"
    spotrac = load_nba_salaries(_write(tmp_path, body))
    rows, _ = verify_salaries(
        ingested={"dunnkr01": {"2026-27": 5684800, "2027-28": 1200000}},
        bbref={"dunnkr01": {"2026-27": 5684800, "2027-28": 1200000}},
        spotrac_rows=spotrac,
        resolver=_resolver(),
        player_names={"dunnkr01": "Kris Dunn"},
        cap_hold_slugs=set(),
        dead_amounts={},
    )
    assert sorted(r.field for r in rows) == ["salary:2026-27", "salary:2027-28"]
    assert {r.verdict for r in rows} == {"match"}
