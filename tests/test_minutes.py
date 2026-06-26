"""Unit tests for the two-model minutes projection (issue 2.2)."""

from __future__ import annotations

import math

from nba_trade_analyzer.engine.constants import (
    GAMES_RECENCY_DECAY,
    MPG_AGE_DELTA,
    MPG_AGE_DELTA_MAX_AGE,
    MPG_AGE_DELTA_MIN_AGE,
    MPG_CEILING,
    MPG_SALARY_COEF,
    MPG_SALARY_REF,
    PROJECTED_GAMES_CEILING,
)
from nba_trade_analyzer.engine.minutes import (
    MinutesProjection,
    cumulative_mpg_age_delta,
    project_games,
    project_minutes,
    project_mpg,
    recency_weighted_games_missed,
    recency_weighted_mpg,
)


# --- recency weighting -------------------------------------------------------


def test_recency_weights_latest_season_dominates():
    # Latest season missed 50%, older seasons fully healthy. A flat average
    # would give ~0.167 missed; recency weighting must land well above that.
    history = [82.0, 82.0, 41.0]  # oldest -> latest
    weighted = recency_weighted_games_missed(history)
    flat = recency_weighted_games_missed(history, flat_average=True)
    assert weighted > flat
    assert weighted > 0.25  # latest 50%-missed season is carrying most weight
    assert math.isclose(flat, (0.0 + 0.0 + 0.5) / 3, rel_tol=1e-9)


def test_recency_weights_geometric_shape():
    # With decay d over 3 seasons the weights are [d^2, d^1, d^0].
    history = [60.0, 70.0, 80.0]
    d = GAMES_RECENCY_DECAY
    w = [d**2, d**1, d**0]
    missed = [1 - 60 / 82, 1 - 70 / 82, 1 - 80 / 82]
    expected = sum(wi * mi for wi, mi in zip(w, missed)) / sum(w)
    assert math.isclose(recency_weighted_games_missed(history), expected, rel_tol=1e-9)


def test_no_history_returns_none():
    assert recency_weighted_games_missed([]) is None
    assert recency_weighted_mpg([]) is None


# --- games model -------------------------------------------------------------


def test_durable_player_projects_near_ceiling():
    games = project_games([78.0, 80.0, 79.0], age=25)
    assert games >= 74.0
    assert games <= PROJECTED_GAMES_CEILING


def test_injury_prone_player_drops_hard():
    games = project_games([40.0, 55.0, 30.0], age=28)
    assert games < 50.0


def test_age_haircut_reduces_out_year_games():
    history = [80.0, 80.0, 80.0]
    young = project_games(history, age=24, year_offset=0)
    old = project_games(history, age=37, year_offset=2)
    assert old < young


def test_no_history_falls_back_to_healthy_minus_age():
    young = project_games([], age=22)
    old = project_games([], age=38)
    assert young > old
    assert young <= PROJECTED_GAMES_CEILING


def test_games_bounded_to_ceiling():
    # Perfect 82-game history for a young player can't exceed the ceiling.
    assert project_games([82.0, 82.0, 82.0], age=23) == PROJECTED_GAMES_CEILING


# --- mpg model ---------------------------------------------------------------


def test_mpg_anchors_on_prior_at_reference_inputs():
    # Impact at ref and salary at ref => no nudge => prior MPG returned.
    assert math.isclose(
        project_mpg(28.0, impact=0.0, salary_share=MPG_SALARY_REF), 28.0, rel_tol=1e-9
    )


def test_mpg_rises_with_impact_and_salary():
    base = project_mpg(24.0, impact=0.0, salary_share=MPG_SALARY_REF)
    high_impact = project_mpg(24.0, impact=4.0, salary_share=MPG_SALARY_REF)
    high_salary = project_mpg(24.0, impact=0.0, salary_share=0.30)
    assert high_impact > base
    assert high_salary > base


def test_mpg_salary_term_is_bounded_in_magnitude():
    # A max-salary, league-average player gains only a few MPG from salary, not
    # a starter's worth — the term is deliberately small (salary guard rationale).
    base = project_mpg(20.0, impact=0.0, salary_share=MPG_SALARY_REF)
    maxed = project_mpg(20.0, impact=0.0, salary_share=0.35)
    salary_bump = maxed - base
    assert math.isclose(salary_bump, (0.35 - MPG_SALARY_REF) * MPG_SALARY_COEF, rel_tol=1e-9)
    assert salary_bump < 4.0


def test_mpg_flat_flag_ignores_impact_and_salary():
    flat = project_mpg(26.0, impact=5.0, salary_share=0.35, flat=True)
    assert math.isclose(flat, 26.0, rel_tol=1e-9)


