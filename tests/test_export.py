"""Tests for the databallr cap-data export.

All data sources are stubbed, so these never touch the network. They lock the
databallr-facing contract: the JSON shape, the WAA formula, the source
taxonomy (epm/darko/aging_epm/aging_darko/replacement), and the fixed
5-season window.
"""

from __future__ import annotations

import logging
import os

import pandas as pd

from nba_trade_analyzer.data.crosswalk import Crosswalk, CrosswalkEntry
from nba_trade_analyzer.data.darko import normalize_name as darko_normalize
from nba_trade_analyzer.data.epm import normalize_name as epm_normalize
from nba_trade_analyzer.export import (
    _epm_cache_vintage,
    build_export,
    compute_waa,
    map_source,
    season_keys,
)
from nba_trade_analyzer.engine.valuation import evaluate_player_multiyear
from nba_trade_analyzer.models.player import Contract, Player


def _salary_df(rows: list[dict]) -> pd.DataFrame:
    columns = [
        "player_name",
        "bbref_slug",
        "team",
        "salary",
        "years_remaining",
        "is_rookie_scale",
        "has_player_option",
        "has_team_option",
        "yearly_salaries",
    ]
    return pd.DataFrame(rows, columns=columns)


def _epm_df(rows: list[dict]) -> pd.DataFrame:
    for row in rows:
        row["player_name_normalized"] = epm_normalize(row["player_name"])
    columns = ["player_name", "player_name_normalized", "team", "epm"]
    return pd.DataFrame(rows, columns=columns)


def _darko_df(rows: list[dict]) -> pd.DataFrame:
    for row in rows:
        row["player_name_normalized"] = darko_normalize(row["player_name"])
    columns = ["player_name", "player_name_normalized", "dpm"]
    return pd.DataFrame(rows, columns=columns)


def _stats_df(rows: list[dict]) -> pd.DataFrame:
    columns = ["nba_player_id", "player_name", "team", "age", "GP", "MPG", "NET_RATING"]
    return pd.DataFrame(rows, columns=columns)


def _crosswalk(pairs: list[tuple[int, str, str]]) -> Crosswalk:
    return Crosswalk(
        [
            CrosswalkEntry(nba_id=nid, nba_name=name, bbref_slug=slug, bbref_name=name)
            for nid, name, slug in pairs
        ]
    )


