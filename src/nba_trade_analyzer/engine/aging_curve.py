"""NBA aging curve for projecting EPM forward year-by-year.

The curve is a piecewise-constant annual rate compounded across the years
between ``current_age`` and ``current_age + years_forward``. Each year of
aging picks up the rate of the bracket containing the player's age at the
start of that year, so the resulting factor is continuous in both age and
horizon — no jumps when a player ages into a new bracket mid-projection.

Bracket rates live in ``engine/constants.py`` (see ``AGING_*``).
"""

from __future__ import annotations

from nba_trade_analyzer.engine.constants import (
    AGING_DECLINE_RATE_30_32,
    AGING_DECLINE_RATE_33_35,
    AGING_DECLINE_RATE_36_PLUS,
    AGING_GROWTH_RATE_20_24,
    AGING_GROWTH_RATE_25_27,
    AGING_PLATEAU_RATE_28_29,
)


def _annual_rate(age: int) -> float:
    """Per-year EPM change rate for a player at the given age."""
    if age <= 24:
        return AGING_GROWTH_RATE_20_24
    if age <= 27:
        return AGING_GROWTH_RATE_25_27
    if age <= 29:
        return AGING_PLATEAU_RATE_28_29
    if age <= 32:
        return AGING_DECLINE_RATE_30_32
    if age <= 35:
        return AGING_DECLINE_RATE_33_35
    return AGING_DECLINE_RATE_36_PLUS


def get_aging_factor(current_age: int, years_forward: int) -> float:
    """Multiplier on EPM for a player aged ``years_forward`` years from now.

    ``years_forward=0`` always returns 1.0 (the current-season anchor).
    Negative horizons are not supported and clamped to 0. The result is
    monotonically increasing during the growth brackets, ≈1.0 during the
    prime plateau, and monotonically decreasing thereafter.
    """
    if years_forward <= 0:
        return 1.0
    factor = 1.0
    for offset in range(years_forward):
        age_this_year = current_age + offset
        factor *= 1.0 + _annual_rate(age_this_year)
    return factor
