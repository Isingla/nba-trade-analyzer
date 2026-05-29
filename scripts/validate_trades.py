"""Lightweight trade-validation smoke test.

Runs a handful of predefined trades through the full grading pipeline and
checks for *obvious* data-pipeline failures and sanity violations — it is a
smoke test, not a calibration or grading-quality validation. The point is to
catch a broken data source or a regression that makes the engine emit garbage
(empty prose, scores that don't sum to 100, a contender "giving up" a lottery
pick, a player with no current stats described as a non-shooter, ...).

The trades are defined as *data* (the ``TRADES`` list below): each entry names
the teams and assets, the expected legality, and — for legal trades — the score
band each side should land in. The runner loads every data source once, reuses
it across all trades, and prints a PASS/FAIL report.

Run from the repo root::

    uv run python scripts/validate_trades.py

Exits 0 if every trade passes, 1 if any fails.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd
import typer

from nba_trade_analyzer.cli import (
    _build_team,
    _load_core_data,
    _parse_assets,
    _stats_lookup,
)
from nba_trade_analyzer.data.crosswalk import Crosswalk, load_crosswalk
from nba_trade_analyzer.data.darko import fetch_darko_data
from nba_trade_analyzer.data.epm import normalize_name
from nba_trade_analyzer.data.players import fetch_roster_player_ids
from nba_trade_analyzer.engine.grader import (
    _pick_team_wins,
    _projected_pick_number,
    _roster_dicts,
    grade_trade,
)
from nba_trade_analyzer.engine.team_context import (
    _minutes_by_position,
    filter_to_current_roster,
)
from nba_trade_analyzer.models.team import Team
from nba_trade_analyzer.models.trade import Trade, TradeAssets
from nba_trade_analyzer.report import force_utf8_stdout
from nba_trade_analyzer.teams import resolve_team

# ---------------------------------------------------------------------------
# Trades (data, not code)
#
# Each trade carries the teams + assets, the expected legality, the per-side
# score band (legal trades only), and a list of human-readable ``checks`` that
# document what the trade is meant to exercise. The runner applies the same
# universal sanity suite (see ``evaluate_trade``) to every trade; the ``checks``
# strings are notes for a human reading a failure, not a separate DSL.
# ---------------------------------------------------------------------------

TRADES: list[dict[str, Any]] = [
    {
        "name": "Klay/Aldama (DAL↔MEM)",
        "team_a": "DAL",
        "team_b": "MEM",
        "sends_a": ["Klay Thompson"],
        "sends_b": ["Santi Aldama"],
        "expected_legal": True,
        "score_range_a": [45, 55],
        "score_range_b": [45, 55],
        "checks": [
            "legal under salary matching",
            "near-even swap — both sides in the fair-trade band",
            "scores sum to 100",
        ],
    },
    {
        "name": "Klay/Kispert (DAL↔ATL)",
        "team_a": "DAL",
        "team_b": "ATL",
        "sends_a": ["Klay Thompson"],
        "sends_b": ["Corey Kispert"],
        "expected_legal": True,
        "score_range_a": [35, 65],
        "score_range_b": [35, 65],
        "checks": [
            "legal under salary matching",
            "modest value gap, still inside a reasonable band",
            "scores sum to 100",
        ],
    },
    {
        "name": "Kyrie to DET (DAL↔DET)",
        "team_a": "DAL",
        "team_b": "DET",
        "sends_a": ["Kyrie Irving"],
        "sends_b": [
            "Isaiah Stewart",
            "Caris LeVert",
            "Ausar Thompson",
            "2027 DET 1st unprotected",
        ],
        "expected_legal": True,
        "score_range_a": [40, 60],
        "score_range_b": [40, 60],
        "checks": [
            "legal under salary matching",
            "an injured/absent Kyrie must read as 'No current shooting data', "
            "not '0.0 attempts'",
            "if DET is a 50+ win team, its 2027 1st must project late (> 20)",
        ],
    },
    {
        # Single-for-single with a wide salary gap into a second-apron team:
        # NYK (over the second apron) can't take back $35M for $12.9M out, so
        # the deal is illegal. Both players are on their current rosters, so the
        # new roster-membership check passes and the engine reaches legality.
        "name": "Clarkson/Gobert (NYK↔MIN)",
        "team_a": "NYK",
        "team_b": "MIN",
        "sends_a": ["Jordan Clarkson"],
        "sends_b": ["Rudy Gobert"],
        "expected_legal": False,
        "score_range_a": None,
        "score_range_b": None,
        "checks": [
            "illegal — single-for-single salaries don't match",
            "no grades produced for an illegal trade",
        ],
    },
    {
        # Salary dump: a team ships an overpaid vet to an OVER_CAP contender for
        # a cheaper rotation piece plus the contender's own near-future 1st.
        # Kuzma (now on MIL) is the overpay; DET (OVER_CAP) absorbs the salary
        # and sends out its late first. All players are on their current
        # rosters, so the roster-membership check passes.
        "name": "Kuzma dump (MIL↔DET)",
        "team_a": "MIL",
        "team_b": "DET",
        "sends_a": ["Kyle Kuzma"],
        "sends_b": ["Duncan Robinson", "2026 DET 1st unprotected"],
        "expected_legal": True,
        "score_range_a": [70, 90],
        "score_range_b": [10, 30],
        "checks": [
            "legal — an OVER_CAP contender absorbs the salary via the Expanded TPE",
            "MIL clearly wins (sheds an overpay for a cheaper piece + a 1st)",
            "DET is a ~60-win team, so its 2026 1st must project late (> 20)",
            "scores sum to 100",
        ],
    },
]

# The seven breakdowns every legal TeamGrade must populate.
METRIC_FIELDS = (
    "impact",
    "contract",
    "win_curve",
    "timeline",
    "positional_fit",
    "spacing",
    "draft_capital",
)

# A coarse position bucket (G / F / C) can't exceed a full game's worth of
# minutes — 5 positions × 48 = 240 total. A bucket over this means the roster
# feed is double-counting (e.g. traded-away players never dropped).
MINUTES_CEILING = 240.0

# Above this projected-win total a team is a contender, so its own first-round
# pick must land outside the lottery/late-lottery range.
CONTENDER_WINS = 50.0
LATE_PICK_FLOOR = 20


@dataclass
class TradeResult:
    """Outcome of running one trade through the sanity suite."""

    name: str
    passed: bool
    summary: str  # "50/50", "illegal", or "error"
    expected: str  # "45-55/45-55", "illegal", ...
    failures: list[str] = field(default_factory=list)
    declared_checks: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def _range_str(spec: dict[str, Any]) -> str:
    (lo_a, hi_a) = spec["score_range_a"]
    (lo_b, hi_b) = spec["score_range_b"]
    return f"{lo_a}-{hi_a}/{lo_b}-{hi_b}"


def _position_minutes(
    team: Team, stats_df: pd.DataFrame, roster_ids: set[int]
) -> dict[str, float]:
    """Minutes-by-position for a team's *current* roster.

    Mirrors what the grader builds: season-stats rows filtered to the current
    nba_api roster id set (which drops mid-season departures), bucketed G/F/C.
    """
    roster = _roster_dicts(stats_df, team.abbreviation)
    roster = filter_to_current_roster(roster, roster_ids)
    return _minutes_by_position(roster)


def _raw_gp(name: str, stats_lookup: dict[str, pd.Series]) -> int | None:
    """Raw current-season GP from the stats feed (``None`` if no usable row).

    Reads the source row rather than the built ``Player`` because the player
    builder fills a fallback GP for absent players — masking the very gap this
    check looks for.
    """
    row = stats_lookup.get(normalize_name(name))
    if row is None:
        return None
    gp = row.get("GP")
    if gp is None or pd.isna(gp):
        return None
    return int(gp)


def _check_player_gp(
    name: str,
    acquiring_grade: Any,
    acquiring_abbr: str,
    stats_lookup: dict[str, pd.Series],
    failures: list[str],
) -> None:
    """A player with no current GP must be flagged, never faked as a non-shooter.

    If the player logged games this season, fine. If not, the acquiring team's
    spacing prose must say "No current shooting data" — it must not assert
    "0.0 attempts" (which would falsely brand an absent player a non-shooter).
    """
    gp = _raw_gp(name, stats_lookup)
    if gp is not None and gp > 0:
        return
    prose = acquiring_grade.spacing.explanation or ""
    if "No current shooting data" in prose:
        return
    if "0.0 attempts" in prose:
        failures.append(
            f"{name} has no current-season GP but spacing prose claims "
            f"'0.0 attempts' (falsely brands an absent player a non-shooter)"
        )
    else:
        failures.append(
            f"{name} has no current-season GP and {acquiring_abbr} spacing prose "
            f"doesn't flag it as 'No current shooting data'"
        )


def _check_pick_projection(
    pick: Any, stats_df: pd.DataFrame, failures: list[str]
) -> None:
    """A contender's own first must project to a late pick, not the lottery."""
    wins = _pick_team_wins(pick, stats_df)
    if wins <= CONTENDER_WINS:
        return
    number = _projected_pick_number(pick, wins)
    if number <= LATE_PICK_FLOOR:
        rnd = "1st" if pick.round == 1 else "2nd"
        failures.append(
            f"{pick.team} {rnd} projected pick {number} "
            f"(expected > {LATE_PICK_FLOOR} for {wins:.0f}-win team)"
        )


