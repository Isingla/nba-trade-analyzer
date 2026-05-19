from __future__ import annotations

from unittest.mock import patch

import httpx
import pandas as pd
import pytest

from nba_trade_analyzer.data.cache import JsonCache
from nba_trade_analyzer.data.epm import (
    EXPECTED_COLUMNS,
    fetch_epm_data,
    get_player_epm,
    normalize_name,
)

# A trimmed-down chunk of the actual dunksandthrees.com payload — three
# players, one with diacritics, one a bench player, one a star with a
# non-ASCII first name. The format mirrors the SvelteKit JS object literal
# (unquoted keys, leading-dot decimals, etc.) so our regex parser is exercised
# against the real shape.
_FIXTURE_HTML = """
prefix junk ... <html><body>
{season:2026,game_dt:"2026-05-18",player_id:203999,player_name:"Nikola Jokić",team_id:1610612743,team_alias:"DEN",age:31,inches:"83",weight:284,rookie_year:2015,position:"C",off:6.4366,def:.98992,tot:7.4265,tot_change:null,p_pct_start:.987641,p_t_poss_48:99.1066,p_mp_48:38.5125,p_usg:.2861},
{season:2026,game_dt:"2026-05-18",player_id:2544,player_name:"LeBron James",team_id:1610612747,team_alias:"LAL",age:41,inches:"81",weight:250,rookie_year:2003,position:"F",off:1.62037,def:.604351,tot:2.2247,tot_change:null,p_pct_start:.990833,p_t_poss_48:93.1563,p_mp_48:33.0,p_usg:.27},
{season:2026,game_dt:"2026-05-18",player_id:1630175,player_name:"Herbert Jones",team_id:1610612740,team_alias:"NOP",age:27,inches:"79",weight:206,rookie_year:2021,position:"F",off:-0.5,def:1.0,tot:0.5018,tot_change:null,p_pct_start:.9,p_t_poss_48:95.0,p_mp_48:32.0,p_usg:.15},
{season:2026,game_dt:"2026-05-18",player_id:1629011,player_name:"Michael Porter Jr.",team_id:1610612743,team_alias:"DEN",age:27,inches:"82",weight:218,rookie_year:2018,position:"F",off:1.2,def:0.1,tot:1.3,tot_change:null,p_pct_start:.95,p_t_poss_48:98.0,p_mp_48:30.0,p_usg:.22},
{season:2026,game_dt:"2026-05-18",player_id:1629023,player_name:"P.J. Washington",team_id:1610612742,team_alias:"DAL",age:27,inches:"79",weight:230,rookie_year:2019,position:"F",off:0.4,def:0.5,tot:0.9,tot_change:null,p_pct_start:.9,p_t_poss_48:96.0,p_mp_48:29.0,p_usg:.20},
{season:2026,game_dt:"2026-05-18",player_id:9999,player_name:"Replacement Player",team_id:1610612761,team_alias:"TOR",age:24,inches:"77",weight:200,rookie_year:2024,position:"G",off:-1.5,def:-.7,tot:-2.2,tot_change:null,p_pct_start:.1,p_t_poss_48:95.0,p_mp_48:12.0,p_usg:.15}
... suffix junk
"""


def _mocked_response(text: str = _FIXTURE_HTML) -> httpx.Response:
    return httpx.Response(
        status_code=200,
        text=text,
        request=httpx.Request("GET", "https://dunksandthrees.com/epm"),
    )


def test_normalize_name_strips_diacritics():
    assert normalize_name("Nikola Jokić") == "nikola jokic"
    assert normalize_name("Luka Dončić") == "luka doncic"
    assert normalize_name("  LeBron James  ") == "lebron james"


def test_normalize_name_strips_generational_suffixes():
    assert normalize_name("Michael Porter Jr.") == "michael porter"
    assert normalize_name("Michael Porter Jr") == "michael porter"
    assert normalize_name("Robert Williams III") == "robert williams"
    assert normalize_name("Gary Payton II") == "gary payton"
    assert normalize_name("Jimmy Butler III") == "jimmy butler"


def test_normalize_name_strips_periods_in_initials():
    assert normalize_name("P.J. Washington") == "pj washington"
    assert normalize_name("T.J. McConnell") == "tj mcconnell"
    assert normalize_name("A.J. Lawson") == "aj lawson"


def test_normalize_name_does_not_eat_real_surname_substrings():
    # "Curry" ends in nothing-suffix-like; must pass through clean.
    assert normalize_name("Stephen Curry") == "stephen curry"
    # "Jones" is not a suffix even though "jr" is — leave it alone.
    assert normalize_name("Tyus Jones") == "tyus jones"


