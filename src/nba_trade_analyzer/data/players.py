"""Player stats fetcher backed by nba_api with local cache.

Pulls per-game and advanced league dashboards from NBA.com, merges them,
and returns the columns the rest of the engine needs.
"""

from __future__ import annotations

import pandas as pd
from nba_api.stats.endpoints import LeagueDashPlayerStats

from nba_trade_analyzer.data.cache import JsonCache
from nba_trade_analyzer.engine.constants import (
    LEAGUE_AVG_3PT_PCT,
    LEAGUE_AVG_3PT_RATE,
)

_CACHE_TTL_HOURS = 24.0
_DEFAULT_SEASON = "2025-26"

EXPECTED_COLUMNS: tuple[str, ...] = (
    "player_name",
    "team",
    "age",
    "GP",
    "MPG",
    "W",
    "L",
    "FGA",
    "FG3A",
    "FG3_PCT",
    "FG3_RATE",
    "PIE",
    "USG_PCT",
    "NET_RATING",
    "OFF_RATING",
)


def _fetch_measure(season: str, measure_type: str) -> pd.DataFrame:
    resp = LeagueDashPlayerStats(
        season=season,
        per_mode_detailed="PerGame",
        measure_type_detailed_defense=measure_type,
    )
    return resp.get_data_frames()[0]


def _shape(base: pd.DataFrame, advanced: pd.DataFrame) -> pd.DataFrame:
    # Merge advanced onto base by player id; suffix collisions go to base.
    adv_extra = [
        c for c in advanced.columns if c not in base.columns or c == "PLAYER_ID"
    ]
    merged = base.merge(advanced[adv_extra], on="PLAYER_ID", how="left")

    nan_series = pd.Series([float("nan")] * len(merged), index=merged.index)

    def _col(name: str) -> pd.Series:
        return merged[name] if name in merged.columns else nan_series

    fga = _col("FGA")
    fg3a = _col("FG3A")
    # Per-game volumes; ratio collapses cleanly. Guard divide-by-zero with NaN.
    fg3_rate = fg3a.where(fga > 0).div(fga.where(fga > 0))

    out = pd.DataFrame(
        {
            "player_name": merged["PLAYER_NAME"],
            "team": merged["TEAM_ABBREVIATION"],
            "age": merged["AGE"],
            "GP": merged["GP"],
            "MPG": merged["MIN"],
            "W": _col("W"),
            "L": _col("L"),
            "FGA": fga,
            "FG3A": fg3a,
            "FG3_PCT": _col("FG3_PCT"),
            "FG3_RATE": fg3_rate,
            "PIE": merged["PIE"],
            "USG_PCT": merged["USG_PCT"],
            "NET_RATING": merged["NET_RATING"],
            "OFF_RATING": merged["OFF_RATING"],
        }
    )
    return out


def fetch_player_stats(
    season: str = _DEFAULT_SEASON,
    cache: JsonCache | None = None,
) -> pd.DataFrame:
    cache = cache or JsonCache()
    cache_key = f"player_stats_{season}"

    cached = cache.get(cache_key)
    if cached is not None:
        return pd.DataFrame(cached)

    base = _fetch_measure(season, "Base")
    advanced = _fetch_measure(season, "Advanced")
    df = _shape(base, advanced)

    # NaN survives a JSON round-trip as null; pd.DataFrame restores it.
    cache.set(cache_key, df.to_dict(orient="records"), ttl_hours=_CACHE_TTL_HOURS)
    return df


def get_team_net_rating(df: pd.DataFrame, team_abbr: str) -> float:
    """Minutes-weighted average NET_RATING for all players on `team_abbr`.

    Weighting by total minutes (GP × MPG) means a starter playing 2,500 minutes
    contributes far more to the team average than a bench player who logged 50.
    """
    team_df = df[df["team"] == team_abbr]
    if team_df.empty:
        return 0.0
    minutes = team_df["GP"] * team_df["MPG"]
    total_minutes = minutes.sum()
    if total_minutes <= 0:
        return 0.0
    return float((team_df["NET_RATING"] * minutes).sum() / total_minutes)


def get_all_team_net_ratings(df: pd.DataFrame) -> dict[str, float]:
    """Map every team abbreviation to its minutes-weighted NET_RATING."""
    return {team: get_team_net_rating(df, team) for team in df["team"].unique()}


REGULAR_SEASON_GAMES = 82


def get_team_projected_wins(df: pd.DataFrame, team_abbr: str) -> float:
    """Project the team's full-season win total.

    Each player row carries the W/L of games they appeared in, so the
    most-played player on a team is the most accurate proxy for the team's
    actual win count to date. Extrapolate linearly to 82 games.
    """
    team_df = df[df["team"] == team_abbr]
    if team_df.empty:
        return 0.0
    games_played = float(team_df["GP"].max())
    if games_played <= 0:
        return 0.0
    # Pick the row with the most games played as the team's W/L proxy.
    anchor = team_df.loc[team_df["GP"].idxmax()]
    wins = float(anchor["W"])
    return (wins / games_played) * REGULAR_SEASON_GAMES


def get_team_3pt_stats(df: pd.DataFrame, team_abbr: str) -> tuple[float, float]:
    """Return ``(team_3pt_rate, team_3pt_pct)`` for the acquiring team.

    Both quantities are minutes-weighted across players, so high-volume
    starters drive the team profile far more than spot-minutes bench guys.
    Falls back to the league averages when minutes or attempts are missing.
    """
    team_df = df[df["team"] == team_abbr]
    if team_df.empty:
        return LEAGUE_AVG_3PT_RATE, LEAGUE_AVG_3PT_PCT

    minutes = team_df["GP"] * team_df["MPG"]
    total_minutes = minutes.sum()
    if total_minutes <= 0:
        return LEAGUE_AVG_3PT_RATE, LEAGUE_AVG_3PT_PCT

    rate_series = team_df["FG3_RATE"].fillna(LEAGUE_AVG_3PT_RATE)
    pct_series = team_df["FG3_PCT"].fillna(LEAGUE_AVG_3PT_PCT)
    rate = float((rate_series * minutes).sum() / total_minutes)
    pct = float((pct_series * minutes).sum() / total_minutes)
    return rate, pct
