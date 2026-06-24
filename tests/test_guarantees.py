"""Non-guaranteed (NG) allowlist resolver + export marking — issue 3a, v1."""

from __future__ import annotations

import pandas as pd

from nba_trade_analyzer.data.crosswalk import Crosswalk, CrosswalkEntry
from nba_trade_analyzer.data.guarantees import (
    NgAllowEntry,
    NonGuaranteeResolver,
    build_allowlist_index,
    load_ng_allowlist,
    load_spread_ng_codes,
)
from nba_trade_analyzer.export import build_export


def _resolver(allow_entries, spread_ng):
    return NonGuaranteeResolver(build_allowlist_index(allow_entries), set(spread_ng))


# ---------------------------------------------------------------------------
# Reader: positive-confirmation requires BOTH spread-NG AND allowlist.
# ---------------------------------------------------------------------------
def test_positive_confirmation_requires_both():
    allow = [NgAllowEntry("Kris Dunn", 1627739, "LAC", "2026-27", 5684800, True)]
    spread = {("id", 1627739, "2026-27"), ("nameteam", "kris dunn", "lac", "2026-27")}
    r = _resolver(allow, spread)
    # allowlisted + spread NG -> fires
    assert r.is_non_guaranteed("2026-27", nba_id=1627739, player="Kris Dunn", team="LAC") is True
    # allowlisted but spread NOT NG -> does not fire
    assert (
        _resolver(allow, set()).is_non_guaranteed(
            "2026-27", nba_id=1627739, player="Kris Dunn", team="LAC"
        )
        is False
    )
    # a different (non-NG/non-allowlisted) season for the same player -> no
    assert r.is_non_guaranteed("2025-26", nba_id=1627739, player="Kris Dunn", team="LAC") is False


def test_spread_ng_but_not_allowlisted_stays_committed():
    # Zion IS coded NG in the spread but is NOT on the allowlist -> stays committed.
    allow = [NgAllowEntry("Kris Dunn", 1627739, "LAC", "2026-27", 5684800, True)]
    spread = {("id", 1629627, "2026-27"), ("nameteam", "zion williamson", "nop", "2026-27")}
    r = _resolver(allow, spread)
    assert r.is_non_guaranteed("2026-27", nba_id=1629627, player="Zion Williamson", team="NOP") is False


def test_blank_id_matches_by_name_team():
    # Cam Christie has a blank nba_id in site_Data — must match by name+team.
    allow = [NgAllowEntry("Cameron Christie", None, "LAC", "2026-27", 2296271, True)]
    spread = {("nameteam", "cameron christie", "lac", "2026-27")}
    r = _resolver(allow, spread)
    assert r.is_non_guaranteed("2026-27", nba_id=None, player="Cameron Christie", team="LAC") is True
    # wrong team must not match
    assert r.is_non_guaranteed("2026-27", nba_id=None, player="Cameron Christie", team="NOP") is False


# ---------------------------------------------------------------------------
# Short/long first-name fallback: the BBRef salary source's "Cam Christie" must
# bridge to the allowlist/spread's "Cameron Christie" (blank id => name+team
# only), WITHOUT ever colliding two genuinely different players.
# ---------------------------------------------------------------------------
def _christie_resolver():
    # Allowlist + spread both carry the LONG name "Cameron Christie", blank id.
    allow = [NgAllowEntry("Cameron Christie", None, "LAC", "2026-27", 2296271, True)]
    spread = {("nameteam", "cameron christie", "lac", "2026-27")}
    return _resolver(allow, spread)


def test_short_first_name_bridges_to_long_via_fallback():
    r = _christie_resolver()
    # The exact name differs ("cam" != "cameron") and the id is blank, so this
    # only fires through the new prefix fallback.
    assert r.is_non_guaranteed("2026-27", nba_id=None, player="Cam Christie", team="LAC") is True
    # Symmetric: a long query against a short-named entry also bridges.
    allow = [NgAllowEntry("Cam Christie", None, "LAC", "2026-27", 2296271, True)]
    spread = {("nameteam", "cam christie", "lac", "2026-27")}
    r2 = _resolver(allow, spread)
    assert r2.is_non_guaranteed("2026-27", nba_id=None, player="Cameron Christie", team="LAC") is True