def test_fetch_epm_data_returns_expected_columns(tmp_path):
    cache = JsonCache(tmp_path)
    with patch(
        "nba_trade_analyzer.data.epm.httpx.get", return_value=_mocked_response()
    ):
        df = fetch_epm_data(cache=cache)

    assert not df.empty
    assert set(EXPECTED_COLUMNS) == set(df.columns)
    assert len(df) == 6


def test_fetch_epm_data_parses_jokic_row(tmp_path):
    cache = JsonCache(tmp_path)
    with patch(
        "nba_trade_analyzer.data.epm.httpx.get", return_value=_mocked_response()
    ):
        df = fetch_epm_data(cache=cache)

    jokic = df[df["player_name"] == "Nikola Jokić"].iloc[0]
    assert jokic["team"] == "DEN"
    assert jokic["epm"] == pytest.approx(7.4265)
    assert jokic["epm_off"] == pytest.approx(6.4366)
    assert jokic["epm_def"] == pytest.approx(0.98992)
    assert jokic["mpg"] == pytest.approx(38.5125)
    assert jokic["position"] == "C"
    assert jokic["age"] == 31


def test_fetch_epm_data_raises_on_empty_payload(tmp_path):
    cache = JsonCache(tmp_path)
    with patch(
        "nba_trade_analyzer.data.epm.httpx.get",
        return_value=_mocked_response(text="<html>nothing here</html>"),
    ):
        with pytest.raises(RuntimeError, match="no player records"):
            fetch_epm_data(cache=cache)


def test_fetch_epm_data_uses_cache_on_second_call(tmp_path):
    cache = JsonCache(tmp_path)
    with patch(
        "nba_trade_analyzer.data.epm.httpx.get", return_value=_mocked_response()
    ) as mocked:
        fetch_epm_data(cache=cache)
        df = fetch_epm_data(cache=cache)

    assert mocked.call_count == 1
    assert len(df) == 6


def test_get_player_epm_finds_known_player(tmp_path):
    cache = JsonCache(tmp_path)
    with patch(
        "nba_trade_analyzer.data.epm.httpx.get", return_value=_mocked_response()
    ):
        df = fetch_epm_data(cache=cache)

    row = get_player_epm(df, "Nikola Jokić")
    assert row is not None
    assert row["epm"] == pytest.approx(7.4265)


def test_get_player_epm_matches_without_diacritics(tmp_path):
    # Plain-ASCII search should still find Jokić — that's the whole point of
    # the normalization layer.
    cache = JsonCache(tmp_path)
    with patch(
        "nba_trade_analyzer.data.epm.httpx.get", return_value=_mocked_response()
    ):
        df = fetch_epm_data(cache=cache)

    row = get_player_epm(df, "Nikola Jokic")
    assert row is not None
    assert row["team"] == "DEN"


def test_get_player_epm_returns_none_for_unknown(tmp_path):
    cache = JsonCache(tmp_path)
    with patch(
        "nba_trade_analyzer.data.epm.httpx.get", return_value=_mocked_response()
    ):
        df = fetch_epm_data(cache=cache)

    assert get_player_epm(df, "Fake McNobody") is None


def test_get_player_epm_handles_empty_frame():
    empty = pd.DataFrame(columns=list(EXPECTED_COLUMNS))
    assert get_player_epm(empty, "Anyone") is None


def test_get_player_epm_resolves_colloquial_alias(tmp_path):
    # User types "Herb Jones" but D&T canonical name is "Herbert Jones".
    cache = JsonCache(tmp_path)
    with patch(
        "nba_trade_analyzer.data.epm.httpx.get", return_value=_mocked_response()
    ):
        df = fetch_epm_data(cache=cache)

    row = get_player_epm(df, "Herb Jones")
    assert row is not None
    assert row["player_name"] == "Herbert Jones"


def test_get_player_epm_matches_when_user_omits_jr_suffix(tmp_path):
    cache = JsonCache(tmp_path)
    with patch(
        "nba_trade_analyzer.data.epm.httpx.get", return_value=_mocked_response()
    ):
        df = fetch_epm_data(cache=cache)

    row = get_player_epm(df, "Michael Porter")
    assert row is not None
    assert row["player_name"] == "Michael Porter Jr."


def test_get_player_epm_matches_initials_without_periods(tmp_path):
    cache = JsonCache(tmp_path)
    with patch(
        "nba_trade_analyzer.data.epm.httpx.get", return_value=_mocked_response()
    ):
        df = fetch_epm_data(cache=cache)

    row = get_player_epm(df, "PJ Washington")
    assert row is not None
    assert row["player_name"] == "P.J. Washington"
