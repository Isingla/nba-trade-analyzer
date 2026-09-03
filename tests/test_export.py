"""Tests for the databallr cap-data export.

All data sources are stubbed, so these never touch the network. They lock the
databallr-facing contract: the JSON shape, the WAA formula, the source
taxonomy (epm/darko/aging_epm/aging_darko/replacement), and the fixed
5-season window.
"""

from __future__ import annotations

import logging
import os

import json

import pandas as pd
import pytest

from nba_trade_analyzer.data.cache import JsonCache
from nba_trade_analyzer.data.crosswalk import Crosswalk, CrosswalkEntry
from nba_trade_analyzer.data.epm import api_cache_file
from nba_trade_analyzer.data.darko import normalize_name as darko_normalize
from nba_trade_analyzer.data.epm import normalize_name as epm_normalize
from nba_trade_analyzer.export import (
    API_ACTUALS_SEASON,
    _epm_cache_vintage,
    default_epm_frame,
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


def test_epm_cache_vintage_reads_the_api_cache_file_date(tmp_path):
    # The vintage must date the CURRENT-SEASON API ACTUALS cache — the file
    # the export reads — never the demoted scrape cache (review finding 1).
    from nba_trade_analyzer.data.cache import JsonCache as _JC
    from nba_trade_analyzer.data.epm import api_cache_file as _acf
    from nba_trade_analyzer.export import API_ACTUALS_SEASON as _SEASON

    # A scrape cache sitting beside it must NOT be the vintage source.
    (tmp_path / "epm_dunksandthrees.json").write_text("{}", encoding="utf-8")
    api_file = _acf(_SEASON, _JC(tmp_path))
    api_file.write_text("{}", encoding="utf-8")
    fixed = 1_754_664_000  # arbitrary known epoch
    os.utime(api_file, (fixed, fixed))
    vintage = _epm_cache_vintage(cache_dir=tmp_path)
    assert vintage is not None
    # The date is READ from the API file, never invented: byte-equal.
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


# ---------------------------------------------------------------------------
# EPM source flip (feat/epm-source-flip): the export's EPM input is the D&T
# Premium API cache (season ACTUALS) with a ONE-season fallback for players
# missing from the current cache (ruled 2026-08-25). The scrape's Expected
# values feed nothing. All core guards are HERMETIC (committed inline
# fixtures, tmp cache dirs, no personal-cache dependence).
# ---------------------------------------------------------------------------



def _api_row(pid: int, name: str, epm: float, mpg: float = 30.0, age: int = 25):
    return {
        "player_id": pid,
        "player_name": name,
        "player_name_normalized": epm_normalize(name),
        "team": "TST",
        "epm": epm,
        "epm_off": epm / 2,
        "epm_def": epm / 2,
        "mpg": mpg,
        "gp": 60,
        "mp": mpg * 60,
        "position": "G",
        "age": age,
    }


def _seed_api_cache(tmp_path, season: int, rows: list[dict]) -> None:
    # Compose the filename through the SAME helper the production code uses
    # (JsonCache path + api cache key) — never a hardcoded name or home dir.
    path = api_cache_file(season, JsonCache(tmp_path))
    path.write_text(json.dumps({"expires_at": 9e12, "value": rows}))


def test_api_actuals_season_derives_from_projection_window():
    # The actuals season is the completed season the projection window starts
    # from — end-year 2026 (2025-26) while _FIRST_SEASON_START is 2026. A
    # rollover bumps both together; a hardcoded 2026 would freeze this.
    from nba_trade_analyzer.export import _FIRST_SEASON_START

    assert API_ACTUALS_SEASON == _FIRST_SEASON_START


def test_default_epm_frame_reads_current_and_falls_back_one_season(tmp_path):
    # Haliburton-class: absent from the current cache (didn't play), present
    # in the prior cache — served the PRIOR actuals. Wemby-class: present in
    # current — served current, prior row ignored.
    _seed_api_cache(
        tmp_path,
        API_ACTUALS_SEASON,
        [_api_row(1641705, "Victor Wembanyama", 8.74)],
    )
    _seed_api_cache(
        tmp_path,
        API_ACTUALS_SEASON - 1,
        [
            _api_row(1641705, "Victor Wembanyama", 6.10),
            _api_row(1630169, "Tyrese Haliburton", 4.85, mpg=33.6),
        ],
    )
    frame, prior_ids = default_epm_frame(cache_dir=tmp_path)
    wemby = frame[frame["player_id"] == 1641705]
    hali = frame[frame["player_id"] == 1630169]
    assert float(wemby.iloc[0]["epm"]) == 8.74  # current wins
    assert float(hali.iloc[0]["epm"]) == 4.85  # prior-season fallback
    assert prior_ids == {1630169}


def test_fallback_never_reaches_two_seasons_back(tmp_path):
    # A player in NEITHER cache stays absent — never invented, never pulled
    # from any older season (Lyles-class stays a dash). The two-seasons-back
    # cache IS seeded, with a player found nowhere else, so an implementation
    # that walks deeper than one season MUST fail this test.
    _seed_api_cache(tmp_path, API_ACTUALS_SEASON, [_api_row(1, "Current Guy", 1.0)])
    _seed_api_cache(tmp_path, API_ACTUALS_SEASON - 1, [_api_row(2, "Prior Guy", 2.0)])
    _seed_api_cache(
        tmp_path, API_ACTUALS_SEASON - 2, [_api_row(3, "Two Back Ghost", 3.0)]
    )
    frame, _ = default_epm_frame(cache_dir=tmp_path)
    assert set(frame["player_id"]) == {1, 2}
    assert 3 not in set(frame["player_id"])


def test_fallback_rows_carry_basis_in_export_payload(tmp_path, monkeypatch):
    # The exported projection tags WHICH actuals priced each player: current
    # rows carry the current basis; fallback rows say so explicitly; a player
    # with no row anywhere carries none.
    import nba_trade_analyzer.export as export_mod

    _seed_api_cache(
        tmp_path, API_ACTUALS_SEASON, [_api_row(101, "Current Star", 3.0)]
    )
    _seed_api_cache(
        tmp_path, API_ACTUALS_SEASON - 1, [_api_row(202, "Injured Star", 2.5, age=27)]
    )
    monkeypatch.setattr(
        export_mod, "default_epm_frame", lambda: default_epm_frame(cache_dir=tmp_path)
    )
    salary_df = _salary_df(
        [
            {
                "player_name": "Current Star",
                "bbref_slug": "currsta01",
                "team": "TST",
                "salary": 10_000_000,
                "years_remaining": 2,
                "is_rookie_scale": False,
                "has_player_option": False,
                "has_team_option": False,
                "yearly_salaries": [10_000_000, 11_000_000],
            },
            {
                "player_name": "Injured Star",
                "bbref_slug": "injusta01",
                "team": "TST",
                "salary": 40_000_000,
                "years_remaining": 2,
                "is_rookie_scale": False,
                "has_player_option": False,
                "has_team_option": False,
                "yearly_salaries": [40_000_000, 41_000_000],
            },
            {
                "player_name": "Nowhere Man",
                "bbref_slug": "nowhema01",
                "team": "TST",
                "salary": 2_000_000,
                "years_remaining": 1,
                "is_rookie_scale": False,
                "has_player_option": False,
                "has_team_option": False,
                "yearly_salaries": [2_000_000],
            },
        ]
    )
    export = build_export(
        salary_df=salary_df,
        darko_df=_darko_df([]),
        stats_df=_stats_df([]),
        crosswalk=_crosswalk(
            [
                (101, "Current Star", "currsta01"),
                (202, "Injured Star", "injusta01"),
            ]
        ),
    )
    assert export.projections["currsta01"].epm_basis == "2025-26 actuals"
    assert (
        export.projections["injusta01"].epm_basis
        == "2024-25 actuals (no 2025-26 season)"
    )
    # Fallback actually priced him: an Injured Star with age from the prior
    # row must NOT be a zeroed replacement shell.
    first = export.projections["injusta01"].seasons[season_keys()[0]]
    assert first.source != "replacement"
    assert export.projections["nowhema01"].epm_basis is None


def test_export_carries_source_minutes_only_for_current_basis_rows(
    tmp_path, monkeypatch
):
    # The site's "reduced mins" chip (ruled 2026-09-01: actual < 50% of
    # projectedGames x projectedMpg) needs each player's ACTUAL current-season
    # minutes and games — the mp/gp of the SAME row epm_basis is stamped from.
    # A fallback-basis player's PRIOR-season row must not leak in as if it
    # were current actuals, and a no-data player carries nothing. Shaped on
    # Tatum's 2025-26 line: 521.85 minutes over 16 games.
    import nba_trade_analyzer.export as export_mod

    current = _api_row(101, "Current Star", 3.0, mpg=32.6)
    current["gp"] = 16.0  # a float, as the API cache stores it
    current["mp"] = 521.85
    _seed_api_cache(tmp_path, API_ACTUALS_SEASON, [current])
    _seed_api_cache(
        tmp_path, API_ACTUALS_SEASON - 1, [_api_row(202, "Injured Star", 2.5, age=27)]
    )
    monkeypatch.setattr(
        export_mod, "default_epm_frame", lambda: default_epm_frame(cache_dir=tmp_path)
    )
    salary_df = _salary_df(
        [
            {
                "player_name": "Current Star",
                "bbref_slug": "currsta01",
                "team": "TST",
                "salary": 10_000_000,
                "years_remaining": 2,
                "is_rookie_scale": False,
                "has_player_option": False,
                "has_team_option": False,
                "yearly_salaries": [10_000_000, 11_000_000],
            },
            {
                "player_name": "Injured Star",
                "bbref_slug": "injusta01",
                "team": "TST",
                "salary": 40_000_000,
                "years_remaining": 2,
                "is_rookie_scale": False,
                "has_player_option": False,
                "has_team_option": False,
                "yearly_salaries": [40_000_000, 41_000_000],
            },
            {
                "player_name": "Nowhere Man",
                "bbref_slug": "nowhema01",
                "team": "TST",
                "salary": 2_000_000,
                "years_remaining": 1,
                "is_rookie_scale": False,
                "has_player_option": False,
                "has_team_option": False,
                "yearly_salaries": [2_000_000],
            },
        ]
    )
    export = build_export(
        salary_df=salary_df,
        darko_df=_darko_df([]),
        stats_df=_stats_df([]),
        crosswalk=_crosswalk(
            [
                (101, "Current Star", "currsta01"),
                (202, "Injured Star", "injusta01"),
            ]
        ),
    )

    # (a) current basis: the row's own mp/gp, basis unchanged.
    current_proj = export.projections["currsta01"]
    assert current_proj.epm_basis == "2025-26 actuals"
    assert current_proj.source_minutes == 521.85
    assert current_proj.source_games == 16
    # (b) fallback basis: the prior row carries 1800 minutes / 60 games, and
    # NONE of it may pass as 2025-26 actuals.
    fallback_proj = export.projections["injusta01"]
    assert fallback_proj.epm_basis == "2024-25 actuals (no 2025-26 season)"
    assert fallback_proj.source_minutes is None
    assert fallback_proj.source_games is None
    # (c) no data anywhere: nothing.
    nowhere_proj = export.projections["nowhema01"]
    assert nowhere_proj.epm_basis is None
    assert nowhere_proj.source_minutes is None
    assert nowhere_proj.source_games is None

    # (d) the payload keys, in the same camelCase as epmBasis, present and
    # null (never 0) for the two None cases.
    dumped = export.model_dump(by_alias=True)["projections"]
    assert dumped["currsta01"]["sourceMinutes"] == 521.85
    assert dumped["currsta01"]["sourceGames"] == 16
    for slug in ("injusta01", "nowhema01"):
        assert "sourceMinutes" in dumped[slug]
        assert "sourceGames" in dumped[slug]
        assert dumped[slug]["sourceMinutes"] is None
        assert dumped[slug]["sourceGames"] is None


def test_source_actuals_is_both_or_neither():
    # Injected test frames omit gp/mp entirely, and a row may carry NaN in
    # one of them: any such row yields None for BOTH, never half a line.
    from nba_trade_analyzer.export import _source_actuals

    assert _source_actuals(pd.Series({"epm": 1.0})) == (None, None)
    assert _source_actuals(pd.Series({"mp": 521.85})) == (None, None)
    assert _source_actuals(pd.Series({"mp": 521.85, "gp": float("nan")})) == (
        None,
        None,
    )
    assert _source_actuals(pd.Series({"mp": float("nan"), "gp": 16.0})) == (
        None,
        None,
    )
    assert _source_actuals(pd.Series({"mp": 521.85, "gp": 16.0})) == (521.85, 16)


def test_build_export_default_path_routes_through_default_epm_frame(monkeypatch):
    # THE hermetic source-flip guard: with no injected epm_df, build_export
    # must take its frame from default_epm_frame (the API loader path). A
    # revert to the scrape fetch cannot pass this — the sentinel player only
    # exists in the patched frame.
    import nba_trade_analyzer.export as export_mod

    sentinel = pd.DataFrame([_api_row(999, "Sentinel Actual", 5.0)])
    monkeypatch.setattr(
        export_mod, "default_epm_frame", lambda: (sentinel, set())
    )
    monkeypatch.setattr(
        export_mod, "fetch_darko_data", lambda: _darko_df([])
    )
    monkeypatch.setattr(export_mod, "fetch_player_stats", lambda: _stats_df([]))
    monkeypatch.setattr(
        export_mod,
        "fetch_all_salaries",
        lambda: _salary_df(
            [
                {
                    "player_name": "Sentinel Actual",
                    "bbref_slug": "sentise01",
                    "team": "TST",
                    "salary": 5_000_000,
                    "years_remaining": 1,
                    "is_rookie_scale": False,
                    "has_player_option": False,
                    "has_team_option": False,
                    "yearly_salaries": [5_000_000],
                }
            ]
        ),
    )
    monkeypatch.setattr(export_mod, "load_crosswalk", lambda: _crosswalk([(999, "Sentinel Actual", "sentise01")]))
    monkeypatch.setattr(export_mod, "_fetch_minutes_history", lambda: {})
    export = build_export()
    assert export.projections["sentise01"].seasons[season_keys()[0]].impact == 5.0


def test_missing_current_cache_fails_loud_with_fix_command(tmp_path):
    # The export is a pure cache reader: a missing current-season cache is an
    # actionable error naming the file and the command that fixes it.
    with pytest.raises(FileNotFoundError) as exc:
        default_epm_frame(cache_dir=tmp_path)
    msg = str(exc.value)
    assert f"epm_dunksandthrees_api_{API_ACTUALS_SEASON}.json" in msg
    assert "ingest" in msg


def test_stale_current_cache_fails_loud_past_48h(tmp_path):
    _seed_api_cache(tmp_path, API_ACTUALS_SEASON, [_api_row(1, "Old Guy", 1.0)])
    path = api_cache_file(API_ACTUALS_SEASON, JsonCache(tmp_path))
    old = 1_600_000_000  # 2020 epoch — far past any 48h window
    os.utime(path, (old, old))
    with pytest.raises(RuntimeError) as exc:
        default_epm_frame(cache_dir=tmp_path)
    msg = str(exc.value)
    assert "48" in msg and "ingest" in msg


def test_missing_prior_cache_is_tolerated(tmp_path):
    # Only the CURRENT cache is mandatory; a missing prior-season cache just
    # means no fallback pool (first season of API history).
    _seed_api_cache(tmp_path, API_ACTUALS_SEASON, [_api_row(1, "Only Guy", 1.0)])
    frame, prior_ids = default_epm_frame(cache_dir=tmp_path)
    assert list(frame["player_id"]) == [1]
    assert prior_ids == set()


def _live_cache_fresh() -> bool:
    path = api_cache_file(API_ACTUALS_SEASON)
    if not path.exists():
        return False
    import time

    return (time.time() - path.stat().st_mtime) / 3600 <= 48


@pytest.mark.skipif(
    not _live_cache_fresh(),
    reason="local API cache absent or past the 48h freshness gate",
)
def test_live_cache_wemby_pin_and_lyles_absence():
    # Bonus live-cache pin (skip-gated): Wembanyama's 2026 API ACTUAL is
    # 8.7437 @ 29.14 mpg — the scrape's Expected 7.80 @ 41.2 cannot pass.
    # Positive control for the absence claim: the frame is full-population
    # (>400 rows) AND carries Wemby, so Lyles' absence means absence, not a
    # truncated or wrong file.
    frame, _ = default_epm_frame()
    assert len(frame) > 400
    wemby = frame[frame["player_id"] == 1641705]
    assert len(wemby) == 1
    assert float(wemby.iloc[0]["epm"]) == pytest.approx(8.7437, abs=0.01)
    assert float(wemby.iloc[0]["mpg"]) == pytest.approx(29.14, abs=0.1)
    current_only_ids = set(
        pd.DataFrame(
            __import__("nba_trade_analyzer.engine.clean_engine", fromlist=["load_epm_api_cache"]).load_epm_api_cache(season=API_ACTUALS_SEASON)
        )["player_id"]
    )
    # Trey Lyles: legitimately no CURRENT-season row (fallback may still
    # serve his prior season — the ruled behavior; absence here means absent
    # from the current actuals, never invented into them).
    assert 1626168 not in current_only_ids


def test_fallback_rows_age_incremented_by_one(tmp_path):
    # Ruled 2026-08-25 (closing fix): a fallback row's age is his PRIOR-season
    # age; every downstream consumer (aging curve, minutes model, exported
    # age) must see it corrected by exactly +1. Current-season rows untouched.
    _seed_api_cache(
        tmp_path,
        API_ACTUALS_SEASON,
        [_api_row(11, "Current Kid", 2.0, age=23)],
    )
    _seed_api_cache(
        tmp_path,
        API_ACTUALS_SEASON - 1,
        [
            _api_row(11, "Current Kid", 1.5, age=22),  # ignored: current wins
            _api_row(22, "Fallback Vet", 3.0, age=32),  # prior-cache age
        ],
    )
    frame, prior_ids = default_epm_frame(cache_dir=tmp_path)
    assert prior_ids == {22}
    current = frame[frame["player_id"] == 11].iloc[0]
    fallback = frame[frame["player_id"] == 22].iloc[0]
    assert int(current["age"]) == 23  # untouched
    assert int(fallback["age"]) == 33  # 32 + 1, corrected at the merge point