# A self-contained fixture league: an EPM+DARKO star, an EPM-only player, a
# stats-only player (no impact metric), and a player missing from stats.
def _build_sample_export(minutes_history=None):
    salary_df = _salary_df(
        [
            {
                "player_name": "Stephen Curry",
                "bbref_slug": "curryst01",
                "team": "GSW",
                "salary": 59_606_817,
                "years_remaining": 2,
                "is_rookie_scale": False,
                "has_player_option": False,
                "has_team_option": False,
                "yearly_salaries": "59606817|62587158",
            },
            {
                "player_name": "Epm Only",
                "bbref_slug": "epmon01",
                "team": "BOS",
                "salary": 20_000_000,
                "years_remaining": 4,
                "is_rookie_scale": False,
                "has_player_option": False,
                "has_team_option": False,
                "yearly_salaries": "20000000|21000000|22000000|23000000",
            },
            {
                "player_name": "Bench Guy",
                "bbref_slug": "benchg01",
                "team": "MIA",
                "salary": 2_000_000,
                "years_remaining": 1,
                "is_rookie_scale": False,
                "has_player_option": False,
                "has_team_option": False,
                "yearly_salaries": "2000000",
            },
            {
                "player_name": "No Stats",
                "bbref_slug": "nostat01",
                "team": "UTA",
                "salary": 5_000_000,
                "years_remaining": 3,
                "is_rookie_scale": False,
                "has_player_option": False,
                "has_team_option": False,
                "yearly_salaries": "5000000|5000000|5000000",
            },
        ]
    )
    epm_df = _epm_df(
        [
            {"player_name": "Stephen Curry", "team": "GSW", "epm": 4.47},
            {"player_name": "Epm Only", "team": "BOS", "epm": 2.10},
        ]
    )
    darko_df = _darko_df([{"player_name": "Stephen Curry", "dpm": 1.99}])
    stats_df = _stats_df(
        [
            {
                "nba_player_id": 201939,
                "player_name": "Stephen Curry",
                "team": "GSW",
                "age": 38,
                "GP": 70,
                "MPG": 30.9,
                "NET_RATING": 5.0,
            },
            {
                "nba_player_id": 1001,
                "player_name": "Epm Only",
                "team": "BOS",
                "age": 25,
                "GP": 70,
                "MPG": 32.0,
                "NET_RATING": 3.0,
            },
            {
                "nba_player_id": 1002,
                "player_name": "Bench Guy",
                "team": "MIA",
                "age": 27,
                "GP": 40,
                "MPG": 12.0,
                "NET_RATING": -1.0,
            },
        ]
    )
    crosswalk = _crosswalk(
        [
            (201939, "Stephen Curry", "curryst01"),
            (1001, "Epm Only", "epmon01"),
            (1002, "Bench Guy", "benchg01"),
            (9999, "No Stats", "nostat01"),
        ]
    )
    return build_export(
        salary_df=salary_df,
        epm_df=epm_df,
        darko_df=darko_df,
        stats_df=stats_df,
        crosswalk=crosswalk,
        minutes_history=minutes_history,
        # Injected empty so stub builds never read the live site_Data CSV
        # (the None default triggers the scrape-mode loader).
        dead_money=[],
    )


def test_season_keys_is_five_year_window():
    assert season_keys() == ["2026-27", "2027-28", "2028-29", "2029-30", "2030-31"]


def test_salary_rows_map_one_to_one():
    export = _build_sample_export()
    by_slug = {row.bbref_slug: row for row in export.salaries}
    assert len(export.salaries) == 4
    curry = by_slug["curryst01"]
    assert curry.player_name == "Stephen Curry"
    assert curry.team == "GSW"
    assert curry.salary == 59_606_817
    assert curry.years_remaining == 2
    # Pipe string is parsed back into an int list.
    assert curry.yearly_salaries == [59_606_817, 62_587_158]


def test_epm_then_darko_then_aging_darko_source_chain():
    export = _build_sample_export()
    seasons = export.projections["curryst01"].seasons
    assert seasons["2026-27"].source == "epm"
    assert seasons["2027-28"].source == "darko"
    assert seasons["2028-29"].source == "aging_darko"
    assert seasons["2029-30"].source == "aging_darko"
    assert seasons["2030-31"].source == "aging_darko"
    # Year 1 impact is the raw EPM; year 2 is the raw DARKO DPM.
    assert seasons["2026-27"].impact == 4.47
    assert seasons["2027-28"].impact == 1.99


def test_epm_only_player_ages_on_epm_anchor():
    export = _build_sample_export()
    seasons = export.projections["epmon01"].seasons
    assert seasons["2026-27"].source == "epm"
    # No DARKO row -> year 2 onward ages off the EPM anchor.
    assert seasons["2027-28"].source == "aging_epm"
    assert seasons["2030-31"].source == "aging_epm"


def test_no_impact_metric_falls_back_to_replacement():
    export = _build_sample_export()
    seasons = export.projections["benchg01"].seasons
    assert all(s.source == "replacement" for s in seasons.values())
    assert all(s.impact == 0.0 and s.waa == 0.0 for s in seasons.values())
    # Replacement still carries the player's real minutes.
    assert seasons["2026-27"].mpg == 12.0


def test_missing_stats_player_is_replacement_with_zero_minutes():
    export = _build_sample_export()
    proj = export.projections["nostat01"]
    assert proj.age is None
    seasons = proj.seasons
    assert all(s.source == "replacement" for s in seasons.values())
    assert all(s.mpg == 0.0 for s in seasons.values())


