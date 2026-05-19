from __future__ import annotations

import math

import pandas as pd
import pytest

from nba_trade_analyzer.engine.constants import (
    DOLLARS_PER_WIN,
    LEAGUE_AVG_3PT_PCT,
    LEAGUE_AVG_3PT_RATE,
    POSITIONAL_MAX_ADJUSTMENT,
    SPACING_MAX_ADJUSTMENT,
    TIMELINE_MAX_ADJUSTMENT,
    WIN_CURVE_MAX_MULTIPLIER,
    WIN_CURVE_MIDPOINT,
    WIN_CURVE_MIN_MULTIPLIER,
)
from nba_trade_analyzer.engine.team_context import (
    calculate_positional_modifier,
    calculate_spacing_modifier,
    calculate_timeline_modifier,
    calculate_win_curve_multiplier,
    evaluate_player_in_team_context,
)
from nba_trade_analyzer.engine.valuation import TeamContext, evaluate_trade_assets
from nba_trade_analyzer.models.player import Contract, Player
from nba_trade_analyzer.models.team import RosterEntry
from nba_trade_analyzer.models.trade import TradeAssets


# ----- win curve ---------------------------------------------------------


def test_win_curve_bounded_by_min_and_max():
    # Extremes saturate at the bounds without overflow.
    assert calculate_win_curve_multiplier(0) >= WIN_CURVE_MIN_MULTIPLIER
    assert calculate_win_curve_multiplier(100) <= WIN_CURVE_MAX_MULTIPLIER
    assert calculate_win_curve_multiplier(-50) >= WIN_CURVE_MIN_MULTIPLIER
    assert calculate_win_curve_multiplier(200) <= WIN_CURVE_MAX_MULTIPLIER


def test_win_curve_midpoint_is_between_min_and_max():
    mid = calculate_win_curve_multiplier(WIN_CURVE_MIDPOINT)
    expected = (WIN_CURVE_MIN_MULTIPLIER + WIN_CURVE_MAX_MULTIPLIER) / 2
    assert mid == pytest.approx(expected, rel=1e-6)


def test_win_curve_tanking_team_low_multiplier():
    mult = calculate_win_curve_multiplier(20)
    assert WIN_CURVE_MIN_MULTIPLIER <= mult <= 0.6


def test_win_curve_bubble_team_high_multiplier():
    # 42 wins is the midpoint; should be peak marginal value zone.
    mult = calculate_win_curve_multiplier(42)
    assert mult > 1.0


def test_win_curve_contender_higher_than_bubble():
    bubble = calculate_win_curve_multiplier(42)
    contender = calculate_win_curve_multiplier(55)
    # Sigmoid is monotonically increasing — contender beats bubble.
    assert contender > bubble


def test_win_curve_juggernaut_approaches_ceiling():
    mult = calculate_win_curve_multiplier(60)
    assert mult > 1.5 * (WIN_CURVE_MIN_MULTIPLIER + WIN_CURVE_MAX_MULTIPLIER) / 2
    assert mult <= WIN_CURVE_MAX_MULTIPLIER


def test_win_curve_calibration_table_prints(capsys):
    """Eyeball table for tuning STEEPNESS/MIDPOINT/bounds."""
    targets = [15, 20, 25, 30, 35, 38, 41, 42, 45, 50, 55, 60, 65]
    print("\nwin curve calibration:")
    print("  wins  multiplier")
    for wins in targets:
        print(f"  {wins:4d}  {calculate_win_curve_multiplier(wins):.3f}")
    out = capsys.readouterr().out
    assert "win curve calibration" in out


# ----- timeline ----------------------------------------------------------


def test_timeline_perfect_age_match_is_peak_bonus():
    # Player 24 on a team where the core averages 24 → near-perfect alignment.
    mod = calculate_timeline_modifier(24, [22, 23, 24, 25, 26])
    assert mod == pytest.approx(TIMELINE_MAX_ADJUSTMENT, abs=0.005)


def test_timeline_large_gap_is_strong_penalty():
    # 38-year-old joining a 23-year-old core: ~15 year gap → near floor.
    mod = calculate_timeline_modifier(38, [22, 23, 24, 23, 25])
    assert mod < -0.5 * TIMELINE_MAX_ADJUSTMENT
    assert mod >= -TIMELINE_MAX_ADJUSTMENT


