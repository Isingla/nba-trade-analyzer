"""Player valuation engine.

Three impact-metric paths, picked in order of preference per player:

1. **EPM** (Estimated Plus-Minus from dunksandthrees.com) — primary.
   Already isolates individual impact via RAPM, so no team adjustment is
   applied; the partial-team-adjustment hack from the NET_RATING era is
   bypassed entirely on this path.
2. **DARKO** (DPM projection from Kostya Medvedovsky's sheet) — secondary.
   Treated the same way as EPM mechanically: per-100-possessions impact
   scaled by minutes fraction, no team adjustment.
3. **NET_RATING** (team-adjusted on/off from nba_api) — fallback only.
   Kept for players who appear in neither EPM nor DARKO data (mostly
   end-of-bench, two-way, and very-low-sample players).

All three paths share the same downstream pipeline:
``raw_wins → tanh compression → DOLLARS_PER_WIN → surplus_value``.
"""

from __future__ import annotations

import math

import pandas as pd

from nba_trade_analyzer.data.darko import fetch_darko_data, get_player_darko
from nba_trade_analyzer.data.epm import fetch_epm_data, get_player_epm
from nba_trade_analyzer.engine.constants import (
    DOLLARS_PER_WIN,
    EPM_TO_WINS_FACTOR,
    FULL_SEASON_MINUTES,
    MAX_WINS_ADDED,
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
    """NET_RATING-path wins. Subtracts a replacement floor before scaling.

    `minutes_played` is total minutes for the season (MPG × GP). `games_played`
    is kept in the signature for forward compatibility with availability-aware
    adjustments but is not used in the current formula — scaling is purely
    minutes-based.
    """
    del games_played
    value_above_replacement = adjusted_net_rating - REPLACEMENT_LEVEL_NET_RATING
    minutes_fraction = minutes_played / FULL_SEASON_MINUTES
    raw_wins = (value_above_replacement * NET_RATING_TO_WINS_FACTOR) * minutes_fraction
    return MAX_WINS_ADDED * math.tanh(raw_wins / MAX_WINS_ADDED)


def calculate_wins_added_from_impact(impact: float, minutes_played: float) -> float:
    """EPM/DARKO-path wins. No replacement subtraction — RAPM already centers
    impact around the league average, and "replacement level" in this regime
    is just a low (≈ -2) value of the impact metric itself.
    """
    minutes_fraction = minutes_played / FULL_SEASON_MINUTES
    raw_wins = impact * minutes_fraction * EPM_TO_WINS_FACTOR
    return MAX_WINS_ADDED * math.tanh(raw_wins / MAX_WINS_ADDED)


def calculate_player_value(wins_added: float) -> float:
    return wins_added * DOLLARS_PER_WIN


def calculate_surplus_value(player_value: float, salary: int) -> float:
    return player_value - salary


def _confidence_from_minutes(minutes_played: float) -> float:
    raw = minutes_played / FULL_CONFIDENCE_MINUTES
    return max(MIN_CONFIDENCE, min(1.0, raw))


def _evaluate_with_epm(
    player: Player,
    contract: Contract,
    epm_row: pd.Series,
    minutes_played: float,
) -> PlayerValuation:
    impact = float(epm_row["epm"])
    wins_added = calculate_wins_added_from_impact(impact, minutes_played)
    player_value = calculate_player_value(wins_added)
    surplus_value = calculate_surplus_value(player_value, contract.salary)
    return PlayerValuation(
        player_name=player.name,
        team=player.team,
        adjusted_net_rating=impact,
        wins_added=wins_added,
        player_value=player_value,
        surplus_value=surplus_value,
        salary=contract.salary,
        confidence=_confidence_from_minutes(minutes_played),
        metric_source="epm",
    )


def _evaluate_with_darko(
    player: Player,
    contract: Contract,
    darko_row: pd.Series,
    minutes_played: float,
) -> PlayerValuation:
    impact = float(darko_row["dpm"])
    wins_added = calculate_wins_added_from_impact(impact, minutes_played)
    player_value = calculate_player_value(wins_added)
    surplus_value = calculate_surplus_value(player_value, contract.salary)
    return PlayerValuation(
        player_name=player.name,
        team=player.team,
        adjusted_net_rating=impact,
        wins_added=wins_added,
        player_value=player_value,
        surplus_value=surplus_value,
        salary=contract.salary,
        confidence=_confidence_from_minutes(minutes_played),
        metric_source="darko",
    )


def _evaluate_with_net_rating(
    player: Player,
    contract: Contract,
    team_net_rating: float,
    minutes_played: float,
    games_played: int,
) -> PlayerValuation:
    net_rating = float(player.stats.get("NET_RATING", 0.0))
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
        metric_source="net_rating",
    )


def evaluate_player(
    player: Player,
    contract: Contract,
    team_net_rating: float = 0.0,
    epm_df: pd.DataFrame | None = None,
    darko_df: pd.DataFrame | None = None,
) -> PlayerValuation:
    """Evaluate a single player, picking the best available impact source.

    Pre-fetch ``epm_df`` and ``darko_df`` once when scoring many players —
    each one is a network call the first time. If left unset, this function
    will fetch them on demand (cached for 24h).
    """
    games_played = int(player.stats.get("GP", 0))
    mpg = float(player.stats.get("MPG", 0.0))
    minutes_played = mpg * games_played

    if epm_df is None:
        epm_df = fetch_epm_data()
    epm_row = get_player_epm(epm_df, player.name) if epm_df is not None else None
    if epm_row is not None:
        return _evaluate_with_epm(player, contract, epm_row, minutes_played)

    if darko_df is None:
        darko_df = fetch_darko_data()
    darko_row = (
        get_player_darko(darko_df, player.name) if darko_df is not None else None
    )
    if darko_row is not None:
        return _evaluate_with_darko(player, contract, darko_row, minutes_played)

    return _evaluate_with_net_rating(
        player, contract, team_net_rating, minutes_played, games_played
    )


def evaluate_trade_assets(
    trade_assets: TradeAssets,
    team_net_rating: float = 0.0,
    epm_df: pd.DataFrame | None = None,
    darko_df: pd.DataFrame | None = None,
) -> float:
    """Sum surplus value across the player package. Draft picks are scored separately."""
    return sum(
        evaluate_player(
            entry.player,
            entry.contract,
            team_net_rating,
            epm_df=epm_df,
            darko_df=darko_df,
        ).surplus_value
        for entry in trade_assets.players
    )