def test_waa_is_priced_on_projected_games_times_mpg():
    # issue 2.2: WAA is unified onto the two-model minutes. It must equal
    # compute_waa(impact, projected_games * projected_mpg), proving WAA and the
    # TS WAR/surplus path are priced on the SAME availability-adjusted minutes.
    export = _build_sample_export()
    season = export.projections["curryst01"].seasons["2026-27"]
    expected = round(
        compute_waa(season.impact, season.projected_games * season.projected_mpg), 1
    )
    assert season.waa == expected
    # The minutes channel is populated and bounded by the model's ceilings.
    assert 0 < season.projected_games <= 78
    assert 0 < season.projected_mpg <= 38


def test_every_season_exposes_projected_games_and_mpg():
    export = _build_sample_export()
    for proj in export.projections.values():
        for season in proj.seasons.values():
            assert season.projected_games >= 0
            assert season.projected_mpg >= 0


def test_injury_history_cuts_projected_games_and_waa():
    # Same star, but injected with an injury-prone GP history: fewer projected
    # games -> fewer minutes -> lower WAA than the no-history (healthy) baseline.
    healthy = _build_sample_export().projections["curryst01"].seasons["2026-27"]
    injured = _build_sample_export(
        minutes_history={
            201939: {"gp": [82.0, 82.0, 25.0], "mpg": [30.9, 30.9, 30.9]}
        }
    ).projections["curryst01"].seasons["2026-27"]
    assert injured.projected_games < healthy.projected_games
    assert injured.waa < healthy.waa


def test_metadata_counts_and_window():
    export = _build_sample_export()
    meta = export.metadata
    assert meta.salary_rows == 4
    assert meta.epm_rows == 2
    assert meta.darko_rows == 1
    assert meta.stats_rows == 3
    assert meta.projection_seasons == season_keys()
    # Every projected player contributes exactly 5 season-source tallies.
    assert sum(meta.source_counts.values()) == len(export.projections) * 5


def test_json_uses_camelcase_keys():
    export = _build_sample_export()
    dumped = export.model_dump(by_alias=True)
    salary = dumped["salaries"][0]
    assert "bbrefSlug" in salary
    assert "yearlySalaries" in salary
    assert "yearsRemaining" in salary
    player = dumped["projections"]["curryst01"]
    assert "nbaId" in player
    assert "playerName" in player


def test_nba_id_carried_from_crosswalk():
    export = _build_sample_export()
    assert export.projections["curryst01"].nba_id == 201939
    assert export.projections["nostat01"].nba_id == 9999


def test_injured_star_uses_epm_age_mpg_fallback():
    """A rostered player with no nba_api stats row still projects off EPM."""
    salary_df = _salary_df(
        [
            {
                "player_name": "Injured Star",
                "bbref_slug": "injure01",
                "team": "IND",
                "salary": 45_000_000,
                "years_remaining": 3,
                "is_rookie_scale": False,
                "has_player_option": False,
                "has_team_option": False,
                "yearly_salaries": "45000000|47000000|49000000",
            }
        ]
    )
    # In EPM (carries age + mpg) but absent from the nba_api stats frame.
    epm_df = _epm_df([{"player_name": "Injured Star", "team": "IND", "epm": 3.7}])
    epm_df["mpg"] = [32.5]
    epm_df["age"] = [26]
    export = build_export(
        salary_df=salary_df,
        epm_df=epm_df,
        darko_df=_darko_df([]),
        stats_df=_stats_df([]),
        crosswalk=_crosswalk([(500, "Injured Star", "injure01")]),
    )
    proj = export.projections["injure01"]
    assert proj.age == 26
    seasons = proj.seasons
    assert seasons["2026-27"].source == "epm"
    assert seasons["2026-27"].impact == 3.7
    assert seasons["2026-27"].mpg == 32.5
    assert seasons["2026-27"].waa > 0  # not a zeroed replacement


