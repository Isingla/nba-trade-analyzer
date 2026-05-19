from __future__ import annotations

from unittest.mock import patch

import httpx
import pandas as pd
import pytest

from nba_trade_analyzer.data.cache import JsonCache
from nba_trade_analyzer.data.darko import (
    EXPECTED_COLUMNS,
    fetch_darko_data,
    get_player_darko,
    normalize_name,
)

_FIXTURE_CSV = (
    "NBA ID,Player Name,Position,Age,DPM,Offensive DPM,Defensive DPM,"
    "Box Only O-DPM,Box Only D-DPM,On Off O-DPM,On Off D-DPM\n"
    "203999,Nikola Jokić,c_pos,31.2,6.95,5.08,1.87,4.3,1.18,5.21,1.94\n"
    "2544,LeBron James,sf_pos,41.4,2.10,1.20,0.90,1.5,0.85,1.30,0.95\n"
    "1628973,Jalen Brunson,pg_pos,29.7,2.88,3.9,-1.02,3.78,-2.14,3.94,-0.85\n"
)


def _mocked_response(text: str = _FIXTURE_CSV) -> httpx.Response:
    return httpx.Response(
        status_code=200,
        text=text,
        request=httpx.Request("GET", "https://docs.google.com/"),
    )


def test_normalize_name_strips_diacritics():
    assert normalize_name("Nikola Jokić") == "nikola jokic"


def test_fetch_darko_data_returns_expected_columns(tmp_path):
    cache = JsonCache(tmp_path)
    with patch(
        "nba_trade_analyzer.data.darko.httpx.get", return_value=_mocked_response()
    ):
        df = fetch_darko_data(cache=cache)

    assert df is not None
    assert set(EXPECTED_COLUMNS) == set(df.columns)
    assert len(df) == 3


def test_fetch_darko_data_parses_jokic_row(tmp_path):
    cache = JsonCache(tmp_path)
    with patch(
        "nba_trade_analyzer.data.darko.httpx.get", return_value=_mocked_response()
    ):
        df = fetch_darko_data(cache=cache)

    assert df is not None
    jokic = df[df["player_name"] == "Nikola Jokić"].iloc[0]
    assert jokic["dpm"] == pytest.approx(6.95)
    assert jokic["dpm_off"] == pytest.approx(5.08)
    assert jokic["dpm_def"] == pytest.approx(1.87)


def test_fetch_darko_data_uses_cache_on_second_call(tmp_path):
    cache = JsonCache(tmp_path)
    with patch(
        "nba_trade_analyzer.data.darko.httpx.get", return_value=_mocked_response()
    ) as mocked:
        fetch_darko_data(cache=cache)
        df = fetch_darko_data(cache=cache)

    assert mocked.call_count == 1
    assert df is not None
    assert len(df) == 3


def test_fetch_darko_data_returns_none_on_http_error(tmp_path):
    cache = JsonCache(tmp_path)

    def boom(*args, **kwargs):
        raise httpx.ConnectError("network unreachable")

    with patch("nba_trade_analyzer.data.darko.httpx.get", side_effect=boom):
        df = fetch_darko_data(cache=cache)

    assert df is None


def test_get_player_darko_finds_known_player(tmp_path):
    cache = JsonCache(tmp_path)
    with patch(
        "nba_trade_analyzer.data.darko.httpx.get", return_value=_mocked_response()
    ):
        df = fetch_darko_data(cache=cache)

    row = get_player_darko(df, "Nikola Jokić")
    assert row is not None
    assert row["dpm"] == pytest.approx(6.95)


def test_get_player_darko_matches_without_diacritics(tmp_path):
    cache = JsonCache(tmp_path)
    with patch(
        "nba_trade_analyzer.data.darko.httpx.get", return_value=_mocked_response()
    ):
        df = fetch_darko_data(cache=cache)

    row = get_player_darko(df, "Nikola Jokic")
    assert row is not None
    assert row["dpm"] == pytest.approx(6.95)


def test_get_player_darko_returns_none_for_unknown(tmp_path):
    cache = JsonCache(tmp_path)
    with patch(
        "nba_trade_analyzer.data.darko.httpx.get", return_value=_mocked_response()
    ):
        df = fetch_darko_data(cache=cache)

    assert get_player_darko(df, "Fake McNobody") is None


def test_get_player_darko_handles_none_frame():
    assert get_player_darko(None, "Anyone") is None


def test_get_player_darko_handles_empty_frame():
    empty = pd.DataFrame(columns=list(EXPECTED_COLUMNS))
    assert get_player_darko(empty, "Anyone") is None