def test_fallback_logs_the_bridged_names(caplog):
    import logging

    r = _christie_resolver()
    with caplog.at_level(logging.INFO, logger="nba_trade_analyzer.data.guarantees"):
        assert r.is_non_guaranteed("2026-27", nba_id=None, player="Cam Christie", team="LAC") is True
    # Every fallback bridge is auditable: both names + team/season appear.
    msgs = [rec.getMessage() for rec in caplog.records]
    assert any(
        "Cam Christie" in m and "cameron christie" in m and "LAC" in m and "2026-27" in m
        for m in msgs
    ), msgs


def test_exact_match_does_not_log(caplog):
    import logging

    # Exact id match for Dunn must stay silent — only the fallback path logs.
    allow = [NgAllowEntry("Kris Dunn", 1627739, "LAC", "2026-27", 5684800, True)]
    spread = {("id", 1627739, "2026-27"), ("nameteam", "kris dunn", "lac", "2026-27")}
    r = _resolver(allow, spread)
    with caplog.at_level(logging.INFO, logger="nba_trade_analyzer.data.guarantees"):
        assert r.is_non_guaranteed("2026-27", nba_id=1627739, player="Kris Dunn", team="LAC") is True
    assert caplog.records == []


def test_fallback_does_not_collide_different_last_name():
    # "Cam Johnson" (Cameron Johnson is a real, distinct player) must NOT bridge
    # to "Cameron Christie" — different last name => different bucket.
    r = _christie_resolver()
    assert r.is_non_guaranteed("2026-27", nba_id=None, player="Cam Johnson", team="LAC") is False


def test_fallback_blocks_ambiguous_short_name():
    # Two genuinely different players share a team/season and both have a first
    # name compatible with "cam" (cameron, camron). The short query is ambiguous
    # and must NOT fire (could otherwise mark the wrong player's salary NG).
    allow = [
        NgAllowEntry("Cameron Smith", None, "LAC", "2026-27", 2000000, True),
        NgAllowEntry("Camron Smith", None, "LAC", "2026-27", 2000000, True),
    ]
    spread = {
        ("nameteam", "cameron smith", "lac", "2026-27"),
        ("nameteam", "camron smith", "lac", "2026-27"),
    }
    r = _resolver(allow, spread)
    assert r.is_non_guaranteed("2026-27", nba_id=None, player="Cam Smith", team="LAC") is False
    # An unambiguous exact long query still fires.
    assert r.is_non_guaranteed("2026-27", nba_id=None, player="Cameron Smith", team="LAC") is True


def test_fallback_rejects_too_short_prefix():
    # 2-letter abbreviations/initials must not prefix-match a longer name.
    allow = [NgAllowEntry("Alex Sarr", None, "WAS", "2026-27", 12000000, True)]
    spread = {("nameteam", "alex sarr", "was", "2026-27")}
    r = _resolver(allow, spread)
    assert r.is_non_guaranteed("2026-27", nba_id=None, player="Al Sarr", team="WAS") is False


def test_fallback_rejects_non_prefix_nickname():
    # The fix is prefix-only, NOT a nickname table: "Mike" must not match
    # "Michael" (not a prefix), so distinct players can't be conflated.
    allow = [NgAllowEntry("Michael Porter", None, "DEN", "2026-27", 35000000, True)]
    spread = {("nameteam", "michael porter", "den", "2026-27")}
    r = _resolver(allow, spread)
    assert r.is_non_guaranteed("2026-27", nba_id=None, player="Mike Porter", team="DEN") is False


