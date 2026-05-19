"""Draft pick value curve.

Approximates the surplus value of a draft pick (production over the full
rookie contract, net of rookie-scale salary) using an exponential decay
across pick numbers 1-60, with a sharp penalty after pick 30 to reflect
the lack of guaranteed contracts in the second round.
"""

from __future__ import annotations

from math import exp

from nba_trade_analyzer.engine.constants import (
    PICK_VALUE_DECAY,
    PICK_VALUE_SCALE,
    PICK_VALUE_SECOND_ROUND_PENALTY,
)

FIRST_PICK = 1
LAST_PICK = 60
SECOND_ROUND_START = 31


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
