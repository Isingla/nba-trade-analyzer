from __future__ import annotations

from unittest.mock import patch

import pandas as pd

from nba_trade_analyzer.data.cache import JsonCache
from nba_trade_analyzer.data.players import EXPECTED_COLUMNS, fetch_player_stats


def _fake_base() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "PLAYER_ID": [1, 2, 3],
            "PLAYER_NAME": ["LeBron James", "Stephen Curry", "Nikola Jokic"],
            "TEAM_ABBREVIATION": ["LAL", "GSW", "DEN"],
            "AGE": [40, 37, 31],
            "GP": [70, 65, 78],
            "MIN": [35.2, 32.8, 34.6],
        }
    )


def _fake_advanced() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "PLAYER_ID": [1, 2, 3],
            "USG_PCT": [0.30, 0.31, 0.29],
            "PIE": [0.18, 0.17, 0.22],
        }
    )


def test_fetch_player_stats_returns_expected_columns(tmp_path):
    cache = JsonCache(tmp_path)

    with patch(
        "nba_trade_analyzer.data.players._fetch_measure",
        side_effect=[_fake_base(), _fake_advanced()],
    ):
        df = fetch_player_stats(season="2025-26", cache=cache)

    assert not df.empty
    assert set(EXPECTED_COLUMNS).issubset(df.columns)
    assert df.loc[0, "player_name"] == "LeBron James"
    assert df.loc[0, "team"] == "LAL"


def test_fetch_player_stats_uses_cache_on_second_call(tmp_path):
    cache = JsonCache(tmp_path)

    with patch(
        "nba_trade_analyzer.data.players._fetch_measure",
        side_effect=[_fake_base(), _fake_advanced()],
    ) as mocked:
        fetch_player_stats(season="2025-26", cache=cache)
        # Second call should hit the cache, not the endpoint.
        df = fetch_player_stats(season="2025-26", cache=cache)

    assert mocked.call_count == 2  # one Base + one Advanced, both from first call
    assert set(EXPECTED_COLUMNS).issubset(df.columns)
    assert len(df) == 3
