"""Draft pick value curve.

Approximates the surplus value of a draft pick (production over the full
rookie contract, net of rookie-scale salary) using an exponential decay
across pick numbers 1-60, with a sharp penalty after pick 30 to reflect
the lack of guaranteed contracts in the second round.
"""

from __future__ import annotations

from math import exp

from nba_trade_analyzer.engine.constants import (
    CURRENT_SEASON_START,
    LEAGUE_MEAN_WINS,
    PICK_REGRESSION_RATE,
    PICK_SWAP_VALUE_FRACTION,
    PICK_VALUE_DECAY,
    PICK_VALUE_SCALE,
    PICK_VALUE_SECOND_ROUND_PENALTY,
    PICK_YEAR_DISCOUNT_RATE,
)
from nba_trade_analyzer.models.draft_pick import DraftPick

FIRST_PICK = 1
LAST_PICK = 60
SECOND_ROUND_START = 31
FIRST_ROUND_PICKS = 30
NUM_TEAMS = 30
REGULAR_SEASON_GAMES = 82

# Multiplicative discount applied to a protected pick's value. The chance a
# protected pick simply doesn't convey (and the holder gets a worse asset, or
# nothing this year) scales with how much of the draft the protection covers.
# Lookup is by the human-readable protection string on DraftPick; anything
# unrecognized falls back to DEFAULT_PROTECTION_DISCOUNT.
# TODO: replace this lookup table with a structured PickProtection model and a
# probabilistic convey/no-convey integral over the team's standing distribution
# (Travis Chen's approach) once a pick-projection source exists.
PROTECTION_DISCOUNT: dict[str | None, float] = {
    None: 1.0,
    "unprotected": 1.0,
    "top-1 protected": 0.95,
    "top-3 protected": 0.85,
    "top-4 protected": 0.80,
    "top-5 protected": 0.75,
    "top-10 protected": 0.55,
    "lottery protected": 0.45,  # top-14 protected
}
DEFAULT_PROTECTION_DISCOUNT = 0.70


def calculate_pick_value(pick_number: int) -> float:
    if pick_number < FIRST_PICK or pick_number > LAST_PICK:
        raise ValueError(
            f"pick_number must be between {FIRST_PICK} and {LAST_PICK}, "
            f"got {pick_number}."
        )
    curve = PICK_VALUE_SCALE * exp(-PICK_VALUE_DECAY * (pick_number - 1))
    if pick_number >= SECOND_ROUND_START:
        curve *= PICK_VALUE_SECOND_ROUND_PENALTY
    return curve


def calculate_pick_value_with_protections(
    pick_number: int, protection_top: int | None = None
) -> float:
    """Discount a protected pick.

    If `protection_top` is set and the pick lands inside that range, treat
    it as not conveying (value = 0). Otherwise return the full curve value.

    TODO: this is a sharp cutoff; a future version should integrate over the
    probability distribution of where the pick actually lands (e.g. weighted
    lottery odds × convey/no-convey × value at each slot) instead of taking
    a single realized pick_number as input.
    """
    full_value = calculate_pick_value(pick_number)
    if protection_top is None:
        return full_value
    if pick_number <= protection_top:
        return 0.0
    return full_value


def get_pick_value_curve() -> dict[int, float]:
    return {n: calculate_pick_value(n) for n in range(FIRST_PICK, LAST_PICK + 1)}


def estimate_pick_position(team_projected_wins: float, year_offset: int = 0) -> float:
    """Estimate where a team's first-round pick will land from its record.

    The worst team (0 wins) is expected to pick 1st and the best (82 wins)
    30th — i.e. *more* projected wins means a *later*, less valuable pick. This
    is intentionally a simple linear expected-value map; the lottery adds
    variance around it, but the expectation is still monotonic with record.

    For future years (``year_offset > 0``) the team's record is first regressed
    toward the league mean to reflect uncertainty — a 60-win team won't stay
    elite forever, a 20-win team won't stay bad. ``PICK_REGRESSION_RATE`` sets
    how fast: ``regression_factor = 1 - PICK_REGRESSION_RATE ** year_offset``,
    so a 60-win team projects to ~49 wins three years out.

    Returns a (possibly fractional) pick number in ``[1, 30]``. Second-round
    handling lives in :func:`evaluate_draft_pick`.

    NOTE: the originating spec wrote this map as ``30 - (wins / 82) * 29``,
    which inverts the orientation it states in the same breath ("worst = pick 1,
    best = pick 30") and would make a contender's pick more valuable than a
    tanking team's. We implement the stated orientation instead.
    """
    if year_offset > 0:
        regression_factor = 1.0 - PICK_REGRESSION_RATE**year_offset
        adjusted_wins = (
            team_projected_wins
            + (LEAGUE_MEAN_WINS - team_projected_wins) * regression_factor
        )
    else:
        adjusted_wins = team_projected_wins

    position = 1.0 + (adjusted_wins / REGULAR_SEASON_GAMES) * (NUM_TEAMS - 1)
    return min(float(FIRST_ROUND_PICKS), max(float(FIRST_PICK), position))


def evaluate_draft_pick(pick: DraftPick, team_projected_wins: float) -> float:
    """Value a draft pick, team-aware.

    1. Estimate the pick's landing slot from the originating team's projected
       wins (regressed toward the mean for future years).
    2. Map that slot to a dollar value via the existing decay curve — shifting
       a second-round pick into slots 31-60 so the second-round penalty applies.
    3. Discount for years into the future, for protection, and for swap rights.
    """
    year_offset = max(0, pick.year - CURRENT_SEASON_START)
    position = estimate_pick_position(team_projected_wins, year_offset)
    if pick.round == 2:
        position += FIRST_ROUND_PICKS
    position_slot = min(LAST_PICK, max(FIRST_PICK, round(position)))
    value = calculate_pick_value(position_slot)

    value *= 1.0 / (1.0 + PICK_YEAR_DISCOUNT_RATE) ** year_offset
    value *= PROTECTION_DISCOUNT.get(pick.protections, DEFAULT_PROTECTION_DISCOUNT)
    if pick.swap:
        value *= PICK_SWAP_VALUE_FRACTION
    return value
