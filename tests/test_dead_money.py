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
    # The survivor's amounts are BELOW the dead charge — not blends containing
    # it — so the decomposition leaves them untouched.
    assert result.kept[0].amounts == real
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
    assert result.flags[0].resolved_by_spotrac is False
    assert result.dropped == []


# ---------------------------------------------------------------------------
# Tier-3 Spotrac tie-break (the davisjd01 class: two-stint BBRef duplicates
# with NO dead-money signal on either side — file order was the old,
# arbitrary rule). Spotrac only CHOOSES between the BBRef rows.
# ---------------------------------------------------------------------------

DAVISON_SCHEDULE = {"2025-26": 2352765, "2026-27": 2584539}  # real cache rows


def test_davisjd01_tie_break_keeps_hou_not_file_first_bos():
    # BBRef file order: BOS (#399) before HOU (#525) — identical schedules,
    # no dead money anywhere. Spotrac's current-team opinion is HOU.
    contracts = [
        _contract("davisjd01", "BOS", dict(DAVISON_SCHEDULE)),
        _contract("davisjd01", "HOU", dict(DAVISON_SCHEDULE)),
    ]
    result = separate_dead_money(
        contracts, {}, DISPLAY_TO_BBREF, spotrac_teams={"davisjd01": "HOU"}
    )
    assert [c.team for c in result.kept] == ["HOU"]
    assert result.kept[0].amounts == DAVISON_SCHEDULE  # dollars stay BBRef's
    # The duplicate is STILL flagged — resolution changes which row is kept,
    # never its visibility in verification.
    assert len(result.flags) == 1
    flag = result.flags[0]
    assert flag.kept_team == "HOU"
    assert flag.other_teams == ("BOS",)
    assert flag.resolved_by_spotrac is True
    assert result.dropped == []


def test_tie_break_is_file_order_independent():
    # Same case with the rows reversed: the outcome is the Spotrac side
    # either way — the rule keys on the opinion, not on position.
    contracts = [
        _contract("davisjd01", "HOU", dict(DAVISON_SCHEDULE)),
        _contract("davisjd01", "BOS", dict(DAVISON_SCHEDULE)),
    ]
    result = separate_dead_money(
        contracts, {}, DISPLAY_TO_BBREF, spotrac_teams={"davisjd01": "HOU"}
    )
    assert [c.team for c in result.kept] == ["HOU"]
    assert result.flags[0].other_teams == ("BOS",)


def test_tie_break_no_spotrac_row_falls_back_to_file_first_plus_flag():
    contracts = [
        _contract("x01", "BOS", {"2026-27": 1000000}),
        _contract("x01", "HOU", {"2026-27": 1000000}),
    ]
    result = separate_dead_money(
        contracts, {}, DISPLAY_TO_BBREF, spotrac_teams={}  # no opinion for x01
    )
    assert [c.team for c in result.kept] == ["BOS"]  # file-first, unchanged
    assert result.flags[0].kept_team == "BOS"
    assert result.flags[0].resolved_by_spotrac is False


def test_tie_break_matching_neither_row_falls_back_never_guesses():
    # Spotrac says MEM; neither BBRef row is MEM. Spotrac must never SUPPLY
    # a team — zero matches means keep file-first + flag.
    contracts = [
        _contract("x01", "BOS", {"2026-27": 1000000}),
        _contract("x01", "HOU", {"2026-27": 1000000}),
    ]
    result = separate_dead_money(
        contracts, {}, DISPLAY_TO_BBREF, spotrac_teams={"x01": "MEM"}
    )
    assert [c.team for c in result.kept] == ["BOS"]
    assert all(c.team != "MEM" for c in result.kept)  # no invented team
    assert result.flags[0].resolved_by_spotrac is False


def test_tie_break_never_reaches_tier2_split_cases():
    # A dead-money-explained pair (the Beal shape) resolves at tier 2; the
    # Spotrac opinion — even a WRONG one — must have no effect there.
    amounts = {"2026-27": 19383010}
    contracts = [
        _contract("bealbr01", "PHO", amounts),
        _contract("bealbr01", "LAC", {"2026-27": 5000000}),
    ]
    dead = DeadMoneyRow(
        player_raw="Bradley Beal", player_name="Bradley Beal", team="PHX", amounts=dict(amounts)
    )
    result = separate_dead_money(
        contracts,
        {"bealbr01": [dead]},
        DISPLAY_TO_BBREF,
        spotrac_teams={"bealbr01": "PHO"},  # wrong opinion, must be ignored
    )
    assert [c.team for c in result.kept] == ["LAC"]  # tier 1/2 outcome unchanged
    assert result.dropped[0][:2] == ("bealbr01", "PHO")
    assert result.flags == []


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


