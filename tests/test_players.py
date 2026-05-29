from __future__ import annotations

from unittest.mock import patch

import pandas as pd
import pytest

from nba_trade_analyzer.data.cache import JsonCache
from nba_trade_analyzer.data.players import (
    EXPECTED_COLUMNS,
    fetch_player_stats,
    fetch_roster_player_ids,
    fetch_team_roster,
    get_all_team_net_ratings,
    get_team_net_rating,
)


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
            "PIE": [0.18, 0.17, 0.22],
            "USG_PCT": [0.30, 0.31, 0.29],
            "NET_RATING": [8.2, 9.5, 11.3],
            "OFF_RATING": [118.4, 121.0, 124.7],
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
    # The nba_api player id rides along so roster filtering can join by id.
    assert df.loc[0, "nba_player_id"] == 1
    assert df["nba_player_id"].tolist() == [1, 2, 3]


# ----- current-roster fetch (CommonTeamRoster) ----------------------------


def _fake_roster_df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "PLAYER_ID": [201939, 1626172],
            "PLAYER": ["Stephen Curry", "Kevon Looney"],
            "POSITION": ["G", "C"],
        }
    )


def test_fetch_team_roster_returns_id_records(tmp_path):
    cache = JsonCache(tmp_path)
    with patch(
        "nba_trade_analyzer.data.players._fetch_roster",
        return_value=_fake_roster_df(),
    ):
        roster = fetch_team_roster("GSW", cache=cache)

    assert roster == [
        {"nba_player_id": 201939, "player_name": "Stephen Curry", "team": "GSW"},
        {"nba_player_id": 1626172, "player_name": "Kevon Looney", "team": "GSW"},
    ]


def test_fetch_team_roster_uses_cache_on_second_call(tmp_path):
    cache = JsonCache(tmp_path)
    with patch(
        "nba_trade_analyzer.data.players._fetch_roster",
        return_value=_fake_roster_df(),
    ) as mocked:
        fetch_team_roster("GSW", cache=cache)
        fetch_team_roster("GSW", cache=cache)
    assert mocked.call_count == 1


def test_fetch_roster_player_ids_returns_id_set(tmp_path):
    cache = JsonCache(tmp_path)
    with patch(
        "nba_trade_analyzer.data.players._fetch_roster",
        return_value=_fake_roster_df(),
    ):
        ids = fetch_roster_player_ids("GSW", cache=cache)
    assert ids == {201939, 1626172}


def test_fetch_team_roster_unknown_abbr_raises():
    with pytest.raises(KeyError):
        fetch_team_roster("XXX")


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


# ----- team net rating ----------------------------------------------------


def _roster_df() -> pd.DataFrame:
    # MIN: one heavy-minutes starter at +10, three bench scrubs at -5.
    # GSW: balanced four-man rotation around +3.
    return pd.DataFrame(
        {
            "player_name": ["Starter", "Bench A", "Bench B", "Bench C", "G1", "G2"],
            "team": ["MIN", "MIN", "MIN", "MIN", "GSW", "GSW"],
            "GP": [80, 30, 25, 20, 75, 70],
            "MPG": [34.0, 8.0, 6.0, 5.0, 30.0, 28.0],
            "NET_RATING": [10.0, -5.0, -5.0, -5.0, 4.0, 2.0],
        }
    )


def test_get_team_net_rating_weighted_differs_from_unweighted_mean():
    df = _roster_df()
    weighted = get_team_net_rating(df, "MIN")
    unweighted = df[df["team"] == "MIN"]["NET_RATING"].mean()
    # Unweighted mean of [10, -5, -5, -5] = -1.25; weighted should be far higher.
    assert weighted != pytest.approx(unweighted)
    assert weighted - unweighted > 5.0


def test_get_team_net_rating_dominated_by_high_minutes_starter():
    df = _roster_df()
    weighted = get_team_net_rating(df, "MIN")
    # The starter logs 80 * 34 = 2720 minutes; the bench logs <500 combined.
    # Result should be closer to +10 (starter) than -5 (bench).
    assert abs(weighted - 10.0) < abs(weighted - (-5.0))
    assert weighted > 0


def test_get_team_net_rating_unknown_team_returns_zero():
    df = _roster_df()
    assert get_team_net_rating(df, "XXX") == 0.0


def test_get_all_team_net_ratings_covers_every_team():
    df = _roster_df()
    ratings = get_all_team_net_ratings(df)
    assert set(ratings.keys()) == {"MIN", "GSW"}
    assert ratings["MIN"] == pytest.approx(get_team_net_rating(df, "MIN"))
    assert ratings["GSW"] == pytest.approx(get_team_net_rating(df, "GSW"))
