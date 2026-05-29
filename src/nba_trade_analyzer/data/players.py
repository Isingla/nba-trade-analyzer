"""Player stats fetcher backed by nba_api with local cache.

Pulls per-game and advanced league dashboards from NBA.com, merges them,
and returns the columns the rest of the engine needs.
"""

from __future__ import annotations

import pandas as pd
from nba_api.stats.endpoints import CommonTeamRoster, LeagueDashPlayerStats
from nba_api.stats.static import teams as _static_teams

from nba_trade_analyzer.data.cache import JsonCache
from nba_trade_analyzer.engine.constants import (
    LEAGUE_AVG_3PT_PCT,
    LEAGUE_AVG_3PT_RATE,
)

_CACHE_TTL_HOURS = 24.0
_DEFAULT_SEASON = "2025-26"
_HTTP_TIMEOUT = 30.0

EXPECTED_COLUMNS: tuple[str, ...] = (
    "nba_player_id",
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
            # nba_api PLAYER_ID — the join key that lets roster filtering
            # intersect this frame with CommonTeamRoster (same ID system) by ID
            # rather than by name. See ``fetch_roster_player_ids``.
            "nba_player_id": merged["PLAYER_ID"].astype("int64"),
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


# ---------------------------------------------------------------------------
# Current-roster membership (CommonTeamRoster)
# ---------------------------------------------------------------------------
#
# ``LeagueDashPlayerStats`` lists everyone who logged minutes for a team this
# season — including players since traded away — which inflates positional
# minute totals. ``CommonTeamRoster`` is the authoritative *current* roster.
# Both endpoints are nba_api, so they share the PLAYER_ID system: roster
# filtering is a pure ID intersection, no name matching (see team_context).

# nba_api static team registry is offline-bundled; map its abbreviation (the
# canonical BKN/CHA/PHX form ``Team.abbreviation`` uses) to the numeric team id
# CommonTeamRoster requires.
_TEAM_ID_BY_ABBR: dict[str, int] = {
    t["abbreviation"]: int(t["id"]) for t in _static_teams.get_teams()
}


def _fetch_roster(team_id: int, season: str) -> pd.DataFrame:
    resp = CommonTeamRoster(team_id=team_id, season=season, timeout=_HTTP_TIMEOUT)
    return resp.get_data_frames()[0]


def fetch_team_roster(
    team_abbr: str,
    season: str = _DEFAULT_SEASON,
    cache: JsonCache | None = None,
) -> list[dict]:
    """Fetch a team's current roster from nba_api ``CommonTeamRoster``.

    Returns a list of ``{"nba_player_id", "player_name", "team"}`` records for
    the players on ``team_abbr`` *right now*. ``team_abbr`` must be the nba_api
    abbreviation (BKN/CHA/PHX), i.e. ``Team.abbreviation``. Cached per team for
    24h via the shared :class:`JsonCache`; 30 teams = 30 cached calls.

    Raises ``KeyError`` for an unknown abbreviation — a programming error the
    caller should not paper over.
    """
    team_id = _TEAM_ID_BY_ABBR[team_abbr]
    cache = cache or JsonCache()
    cache_key = f"roster_{team_abbr}_{season}"

    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    df = _fetch_roster(team_id, season)
    records = [
        {
            "nba_player_id": int(row["PLAYER_ID"]),
            "player_name": str(row["PLAYER"]),
            "team": team_abbr,
        }
        for _, row in df.iterrows()
    ]
    cache.set(cache_key, records, ttl_hours=_CACHE_TTL_HOURS)
    return records


def fetch_roster_player_ids(
    team_abbr: str,
    season: str = _DEFAULT_SEASON,
    cache: JsonCache | None = None,
) -> set[int]:
    """Return the set of nba_api player ids on ``team_abbr``'s current roster.

    Thin wrapper over :func:`fetch_team_roster` for the common case where only
    membership (not names) is needed — e.g. roster filtering.
    """
    return {rec["nba_player_id"] for rec in fetch_team_roster(team_abbr, season, cache)}


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
