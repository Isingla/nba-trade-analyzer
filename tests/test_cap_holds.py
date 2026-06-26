"""Own-FA cap-holds loader + export wiring — Tier 3c, Phase A."""

from __future__ import annotations

import logging
import os

import pandas as pd

from nba_trade_analyzer.data.cap_holds import load_cap_holds
from nba_trade_analyzer.data.crosswalk import Crosswalk, CrosswalkEntry
from nba_trade_analyzer.export import build_export

_HEADER = "Team,Player,Pos,Age,2024-25,2025-26,2026-27,2027-28,2028-29,2029-30,2030-31\n"


def _write(tmp_path, body: str):
    p = tmp_path / "nba_cap_holds.csv"
    p.write_text(_HEADER + body, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Loader: per (team, season) summation, future-only gate, safe defaults.
# ---------------------------------------------------------------------------
def test_sums_holds_per_team_season(tmp_path):
    # Two GSW holds + one DAL hold; assert per-team-per-season totals.
    body = (
        "GSW,Player A,SF,30,2100000.0,2300000.0,2500000.0,2800000.0,3100000.0,,\n"
        "GSW,Player B,PF,28,1900000.0,2000000.0,2300000.0,2500000.0,2700000.0,,\n"
        "DAL,Player C,C,29,2100000.0,2300000.0,2500000.0,2800000.0,3100000.0,,\n"
    )
    totals = load_cap_holds(_write(tmp_path, body))
    # 2500000 + 2300000 summed for GSW 2026-27.
    assert totals["GSW"]["2026-27"] == 4800000
    assert totals["GSW"]["2027-28"] == 2800000 + 2500000
    assert totals["DAL"]["2026-27"] == 2500000


def test_gates_out_elapsed_seasons(tmp_path):
    # 2024-25 and 2025-26 are at/before the current league year -> excluded;
    # only future seasons survive.
    body = "GSW,Player A,SF,30,9990000.0,8880000.0,2500000.0,2800000.0,,,\n"
    totals = load_cap_holds(_write(tmp_path, body), current_league_year="2025-26")
    assert "2024-25" not in totals["GSW"]
    assert "2025-26" not in totals["GSW"]
    assert totals["GSW"] == {"2026-27": 2500000, "2027-28": 2800000}
    # The gate is a single bump-able year: rolling forward gates 2026-27 too.
    rolled = load_cap_holds(_write(tmp_path, body), current_league_year="2026-27")
    assert "2026-27" not in rolled["GSW"]
    assert rolled["GSW"] == {"2027-28": 2800000}


def test_missing_file_returns_empty(tmp_path):
    assert load_cap_holds(tmp_path / "does_not_exist.csv") == {}


def test_skips_blank_team_and_logs(tmp_path, caplog):
    body = (
        ",Orphan Hold,SF,30,0,0,2500000.0,0,0,,\n"  # blank team -> skipped
        "GSW,Player A,SF,30,0,0,2500000.0,0,0,,\n"
    )
    with caplog.at_level(logging.WARNING, logger="nba_trade_analyzer.data.cap_holds"):
        totals = load_cap_holds(_write(tmp_path, body))
    assert list(totals.keys()) == ["GSW"]  # orphan row not summed anywhere
    assert any("blank team" in r.getMessage() for r in caplog.records)


def test_skips_malformed_cells_and_logs(tmp_path, caplog):
    # A non-numeric junk cell is flagged + skipped; blank/zero cells are silently
    # ignored; the rest of the row still sums.
    body = "GSW,Player A,SF,30,,,N/A,2800000.0,0,,\n"
    with caplog.at_level(logging.WARNING, logger="nba_trade_analyzer.data.cap_holds"):
        totals = load_cap_holds(_write(tmp_path, body))
    assert totals["GSW"] == {"2027-28": 2800000}  # only the valid future cell
    assert "2026-27" not in totals["GSW"]  # the "N/A" cell did not sum
    assert any("malformed cell" in r.getMessage() for r in caplog.records)


def test_real_file_gsw_2026_27_if_present():
    # Nails the concrete example from the task against the actual source file.
    real = os.path.join(
        os.environ.get("SITE_DATA_ROOT", os.path.expanduser("~/site_Data")),
        "nba_cap_holds.csv",
    )
    if not os.path.exists(real):
        import pytest

        pytest.skip("nba_cap_holds.csv not present in SITE_DATA_ROOT")
    totals = load_cap_holds(real)
    assert totals["GSW"]["2026-27"] == 27_100_000


# ---------------------------------------------------------------------------
# Export wiring: cap holds land on DataballrExport.cap_holds (team-level),
# NOT on any salary row, and carry the `estimated` caveat.
# ---------------------------------------------------------------------------
def _df(cols, rows):
    return pd.DataFrame(rows, columns=cols)


def test_export_carries_cap_holds_separate_from_salaries():
    salary_cols = [
        "player_name", "bbref_slug", "team", "salary", "years_remaining",
        "is_rookie_scale", "has_player_option", "has_team_option", "yearly_salaries",
    ]
    salary_df = _df(
        salary_cols,
        [{
            "player_name": "Stephen Curry", "bbref_slug": "curryst01", "team": "GSW",
            "salary": 59606817, "years_remaining": 2, "is_rookie_scale": False,
            "has_player_option": False, "has_team_option": False,
            "yearly_salaries": "59606817|62587158",
        }],
    )
    empty = {
        "epm_df": _df(["player_name", "player_name_normalized", "team", "epm"], []),
        "darko_df": _df(["player_name", "player_name_normalized", "dpm"], []),
        "stats_df": _df(["nba_player_id", "player_name", "team", "age", "GP", "MPG", "NET_RATING"], []),
    }
    crosswalk = Crosswalk(
        [CrosswalkEntry(nba_id=201939, nba_name="Stephen Curry", bbref_slug="curryst01", bbref_name="Stephen Curry")]
    )
    export = build_export(
        salary_df=salary_df,
        crosswalk=crosswalk,
        cap_holds={"GSW": {"2026-27": 27100000}},
        **empty,
    )

    assert export.cap_holds.totals == {"GSW": {"2026-27": 27100000}}
    assert export.cap_holds.estimated is True
    assert export.cap_holds.team_seasons == 1
    assert "ESTIMATES" in export.cap_holds.note
    # Holds are team-level: they never appear on a player salary row.
    assert all(not hasattr(s, "cap_holds") for s in export.salaries)
    # JSON serializes under camelCase `capHolds` to match the databallr shape.
    assert "capHolds" in export.model_dump(by_alias=True)