def test_map_source_taxonomy():
    assert map_source("epm", "epm") == "epm"
    assert map_source("darko", "darko") == "darko"
    assert map_source("aging_curve", "darko") == "aging_darko"
    assert map_source("aging_curve", "epm") == "aging_epm"
    assert map_source("net_rating", "epm") == "replacement"


def test_horizon_years_override_projects_full_window():
    """A 1-year contract still projects 5 seasons when horizon_years is set."""
    player = Player(
        name="Stephen Curry",
        team="GSW",
        age=30,
        stats={"MPG": 34.0, "GP": 70, "NET_RATING": 5.0},
    )
    contract = Contract(salary=50_000_000, years_remaining=1)
    epm_df = _epm_df([{"player_name": "Stephen Curry", "team": "GSW", "epm": 5.0}])
    darko_df = _darko_df([])
    multi = evaluate_player_multiyear(
        player, contract, epm_df=epm_df, darko_df=darko_df, horizon_years=5
    )
    assert len(multi.year_by_year) == 5
    # Default behavior (no override) still honors the contract length.
    default = evaluate_player_multiyear(
        player, contract, epm_df=epm_df, darko_df=darko_df
    )
    assert len(default.year_by_year) == 1


# ---- capThresholds block (Cap Sheet, Stage 1) --------------------------------


def test_cap_thresholds_block_covers_window_with_certified_and_projected():
    from nba_trade_analyzer.engine.constants import CAP_THRESHOLDS_BY_SEASON

    export = _build_sample_export()
    block = export.cap_thresholds
    assert set(block.seasons) == set(season_keys())

    for season_key, emitted in block.seasons.items():
        table = CAP_THRESHOLDS_BY_SEASON[season_key]
        assert emitted.salary_cap == table["salary_cap"]
        assert emitted.minimum_team_salary == table["minimum_team_salary"]
        assert emitted.luxury_tax == table["luxury_tax"]
        assert emitted.first_apron == table["first_apron"]
        assert emitted.second_apron == table["second_apron"]
        assert emitted.certified == table["certified"]

    # Certified seasons emit the official announced figures verbatim.
    y26 = block.seasons["2026-27"]
    assert y26.certified is True
    assert y26.salary_cap == 164_961_000
    assert y26.luxury_tax == 200_428_000
    assert y26.first_apron == 209_015_000
    assert y26.second_apron == 221_686_000
    # 2025-26 left the window at the 2026-07-11 rollover; 2026-27 is now the
    # only certified season in it. Out-years are honest projections.
    assert "2025-26" not in block.seasons
    for season_key in ("2027-28", "2028-29", "2029-30", "2030-31"):
        assert block.seasons[season_key].certified is False


def test_cap_thresholds_is_additive_to_the_wire_shape():
    """The new block must not disturb any pre-existing top-level key."""
    export = _build_sample_export()
    payload = export.model_dump(by_alias=True)
    assert set(payload) == {
        "metadata",
        "salaries",
        "projections",
        "capHolds",
        "capThresholds",
        # Dead-money charges (additive, Phase 2 Day 2): totals + audit rows,
        # deduped by resolved player; empty block when no charges injected.
        "deadMoney",
        # Staleness marker (additive, 2026-07-07): None on live exports,
        # populated when salaries came from the committed-CSV fallback.
        "sourceNote",
    }
    assert payload["sourceNote"] is None  # sample export is a live-shaped build
    # The pre-existing metadata scalar is untouched by this stage.
    assert payload["metadata"]["salaryCap"] == 154_647_000
    season = payload["capThresholds"]["seasons"]["2026-27"]
    # Wire field names are camelCase like every other block.
    assert set(season) == {
        "salaryCap",
        "minimumTeamSalary",
        "luxuryTax",
        "firstApron",
        "secondApron",
        "certified",
        "source",
    }


