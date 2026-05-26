from __future__ import annotations

from unittest.mock import patch

import httpx
import pandas as pd

from nba_trade_analyzer.data.cache import JsonCache
from nba_trade_analyzer.data.salaries import (
    EXPECTED_COLUMNS,
    _detect_rookie_scale,
    _load_csv_fallback,
    _parse_money,
    build_contract,
    fetch_all_salaries,
    get_player_salary,
)

# A trimmed contracts table mirroring the live Basketball Reference markup:
# the ``player-contracts`` table id, a two-row ``thead`` whose second row
# labels the season columns (``y1``=2025-26 ..), salary cells carrying a clean
# integer in ``csk``, empty years marked with class ``iz``, and option years
# tagged ``salary-pl`` (player) / ``salary-tm`` (team). Rows cover every parse
# path: a plain multi-year deal, a player option, a team option, a rookie-scale
# deal, a one-year deal with diacritics, plus a repeated header row, an empty
# player cell, and a no-salary row that must all be skipped.
_FIXTURE_HTML = """
<html><body>
<p><strong>Color Key:</strong> <span class='salary-pl'>Player Option</span>,
<span class='salary-tm'>Team Option</span></p>
<table class="sortable stats_table" id="player-contracts">
<thead>
  <tr class="over_header thead"><td></td><td colspan="6">Salary</td><td></td></tr>
  <tr>
    <th data-stat="ranker">Rk</th>
    <td data-stat="player">Player</td>
    <td data-stat="team_id">Tm</td>
    <td data-stat="y1">2025-26</td>
    <td data-stat="y2">2026-27</td>
    <td data-stat="y3">2027-28</td>
    <td data-stat="y4">2028-29</td>
    <td data-stat="y5">2029-30</td>
    <td data-stat="y6">2030-31</td>
    <td data-stat="remain_gtd">Guaranteed</td>
  </tr>
</thead>
<tbody>
  <tr>
    <th data-stat="ranker">1</th>
    <td data-stat="player"><a href="/players/c/curryst01.html">Stephen Curry</a></td>
    <td data-stat="team_id"><a href="/contracts/GSW.html">GSW</a></td>
    <td class="right" csk="59606817" data-stat="y1">$59,606,817</td>
    <td class="right" csk="62587158" data-stat="y2">$62,587,158</td>
    <td class="right iz" data-stat="y3"></td>
    <td class="right iz" data-stat="y4"></td>
    <td class="right iz" data-stat="y5"></td>
    <td class="right iz" data-stat="y6"></td>
    <td class="right" csk="122193975" data-stat="remain_gtd">$122,193,975</td>
  </tr>
  <tr>
    <th data-stat="ranker">2</th>
    <td data-stat="player"><a href="/players/e/embijo01.html">Joel Embiid</a></td>
    <td data-stat="team_id"><a href="/contracts/PHI.html">PHI</a></td>
    <td class="right" csk="55224526" data-stat="y1">$55,224,526</td>
    <td class="right" csk="58100000" data-stat="y2">$58,100,000</td>
    <td class="right" csk="62748000" data-stat="y3">$62,748,000</td>
    <td class="right salary-pl" csk="67396000" data-stat="y4">$67,396,000</td>
    <td class="right iz" data-stat="y5"></td>
    <td class="right iz" data-stat="y6"></td>
    <td class="right" csk="176072526" data-stat="remain_gtd">$176,072,526</td>
  </tr>
  <tr>
    <th data-stat="ranker">3</th>
    <td data-stat="player"><a href="/players/s/suggsja01.html">Jalen Suggs</a></td>
    <td data-stat="team_id"><a href="/contracts/ORL.html">ORL</a></td>
    <td class="right" csk="35000000" data-stat="y1">$35,000,000</td>
    <td class="right" csk="32400000" data-stat="y2">$32,400,000</td>
    <td class="right" csk="29600000" data-stat="y3">$29,600,000</td>
    <td class="right" csk="26800000" data-stat="y4">$26,800,000</td>
    <td class="right salary-tm" csk="26700000" data-stat="y5">$26,700,000</td>
    <td class="right iz" data-stat="y6"></td>
    <td class="right" csk="123800000" data-stat="remain_gtd">$123,800,000</td>
  </tr>
  <tr>
    <th data-stat="ranker">4</th>
    <td data-stat="player"><a href="/players/f/flagco01.html">Cooper Flagg</a></td>
    <td data-stat="team_id"><a href="/contracts/DAL.html">DAL</a></td>
    <td class="right" csk="13825920" data-stat="y1">$13,825,920</td>
    <td class="right" csk="14517216" data-stat="y2">$14,517,216</td>
    <td class="right salary-tm" csk="15208512" data-stat="y3">$15,208,512</td>
    <td class="right salary-tm" csk="17009800" data-stat="y4">$17,009,800</td>
    <td class="right iz" data-stat="y5"></td>
    <td class="right iz" data-stat="y6"></td>
    <td class="right" csk="28343136" data-stat="remain_gtd">$28,343,136</td>
  </tr>
  <tr class="thead"><td colspan="10">Repeated mid-table header</td></tr>
  <tr>
    <th data-stat="ranker">5</th>
    <td data-stat="player"><a href="/players/j/jokicni01.html">Nikola Jokić</a></td>
    <td data-stat="team_id"><a href="/contracts/DEN.html">DEN</a></td>
    <td class="right" csk="55224526" data-stat="y1">$55,224,526</td>
    <td class="right iz" data-stat="y2"></td>
    <td class="right iz" data-stat="y3"></td>
    <td class="right iz" data-stat="y4"></td>
    <td class="right iz" data-stat="y5"></td>
    <td class="right iz" data-stat="y6"></td>
    <td class="right" csk="55224526" data-stat="remain_gtd">$55,224,526</td>
  </tr>
  <tr>
    <th data-stat="ranker">6</th>
    <td data-stat="player"></td>
    <td data-stat="team_id"></td>
    <td class="right iz" data-stat="y1"></td>
    <td class="right iz" data-stat="y2"></td>
    <td class="right iz" data-stat="y3"></td>
    <td class="right iz" data-stat="y4"></td>
    <td class="right iz" data-stat="y5"></td>
    <td class="right iz" data-stat="y6"></td>
    <td class="right iz" data-stat="remain_gtd"></td>
  </tr>
  <tr>
    <th data-stat="ranker">7</th>
    <td data-stat="player"><a href="/players/x/nosalary01.html">No Salary</a></td>
    <td data-stat="team_id"><a href="/contracts/FA.html">FA</a></td>
    <td class="right iz" data-stat="y1"></td>
    <td class="right iz" data-stat="y2"></td>
    <td class="right iz" data-stat="y3"></td>
    <td class="right iz" data-stat="y4"></td>
    <td class="right iz" data-stat="y5"></td>
    <td class="right iz" data-stat="y6"></td>
    <td class="right iz" data-stat="remain_gtd"></td>
  </tr>
</tbody>
</table>
</body></html>
"""

