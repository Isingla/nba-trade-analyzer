"""Player valuation engine.

Converts raw on/off NET_RATING into a dollar surplus value, using an
adjusted-for-team-context net rating, a wins-above-replacement conversion,
and a $/win price. The output is the input the trade grader needs to
decide who came out ahead in a swap.
"""

from __future__ import annotations

from nba_trade_analyzer.engine.constants import (
    DOLLARS_PER_WIN,
    FULL_SEASON_MINUTES,
    NET_RATING_TO_WINS_FACTOR,
    REPLACEMENT_LEVEL_NET_RATING,
    TEAM_ADJUSTMENT_WEIGHT,
)
from nba_trade_analyzer.models.player import Contract, Player
from nba_trade_analyzer.models.trade import TradeAssets
from nba_trade_analyzer.models.valuation import PlayerValuation

FULL_CONFIDENCE_MINUTES = 2000.0
MIN_CONFIDENCE = 0.1


def calculate_adjusted_net_rating(
    player_net_rating: float, team_net_rating: float
) -> float:
    """Partially strip out team context, weighted by TEAM_ADJUSTMENT_WEIGHT."""
    return player_net_rating - (team_net_rating * TEAM_ADJUSTMENT_WEIGHT)


def calculate_wins_added(
    adjusted_net_rating: float, minutes_played: float, games_played: int
) -> float:
    """Convert adjusted net rating into wins, scaled by share of a full season.

    `minutes_played` is total minutes for the season (MPG × GP). `games_played`
    is kept in the signature for forward compatibility with availability-aware
    adjustments but is not used in the current formula — scaling is purely
    minutes-based.
    """
    del games_played
    value_above_replacement = adjusted_net_rating - REPLACEMENT_LEVEL_NET_RATING
    wins_per_full_season = value_above_replacement * NET_RATING_TO_WINS_FACTOR
    return wins_per_full_season * (minutes_played / FULL_SEASON_MINUTES)


def calculate_player_value(wins_added: float) -> float:
    return wins_added * DOLLARS_PER_WIN


def calculate_surplus_value(player_value: float, salary: int) -> float:
    return player_value - salary


def _confidence_from_minutes(minutes_played: float) -> float:
    raw = minutes_played / FULL_CONFIDENCE_MINUTES
    return max(MIN_CONFIDENCE, min(1.0, raw))


def evaluate_player(
    player: Player, contract: Contract, team_net_rating: float = 0.0
) -> PlayerValuation:
    net_rating = float(player.stats.get("NET_RATING", 0.0))
    games_played = int(player.stats.get("GP", 0))
    mpg = float(player.stats.get("MPG", 0.0))
    minutes_played = mpg * games_played

    adjusted = calculate_adjusted_net_rating(net_rating, team_net_rating)
    wins_added = calculate_wins_added(adjusted, minutes_played, games_played)
    player_value = calculate_player_value(wins_added)
    surplus_value = calculate_surplus_value(player_value, contract.salary)

    return PlayerValuation(
        player_name=player.name,
        team=player.team,
        adjusted_net_rating=adjusted,
        wins_added=wins_added,
        player_value=player_value,
        surplus_value=surplus_value,
        salary=contract.salary,
        confidence=_confidence_from_minutes(minutes_played),
    )


def evaluate_trade_assets(
    trade_assets: TradeAssets, team_net_rating: float = 0.0
) -> float:
    """Sum surplus value across the player package. Draft picks are scored separately."""
    return sum(
        evaluate_player(entry.player, entry.contract, team_net_rating).surplus_value
        for entry in trade_assets.players
    )
