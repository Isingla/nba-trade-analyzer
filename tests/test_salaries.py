from __future__ import annotations

from unittest.mock import patch

import httpx
import pandas as pd
import pytest

from nba_trade_analyzer.data.cache import JsonCache
from nba_trade_analyzer.data.salaries import (
    EXPECTED_COLUMNS,
    _detect_rookie_scale,
    _load_csv_fallback,
    _parse_money,
    build_contract,
    fetch_all_salaries,
    get_player_salary,
    _parse_team_contract_amounts,
    attach_raw_amounts,
)

# A trimmed contracts table mirroring the live Basketball Reference markup:
# the ``player-contracts`` table id, a two-row ``thead`` whose second row
# labels the season columns (``y1``=2026-27 .. — the post-rollover page), salary cells carrying a clean
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
    <td data-stat="y1">2026-27</td>
    <td data-stat="y2">2027-28</td>
    <td data-stat="y3">2028-29</td>
    <td data-stat="y4">2029-30</td>
    <td data-stat="y5">2030-31</td>
    <td data-stat="y6">2031-32</td>
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


def test_fetch_all_salaries_captures_bbref_slug(tmp_path):
    cache = JsonCache(tmp_path)
    with patch(
        "nba_trade_analyzer.data.salaries.httpx.get", return_value=_mocked_response()
    ):
        df = fetch_all_salaries(cache=cache)

    # The permanent slug is parsed from each player's page link href.
    assert get_player_salary(df, "Stephen Curry")["bbref_slug"] == "curryst01"
    assert get_player_salary(df, "Nikola Jokić")["bbref_slug"] == "jokicni01"
    assert get_player_salary(df, "Cooper Flagg")["bbref_slug"] == "flagco01"


def test_fetch_all_salaries_captures_per_year_salaries(tmp_path):
    cache = JsonCache(tmp_path)
    with patch(
        "nba_trade_analyzer.data.salaries.httpx.get", return_value=_mocked_response()
    ):
        df = fetch_all_salaries(cache=cache)

    # Escalating multi-year deal: every guaranteed current-onward year captured,
    # current season first (persisted as a pipe-joined string).
    embiid = get_player_salary(df, "Joel Embiid")
    assert embiid["yearly_salaries"] == "55224526|58100000|62748000|67396000"
    # The option year (y4, salary-pl) is included at its listed figure — no
    # discount applied here.
    assert build_contract(embiid).yearly_salaries == (
        55224526,
        58100000,
        62748000,
        67396000,
    )
    # A one-year deal yields a single-element series equal to the flat salary.
    jokic = get_player_salary(df, "Nikola Jokić")
    assert jokic["yearly_salaries"] == "55224526"
    assert build_contract(jokic).yearly_salaries == (55224526,)


# A player Basketball Reference lists on multiple identical contract lines for
# the same team (min / two-way / dead-money players sign several 10-day or
# rest-of-season deals), PLUS the same player carrying dead money on a second
# team. The first kind must collapse to one row; the second must NOT.
_DUP_FIXTURE_HTML = """
<html><body>
<table id="player-contracts">
<thead>
  <tr class="over_header thead"><td></td><td colspan="6">Salary</td><td></td></tr>
  <tr>
    <th data-stat="ranker">Rk</th>
    <td data-stat="player">Player</td>
    <td data-stat="team_id">Tm</td>
    <td data-stat="y1">2026-27</td>
    <td data-stat="y2">2027-28</td>
    <td data-stat="remain_gtd">Guaranteed</td>
  </tr>
</thead>
<tbody>
  {rows}
</tbody>
</table>
</body></html>
"""


def _contract_row(slug: str, name: str, team: str, y1: int) -> str:
    return (
        '<tr><th data-stat="ranker">0</th>'
        f'<td data-stat="player"><a href="/players/x/{slug}.html">{name}</a></td>'
        f'<td data-stat="team_id"><a href="/contracts/{team}.html">{team}</a></td>'
        f'<td class="right" csk="{y1}" data-stat="y1">${y1}</td>'
        '<td class="right iz" data-stat="y2"></td>'
        f'<td class="right" csk="{y1}" data-stat="remain_gtd">${y1}</td></tr>'
    )