def test_fallback_requires_both_indexes():
    # Short/long match in the allowlist only (spread does NOT code it NG) -> no
    # fire. The fallback can't manufacture a spread-NG coding.
    allow = [NgAllowEntry("Cameron Christie", None, "LAC", "2026-27", 2296271, True)]
    r_allow_only = _resolver(allow, set())
    assert r_allow_only.is_non_guaranteed("2026-27", nba_id=None, player="Cam Christie", team="LAC") is False
    # And the reverse: spread-NG match but not allowlisted -> no fire.
    spread = {("nameteam", "cameron christie", "lac", "2026-27")}
    r_spread_only = _resolver([], spread)
    assert r_spread_only.is_non_guaranteed("2026-27", nba_id=None, player="Cam Christie", team="LAC") is False


def test_fallback_still_gated_to_future_seasons():
    # The future-season gate runs before the fallback: a current-year NG short
    # name must not fire.
    allow = [NgAllowEntry("Cameron Christie", None, "LAC", "2025-26", 2296271, True)]
    spread = {("nameteam", "cameron christie", "lac", "2025-26")}
    r = NonGuaranteeResolver(
        build_allowlist_index(allow), set(spread), current_league_year="2025-26"
    )
    assert r.is_non_guaranteed("2025-26", nba_id=None, player="Cam Christie", team="LAC") is False


def test_ng_on_any_year_non_contiguous():
    # NG on a middle year only; neighbours must not fire.
    allow = [NgAllowEntry("X Player", 999, "ABC", "2027-28", 1000000, True)]
    spread = {("id", 999, "2027-28")}
    r = _resolver(allow, spread)
    assert r.is_non_guaranteed("2027-28", nba_id=999, player="X Player", team="ABC") is True
    assert r.is_non_guaranteed("2026-27", nba_id=999, player="X Player", team="ABC") is False
    assert r.is_non_guaranteed("2028-29", nba_id=999, player="X Player", team="ABC") is False


def test_gate_skips_current_and_earlier_seasons():
    # A fully-NG player (allowlisted + spread-NG on BOTH 2025-26 and 2026-27):
    # the elapsed/current league year (2025-26) is gated out; the future year fires.
    allow = [
        NgAllowEntry("Svi", 1629004, "UTA", "2025-26", 3675000, True),
        NgAllowEntry("Svi", 1629004, "UTA", "2026-27", 3850000, True),
    ]
    spread = {("id", 1629004, "2025-26"), ("id", 1629004, "2026-27")}
    r = NonGuaranteeResolver(
        build_allowlist_index(allow), set(spread), current_league_year="2025-26"
    )
    assert r.is_non_guaranteed("2025-26", nba_id=1629004, player="Svi", team="UTA") is False  # gated
    assert r.is_non_guaranteed("2026-27", nba_id=1629004, player="Svi", team="UTA") is True
    # The gate is a single bump-able constant: rolling to 2026-27 gates 2026-27 too.
    r2 = NonGuaranteeResolver(
        build_allowlist_index(allow), set(spread), current_league_year="2026-27"
    )
    assert r2.is_non_guaranteed("2026-27", nba_id=1629004, player="Svi", team="UTA") is False


def test_load_spread_skips_malformed_money_cells(tmp_path):
    csv_text = (
        "Player,Team,nba_id,2026-27,2028-29,option_2026-27,option_2028-29\n"
        "Kris Dunn,LAC,1627739,5684800,0,NG,\n"
        'DaRon Holmes II,DEN,1642347,0,8586971,0,"2028-29 $8,586,971"\n'
    )
    p = tmp_path / "salary_spread.csv"
    p.write_text(csv_text, encoding="utf-8")
    ng = load_spread_ng_codes(p)
    assert ("id", 1627739, "2026-27") in ng  # real NG captured
    # The mis-loaded salary string in Holmes II's option column is NOT read as a code.
    assert not any("holmes" in str(k).lower() for k in ng)


def test_allowlist_file_loads_with_flags():
    entries = load_ng_allowlist()
    keys = {(e.player, e.season) for e in entries}
    assert ("Kris Dunn", "2026-27") in keys
    cam = next(e for e in entries if e.player == "Cameron Christie")
    assert cam.nba_id is None  # blank id recorded explicitly, not dropped
    # Svi has two NG seasons -> two entries.
    svi = [e for e in entries if e.player == "Sviatoslav Mykhailiuk"]
    assert {e.season for e in svi} == {"2025-26", "2026-27"}
    # The 3 spread-unconfirmed players are present but flagged false.
    dru = next(e for e in entries if e.player == "Dru Smith")
    assert dru.spread_ng_confirmed is False