# --- combined ----------------------------------------------------------------


def test_project_minutes_exposes_both_factors():
    proj = project_minutes(
        gp_history=[70.0, 72.0, 68.0],
        mpg_history=[30.0, 31.0, 29.0],
        age=29,
        impact=2.0,
        salary_share=0.20,
    )
    assert isinstance(proj, MinutesProjection)
    assert proj.projected_games > 0
    assert proj.projected_mpg > 0
    assert math.isclose(
        proj.projected_minutes,
        proj.projected_games * proj.projected_mpg,
        rel_tol=1e-9,
    )


def test_injury_prone_loses_minutes_vs_durable_same_role():
    durable = project_minutes(
        [80.0, 79.0, 81.0], [30.0, 30.0, 30.0], age=27, impact=1.0, salary_share=0.15
    )
    fragile = project_minutes(
        [45.0, 30.0, 35.0], [30.0, 30.0, 30.0], age=27, impact=1.0, salary_share=0.15
    )
    # Same role/impact/salary => identical MPG; minutes gap is pure availability.
    assert math.isclose(durable.projected_mpg, fragile.projected_mpg, rel_tol=1e-9)
    assert fragile.projected_minutes < durable.projected_minutes * 0.7


# --- minutes aging curve (Phase 1-3) ----------------------------------------

def test_age_curve_shape_young_bump_prime_decline():
    """Young bump (positive), prime near zero, increasingly negative past prime."""
    assert MPG_AGE_DELTA[21] > 0.5          # young ramp
    assert abs(MPG_AGE_DELTA[26]) < 0.4     # ~prime, near zero
    assert MPG_AGE_DELTA[34] < MPG_AGE_DELTA[30] < 0  # steepening decline


def test_age_curve_non_positive_and_non_increasing_past_prime():
    """Tail guard: never positive, never drifts back toward zero past prime."""
    past_prime = [a for a in MPG_AGE_DELTA if a >= 31]
    for a in past_prime:
        assert MPG_AGE_DELTA[a] <= 0, f"age {a} must be non-positive"
    ordered = sorted(past_prime)
    for older, younger in zip(ordered[1:], ordered):
        assert MPG_AGE_DELTA[older] <= MPG_AGE_DELTA[younger], (
            f"age {older} delta drifted up vs {younger}"
        )


def test_cumulative_zero_at_current_season():
    """year_offset 0 applies no aging (doesn't repeat the current-age delta)."""
    assert cumulative_mpg_age_delta(30, 0) == 0.0


def test_cumulative_sums_forward_by_projected_age():
    """Cumulative = sum of deltas at age+1 .. age+offset, not the current age."""
    age = 33
    expected = MPG_AGE_DELTA[34] + MPG_AGE_DELTA[35] + MPG_AGE_DELTA[36]
    assert math.isclose(cumulative_mpg_age_delta(age, 3), expected, rel_tol=1e-9)
    # Strictly more decline two years out than one year out for a vet.
    assert cumulative_mpg_age_delta(age, 2) < cumulative_mpg_age_delta(age, 1) < 0


def test_cumulative_clamps_outside_fitted_range():
    """41+ keeps shedding the steepest fitted decline; <20 holds the young bump."""
    steepest = MPG_AGE_DELTA[MPG_AGE_DELTA_MAX_AGE]
    # A 44-year-old projected 3 years out ages entirely past the table -> 3x floor.
    assert math.isclose(cumulative_mpg_age_delta(44, 3), steepest * 3, rel_tol=1e-9)
    youngest = MPG_AGE_DELTA[MPG_AGE_DELTA_MIN_AGE]
    assert math.isclose(cumulative_mpg_age_delta(18, 1), youngest, rel_tol=1e-9)


def test_project_mpg_applies_aging_vet_declines_young_ramps():
    """With age threaded, a vet's out-year mpg drops and a youngster's rises."""
    vet_now = project_mpg(34.0, impact=2.0, salary_share=0.30, age=34, year_offset=0)
    vet_2y = project_mpg(34.0, impact=2.0, salary_share=0.30, age=34, year_offset=2)
    assert vet_2y < vet_now  # aging beats the (constant) impact/salary nudge

    young_now = project_mpg(22.0, impact=1.0, salary_share=0.10, age=21, year_offset=0)
    young_2y = project_mpg(22.0, impact=1.0, salary_share=0.10, age=21, year_offset=2)
    assert young_2y > young_now  # young bump ramps minutes up


def test_project_mpg_ceiling_is_36():
    """A max-impact star clamps at the re-tuned 36 ceiling, not 38."""
    star = project_mpg(38.0, impact=8.0, salary_share=0.35, age=27, year_offset=0)
    assert star == MPG_CEILING == 36.0
