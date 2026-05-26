"""Tests for the Phase 9 typer CLI.

All data loading is mocked — no HTTP. Pick parsing and team validation are pure
functions tested directly; the ``grade`` / ``lookup`` commands are driven
through ``typer.testing.CliRunner`` with the four data fetchers patched to
return small synthetic frames.
"""

from __future__ import annotations

from unittest.mock import patch

import pandas as pd
import pytest
from typer.testing import CliRunner

from nba_trade_analyzer.cli import app, parse_pick
from nba_trade_analyzer.data.epm import normalize_name
from nba_trade_analyzer.data.salaries import EXPECTED_COLUMNS
from nba_trade_analyzer.teams import resolve_team

runner = CliRunner()


# ---------------------------------------------------------------------------
# Synthetic data builders (mirror the real frame schemas)
# ---------------------------------------------------------------------------


def _epm_df(specs: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "player_name": s["name"],
                "player_name_normalized": normalize_name(s["name"]),
                "team": s["team"],
                "epm": s["epm"],
                "epm_off": s["epm"] * 0.6,
                "epm_def": s["epm"] * 0.4,
                "mpg": s["mpg"],
                "position": s["position"],
                "age": s["age"],
            }
            for s in specs
        ]
    )


def _stats_df(specs: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "player_name": s["name"],
                "team": s["team"],
                "age": s["age"],
                "GP": s["gp"],
                "MPG": s["mpg"],
                "W": s["w"],
                "L": s["gp"] - s["w"],
                "FGA": s["fga"],
                "FG3A": s["fg3a"],
                "FG3_PCT": s["fg3_pct"],
                "FG3_RATE": (s["fg3a"] / s["fga"]) if s["fga"] else 0.0,
                "NET_RATING": 0.0,
            }
            for s in specs
        ]
    )


def _salary_df(specs: list[dict]) -> pd.DataFrame:
    rows = [
        {
            "player_name": s["name"],
            "team": s["salary_team"],
            "salary": s["salary"],
            "years_remaining": s["years"],
            "is_rookie_scale": False,
            "has_player_option": False,
            "has_team_option": False,
        }
        for s in specs
    ]
    return pd.DataFrame(rows, columns=list(EXPECTED_COLUMNS))


def _spec(
    name: str,
    *,
    team: str = "DAL",
    salary_team: str = "DAL",
    epm: float = 0.1,
    position: str = "C",
    age: int = 27,
    gp: int = 70,
    mpg: float = 30.0,
    w: int = 35,
    fga: float = 10.0,
    fg3a: float = 2.0,
    fg3_pct: float = 0.36,
    salary: int = 14_386_320,
    years: int = 4,
) -> dict:
    return {
        "name": name,
        "team": team,
        "salary_team": salary_team,
        "epm": epm,
        "position": position,
        "age": age,
        "gp": gp,
        "mpg": mpg,
        "w": w,
        "fga": fga,
        "fg3a": fg3a,
        "fg3_pct": fg3_pct,
        "salary": salary,
        "years": years,
    }


def _patch_data(specs: list[dict]):
    """Patch the CLI's four data fetchers to return synthetic frames.

    Returns a ``patch.multiple`` context manager, so callers use it with
    ``with _patch_data(...):`` — no real HTTP or cache access occurs.
    """
    epm = _epm_df(specs)
    stats = _stats_df(specs)
    salary = _salary_df(specs)
    darko = pd.DataFrame(
        columns=[
            "player_name",
            "player_name_normalized",
            "dpm",
            "dpm_off",
            "dpm_def",
            "position",
            "age",
        ]
    )
    return patch.multiple(
        "nba_trade_analyzer.cli",
        fetch_epm_data=lambda *a, **k: epm,
        fetch_player_stats=lambda *a, **k: stats,
        fetch_all_salaries=lambda *a, **k: salary,
        fetch_darko_data=lambda *a, **k: darko,
    )


# ---------------------------------------------------------------------------
# 1-5. Pick parsing
# ---------------------------------------------------------------------------


def test_parse_pick_unprotected_first():
    pick = parse_pick("2026 LAL 1st unprotected")
    assert pick is not None
    assert pick.team == "LAL"
    assert pick.year == 2026
    assert pick.round == 1
    # "unprotected" normalizes to None (the model's representation of no protection).
    assert pick.protections is None