def test_fetch_all_salaries_collapses_duplicate_bbref_lines(tmp_path):
    rows = "".join(
        [
            _contract_row("mingu01", "Min Guy", "IND", 2_000_000),  # 3 identical IND
            _contract_row("mingu01", "Min Guy", "IND", 2_000_000),
            _contract_row("mingu01", "Min Guy", "IND", 2_000_000),
            _contract_row("mingu01", "Min Guy", "DAL", 2_000_000),  # dead money on DAL
            _contract_row("starxx01", "Star Player", "BOS", 30_000_000),  # unique
        ]
    )
    html = _DUP_FIXTURE_HTML.format(rows=rows)
    cache = JsonCache(tmp_path)
    with patch(
        "nba_trade_analyzer.data.salaries.httpx.get",
        return_value=_mocked_response(html),
    ):
        df = fetch_all_salaries(cache=cache)

    # 3 IND lines collapse to 1; the DAL line is a legitimate separate cap hit.
    assert len(df) == 3
    min_rows = df[df["bbref_slug"] == "mingu01"]
    assert len(min_rows) == 2
    assert set(min_rows["team"]) == {"IND", "DAL"}
    # The kept row preserves the real salary (no double-count, no drop).
    assert int(min_rows[min_rows["team"] == "IND"].iloc[0]["salary"]) == 2_000_000
    assert df["bbref_slug"].tolist().count("starxx01") == 1


def test_build_contract_defaults_yearly_salaries_to_empty():
    # Missing column / pre-feature row → empty tuple → flat-salary fallback.
    contract = build_contract({"salary": 5_000_000, "years_remaining": 2})
    assert contract.yearly_salaries == ()


def test_csv_fallback_tolerates_missing_slug_column(tmp_path):
    # Pre-slug snapshots have no bbref_slug column; the loader adds an empty one
    # so the schema is intact and the crosswalk build can flag the gap.
    df = _load_csv_fallback(_write_fixture_csv(tmp_path))
    assert "bbref_slug" in df.columns
    assert (df["bbref_slug"] == "").all()


def test_csv_fallback_tolerates_missing_yearly_salaries_column(tmp_path):
    # Snapshots predating per-year salaries have no yearly_salaries column; the
    # loader adds an empty one so valuation falls back to the flat salary.
    df = _load_csv_fallback(_write_fixture_csv(tmp_path))
    assert "yearly_salaries" in df.columns
    assert (df["yearly_salaries"] == "").all()
    # build_contract then yields an empty tuple (flat behavior).
    assert build_contract(get_player_salary(df, "Stephen Curry")).yearly_salaries == ()


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
# Rollover fail-loud: a header WITHOUT the expected current season must abort,
# never fall back to positional y1 (the silent fallback shifted every salary
# one season early the night BBRef rolled to 2026-27; only the collapse
# guards stopped the write).
# ---------------------------------------------------------------------------

# Note the anchor is the LABEL, not the position: a table carrying 2026-27
# at any y-column parses fine. "Missing" means a header from a future roll
# (y1 = 2027-28, the July-2027 shape) or a mangled page.
def _header_without(season: str) -> str:
    """The fixture with every year label bumped so ``season`` is absent."""
    out = _FIXTURE_HTML
    for old, new in [
        ("2031-32", "2032-33"),
        ("2030-31", "2031-32"),
        ("2029-30", "2030-31"),
        ("2028-29", "2029-30"),
        ("2027-28", "2028-29"),
        ("2026-27", "2027-28"),
    ]:
        out = out.replace(f'>{old}</td>', f'>{new}</td>')
    assert f">{season}</td>" not in out
    return out


def test_missing_current_season_header_fails_loud_in_strict_mode(tmp_path):
    cache = JsonCache(tmp_path)
    with patch(
        "nba_trade_analyzer.data.salaries.httpx.get",
        return_value=_mocked_response(_header_without("2026-27")),
    ):
        with pytest.raises(RuntimeError, match="no '2026-27' column"):
            fetch_all_salaries(cache=cache, strict=True)


def test_missing_current_season_header_falls_back_to_csv_when_not_strict(tmp_path):
    # Export path: same abort, but degrades to the committed CSV — loudly,
    # with the fallback marker set for the payload sourceNote.
    cache = JsonCache(tmp_path)
    csv_path = _write_fixture_csv(tmp_path)
    with patch(
        "nba_trade_analyzer.data.salaries.httpx.get",
        return_value=_mocked_response(_header_without("2026-27")),
    ):
        df = fetch_all_salaries(cache=cache, csv_path=csv_path)
    assert df.attrs.get("bbref_fallback") is not None
    assert "no '2026-27' column" in df.attrs["bbref_fallback"]["reason"]


