"""Dead-money loader + the duplicate-row separation matcher (Phase 2A)."""

from __future__ import annotations

import pytest

from nba_trade_analyzer.data.dead_money import (
    DeadMoneyRow,
    clean_player_name,
    load_dead_money,
)
from nba_trade_analyzer.ingest.plans import (
    ContractSeasonAmounts,
    separate_dead_money,
)

_HEADER = "Player,Pos,Age,2026-27,2027-28,2028-29,2029-30,2030-31,Team\n"

DISPLAY_TO_BBREF = {"PHX": "PHO", "MIL": "MIL", "POR": "POR"}


def _write(tmp_path, body: str):
    p = tmp_path / "nba_dead_money.csv"
    p.write_text(_HEADER + body, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def test_loads_rows_with_waived_names(tmp_path):
    body = (
        "Lillard Damian WAIVED,0.0,0.0,22516603.0,22516603.0,22516603.0,22516603.0,0.0,MIL\n"
        "Bradley Beal,0.0,0.0,19383010.0,19383010.0,19383010.0,19383010.0,0.0,PHX\n"
    )
    rows = load_dead_money(_write(tmp_path, body))
    assert len(rows) == 2
    lillard, beal = rows
    assert lillard.player_raw == "Lillard Damian WAIVED"
    assert lillard.player_name == "Lillard Damian"  # marker stripped, order preserved
    assert lillard.was_waived_marker
    assert lillard.amounts == {s: 22516603 for s in ("2026-27", "2027-28", "2028-29", "2029-30")}
    assert not beal.was_waived_marker
    assert beal.team == "PHX"


def test_skips_zero_and_malformed_cells(tmp_path, caplog):
    body = "E.J. Liddell,0.0,0.0,706898.0,junk,0.0,0.0,,PHX\n"
    with caplog.at_level("WARNING"):
        rows = load_dead_money(_write(tmp_path, body))
    assert rows[0].amounts == {"2026-27": 706898}
    assert any("malformed" in r.message for r in caplog.records)


def test_missing_file_raises(tmp_path):
    # Ingest turns this into guard_blocked — never an empty write.
    with pytest.raises(FileNotFoundError):
        load_dead_money(tmp_path / "nope.csv")


def test_clean_player_name():
    assert clean_player_name("Lillard Damian WAIVED") == "Lillard Damian"
    assert clean_player_name("  Bradley  Beal ") == "Bradley Beal"
    assert clean_player_name("Micic Vasilije waived") == "Micic Vasilije"


# ---------------------------------------------------------------------------
# Separation matcher
# ---------------------------------------------------------------------------

def _contract(slug, team, amounts):
    return ContractSeasonAmounts(
        slug=slug, player_name=slug, team=team, amounts=amounts
    )


def test_exact_match_drops_the_dead_money_team_row():
    stretch = {"2026-27": 22516603, "2027-28": 22516603}
    real = {"2026-27": 3500000, "2027-28": 3700000}
    contracts = [
        _contract("lillada01", "MIL", stretch),  # phantom: matches dead money exactly
        _contract("lillada01", "POR", real),
    ]
    dead = DeadMoneyRow(
        player_raw="Lillard Damian WAIVED",
        player_name="Lillard Damian",
        team="MIL",
        amounts=dict(stretch),
    )
    result = separate_dead_money(contracts, {"lillada01": [dead]}, DISPLAY_TO_BBREF)
    assert [c.team for c in result.kept] == ["POR"]
    assert result.dropped == [("lillada01", "MIL", "dead-money match (Lillard Damian WAIVED)")]
    assert result.flags == []


def test_display_team_code_is_translated_before_matching():
    # Dead money says PHX (display); the salary row says PHO (BBRef).
    amounts = {"2026-27": 19383010}
    contracts = [
        _contract("bealbr01", "PHO", amounts),
        _contract("bealbr01", "LAC", {"2026-27": 5000000}),
    ]
    dead = DeadMoneyRow(
        player_raw="Bradley Beal", player_name="Bradley Beal", team="PHX", amounts=dict(amounts)
    )
    result = separate_dead_money(contracts, {"bealbr01": [dead]}, DISPLAY_TO_BBREF)
    assert [c.team for c in result.kept] == ["LAC"]
    assert result.dropped[0][:2] == ("bealbr01", "PHO")


def test_off_by_one_dollar_does_not_match_and_flags_instead():
    contracts = [
        _contract("bealbr01", "PHO", {"2026-27": 19383010}),
        _contract("bealbr01", "LAC", {"2026-27": 19383011}),  # 1 dollar off
    ]
    dead = DeadMoneyRow(
        player_raw="Bradley Beal",
        player_name="Bradley Beal",
        team="LAC",
        amounts={"2026-27": 19383010},  # matches neither LAC row (off by 1)...
    )
    # LAC dead row: LAC salary row has 19383011 != 19383010 -> no drop anywhere.
    result = separate_dead_money(contracts, {"bealbr01": [dead]}, DISPLAY_TO_BBREF)
    assert len(result.kept) == 1  # first row kept
    assert result.kept[0].team == "PHO"
    assert len(result.flags) == 1
    flag = result.flags[0]
    assert flag.kept_team == "PHO"
    assert flag.other_teams == ("LAC",)


def test_duplicate_without_any_dead_money_is_flagged_for_review():
    contracts = [
        _contract("smartma01", "WAS", {"2026-27": 5000000}),
        _contract("smartma01", "LAL", {"2026-27": 5000000}),
    ]
    result = separate_dead_money(contracts, {}, DISPLAY_TO_BBREF)
    assert [c.team for c in result.kept] == ["WAS"]  # first team kept
    assert result.flags[0].other_teams == ("LAL",)
    assert result.dropped == []


def test_single_rows_and_unslugged_rows_pass_through():
    contracts = [
        _contract("curryst01", "GSW", {"2026-27": 59606817}),
        _contract("", "BOS", {"2026-27": 1000000}),  # un-slugged
    ]
    result = separate_dead_money(contracts, {}, DISPLAY_TO_BBREF)
    assert len(result.kept) == 2
    assert result.flags == []
    assert result.dropped == []


# ---------------------------------------------------------------------------
# Season-level split: the REAL Lillard/Beal mixed-schedule pattern
# (verbatim schedules from docs/PHASE0-DATA-TRACE.md Path 1d + nba_dead_money.csv).
# ---------------------------------------------------------------------------

# yearlySalaries index 0 = 2025-26 (Phase 0 Path 1b).
LILLARD_SCHEDULE = {
    "2025-26": 36620603,
    "2026-27": 35915403,
    "2027-28": 36620603,
    "2028-29": 22516603,
    "2029-30": 22516603,
}
LILLARD_DEAD = DeadMoneyRow(
    player_raw="Lillard Damian WAIVED",
    player_name="Lillard Damian",
    team="MIL",
    amounts={s: 22516603 for s in ("2026-27", "2027-28", "2028-29", "2029-30")},
)

BEAL_SCHEDULE = {
    "2025-26": 24737010,
    "2026-27": 25004710,
    "2027-28": 19383010,
    "2028-29": 19383010,
    "2029-30": 19383010,
}
BEAL_DEAD = DeadMoneyRow(
    player_raw="Bradley Beal",
    player_name="Bradley Beal",
    team="PHX",
    amounts={s: 19383010 for s in ("2026-27", "2027-28", "2028-29", "2029-30")},
)


def test_real_lillard_splits_active_contract_onto_por():
    contracts = [
        _contract("lillada01", "MIL", dict(LILLARD_SCHEDULE)),
        _contract("lillada01", "POR", dict(LILLARD_SCHEDULE)),
    ]
    result = separate_dead_money(
        contracts, {"lillada01": [LILLARD_DEAD]}, DISPLAY_TO_BBREF
    )
    # Active contract lands on POR (MIL holds the dead money); the seasons
    # equal to the stretch charge (22516603) are classified out of it.
    assert len(result.kept) == 1
    kept = result.kept[0]
    assert kept.team == "POR"
    assert kept.amounts == {
        "2025-26": 36620603,
        "2026-27": 35915403,
        "2027-28": 36620603,
    }
    assert [d[:2] for d in result.dropped] == [("lillada01", "MIL")]
    assert result.flags == []  # no duplicate_team_rows flag


def test_real_beal_splits_active_contract_onto_lac():
    contracts = [
        _contract("bealbr01", "PHO", dict(BEAL_SCHEDULE)),
        _contract("bealbr01", "LAC", dict(BEAL_SCHEDULE)),
    ]
    result = separate_dead_money(contracts, {"bealbr01": [BEAL_DEAD]}, DISPLAY_TO_BBREF)
    assert len(result.kept) == 1
    kept = result.kept[0]
    assert kept.team == "LAC"  # PHX (=PHO) holds the dead money
    assert kept.amounts == {"2025-26": 24737010, "2026-27": 25004710}
    assert [d[:2] for d in result.dropped] == [("bealbr01", "PHO")]
    assert result.flags == []


def test_split_requires_exactly_two_teams():
    contracts = [
        _contract("x01", "MIL", dict(LILLARD_SCHEDULE)),
        _contract("x01", "POR", dict(LILLARD_SCHEDULE)),
        _contract("x01", "BOS", dict(LILLARD_SCHEDULE)),
    ]
    dead = DeadMoneyRow("X WAIVED", "X", "MIL", dict(LILLARD_DEAD.amounts))
    result = separate_dead_money(contracts, {"x01": [dead]}, DISPLAY_TO_BBREF)
    # 3+ teams -> conservative path: first team kept, others flagged.
    assert [c.team for c in result.kept] == ["MIL"]
    assert result.flags[0].other_teams == ("POR", "BOS")
    assert result.dropped == []


def test_split_refuses_when_both_teams_hold_dead_money():
    contracts = [
        _contract("x01", "MIL", dict(LILLARD_SCHEDULE)),
        _contract("x01", "POR", dict(LILLARD_SCHEDULE)),
    ]
    dead_mil = DeadMoneyRow("X WAIVED", "X", "MIL", {"2028-29": 22516603})
    dead_por = DeadMoneyRow("X WAIVED", "X", "POR", {"2029-30": 22516603})
    result = separate_dead_money(
        contracts, {"x01": [dead_mil, dead_por]}, DISPLAY_TO_BBREF
    )
    assert len(result.flags) == 1
    assert result.dropped == []


def test_split_refuses_when_schedules_differ():
    schedule_b = dict(LILLARD_SCHEDULE)
    schedule_b["2025-26"] += 1
    contracts = [
        _contract("x01", "MIL", dict(LILLARD_SCHEDULE)),
        _contract("x01", "POR", schedule_b),
    ]
    result = separate_dead_money(
        contracts, {"x01": [LILLARD_DEAD]}, DISPLAY_TO_BBREF
    )
    assert len(result.flags) == 1
    assert result.dropped == []


def test_split_refuses_when_no_season_matches_dead_money():
    schedule = {"2026-27": 30000000, "2027-28": 31000000}
    contracts = [
        _contract("x01", "MIL", dict(schedule)),
        _contract("x01", "POR", dict(schedule)),
    ]
    dead = DeadMoneyRow("X WAIVED", "X", "MIL", {"2026-27": 22516603})
    result = separate_dead_money(contracts, {"x01": [dead]}, DISPLAY_TO_BBREF)
    assert len(result.flags) == 1
    assert result.dropped == []


def test_split_refuses_when_every_season_matches_dead_money():
    # All-dead identical pairs are NOT the mixed-schedule case: the dead-team
    # row is the whole-schedule matcher's job, and dropping BOTH rows on a
    # season-level theory would erase a player entirely. Whole-schedule pass
    # drops the MIL row; the POR survivor renders as-is (degenerate but
    # pre-existing behavior, flag-free single survivor).
    schedule = {s: 22516603 for s in ("2026-27", "2027-28")}
    contracts = [
        _contract("x01", "MIL", dict(schedule)),
        _contract("x01", "POR", dict(schedule)),
    ]
    dead = DeadMoneyRow("X WAIVED", "X", "MIL", dict(schedule))
    result = separate_dead_money(contracts, {"x01": [dead]}, DISPLAY_TO_BBREF)
    assert [c.team for c in result.kept] == ["POR"]
    assert result.dropped[0][:2] == ("x01", "MIL")


def test_partial_season_coverage_must_still_match_every_dead_season():
    # Dead money spans two seasons; the salary row only carries one -> no match.
    contracts = [
        _contract("x01", "MIL", {"2026-27": 22516603}),
        _contract("x01", "POR", {"2026-27": 3500000}),
    ]
    dead = DeadMoneyRow(
        player_raw="X WAIVED",
        player_name="X",
        team="MIL",
        amounts={"2026-27": 22516603, "2027-28": 22516603},
    )
    result = separate_dead_money(contracts, {"x01": [dead]}, DISPLAY_TO_BBREF)
    assert result.dropped == []
    assert len(result.flags) == 1