def test_timeline_three_year_gap_small_effect():
    # Brief described spec: 3-year gap ≈ ~5% penalty or less in magnitude.
    mod = calculate_timeline_modifier(27, [24, 24, 24, 24, 24])
    assert -TIMELINE_MAX_ADJUSTMENT < mod < TIMELINE_MAX_ADJUSTMENT
    assert abs(mod) < 0.06


def test_timeline_no_core_returns_zero():
    assert calculate_timeline_modifier(28, []) == 0.0


def test_timeline_modifier_bounded():
    # Even a 60-year gap shouldn't blow past the cap.
    mod = calculate_timeline_modifier(80, [20])
    assert mod >= -TIMELINE_MAX_ADJUSTMENT - 1e-9


# ----- positional fit ---------------------------------------------------


def _roster_with_position_minutes(
    g_mpg: float, f_mpg: float, c_mpg: float
) -> list[dict]:
    """One synthetic player per coarse position bucket with the given MPG."""
    return [
        {"player_name": "G1", "MPG": g_mpg, "GP": 70, "age": 25, "position": "G"},
        {"player_name": "F1", "MPG": f_mpg, "GP": 70, "age": 25, "position": "F"},
        {"player_name": "C1", "MPG": c_mpg, "GP": 70, "age": 25, "position": "C"},
    ]


def test_positional_player_fills_empty_position_is_bonus():
    # Center slot is completely empty.
    roster = _roster_with_position_minutes(g_mpg=72, f_mpg=72, c_mpg=10)
    mod = calculate_positional_modifier("C", roster)
    assert mod == pytest.approx(POSITIONAL_MAX_ADJUSTMENT, abs=1e-6)


def test_positional_player_in_logjam_is_penalty():
    # Forward slot is overloaded.
    roster = _roster_with_position_minutes(g_mpg=48, f_mpg=80, c_mpg=48)
    mod = calculate_positional_modifier("F", roster)
    assert mod == pytest.approx(-POSITIONAL_MAX_ADJUSTMENT, abs=1e-6)


def test_positional_balanced_roster_near_zero():
    # All buckets at the mid-band (~32 MPG): modifier within ±MAX.
    roster = _roster_with_position_minutes(g_mpg=32, f_mpg=32, c_mpg=32)
    mod = calculate_positional_modifier("G", roster)
    assert -POSITIONAL_MAX_ADJUSTMENT < mod < POSITIONAL_MAX_ADJUSTMENT


def test_positional_no_position_label_returns_zero():
    roster = _roster_with_position_minutes(g_mpg=32, f_mpg=32, c_mpg=32)
    assert calculate_positional_modifier(None, roster) == 0.0
    assert calculate_positional_modifier("", roster) == 0.0


def test_positional_split_position_blends():
    # G-F label averages G and F minutes contributions.
    roster = _roster_with_position_minutes(g_mpg=10, f_mpg=80, c_mpg=40)
    mod = calculate_positional_modifier("G-F", roster)
    # G is thin (+MAX), F is logjam (-MAX) — split should land near 0.
    assert abs(mod) < 0.5 * POSITIONAL_MAX_ADJUSTMENT


# ----- spacing ----------------------------------------------------------


def test_spacing_elite_shooter_on_spacing_poor_team_positive():
    # Elite shooter, team well below league average.
    mod = calculate_spacing_modifier(
        player_3pt_rate=0.55,
        player_3pt_pct=0.41,
        team_3pt_rate=0.28,
        team_3pt_pct=0.33,
    )
    assert mod > 0
    assert mod <= SPACING_MAX_ADJUSTMENT


def test_spacing_non_shooter_on_spacing_poor_team_negative():
    mod = calculate_spacing_modifier(
        player_3pt_rate=0.08,
        player_3pt_pct=0.25,
        team_3pt_rate=0.28,
        team_3pt_pct=0.33,
    )
    assert mod < 0
    assert mod >= -SPACING_MAX_ADJUSTMENT


def test_spacing_modifier_shrinks_for_elite_spacing_team():
    # Same player profile; one team has elite spacing, other is poor.
    poor_team = calculate_spacing_modifier(
        player_3pt_rate=0.55,
        player_3pt_pct=0.41,
        team_3pt_rate=0.28,
        team_3pt_pct=0.33,
    )
    elite_team = calculate_spacing_modifier(
        player_3pt_rate=0.55,
        player_3pt_pct=0.41,
        team_3pt_rate=0.52,
        team_3pt_pct=0.39,
    )
    # Elite-spacing team values the same shooter less (spacing isn't binding).
    assert abs(elite_team) < abs(poor_team)


