"""Player stats fetcher backed by nba_api with local cache.

Pulls per-game and advanced league dashboards from NBA.com, merges them,
and returns the columns the rest of the engine needs.

Note: PER / BPM / VORP / WS are Basketball Reference metrics and are not
exposed by nba_api. They are included as NaN columns here so downstream
code has a stable schema; a later BR scraper will populate them.
"""

from __future__ import annotations

import pandas as pd
from nba_api.stats.endpoints import LeagueDashPlayerStats

from nba_trade_analyzer.data.cache import JsonCache

_CACHE_TTL_HOURS = 24.0
_DEFAULT_SEASON = "2025-26"

EXPECTED_COLUMNS: tuple[str, ...] = (
    "player_name",
    "team",
    "age",
    "GP",
    "MPG",
    "PER",
    "BPM",
    "VORP",
    "WS",
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

    out = pd.DataFrame(
        {
            "player_name": merged["PLAYER_NAME"],
            "team": merged["TEAM_ABBREVIATION"],
            "age": merged["AGE"],
            "GP": merged["GP"],
            "MPG": merged["MIN"],
            "PER": pd.NA,
            "BPM": pd.NA,
            "VORP": pd.NA,
            "WS": pd.NA,
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
