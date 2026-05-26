"""Tests for the EPM aging curve."""

from __future__ import annotations

import pytest

from nba_trade_analyzer.engine.aging_curve import get_aging_factor
from nba_trade_analyzer.engine.constants import (
    AGING_DECLINE_RATE_30_32,
    AGING_DECLINE_RATE_33_35,
    AGING_GROWTH_RATE_20_24,
)


# ----- identity at horizon 0 ----------------------------------------------


@pytest.mark.parametrize("age", [20, 24, 27, 30, 35, 40])
def test_factor_is_identity_at_horizon_zero(age: int) -> None:
    assert get_aging_factor(age, 0) == pytest.approx(1.0)


def test_negative_horizon_clamped_to_identity() -> None:
    # Aging "backward" isn't meaningful — function should not crash, and
    # should return 1.0 so downstream multiplications no-op.
    assert get_aging_factor(27, -3) == pytest.approx(1.0)


# ----- growth phase --------------------------------------------------------


def test_growth_phase_22_year_old_three_years_out_above_one() -> None:
    assert get_aging_factor(22, 3) > 1.0


def test_growth_phase_compounds_with_horizon() -> None:
    # Each additional year in the growth phase should multiply the factor
    # by another (1 + growth_rate), so longer horizons strictly increase
    # the projected EPM.
    one_year = get_aging_factor(22, 1)
    two_years = get_aging_factor(22, 2)
    three_years = get_aging_factor(22, 3)
    assert one_year < two_years < three_years


# ----- decline phase -------------------------------------------------------


def test_decline_phase_33_year_old_three_years_out_below_one() -> None:
    assert get_aging_factor(33, 3) < 1.0


def test_sharp_decline_36_year_old_three_years_significantly_below_one() -> None:
    # Three years at -10%/yr compounds to roughly 0.73.
    factor = get_aging_factor(36, 3)
    assert factor < 0.80


def test_decline_compounds_with_horizon() -> None:
    one_year = get_aging_factor(34, 1)
    two_years = get_aging_factor(34, 2)
    three_years = get_aging_factor(34, 3)
    assert one_year > two_years > three_years


# ----- plateau -------------------------------------------------------------


def test_plateau_28_29_is_flat() -> None:
    # 28 and 29 are pure-plateau years — factor stays at 1.0 for short
    # horizons that don't yet cross into the decline brackets.
    assert get_aging_factor(28, 1) == pytest.approx(1.0)
    assert get_aging_factor(28, 2) == pytest.approx(1.0)


# ----- bracket continuity --------------------------------------------------


def test_bracket_boundary_24_to_25_no_discontinuity() -> None:
    # Aging a 24-year-old one year (24 is still growth bracket) and a
    # 25-year-old one year (25 is in 25-27 bracket) should differ only
    # by the per-year rate change — no jumps.
    factor_24 = get_aging_factor(24, 1)
    factor_25 = get_aging_factor(25, 1)
    assert factor_24 > factor_25  # 24-year-old grows faster
    # Both factors are within ~5% of each other — the curve doesn't jump.
    assert abs(factor_24 - factor_25) < 0.05


def test_bracket_boundary_29_to_30_no_discontinuity() -> None:
    factor_29 = get_aging_factor(29, 1)  # plateau → 1.0
    factor_30 = get_aging_factor(30, 1)  # decline → 0.97
    assert factor_29 > factor_30
    assert abs(factor_29 - factor_30) < 0.05


def test_bracket_boundary_32_to_33_no_discontinuity() -> None:
    factor_32 = get_aging_factor(32, 1)
    factor_33 = get_aging_factor(33, 1)
    assert factor_32 > factor_33
    assert abs(factor_32 - factor_33) < 0.05


# ----- compounding correctness ---------------------------------------------


def test_compounding_matches_explicit_product_growth() -> None:
    # 23-year-old projected 2 years out: applies growth rate twice (age 23, age 24).
    expected = (1 + AGING_GROWTH_RATE_20_24) ** 2
    assert get_aging_factor(23, 2) == pytest.approx(expected)


def test_compounding_matches_explicit_product_decline() -> None:
    # 33-year-old projected 3 years out: 33, 34, 35 all in -6.5% bracket.
    expected = (1 + AGING_DECLINE_RATE_33_35) ** 3
    assert get_aging_factor(33, 3) == pytest.approx(expected)


def test_compounding_mixes_brackets_correctly() -> None:
    # 32-year-old, 2 years out: ages 32 (-3%) and 33 (-6.5%).
    expected = (1 + AGING_DECLINE_RATE_30_32) * (1 + AGING_DECLINE_RATE_33_35)
    assert get_aging_factor(32, 2) == pytest.approx(expected)


# ----- diagnostic table ----------------------------------------------------


def test_print_full_aging_table(capsys: pytest.CaptureFixture[str]) -> None:
    """Diagnostic — eyeball the curve across ages 20-40, horizons 1-5."""
    print()
    header = "age  " + "  ".join(f"+{y}y" for y in range(0, 6))
    print(header)
    print("-" * len(header))
    for age in range(20, 41):
        cells = [f"{get_aging_factor(age, y):0.3f}" for y in range(0, 6)]
        print(f"{age:>3}  " + "  ".join(cells))
    captured = capsys.readouterr()
    assert "age" in captured.out