# Matches the CSV schema the dump script writes (and the live parser produces).
_FIXTURE_CSV = (
    "player_name,team,salary,years_remaining,is_rookie_scale,"
    "has_player_option,has_team_option\n"
    "Stephen Curry,GSW,59606817,2,False,False,False\n"
    "Joel Embiid,PHI,55224526,4,False,True,False\n"
    "Cooper Flagg,DAL,13825920,4,True,False,True\n"
    "Nikola Jokić,DEN,55224526,1,False,True,False\n"
)


def _mocked_response(text: str = _FIXTURE_HTML) -> httpx.Response:
    return httpx.Response(
        status_code=200,
        text=text,
        request=httpx.Request("GET", "https://www.basketball-reference.com/"),
    )


def _write_fixture_csv(tmp_path) -> "object":
    path = tmp_path / "salaries_2025_26.csv"
    path.write_text(_FIXTURE_CSV, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Salary string parsing
# ---------------------------------------------------------------------------


def test_parse_money_strips_dollar_and_commas():
    assert _parse_money("$46,097,561") == 46097561
    assert _parse_money("$2,743,800") == 2743800


def test_parse_money_returns_none_for_empty():
    assert _parse_money("") is None
    assert _parse_money("   ") is None


# ---------------------------------------------------------------------------
# HTML parsing via fetch_all_salaries (mocked HTTP)
# ---------------------------------------------------------------------------


def test_fetch_all_salaries_returns_expected_schema(tmp_path):
    cache = JsonCache(tmp_path)
    with patch(
        "nba_trade_analyzer.data.salaries.httpx.get", return_value=_mocked_response()
    ):
        df = fetch_all_salaries(cache=cache)

    assert set(EXPECTED_COLUMNS) == set(df.columns)
    # Header repeat, empty-player row, and no-salary row are all skipped.
    assert len(df) == 5


def test_fetch_all_salaries_parses_plain_multiyear_deal(tmp_path):
    cache = JsonCache(tmp_path)
    with patch(
        "nba_trade_analyzer.data.salaries.httpx.get", return_value=_mocked_response()
    ):
        df = fetch_all_salaries(cache=cache)

    curry = get_player_salary(df, "Stephen Curry")
    assert curry["team"] == "GSW"
    assert curry["salary"] == 59606817
    assert curry["years_remaining"] == 2
    assert curry["has_player_option"] is False
    assert curry["has_team_option"] is False
    assert curry["is_rookie_scale"] is False


def test_fetch_all_salaries_detects_player_option(tmp_path):
    cache = JsonCache(tmp_path)
    with patch(
        "nba_trade_analyzer.data.salaries.httpx.get", return_value=_mocked_response()
    ):
        df = fetch_all_salaries(cache=cache)

    embiid = get_player_salary(df, "Joel Embiid")
    assert embiid["has_player_option"] is True
    assert embiid["has_team_option"] is False
    assert embiid["years_remaining"] == 4


def test_fetch_all_salaries_detects_team_option(tmp_path):
    cache = JsonCache(tmp_path)
    with patch(
        "nba_trade_analyzer.data.salaries.httpx.get", return_value=_mocked_response()
    ):
        df = fetch_all_salaries(cache=cache)

    suggs = get_player_salary(df, "Jalen Suggs")
    assert suggs["has_team_option"] is True
    assert suggs["has_player_option"] is False
    assert suggs["years_remaining"] == 5


def test_fetch_all_salaries_detects_rookie_scale(tmp_path):
    cache = JsonCache(tmp_path)
    with patch(
        "nba_trade_analyzer.data.salaries.httpx.get", return_value=_mocked_response()
    ):
        df = fetch_all_salaries(cache=cache)

    flagg = get_player_salary(df, "Cooper Flagg")
    assert flagg["is_rookie_scale"] is True
    assert flagg["years_remaining"] == 4
    assert flagg["has_team_option"] is True


def test_fetch_all_salaries_uses_cache_on_second_call(tmp_path):
    cache = JsonCache(tmp_path)
    with patch(
        "nba_trade_analyzer.data.salaries.httpx.get", return_value=_mocked_response()
    ) as mocked:
        fetch_all_salaries(cache=cache)
        df = fetch_all_salaries(cache=cache)

    assert mocked.call_count == 1
    assert len(df) == 5


def test_fetch_all_salaries_falls_back_to_csv_on_http_error(tmp_path):
    cache = JsonCache(tmp_path)
    csv_path = _write_fixture_csv(tmp_path)

    def boom(*args, **kwargs):
        raise httpx.ConnectError("network unreachable")

    with patch("nba_trade_analyzer.data.salaries.httpx.get", side_effect=boom):
        df = fetch_all_salaries(cache=cache, csv_path=csv_path)

    assert set(EXPECTED_COLUMNS) == set(df.columns)
    curry = get_player_salary(df, "Stephen Curry")
    assert curry["salary"] == 59606817


# ---------------------------------------------------------------------------
# CSV fallback loading
# ---------------------------------------------------------------------------


def test_load_csv_fallback_coerces_dtypes(tmp_path):
    df = _load_csv_fallback(_write_fixture_csv(tmp_path))
    assert list(df.columns) == list(EXPECTED_COLUMNS)
    assert df["salary"].dtype == int
    assert df["is_rookie_scale"].dtype == bool
    flagg = get_player_salary(df, "Cooper Flagg")
    assert flagg["is_rookie_scale"] is True


# ---------------------------------------------------------------------------
# Name lookup
# ---------------------------------------------------------------------------


def test_get_player_salary_matches_without_diacritics(tmp_path):
    df = _load_csv_fallback(_write_fixture_csv(tmp_path))
    row = get_player_salary(df, "Nikola Jokic")
    assert row is not None
    assert row["player_name"] == "Nikola Jokić"


def test_get_player_salary_returns_none_for_unknown(tmp_path):
    df = _load_csv_fallback(_write_fixture_csv(tmp_path))
    assert get_player_salary(df, "Fake McNobody") is None


def test_get_player_salary_handles_empty_frame():
    empty = pd.DataFrame(columns=list(EXPECTED_COLUMNS))
    assert get_player_salary(empty, "Anyone") is None


# ---------------------------------------------------------------------------
# Rookie-scale detection heuristic
# ---------------------------------------------------------------------------


def test_detect_rookie_scale_flags_scale_salary_with_rookie_structure():
    # Pick-1 2025-26 scale, 4-year term, team options in out-years.
    assert _detect_rookie_scale(13_825_920, 4, True) is True


def test_detect_rookie_scale_rejects_scale_salary_without_team_option():
    assert _detect_rookie_scale(13_825_920, 4, False) is False


def test_detect_rookie_scale_rejects_short_term_deal():
    assert _detect_rookie_scale(13_825_920, 2, True) is False


def test_detect_rookie_scale_rejects_off_scale_salary():
    assert _detect_rookie_scale(45_000_000, 4, True) is False


# ---------------------------------------------------------------------------
# build_contract adapter
# ---------------------------------------------------------------------------


def test_build_contract_basic_salary_and_years():
    contract = build_contract({"salary": 34_897_959, "years_remaining": 3})
    assert contract.salary == 34_897_959
    assert contract.years_remaining == 3
    assert contract.has_player_option is False
    assert contract.has_team_option is False
    assert contract.is_rookie_scale is False


def test_build_contract_player_option():
    contract = build_contract(
        {"salary": 55_224_526, "years_remaining": 4, "has_player_option": True}
    )
    assert contract.has_player_option is True
    assert contract.has_team_option is False


def test_build_contract_team_option():
    contract = build_contract(
        {"salary": 35_000_000, "years_remaining": 5, "has_team_option": True}
    )
    assert contract.has_team_option is True
    assert contract.has_player_option is False


def test_build_contract_rookie_scale():
    contract = build_contract(
        {"salary": 13_825_920, "years_remaining": 4, "is_rookie_scale": True}
    )
    assert contract.is_rookie_scale is True


def test_build_contract_clamps_years_remaining_to_one():
    # A row with a positive current salary always has at least one year left.
    contract = build_contract({"salary": 5_000_000, "years_remaining": 0})
    assert contract.years_remaining == 1


def test_build_contract_roundtrips_from_get_player_salary(tmp_path):
    df = _load_csv_fallback(_write_fixture_csv(tmp_path))
    contract = build_contract(get_player_salary(df, "Joel Embiid"))
    assert contract.salary == 55_224_526
    assert contract.has_player_option is True