def test_real_lillard_splits_and_decomposes_blends_onto_por(caplog):
    contracts = [
        _contract("lillada01", "MIL", dict(LILLARD_SCHEDULE)),
        _contract("lillada01", "POR", dict(LILLARD_SCHEDULE)),
    ]
    with caplog.at_level("INFO"):
        result = separate_dead_money(
            contracts, {"lillada01": [LILLARD_DEAD]}, DISPLAY_TO_BBREF
        )
    # Active contract lands on POR (MIL holds the dead money); the seasons
    # equal to the stretch charge (22516603) are classified out of it, and
    # the surviving BLENDED cells are decomposed: BBRef publishes active +
    # dead sums (35,915,403 = 13,398,800 POR + 22,516,603 MIL), so the
    # charge is subtracted to recover the real POR salary. 2025-26 keeps its
    # blend honestly: the dead CSV has no 2025-26 column, so there is no
    # charge to subtract for that season.
    assert len(result.kept) == 1
    kept = result.kept[0]
    assert kept.team == "POR"
    assert kept.amounts == {
        "2025-26": 36620603,
        "2026-27": 13398800,
        "2027-28": 14104000,
    }
    assert [d[:2] for d in result.dropped] == [("lillada01", "MIL")]
    assert result.flags == []  # no duplicate_team_rows flag
    # The decomposition is visible in nightly logs.
    blend_logs = [r.message for r in caplog.records if "dead-money blend" in r.message]
    assert any("13398800" in m for m in blend_logs)


def test_real_beal_splits_and_decomposes_blends_onto_lac():
    contracts = [
        _contract("bealbr01", "PHO", dict(BEAL_SCHEDULE)),
        _contract("bealbr01", "LAC", dict(BEAL_SCHEDULE)),
    ]
    result = separate_dead_money(contracts, {"bealbr01": [BEAL_DEAD]}, DISPLAY_TO_BBREF)
    assert len(result.kept) == 1
    kept = result.kept[0]
    assert kept.team == "LAC"  # PHX (=PHO) holds the dead money
    # 2026-27: 25,004,710 blend - 19,383,010 PHX stretch = 5,621,700 active.
    # 2025-26 keeps its blend (no 2025-26 column in the dead CSV).
    assert kept.amounts == {"2025-26": 24737010, "2026-27": 5621700}
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


# ---------------------------------------------------------------------------
# Blend decomposition guardrails (subtract-when-dead-overlaps).
# ---------------------------------------------------------------------------

def test_survivor_of_whole_row_drop_gets_blends_decomposed():
    # MIL's row IS the dead schedule (whole-row drop); the POR survivor still
    # carries a blended overlap cell — decompose it.
    stretch = {"2026-27": 22516603, "2027-28": 22516603}
    contracts = [
        _contract("lillada01", "MIL", dict(stretch)),
        _contract("lillada01", "POR", {"2026-27": 35915403, "2027-28": 22516603}),
    ]
    dead = DeadMoneyRow("Lillard Damian WAIVED", "Lillard Damian", "MIL", dict(stretch))
    result = separate_dead_money(contracts, {"lillada01": [dead]}, DISPLAY_TO_BBREF)
    assert [c.team for c in result.kept] == ["POR"]
    # 2026-27 blend decomposed; 2027-28 EQUALS the charge (pure-dead season is
    # the exact-match classifiers' territory, never subtraction's) — untouched.
    assert result.kept[0].amounts == {"2026-27": 13398800, "2027-28": 22516603}


def test_no_dead_money_means_no_subtraction():
    # Regression guard: tier-3 duplicates without dead charges keep BBRef's
    # dollars exactly (davisjd01 class).
    contracts = [
        _contract("davisjd01", "BOS", dict(DAVISON_SCHEDULE)),
        _contract("davisjd01", "HOU", dict(DAVISON_SCHEDULE)),
    ]
    result = separate_dead_money(
        contracts, {}, DISPLAY_TO_BBREF, spotrac_teams={"davisjd01": "HOU"}
    )
    assert result.kept[0].amounts == DAVISON_SCHEDULE


def test_implausible_residual_is_refused_with_a_warning(caplog):
    # Blend - charge = $83,397 — far below any real season salary. Keep the
    # original value and say so loudly, naming the player.
    schedule = {"2026-27": 22600000, "2027-28": 22516603}
    contracts = [
        _contract("x01", "MIL", dict(schedule)),
        _contract("x01", "POR", dict(schedule)),
    ]
    dead = DeadMoneyRow(
        "X WAIVED", "X", "MIL", {"2026-27": 22516603, "2027-28": 22516603}
    )
    with caplog.at_level("WARNING"):
        result = separate_dead_money(contracts, {"x01": [dead]}, DISPLAY_TO_BBREF)
    assert [c.team for c in result.kept] == ["POR"]
    assert result.kept[0].amounts == {"2026-27": 22600000}  # original kept
    refusals = [r.message for r in caplog.records if "REFUSING" in r.message]
    assert refusals and "x01" in refusals[0]