def test_spacing_modifier_bounded():
    # Absurd inputs still respect the cap.
    mod = calculate_spacing_modifier(
        player_3pt_rate=5.0,
        player_3pt_pct=1.0,
        team_3pt_rate=-1.0,
        team_3pt_pct=0.0,
    )
    assert -SPACING_MAX_ADJUSTMENT <= mod <= SPACING_MAX_ADJUSTMENT


def test_spacing_league_average_inputs_are_neutral():
    mod = calculate_spacing_modifier(
        player_3pt_rate=LEAGUE_AVG_3PT_RATE,
        player_3pt_pct=LEAGUE_AVG_3PT_PCT,
        team_3pt_rate=LEAGUE_AVG_3PT_RATE,
        team_3pt_pct=LEAGUE_AVG_3PT_PCT,
    )
    assert mod == pytest.approx(0.0, abs=1e-6)


# ----- top-level integration --------------------------------------------


def _epm_with(name: str, epm: float, position: str = "F") -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "player_name": name,
                "player_name_normalized": name.casefold(),
                "team": "TST",
                "epm": epm,
                "epm_off": epm * 0.6,
                "epm_def": epm * 0.4,
                "mpg": 36.0,
                "position": position,
                "age": 26,
            }
        ]
    )


def _player_stats_with(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


def _young_rebuild_roster() -> list[dict]:
    """Detroit-ish 20-win team built around 22-25-year-olds."""
    return [
        {
            "player_name": "Ivey",
            "MPG": 28,
            "GP": 70,
            "age": 23,
            "position": "G",
            "FG3_RATE": 0.35,
            "FG3_PCT": 0.33,
        },
        {
            "player_name": "Duren",
            "MPG": 26,
            "GP": 70,
            "age": 22,
            "position": "C",
            "FG3_RATE": 0.02,
            "FG3_PCT": 0.10,
        },
        {
            "player_name": "AusReaves",
            "MPG": 24,
            "GP": 65,
            "age": 25,
            "position": "G",
            "FG3_RATE": 0.30,
            "FG3_PCT": 0.34,
        },
        {
            "player_name": "Stewart",
            "MPG": 22,
            "GP": 60,
            "age": 25,
            "position": "F",
            "FG3_RATE": 0.20,
            "FG3_PCT": 0.30,
        },
        {
            "player_name": "Bagley",
            "MPG": 20,
            "GP": 55,
            "age": 26,
            "position": "F",
            "FG3_RATE": 0.10,
            "FG3_PCT": 0.28,
        },
    ]


def _contender_roster() -> list[dict]:
    """Lakers-ish ~48-win team with an older core."""
    return [
        {
            "player_name": "AD",
            "MPG": 35,
            "GP": 65,
            "age": 32,
            "position": "C",
            "FG3_RATE": 0.10,
            "FG3_PCT": 0.27,
        },
        {
            "player_name": "Reaves",
            "MPG": 33,
            "GP": 75,
            "age": 27,
            "position": "G",
            "FG3_RATE": 0.42,
            "FG3_PCT": 0.37,
        },
        {
            "player_name": "Hachimura",
            "MPG": 30,
            "GP": 70,
            "age": 28,
            "position": "F",
            "FG3_RATE": 0.38,
            "FG3_PCT": 0.41,
        },
        {
            "player_name": "Russell",
            "MPG": 28,
            "GP": 70,
            "age": 30,
            "position": "G",
            "FG3_RATE": 0.45,
            "FG3_PCT": 0.38,
        },
        {
            "player_name": "Vincent",
            "MPG": 22,
            "GP": 60,
            "age": 31,
            "position": "G",
            "FG3_RATE": 0.40,
            "FG3_PCT": 0.35,
        },
    ]


def test_evaluate_in_context_cade_to_young_rebuild():
    """Cade-shaped player: 24, ~+3 EPM, on a 22-win Pistons-ish team.

    Win curve drags him down (tanking team), but timeline alignment with
    the young core should pull at least some of that back.
    """
    cade = Player(
        name="Cade Cunningham",
        team="DET",
        age=24,
        stats={"NET_RATING": 1.0, "GP": 70, "MPG": 35.0},
    )
    contract = Contract(salary=46_000_000, years_remaining=5)
    epm = _epm_with("Cade Cunningham", 3.0, position="G")
    player_stats = _player_stats_with(
        [
            {
                "player_name": "Cade Cunningham",
                "FG3_RATE": 0.32,
                "FG3_PCT": 0.36,
                "position": "G",
            },
        ]
    )

    base_player_value = 8.0 * DOLLARS_PER_WIN  # placeholder; just need a number
    valuation = evaluate_player_in_team_context(
        player_value=base_player_value,
        wins_added=8.0,
        player=cade,
        contract=contract,
        acquiring_team_wins=22.0,
        acquiring_team_roster=_young_rebuild_roster(),
        epm_df=epm,
        player_stats_df=player_stats,
    )

    # Tanking team → multiplier well below 1.0.
    assert valuation.win_curve_multiplier < 1.0
    # Young player + young core → positive timeline alignment.
    assert valuation.timeline_modifier > 0
    # Team-adjusted value should be below base on the win curve alone.
    assert valuation.team_adjusted_value < base_player_value


def test_evaluate_in_context_veteran_to_contender():
    """Veteran-shaped player on a contender: win curve helps, timeline hurts."""
    veteran = Player(
        name="LeBron James",
        team="LAL",
        age=40,
        stats={"NET_RATING": 6.0, "GP": 70, "MPG": 35.0},
    )
    contract = Contract(salary=52_000_000, years_remaining=1)
    epm = _epm_with("LeBron James", 4.5, position="F")
    player_stats = _player_stats_with(
        [
            {
                "player_name": "LeBron James",
                "FG3_RATE": 0.30,
                "FG3_PCT": 0.35,
                "position": "F",
            },
        ]
    )

    valuation = evaluate_player_in_team_context(
        player_value=12.0 * DOLLARS_PER_WIN,
        wins_added=12.0,
        player=veteran,
        contract=contract,
        acquiring_team_wins=48.0,
        acquiring_team_roster=_contender_roster(),
        epm_df=epm,
        player_stats_df=player_stats,
    )

    # Contender (48 wins) → multiplier > 1.0.
    assert valuation.win_curve_multiplier > 1.0
    # 40-year-old on a ~29-year-old core → negative timeline.
    assert valuation.timeline_modifier < 0


def test_evaluate_trade_assets_with_team_context_differs_from_base():
    """The team_context kwarg must change the returned surplus number."""
    star = Player(
        name="Star",
        team="TST",
        age=26,
        stats={"NET_RATING": 5.0, "GP": 80, "MPG": 35.0},
    )
    contract = Contract(salary=30_000_000, years_remaining=3)
    assets = TradeAssets(players=[RosterEntry(player=star, contract=contract)])

    epm = _epm_with("Star", 5.0, position="G")
    context = TeamContext(
        projected_wins=50.0,
        roster=_contender_roster(),
        player_stats_df=_player_stats_with(
            [
                {
                    "player_name": "Star",
                    "FG3_RATE": 0.40,
                    "FG3_PCT": 0.38,
                    "position": "G",
                },
            ]
        ),
    )

    base_surplus = evaluate_trade_assets(assets, epm_df=epm, darko_df=pd.DataFrame())
    context_surplus = evaluate_trade_assets(
        assets, epm_df=epm, darko_df=pd.DataFrame(), team_context=context
    )

    # Contender pulls value above the baseline by the win-curve multiplier.
    assert context_surplus != pytest.approx(base_surplus)
    assert context_surplus > base_surplus


def test_evaluate_trade_assets_without_context_unchanged():
    """Calling without team_context must produce the same number as before."""
    star = Player(
        name="Star",
        team="TST",
        age=26,
        stats={"NET_RATING": 5.0, "GP": 80, "MPG": 35.0},
    )
    contract = Contract(salary=30_000_000, years_remaining=3)
    assets = TradeAssets(players=[RosterEntry(player=star, contract=contract)])

    epm = _epm_with("Star", 5.0, position="G")
    # No team_context → identical to the Phase 4 surplus.
    result = evaluate_trade_assets(assets, epm_df=epm, darko_df=pd.DataFrame())
    assert isinstance(result, float) or isinstance(result, int)
    # Just a smoke check — actual number is tested in test_valuation.py.
    assert math.isfinite(result)
