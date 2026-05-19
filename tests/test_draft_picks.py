from __future__ import annotations

import pytest

from nba_trade_analyzer.engine.draft_picks import (
    calculate_pick_value,
    calculate_pick_value_with_protections,
    get_pick_value_curve,
)


def test_pick_1_is_approximately_80m():
    assert calculate_pick_value(1) == pytest.approx(80_000_000)


def test_pick_values_decrease_monotonically_first_round():
    for n in range(1, 30):
        assert calculate_pick_value(n) > calculate_pick_value(n + 1)


def test_pick_30_in_expected_range():
    assert 12_000_000 <= calculate_pick_value(30) <= 15_000_000


def test_second_round_penalty_at_pick_31():
    # Penalty kicks in at pick 31: sharp drop relative to pick 30.
    pick_30 = calculate_pick_value(30)
    pick_31 = calculate_pick_value(31)
    assert pick_31 < pick_30 * 0.5


def test_pick_60_is_near_zero_but_positive():
    pick_60 = calculate_pick_value(60)
    assert pick_60 > 0
    assert pick_60 < 2_000_000


def test_protection_swallows_pick_inside_top_n():
    # Top-4 protected, lands at pick 3 → does not convey.
    assert calculate_pick_value_with_protections(3, protection_top=4) == 0.0


def test_protection_does_not_affect_pick_outside_top_n():
    # Top-4 protected, lands at pick 5 → conveys at full value.
    assert calculate_pick_value_with_protections(5, protection_top=4) == pytest.approx(
        calculate_pick_value(5)
    )


def test_protection_none_returns_full_value():
    assert calculate_pick_value_with_protections(
        10, protection_top=None
    ) == pytest.approx(calculate_pick_value(10))


def test_pick_value_rejects_zero():
    with pytest.raises(ValueError):
        calculate_pick_value(0)


def test_pick_value_rejects_61():
    with pytest.raises(ValueError):
        calculate_pick_value(61)


def test_pick_value_curve_has_60_entries():
    curve = get_pick_value_curve()
    assert len(curve) == 60
    assert set(curve.keys()) == set(range(1, 61))


def test_pick_value_curve_all_positive_and_decreasing():
    curve = get_pick_value_curve()
    values = [curve[n] for n in range(1, 61)]
    assert all(v > 0 for v in values)
    for prev, nxt in zip(values, values[1:]):
        assert nxt < prev
