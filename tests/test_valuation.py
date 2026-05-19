from __future__ import annotations

import pytest

from nba_trade_analyzer.engine.constants import DOLLARS_PER_WIN
from nba_trade_analyzer.engine.valuation import (
    calculate_adjusted_net_rating,
    calculate_player_value,
    calculate_surplus_value,
    calculate_wins_added,
    evaluate_player,
    evaluate_trade_assets,
)
from nba_trade_analyzer.models.player import Contract, Player
from nba_trade_analyzer.models.team import RosterEntry
from nba_trade_analyzer.models.trade import TradeAssets


# ----- adjusted net rating ------------------------------------------------


def test_adjusted_net_rating_good_player_on_better_team():
    # +3 on a +5 team is actually carrying -2 relative to teammates.
    assert calculate_adjusted_net_rating(3.0, 5.0) == pytest.approx(-2.0)


def test_adjusted_net_rating_decent_player_on_bad_team():
    # +1 on a -4 team is doing the heavy lifting → +5.
    assert calculate_adjusted_net_rating(1.0, -4.0) == pytest.approx(5.0)


# ----- wins added ---------------------------------------------------------


def test_wins_added_full_season_starter():
    # 82 GP × 36 MPG = 2952 total minutes = full season scaling of 1.0.
    # Adjusted +5.0 → above replacement by 7.0 → 7.0 × 2.75 = 19.25 wins.
    wins = calculate_wins_added(
        adjusted_net_rating=5.0, minutes_played=36 * 82, games_played=82
    )
    assert wins == pytest.approx(19.25)


def test_wins_added_part_time_scales_down():
    # 60 GP × 18 MPG = 1080 minutes ≈ 36.6% of full season.
    full = calculate_wins_added(
        adjusted_net_rating=5.0, minutes_played=36 * 82, games_played=82
    )
    part = calculate_wins_added(
        adjusted_net_rating=5.0, minutes_played=18 * 60, games_played=60
    )
    assert part < full
    assert part == pytest.approx(full * (1080 / 2952))


def test_wins_added_below_replacement_is_negative():
    # Adjusted -5.0 is below the -2.0 replacement floor → value above
    # replacement is -3.0, so wins are negative even at full minutes.
    wins = calculate_wins_added(
        adjusted_net_rating=-5.0, minutes_played=36 * 82, games_played=82
    )
    assert wins < 0
    assert wins == pytest.approx(-3.0 * 2.75)


# ----- player value & surplus value --------------------------------------


def test_player_value_is_wins_times_dollars_per_win():
    assert calculate_player_value(10.0) == pytest.approx(10.0 * DOLLARS_PER_WIN)


def test_surplus_value_underpaid_star_is_positive():
    # 19.25 wins × $3.5M = ~$67.4M of production, paid only $10M.
    player_value = calculate_player_value(19.25)
    surplus = calculate_surplus_value(player_value, salary=10_000_000)
    assert surplus > 0
    assert surplus == pytest.approx(player_value - 10_000_000)


def test_surplus_value_overpaid_vet_is_negative():
    # 2.75 wins × $3.5M ≈ $9.6M of production, paid $30M.
    player_value = calculate_player_value(2.75)
    surplus = calculate_surplus_value(player_value, salary=30_000_000)
    assert surplus < 0


def test_surplus_value_replacement_on_minimum_is_near_zero():
    # Replacement-level production (0 wins) on a $2.5M minimum.
    # Surplus is just -salary; "near zero" relative to a star's $50M+ surplus.
    player_value = calculate_player_value(0.0)
    surplus = calculate_surplus_value(player_value, salary=2_500_000)
    assert abs(surplus) < 3_000_000


# ----- confidence ---------------------------------------------------------


def _player_with_minutes(mpg: float, gp: int, net_rating: float = 0.0) -> Player:
    return Player(
        name="Test Player",
        team="TST",
        age=27,
        stats={"NET_RATING": net_rating, "GP": gp, "MPG": mpg},
    )


def _contract(salary: int = 5_000_000) -> Contract:
    return Contract(salary=salary, years_remaining=1)


def test_confidence_full_at_2500_minutes():
    p = _player_with_minutes(mpg=31.25, gp=80)  # 31.25 * 80 = 2500
    v = evaluate_player(p, _contract())
    assert v.confidence == pytest.approx(1.0)


def test_confidence_half_at_1000_minutes():
    p = _player_with_minutes(mpg=20.0, gp=50)  # 20 * 50 = 1000
    v = evaluate_player(p, _contract())
    assert v.confidence == pytest.approx(0.5)


def test_confidence_floor_at_200_minutes():
    p = _player_with_minutes(mpg=10.0, gp=20)  # 10 * 20 = 200
    v = evaluate_player(p, _contract())
    assert v.confidence == pytest.approx(0.1)


# ----- end-to-end ---------------------------------------------------------


def test_evaluate_player_realistic_stat_line():
    # NET_RATING +4.0 on a +1.0 team → adjusted +3.0.
    # 34 MPG × 75 GP = 2550 minutes → confidence 1.0, scaling 2550/2952.
    # Value above replacement = 3 - (-2) = 5 → 5 × 2.75 × (2550/2952) = ~11.88 wins.
    player = Player(
        name="Anthony Edwards",
        team="MIN",
        age=24,
        stats={"NET_RATING": 4.0, "GP": 75, "MPG": 34.0},
    )
    contract = Contract(salary=40_000_000, years_remaining=4)
    valuation = evaluate_player(player, contract, team_net_rating=1.0)

    assert valuation.player_name == "Anthony Edwards"
    assert valuation.team == "MIN"
    assert valuation.adjusted_net_rating == pytest.approx(3.0)
    assert valuation.wins_added == pytest.approx(5.0 * 2.75 * (34 * 75 / 2952))
    assert valuation.player_value == pytest.approx(
        valuation.wins_added * DOLLARS_PER_WIN
    )
    assert valuation.surplus_value == pytest.approx(
        valuation.player_value - 40_000_000
    )
    assert valuation.confidence == pytest.approx(1.0)
    assert valuation.salary == 40_000_000
    assert valuation.metric_source == "net_rating_adjusted"


def test_evaluate_trade_assets_sums_surplus_across_players():
    p1 = Player(
        name="Star",
        team="MIN",
        age=27,
        stats={"NET_RATING": 5.0, "GP": 80, "MPG": 35.0},
    )
    p2 = Player(
        name="Rotation",
        team="MIN",
        age=29,
        stats={"NET_RATING": 0.0, "GP": 70, "MPG": 22.0},
    )
    assets = TradeAssets(
        players=[
            RosterEntry(player=p1, contract=Contract(salary=20_000_000, years_remaining=2)),
            RosterEntry(player=p2, contract=Contract(salary=8_000_000, years_remaining=1)),
        ]
    )

    expected = (
        evaluate_player(
            p1, Contract(salary=20_000_000, years_remaining=2)
        ).surplus_value
        + evaluate_player(
            p2, Contract(salary=8_000_000, years_remaining=1)
        ).surplus_value
    )
    assert evaluate_trade_assets(assets) == pytest.approx(expected)


def test_evaluate_trade_assets_empty_is_zero():
    assert evaluate_trade_assets(TradeAssets()) == 0