# ---------------------------------------------------------------------------
# Rollover mapping pins — the 2026-07-11 cached scrape shapes, mapped through
# the rolled window. Correctly parsed, the LEADING value of every array is
# CURRENT-season (2026-27) money.
# ---------------------------------------------------------------------------


def test_rolled_window_is_2026_27_through_2030_31():
    from nba_trade_analyzer.export import season_keys

    assert season_keys() == ["2026-27", "2027-28", "2028-29", "2029-30", "2030-31"]


def test_contract_rows_map_cached_fixture_shapes_to_correct_seasons():
    from nba_trade_analyzer.export import season_keys
    from nba_trade_analyzer.ingest.runner import _contract_rows

    records = [
        {
            # Curry, single remaining value — his 2026-27 salary (the shifted
            # pre-fix parse would have labeled this 2025-26).
            "player_name": "Stephen Curry",
            "bbref_slug": "curryst01",
            "team": "GSW",
            "salary": 62587158,
            "years_remaining": 1,
            "is_rookie_scale": False,
            "has_player_option": False,
            "has_team_option": False,
            "yearly_salaries": "62587158",
        },
        {
            # Lillard, 4 values from 2026-27 on; the two 22,516,603 MIL
            # stretch-stub values land on 2028-29/2029-30.
            "player_name": "Damian Lillard",
            "bbref_slug": "lillada01",
            "team": "POR",
            "salary": 35915403,
            "years_remaining": 4,
            "is_rookie_scale": False,
            "has_player_option": True,
            "has_team_option": False,
            "yearly_salaries": "35915403|36620603|22516603|22516603",
        },
    ]
    rows = _contract_rows(records, season_keys())
    by_slug = {r.slug: r for r in rows}
    assert by_slug["curryst01"].amounts == {"2026-27": 62587158}
    assert by_slug["lillada01"].amounts == {
        "2026-27": 35915403,
        "2027-28": 36620603,
        "2028-29": 22516603,
        "2029-30": 22516603,
    }


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


# ---------------------------------------------------------------------------
# Per-team raw amounts for multi-stint players (fix/per-team-raw-salary-
# amounts). BBRef's LEAGUE table prints a combined season total in every
# stint row; the per-TEAM /contracts/{TEAM}.html pages carry the real
# per-team figures. Fixtures below are REAL row HTML captured from the live
# team pages 2026-08-31 (Klay DAL+MIA, verified: 17,460,317 + 5,600,000 =
# the league cell 23,060,317).
# ---------------------------------------------------------------------------

# The REAL header row (captured from the live MIA page 2026-08-31, trimmed
# to the load-bearing cells): season labels on data-stat y1.., which the
# parser maps FAIL-LOUD through _season_to_year_stat — a header-less table
# raises rather than silently assuming y1 = current season (the rollover
# bug class). The fixtures exercise the REAL mapping path, not a fallback.
_TEAM_PAGE_THEAD = (
    '<thead><tr>'
    '<th data-stat="player" scope="col">Player</th>'
    '<th data-stat="age_today" scope="col">Age</th>'
    '<th data-stat="y1" scope="col">2026-27</th>'
    '<th data-stat="y2" scope="col">2027-28</th>'
    '<th data-stat="y3" scope="col">2028-29</th>'
    '</tr></thead>'
)

_TEAM_PAGE_TMPL = (
    '<html><body><table id="contracts">' + _TEAM_PAGE_THEAD + '<tbody>'
    '{rows}'
    '</tbody></table></body></html>'
)

# Verbatim (whitespace-collapsed) rows from the live pages. Note the DAL row's
# `partial_table` class and <em> wrapper — the waived section's markup — and
# the player living in a TH whose csk IS the slug.
_DAL_KLAY_TR = (
    '<tr class="partial_table"><th class="left" csk="thompkl01" data-stat="player" '
    'scope="row"><em><a href="/players/t/thompkl01.html">Klay Thompson</a></em></th>'
    '<td class="center" data-stat="age_today">36</td>'
    '<td class="right" csk="17460317" data-stat="y1">$17,460,317</td>'
    '<td class="right iz" data-stat="y2"></td></tr>'
)
_MIA_KLAY_TR = (
    '<tr><th class="left" csk="thompkl01" data-stat="player" scope="row">'
    '<a href="/players/t/thompkl01.html">Klay Thompson</a></th>'
    '<td class="center" data-stat="age_today">36</td>'
    '<td class="right" csk="5600000" data-stat="y1">$5,600,000</td>'
    '<td class="right salary-pl" csk="5880000" data-stat="y2">$5,880,000</td></tr>'
)


