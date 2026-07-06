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

from nba_trade_analyzer.cli import _standing_line, app, parse_pick
from nba_trade_analyzer.data.crosswalk import Crosswalk, CrosswalkEntry
from nba_trade_analyzer.data.epm import normalize_name
from nba_trade_analyzer.data.salaries import EXPECTED_COLUMNS
from nba_trade_analyzer.teams import resolve_team

runner = CliRunner()


def _fake_nba_id(name: str) -> int:
    """Deterministic synthetic nba_player_id for a test player name."""
    return 900_000 + sum(ord(c) for c in name)


def _fake_slug(name: str) -> str:
    return f"slug{_fake_nba_id(name)}"


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
                "nba_player_id": _fake_nba_id(s["name"]),
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
            "bbref_slug": _fake_slug(s["name"]),
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


def _patch_data(specs: list[dict], roster_extra: dict[str, list[str]] | None = None):
    """Patch the CLI's data fetchers to return synthetic frames.

    Returns a ``patch.multiple`` context manager, so callers use it with
    ``with _patch_data(...):`` — no real HTTP or cache access occurs.

    ``roster_extra`` maps a team abbreviation to extra current-roster player
    names that have NO salary/crosswalk entry (e.g. two-way players), so the
    roster command's no-contract path can be exercised.
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
    # Crosswalk joining each synthetic player's slug <-> nba id, and a roster
    # fetch returning the ids on a given team — so the grade command's fail-loud
    # resolution and roster filtering work without HTTP or the committed file.
    crosswalk = Crosswalk(
        [
            CrosswalkEntry(
                _fake_nba_id(s["name"]), s["name"], _fake_slug(s["name"]), s["name"]
            )
            for s in specs
        ]
    )

    def _roster_ids(team_abbr: str, *a, **k) -> set[int]:
        return {_fake_nba_id(s["name"]) for s in specs if s["team"] == team_abbr}

    def _roster_records(team_abbr: str, *a, **k) -> list[dict]:
        recs = [
            {
                "nba_player_id": _fake_nba_id(s["name"]),
                "player_name": s["name"],
                "team": team_abbr,
            }
            for s in specs
            if s["team"] == team_abbr
        ]
        for extra_name in (roster_extra or {}).get(team_abbr, []):
            recs.append(
                {
                    "nba_player_id": _fake_nba_id(extra_name),
                    "player_name": extra_name,
                    "team": team_abbr,
                }
            )
        return recs

    return patch.multiple(
        "nba_trade_analyzer.cli",
        fetch_epm_data=lambda *a, **k: epm,
        fetch_player_stats=lambda *a, **k: stats,
        fetch_all_salaries=lambda *a, **k: salary,
        fetch_darko_data=lambda *a, **k: darko,
        load_crosswalk=lambda *a, **k: crosswalk,
        fetch_roster_player_ids=_roster_ids,
        fetch_team_roster=_roster_records,
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


# ---------------------------------------------------------------------------
# Pick-ownership verification (on by default; --no-ownership-check disables)
# ---------------------------------------------------------------------------


def _ownership_specs() -> list[dict]:
    return [
        _spec("Player One", team="DAL", salary_team="DAL", salary=20_000_000),
        _spec("Player Two", team="MEM", salary_team="MEM", salary=20_000_000),
    ]


def test_grade_rejects_pick_team_does_not_own_by_default():
    # DAL tries to send LAC's 2028 first — which PHI controls. On by default.
    with _patch_data(_ownership_specs()):
        result = runner.invoke(
            app,
            ["grade", "-a", "DAL", "-b", "MEM",
             "-sa", "Player One", "-sa", "2028 LAC 1st", "-sb", "Player Two"],
        )
    assert result.exit_code == 1
    assert "controlled by PHI" in result.output
    assert "Score:" not in result.output  # rejected before grading


def test_grade_no_ownership_check_flag_skips_verification():
    with _patch_data(_ownership_specs()):
        result = runner.invoke(
            app,
            ["grade", "-a", "DAL", "-b", "MEM",
             "-sa", "Player One", "-sa", "2028 LAC 1st", "-sb", "Player Two",
             "--no-ownership-check"],
        )
    assert result.exit_code == 0
    assert "controlled by OKC" not in result.output
    assert "Score:" in result.output  # graded despite the unowned pick


def test_grade_norecord_pick_warns_instead_of_rejecting():
    # A 2025 first has no registry record -> CLI downgrades to a loud warning.
    with _patch_data(_ownership_specs()):
        result = runner.invoke(
            app,
            ["grade", "-a", "DAL", "-b", "MEM",
             "-sa", "Player One", "-sa", "2025 DAL 1st", "-sb", "Player Two"],
        )
    assert result.exit_code == 0  # not rejected
    assert "no record of this pick" in result.output
    assert "mirror synced 2026-07-06" in result.output  # staleness self-diagnoses
    assert "Score:" in result.output  # still graded


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


# ---------------------------------------------------------------------------
# roster command
# ---------------------------------------------------------------------------


def test_roster_runs_for_valid_team_exits_zero():
    specs = [
        _spec("Star Guard", team="LAL", salary_team="LAL", salary=40_000_000),
        _spec("Role Forward", team="LAL", salary_team="LAL", salary=12_000_000),
    ]
    with _patch_data(specs):
        result = runner.invoke(app, ["roster", "-t", "LAL"])
    assert result.exit_code == 0
    assert "Star Guard" in result.output
    assert "Role Forward" in result.output
    assert "Payroll:" in result.output
    # Sorted by current-year salary descending.
    assert result.output.index("Star Guard") < result.output.index("Role Forward")


def test_roster_invalid_team_exits_one():
    with _patch_data([_spec("Star Guard", team="LAL", salary_team="LAL")]):
        result = runner.invoke(app, ["roster", "-t", "XYZ"])
    assert result.exit_code == 1
    assert "not a valid NBA team abbreviation" in result.output


def test_roster_payroll_excludes_no_contract_players():
    specs = [
        _spec("Star Guard", team="LAL", salary_team="LAL", salary=20_000_000),
        _spec("Role Forward", team="LAL", salary_team="LAL", salary=10_000_000),
    ]
    # A two-way player on the roster with no salary/crosswalk entry.
    with _patch_data(specs, roster_extra={"LAL": ["Two Way Guy"]}):
        result = runner.invoke(app, ["roster", "-t", "LAL"])
    assert result.exit_code == 0
    # Payroll is the sum of the two contracted players only, not the two-way.
    assert "Payroll: $30,000,000" in result.output
    # The no-contract player is listed and marked, never omitted.
    assert "Two Way Guy" in result.output
    assert "no contract data" in result.output
    # And the exclusion is noted so payroll isn't silently incomplete.
    assert "Excluded from payroll (no contract data): 1" in result.output


def test_roster_shows_all_four_line_distances_with_sign():
    # A single $160M contract: above the cap, below tax + both aprons.
    specs = [_spec("Big Deal", team="LAL", salary_team="LAL", salary=160_000_000)]
    with _patch_data(specs):
        result = runner.invoke(app, ["roster", "-t", "LAL"])
    assert result.exit_code == 0
    for label in ("Salary cap", "Luxury tax", "First apron", "Second apron"):
        assert label in result.output
    assert "5.4M above" in result.output  # 160.0M - 154.647M cap
    assert "27.9M below" in result.output  # 187.895M tax - 160.0M
    assert "35.9M below" in result.output  # 195.945M first apron - 160.0M
    assert "47.8M below" in result.output  # 207.824M second apron - 160.0M


def test_standing_line_sign_and_magnitude():
    # Above a line: payroll exceeds it.
    above = _standing_line("Salary cap", 154_647_000, 160_000_000)
    assert "above" in above and "5.4M" in above
    # Below a line: payroll under it.
    below = _standing_line("Second apron", 207_824_000, 160_000_000)
    assert "below" in below and "47.8M" in below