def test_multiple_dead_streams_subtract_their_sum():
    # Two former teams each hold a charge in the same season; the blend
    # contains both, so the SUM is subtracted.
    schedule = {"2026-27": 30000000, "2027-28": 5000000}
    contracts = [
        _contract("x01", "MIL", dict(schedule)),
        _contract("x01", "POR", dict(schedule)),
    ]
    dead_mil = DeadMoneyRow("X WAIVED", "X", "MIL", {"2027-28": 5000000})
    dead_phx = DeadMoneyRow("X", "X", "PHX", {"2026-27": 10000000})
    result = separate_dead_money(
        contracts, {"x01": [dead_mil, dead_phx]}, DISPLAY_TO_BBREF
    )
    # Split: 2027-28 == MIL charge -> dead; kept POR 2026-27 blend contains
    # the PHX stream (30,000,000 - 10,000,000 = 20,000,000 active).
    assert [c.team for c in result.kept] == ["POR"]
    assert result.kept[0].amounts == {"2026-27": 20000000}


def test_amount_below_charge_is_not_a_blend_and_stays():
    # The source-side-fix shape: BBRef already publishes the true active
    # value (smaller than the dead charge) — subtraction must self-disarm.
    contracts = [
        _contract("bealbr01", "PHO", {"2026-27": 19383010, "2027-28": 19383010}),
        _contract("bealbr01", "LAC", {"2026-27": 5354000, "2027-28": 5621700}),
    ]
    dead = DeadMoneyRow(
        "Bradley Beal", "Bradley Beal", "PHX",
        {"2026-27": 19383010, "2027-28": 19383010},
    )
    result = separate_dead_money(contracts, {"bealbr01": [dead]}, DISPLAY_TO_BBREF)
    assert [c.team for c in result.kept] == ["LAC"]
    assert result.kept[0].amounts == {"2026-27": 5354000, "2027-28": 5621700}


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


# ---------------------------------------------------------------------------
# Rollover-corrected blend decomposition (2026-07-11): the first correctly-
# labeled post-rollover schedules. The night BBRef rolled, the shifted labels
# produced exactly ONE blend line (Lillard's, attached to the wrong season);
# correctly parsed, all THREE decompose at the right seasons.
# ---------------------------------------------------------------------------

def test_rolled_lillard_decomposes_2026_27_and_2027_28():
    schedule = {"2026-27": 35915403, "2027-28": 36620603, "2028-29": 22516603, "2029-30": 22516603}
    contracts = [
        _contract("lillada01", "MIL", dict(schedule)),
        _contract("lillada01", "POR", dict(schedule)),
    ]
    dead = DeadMoneyRow(
        "Lillard Damian WAIVED", "Lillard Damian", "MIL",
        {s: 22516603 for s in ("2026-27", "2027-28", "2028-29", "2029-30")},
    )
    result = separate_dead_money(contracts, {"lillada01": [dead]}, DISPLAY_TO_BBREF)
    assert [c.team for c in result.kept] == ["POR"]
    assert result.kept[0].amounts == {"2026-27": 13398800, "2027-28": 14104000}


def test_rolled_beal_decomposes_2026_27():
    schedule = {"2026-27": 25004710, "2027-28": 19383010, "2028-29": 19383010, "2029-30": 19383010}
    contracts = [
        _contract("bealbr01", "PHO", dict(schedule)),
        _contract("bealbr01", "LAC", dict(schedule)),
    ]
    dead = DeadMoneyRow(
        "Bradley Beal", "Bradley Beal", "PHX",
        {s: 19383010 for s in ("2026-27", "2027-28", "2028-29", "2029-30")},
    )
    result = separate_dead_money(contracts, {"bealbr01": [dead]}, DISPLAY_TO_BBREF)
    assert [c.team for c in result.kept] == ["LAC"]
    assert result.kept[0].amounts == {"2026-27": 5621700}


def test_rolled_prosper_decomposes_2026_27():
    schedule = {"2026-27": 3500172, "2027-28": 1002360}
    contracts = [
        _contract("prospol01", "MEM", dict(schedule)),
        _contract("prospol01", "DAL", dict(schedule)),
    ]
    dead = DeadMoneyRow(
        "Olivier-Maxence Prosper", "Olivier-Maxence Prosper", "DAL",
        {"2026-27": 1002360, "2027-28": 1002360},
    )
    result = separate_dead_money(
        contracts, {"prospol01": [dead]}, {**DISPLAY_TO_BBREF, "DAL": "DAL"}
    )
    assert [c.team for c in result.kept] == ["MEM"]
    assert result.kept[0].amounts == {"2026-27": 2497812}