def test_parse_team_page_reads_raw_amounts_including_waived_section():
    dal = _parse_team_contract_amounts(
        _TEAM_PAGE_TMPL.format(rows=_DAL_KLAY_TR), {"thompkl01"}, season="2026-27"
    )
    mia = _parse_team_contract_amounts(
        _TEAM_PAGE_TMPL.format(rows=_MIA_KLAY_TR), {"thompkl01"}, season="2026-27"
    )
    assert dal == {"thompkl01": {"2026-27": 17_460_317}}
    assert mia == {"thompkl01": {"2026-27": 5_600_000, "2027-28": 5_880_000}}


def test_team_page_without_season_headers_fails_loud():
    import pytest as _pytest

    headerless = (
        '<html><body><table id="contracts"><thead></thead><tbody>'
        + _MIA_KLAY_TR
        + '</tbody></table></body></html>'
    )
    with _pytest.raises(RuntimeError):
        _parse_team_contract_amounts(headerless, {"thompkl01"}, season="2026-27")

    no_tbody = (
        '<html><body><table id="contracts">' + _TEAM_PAGE_THEAD + '</table></body></html>'
    )
    with _pytest.raises(RuntimeError):
        _parse_team_contract_amounts(no_tbody, {"thompkl01"}, season="2026-27")


def test_attach_raw_amounts_enforces_sum_invariant(caplog):
    import logging

    import pandas as pd

    # Production-shaped league rows: IDENTICAL blended cells on both stints
    # (the live cache's exact Klay values).
    df = pd.DataFrame(
        [
            {
                "player_name": "Klay Thompson", "bbref_slug": "thompkl01",
                "team": "DAL", "salary": 23_060_317, "years_remaining": 2,
                "is_rookie_scale": False, "has_player_option": False,
                "has_team_option": False, "yearly_salaries": "23060317|5880000",
            },
            {
                "player_name": "Klay Thompson", "bbref_slug": "thompkl01",
                "team": "MIA", "salary": 23_060_317, "years_remaining": 2,
                "is_rookie_scale": False, "has_player_option": True,
                "has_team_option": False, "yearly_salaries": "23060317|5880000",
            },
        ]
    )
    raw = {
        "thompkl01": {
            "DAL": {"2026-27": 17_460_317},
            "MIA": {"2026-27": 5_600_000, "2027-28": 5_880_000},
        }
    }
    out = attach_raw_amounts(df, raw, season="2026-27")
    got = {r["team"]: r.get("raw_amounts") for _, r in out.iterrows()}
    assert got["DAL"] == {"2026-27": 17_460_317}
    assert got["MIA"] == {"2026-27": 5_600_000, "2027-28": 5_880_000}

    # SKEWED raw (vintage drift): the sum no longer matches the league cell —
    # fall back to today's behavior for that player (no raw attached), loudly.
    skewed = {
        "thompkl01": {
            "DAL": {"2026-27": 16_000_000},
            "MIA": {"2026-27": 5_600_000, "2027-28": 5_880_000},
        }
    }
    with caplog.at_level(logging.WARNING, logger="nba_trade_analyzer.data.salaries"):
        out2 = attach_raw_amounts(df, skewed, season="2026-27")
    assert all(r.get("raw_amounts") is None for _, r in out2.iterrows())
    assert any("invariant" in r.message.lower() for r in caplog.records)


def test_contract_rows_thread_raw_amounts_into_separation_input():
    from nba_trade_analyzer.ingest.runner import _contract_rows

    rec = {
        "player_name": "Klay Thompson", "bbref_slug": "thompkl01",
        "team": "MIA", "salary": 23_060_317, "years_remaining": 2,
        "is_rookie_scale": False, "has_player_option": True,
        "has_team_option": False, "yearly_salaries": "23060317|5880000",
        "raw_amounts": {"2026-27": 5_600_000, "2027-28": 5_880_000},
    }
    rows = _contract_rows([rec], ["2026-27", "2027-28"])
    assert rows[0].raw_amounts == {"2026-27": 5_600_000, "2027-28": 5_880_000}
    # Absence stays None — the pre-raw machinery applies.
    rec2 = dict(rec)
    rec2.pop("raw_amounts")
    assert _contract_rows([rec2], ["2026-27"])[0].raw_amounts is None