# ---------------------------------------------------------------------------
# 2026-08-14 P0 batch: slug-first EPM identity, loud demotions, EPM vintage.
# ---------------------------------------------------------------------------

def _one_player_export(*, crosswalk, epm_rows, stats_rows, name="Ron Holland",
                       slug="hollaro01"):
    salary_df = _salary_df(
        [
            {
                "player_name": name,
                "bbref_slug": slug,
                "team": "DET",
                "salary": 9_070_000,
                "years_remaining": 2,
                "is_rookie_scale": True,
                "has_player_option": False,
                "has_team_option": True,
                "yearly_salaries": [9_070_000, 11_000_000],
            }
        ]
    )
    return build_export(
        salary_df=salary_df,
        epm_df=_epm_df(epm_rows),
        darko_df=_darko_df([]),
        stats_df=_stats_df(stats_rows),
        crosswalk=crosswalk,
    )


def test_slug_first_join_survives_source_name_drift():
    # The salary frame's name matches nothing in EPM, but the crosswalk knows
    # the slug's canonical NBA name — the slug join must win before the name
    # fallback gets a chance to silently demote the player.
    export = _one_player_export(
        name="R. Q. Holland-Smythe",  # hopeless as a name key
        crosswalk=_crosswalk([(1631222, "Ronald Holland II", "hollaro01")]),
        epm_rows=[{"player_name": "Ronald Holland II", "team": "DET", "epm": -0.59}],
        stats_rows=[
            {
                "nba_player_id": 1631222,
                "player_name": "Ronald Holland II",
                "team": "DET",
                "age": 21,
                "GP": 70,
                "MPG": 20.0,
                "NET_RATING": 0.0,
            }
        ],
    )
    season = export.projections["hollaro01"].seasons[season_keys()[0]]
    assert season.source == "epm"
    assert season.impact == -0.59


def test_null_age_demotion_is_loud(caplog):
    # No stats row and no EPM row → age unknown → replacement demotion. The
    # demotion survives (by design, for now) but must name the player and the
    # reason out loud — never a silent zeroed shell.
    with caplog.at_level(logging.WARNING, logger="nba_trade_analyzer.export"):
        export = _one_player_export(
            crosswalk=_crosswalk([]),
            epm_rows=[],
            stats_rows=[],
        )
    season = export.projections["hollaro01"].seasons[season_keys()[0]]
    assert season.source == "replacement"
    assert any(
        "Ron Holland" in r.message and "age" in r.message.lower()
        for r in caplog.records
    )


def test_epm_cache_vintage_reads_the_file_date(tmp_path):
    cache_file = tmp_path / "epm_dunksandthrees.json"
    cache_file.write_text("{}", encoding="utf-8")
    fixed = 1_754_664_000  # arbitrary known epoch
    os.utime(cache_file, (fixed, fixed))
    vintage = _epm_cache_vintage(cache_dir=tmp_path)
    assert vintage is not None
    assert vintage.startswith("2025") or vintage.startswith("2026")
    # The date is READ from the file, never invented: byte-equal expectation.
    from datetime import datetime, timezone

    assert vintage == datetime.fromtimestamp(fixed, tz=timezone.utc).isoformat()


def test_epm_cache_vintage_missing_file_is_none(tmp_path):
    assert _epm_cache_vintage(cache_dir=tmp_path) is None


def test_metadata_carries_epm_vintage_field():
    export = _one_player_export(
        crosswalk=_crosswalk([]),
        epm_rows=[{"player_name": "Ron Holland", "team": "DET", "epm": -0.59}],
        stats_rows=[
            {
                "nba_player_id": 1631222,
                "player_name": "Ron Holland",
                "team": "DET",
                "age": 21,
                "GP": 70,
                "MPG": 20.0,
                "NET_RATING": 0.0,
            }
        ],
    )
    dumped = export.metadata.model_dump(by_alias=True)
    assert "epmVintage" in dumped