# ---------------------------------------------------------------------------
# Per-trade driver
# ---------------------------------------------------------------------------


def evaluate_trade(
    spec: dict[str, Any],
    *,
    epm_df: pd.DataFrame,
    stats_df: pd.DataFrame,
    salary_df: pd.DataFrame,
    darko_df: pd.DataFrame,
    stats_lookup: dict[str, pd.Series],
    crosswalk: Crosswalk,
) -> TradeResult:
    """Run one trade through the full pipeline and the universal sanity suite."""
    name = spec["name"]
    declared = spec.get("checks", [])
    failures: list[str] = []

    info_a = resolve_team(spec["team_a"])
    info_b = resolve_team(spec["team_b"])
    if info_a is None or info_b is None:
        bad = spec["team_a"] if info_a is None else spec["team_b"]
        return TradeResult(
            name, False, "error", "—", [f"unknown team abbreviation '{bad}'"], declared
        )

    team_a = _build_team(info_a, salary_df)
    team_b = _build_team(info_b, salary_df)
    roster_ids_a = fetch_roster_player_ids(info_a.abbreviation)
    roster_ids_b = fetch_roster_player_ids(info_b.abbreviation)

    try:
        players_a, picks_a = _parse_assets(
            spec["sends_a"],
            epm_df,
            stats_lookup,
            salary_df,
            info_a.abbreviation,
            crosswalk,
            roster_ids_a,
        )
        players_b, picks_b = _parse_assets(
            spec["sends_b"],
            epm_df,
            stats_lookup,
            salary_df,
            info_b.abbreviation,
            crosswalk,
            roster_ids_b,
        )
    except (typer.Exit, SystemExit):
        return TradeResult(
            name,
            False,
            "error",
            "—",
            ["could not resolve one or more players (see message above)"],
            declared,
        )

    trade = Trade(
        team_a=team_a,
        team_b=team_b,
        team_a_sends=TradeAssets(players=players_a, picks=picks_a),
        team_b_sends=TradeAssets(players=players_b, picks=picks_b),
    )
    grade = grade_trade(
        trade,
        player_stats_df=stats_df,
        epm_df=epm_df,
        darko_df=darko_df,
        roster_ids_by_team={
            info_a.abbreviation: roster_ids_a,
            info_b.abbreviation: roster_ids_b,
        },
    )

    expected_legal = spec["expected_legal"]

    # --- Legality -------------------------------------------------------
    if grade.is_legal != expected_legal:
        if expected_legal:
            failures.append(f"expected legal but ruled illegal: {grade.illegal_reason}")
        else:
            failures.append("expected illegal but ruled legal")

    if not expected_legal:
        # Illegal trades short-circuit in the grader — both grades None.
        summary = "illegal" if not grade.is_legal else "legal"
        if not grade.is_legal and (
            grade.team_a_grade is not None or grade.team_b_grade is not None
        ):
            failures.append("illegal trade still produced a team grade")
        return TradeResult(name, not failures, summary, "illegal", failures, declared)

    if not grade.is_legal:
        # Expected legal but wasn't — can't run the score-dependent checks.
        return TradeResult(name, False, "illegal", _range_str(spec), failures, declared)

    ga = grade.team_a_grade
    gb = grade.team_b_grade
    assert ga is not None and gb is not None  # legal ⇒ both populated
    score_a, score_b = ga.score, gb.score
    summary = f"{score_a}/{score_b}"

    # --- Score bands ----------------------------------------------------
    lo_a, hi_a = spec["score_range_a"]
    lo_b, hi_b = spec["score_range_b"]
    if not lo_a <= score_a <= hi_a:
        failures.append(
            f"{team_a.abbreviation} score {score_a} outside expected {lo_a}-{hi_a}"
        )
    if not lo_b <= score_b <= hi_b:
        failures.append(
            f"{team_b.abbreviation} score {score_b} outside expected {lo_b}-{hi_b}"
        )

    # --- Zero-sum -------------------------------------------------------
    if score_a + score_b != 100:
        failures.append(f"scores sum to {score_a + score_b}, not 100")

    # --- All seven breakdowns populated ---------------------------------
    for g, lbl in ((ga, team_a.abbreviation), (gb, team_b.abbreviation)):
        missing = [m for m in METRIC_FIELDS if getattr(g, m) is None]
        if missing:
            failures.append(f"{lbl} missing breakdowns: {', '.join(missing)}")

    # --- Prose verdict --------------------------------------------------
    for g, lbl in ((ga, team_a.abbreviation), (gb, team_b.abbreviation)):
        prose = (g.prose or "").strip()
        if len(prose) <= 50:
            failures.append(f"{lbl} prose verdict too short ({len(prose)} chars)")

    # --- Position minutes sanity ----------------------------------------
    for team, ids in ((team_a, roster_ids_a), (team_b, roster_ids_b)):
        minutes = _position_minutes(team, stats_df, ids)
        over = {b: round(m) for b, m in minutes.items() if m > MINUTES_CEILING}
        if over:
            failures.append(
                f"{team.abbreviation} position minutes exceed {MINUTES_CEILING:.0f}: "
                f"{over}"
            )

    # --- Per-player GP / spacing-prose honesty --------------------------
    # team A's outgoing players are acquired by team B (graded by gb), and
    # vice versa.
    for entry in players_a:
        _check_player_gp(
            entry.player.name, gb, team_b.abbreviation, stats_lookup, failures
        )
    for entry in players_b:
        _check_player_gp(
            entry.player.name, ga, team_a.abbreviation, stats_lookup, failures
        )

    # --- Pick projection sanity -----------------------------------------
    for pick in list(picks_a) + list(picks_b):
        _check_pick_projection(pick, stats_df, failures)

    return TradeResult(
        name, not failures, summary, _range_str(spec), failures, declared
    )


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def _print_report(results: list[TradeResult]) -> int:
    """Print the PASS/FAIL report; return the process exit code."""
    print()
    for r in results:
        status = "PASS" if r.passed else "FAIL"
        if r.summary in {"illegal", "error"}:
            line = f"{status}  {r.name} — {r.summary} (expected {r.expected})"
        else:
            line = f"{status}  {r.name} — {r.summary} (expected {r.expected})"
        print(line)
        if not r.passed:
            for reason in r.failures:
                print(f"        ✗ {reason}")

    passed = sum(1 for r in results if r.passed)
    failed = len(results) - passed
    print()
    print(f"{passed}/{len(results)} passed, {failed} failed")
    return 0 if failed == 0 else 1


def main() -> None:
    force_utf8_stdout()
    print("Loading data sources (stats, EPM, salaries, DARKO)...")
    epm_df, stats_df, salary_df = _load_core_data()
    darko_df = fetch_darko_data()
    stats_lookup = _stats_lookup(stats_df)
    crosswalk = load_crosswalk()

    results = [
        evaluate_trade(
            spec,
            epm_df=epm_df,
            stats_df=stats_df,
            salary_df=salary_df,
            darko_df=darko_df,
            stats_lookup=stats_lookup,
            crosswalk=crosswalk,
        )
        for spec in TRADES
    ]

    raise SystemExit(_print_report(results))


if __name__ == "__main__":
    main()