# ---------------------------------------------------------------------------
# Export (mark-only): one player MARKED (Dunn), one NG-but-not-allowlisted
# unmarked (Zion), a blank-id player marked via name+team (Cam Christie). In all
# cases yearly_salaries is LEFT UNCHANGED.
# ---------------------------------------------------------------------------
def _df(cols, rows):
    return pd.DataFrame(rows, columns=cols)


def _salary_row(name, slug, team, yearly):
    return {
        "player_name": name,
        "bbref_slug": slug,
        "team": team,
        "salary": int(yearly.split("|")[0]),
        "years_remaining": len(yearly.split("|")),
        "is_rookie_scale": False,
        "has_player_option": False,
        "has_team_option": False,
        "yearly_salaries": yearly,
    }


def test_export_marks_only_confirmed_ng_without_changing_salaries():
    salary_cols = [
        "player_name", "bbref_slug", "team", "salary", "years_remaining",
        "is_rookie_scale", "has_player_option", "has_team_option", "yearly_salaries",
    ]
    salary_df = _df(
        salary_cols,
        [
            _salary_row("Kris Dunn", "dunnkr01", "LAC", "5426400|5684800"),
            _salary_row("Zion Williamson", "willizi01", "NOP", "39446090|42166510"),
            # Cam Christie: slug intentionally absent from crosswalk -> nba_id None.
            _salary_row("Cameron Christie", "chrisca01", "LAC", "2237684|2296271"),
        ],
    )
    epm_df = _df(["player_name", "player_name_normalized", "team", "epm"], [])
    darko_df = _df(["player_name", "player_name_normalized", "dpm"], [])
    stats_df = _df(["nba_player_id", "player_name", "team", "age", "GP", "MPG", "NET_RATING"], [])
    crosswalk = Crosswalk(
        [
            CrosswalkEntry(nba_id=1627739, nba_name="Kris Dunn", bbref_slug="dunnkr01", bbref_name="Kris Dunn"),
            CrosswalkEntry(nba_id=1629627, nba_name="Zion Williamson", bbref_slug="willizi01", bbref_name="Zion Williamson"),
        ]
    )
    allow = [
        NgAllowEntry("Kris Dunn", 1627739, "LAC", "2026-27", 5684800, True),
        NgAllowEntry("Cameron Christie", None, "LAC", "2026-27", 2296271, True),
    ]
    spread = {
        ("id", 1627739, "2026-27"), ("nameteam", "kris dunn", "lac", "2026-27"),
        ("nameteam", "cameron christie", "lac", "2026-27"),
        ("id", 1629627, "2026-27"), ("nameteam", "zion williamson", "nop", "2026-27"),
    }
    export = build_export(
        salary_df=salary_df, epm_df=epm_df, darko_df=darko_df, stats_df=stats_df,
        crosswalk=crosswalk, guarantee_resolver=_resolver(allow, spread),
    )
    by_slug = {s.bbref_slug: s for s in export.salaries}

    # Dunn: 2026-27 MARKED, salary value UNCHANGED (mark-only).
    assert by_slug["dunnkr01"].yearly_salaries == [5426400, 5684800]
    assert by_slug["dunnkr01"].non_guaranteed_seasons == {"2026-27": 5684800}

    # Zion: coded NG in spread but NOT allowlisted -> unmarked, fully committed.
    assert by_slug["willizi01"].yearly_salaries == [39446090, 42166510]
    assert by_slug["willizi01"].non_guaranteed_seasons == {}

    # Cam Christie: blank id (slug not in crosswalk) -> matched by name+team,
    # MARKED, salary unchanged.
    assert by_slug["chrisca01"].yearly_salaries == [2237684, 2296271]
    assert by_slug["chrisca01"].non_guaranteed_seasons == {"2026-27": 2296271}