def test_parse_pick_protected_first():
    pick = parse_pick("2027 DAL 1st top-4 protected")
    assert pick is not None
    assert pick.team == "DAL"
    assert pick.year == 2027
    assert pick.round == 1
    assert pick.protections == "top-4 protected"


def test_parse_pick_second_round():
    pick = parse_pick("2028 OKC 2nd")
    assert pick is not None
    assert pick.team == "OKC"
    assert pick.year == 2028
    assert pick.round == 2
    assert pick.protections is None


def test_parse_pick_player_name_returns_none():
    assert parse_pick("Daniel Gafford") is None


def test_parse_pick_another_player_name_returns_none():
    assert parse_pick("Jarred Vanderbilt") is None


# ---------------------------------------------------------------------------
# 6-7. Team validation
# ---------------------------------------------------------------------------


def test_valid_team_abbreviation():
    assert resolve_team("LAL") is not None
    assert resolve_team("lal") is not None  # case-insensitive
    # Either source convention resolves (Basketball Reference vs nba_api).
    assert resolve_team("BRK") is resolve_team("BKN")


def test_invalid_team_abbreviation():
    assert resolve_team("XYZ") is None
    assert resolve_team("") is None


# ---------------------------------------------------------------------------
# 8. --help exits 0
# ---------------------------------------------------------------------------


def test_help_exits_zero():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "grade" in result.output
    assert "lookup" in result.output


def test_grade_help_exits_zero():
    result = runner.invoke(app, ["grade", "--help"])
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# 9. Missing required team option errors out
# ---------------------------------------------------------------------------


def test_grade_missing_team_errors():
    # --team-a omitted: typer rejects the invocation before any data loads.
    result = runner.invoke(
        app, ["grade", "--team-b", "DAL", "--sends-a", "x", "--sends-b", "y"]
    )
    assert result.exit_code != 0


def test_grade_invalid_team_exits_one():
    with _patch_data([_spec("Daniel Gafford")]):
        result = runner.invoke(
            app,
            ["grade", "-a", "XYZ", "-b", "DAL", "-sa", "x", "-sb", "Daniel Gafford"],
        )
    assert result.exit_code == 1
    assert "not a valid NBA team abbreviation" in result.output


# ---------------------------------------------------------------------------
# 10. lookup with a known player exits 0
# ---------------------------------------------------------------------------


def test_lookup_known_player_exits_zero():
    with _patch_data([_spec("Daniel Gafford")]):
        result = runner.invoke(app, ["lookup", "Daniel Gafford"])
    assert result.exit_code == 0
    assert "Daniel Gafford" in result.output
    assert "EPM:" in result.output
    assert "Salary:" in result.output
    assert "Surplus:" in result.output


def test_lookup_unknown_player_exits_one():
    with _patch_data([_spec("Daniel Gafford")]):
        result = runner.invoke(app, ["lookup", "Nobody McNobody"])
    assert result.exit_code == 1
    assert "Could not find" in result.output


# ---------------------------------------------------------------------------
# Bonus: a legal mocked grade renders a full report end-to-end
# ---------------------------------------------------------------------------


def test_grade_legal_trade_renders_report():
    specs = [
        _spec("Player One", team="DAL", salary_team="DAL", salary=20_000_000),
        _spec("Player Two", team="MEM", salary_team="MEM", salary=20_000_000),
    ]
    with _patch_data(specs):
        result = runner.invoke(
            app,
            [
                "grade",
                "-a",
                "DAL",
                "-b",
                "MEM",
                "-sa",
                "Player One",
                "-sb",
                "Player Two",
            ],
        )
    assert result.exit_code == 0
    assert "LEGALITY" in result.output
    assert "Score:" in result.output


@pytest.mark.parametrize("flag", ["--quick", "-q"])
def test_grade_quick_skips_darko(flag: str):
    specs = [
        _spec("Player One", team="DAL", salary_team="DAL", salary=20_000_000),
        _spec("Player Two", team="MEM", salary_team="MEM", salary=20_000_000),
    ]
    with _patch_data(specs):
        result = runner.invoke(
            app,
            [
                "grade",
                "-a",
                "DAL",
                "-b",
                "MEM",
                "-sa",
                "Player One",
                "-sb",
                "Player Two",
                flag,
            ],
        )
    assert result.exit_code == 0
    assert "Skipping DARKO" in result.output
