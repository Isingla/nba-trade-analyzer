"""Tests for the salary season window (fix/salary-window-y6, 2026-07-16).

The salary window (salary_season_keys) carries BBRef's y6 column (2031-32);
the projection window (season_keys) and the cap-thresholds fail-loud check
stay at MAX_PROJECTION_YEARS. These lock the split and the ingest-side
survival of a 6-season contract.
"""

from __future__ import annotations

from nba_trade_analyzer.engine.constants import (
    CAP_THRESHOLDS_BY_SEASON,
    MAX_PROJECTION_YEARS,
)
from nba_trade_analyzer.export import (
    SALARY_WINDOW_EXTRA_YEARS,
    salary_season_keys,
    season_keys,
)
from nba_trade_analyzer.ingest.runner import _contract_rows


def test_salary_window_is_projection_window_plus_y6():
    proj = season_keys()
    salary = salary_season_keys()
    assert len(proj) == MAX_PROJECTION_YEARS
    assert len(salary) == MAX_PROJECTION_YEARS + SALARY_WINDOW_EXTRA_YEARS
    # Same prefix — the two windows can never disagree on shared seasons.
    assert salary[: len(proj)] == proj
    assert salary[-1] == "2031-32"


def test_projection_window_still_satisfies_thresholds_table():
    # The export's fail-loud check keys on the PROJECTION window; extending
    # the salary window must not require a thresholds entry (G1: thresholds
    # are deliberately NOT extended to 2031-32).
    assert all(season in CAP_THRESHOLDS_BY_SEASON for season in season_keys())
    assert "2031-32" not in CAP_THRESHOLDS_BY_SEASON


def test_contract_rows_carry_a_six_season_record():
    record = {
        "player_name": "Victor Wembanyama",
        "bbref_slug": "wembavi01",
        "team": "SAS",
        "salary": 50_000_000,
        "years_remaining": 6,
        "is_rookie_scale": False,
        "has_player_option": False,
        "has_team_option": False,
        "yearly_salaries": "50000000|51000000|52000000|53000000|55000000|57420000",
    }
    rows = _contract_rows([record], salary_season_keys())
    assert len(rows) == 1
    amounts = rows[0].amounts
    assert amounts["2031-32"] == 57_420_000
    assert len(amounts) == 6


def test_contract_rows_under_old_window_would_truncate_regression_guard():
    # Documents the bug this branch fixes: the projection window drops the
    # y6 season (this is exactly the skipped_seasons.ours_missing receipt).
    record = {
        "player_name": "Victor Wembanyama",
        "bbref_slug": "wembavi01",
        "team": "SAS",
        "salary": 50_000_000,
        "years_remaining": 6,
        "is_rookie_scale": False,
        "has_player_option": False,
        "has_team_option": False,
        "yearly_salaries": "50000000|51000000|52000000|53000000|55000000|57420000",
    }
    truncated = _contract_rows([record], season_keys())[0].amounts
    assert "2031-32" not in truncated
    assert len(truncated) == 5
