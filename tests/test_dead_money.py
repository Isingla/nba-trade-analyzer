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
    assert lillard.amounts == {
        s: 22516603 for s in ("2026-27", "2027-28", "2028-29", "2029-30")
    }
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
    assert result.dropped == [
        ("lillada01", "MIL", "dead-money match (Lillard Damian WAIVED)")
    ]
    assert result.flags == []


def test_display_team_code_is_translated_before_matching():
    # Dead money says PHX (display); the salary row says PHO (BBRef).
    amounts = {"2026-27": 19383010}
    contracts = [
        _contract("bealbr01", "PHO", amounts),
        _contract("bealbr01", "LAC", {"2026-27": 5000000}),
    ]
    dead = DeadMoneyRow(
        player_raw="Bradley Beal",
        player_name="Bradley Beal",
        team="PHX",
        amounts=dict(amounts),
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
        contracts,
        {},
        DISPLAY_TO_BBREF,
        spotrac_teams={},  # no opinion for x01
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
        player_raw="Bradley Beal",
        player_name="Bradley Beal",
        team="PHX",
        amounts=dict(amounts),
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


def test_single_rows_without_dead_equality_and_unslugged_rows_pass_through():
    # NARROWED CLAIM (gap fix 2026-07-18): single rows pass through when
    # their schedule does NOT whole-schedule-equal an own-team dead charge
    # (the pure-dead class is dropped/flagged — see the pure-dead tests).
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
    result = separate_dead_money(contracts, {"x01": [LILLARD_DEAD]}, DISPLAY_TO_BBREF)
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
        "Bradley Beal",
        "Bradley Beal",
        "PHX",
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
# Same-team blend (2026-07-17): the isaacjo01 waive-and-re-sign shape. ORL
# waived Isaac 6/27 ($8,000,000 gtd, not stretched); he re-signed ORL on a
# vet minimum during the moratorium (cap hit $2,449,421). BBRef prints ONE
# ORL row with the undecomposed total $10,449,421 — no duplicate fingerprint,
# so the single-row path must decompose it against the SAME team's charge.
# ---------------------------------------------------------------------------

ISAAC_DEAD = DeadMoneyRow(
    player_raw="Jonathan Isaac WAIVED",
    player_name="Jonathan Isaac",
    team="ORL",
    amounts={"2026-27": 8000000},
)


def test_isaac_same_team_blend_decomposes_single_row(caplog):
    contracts = [_contract("isaacjo01", "ORL", {"2026-27": 10449421})]
    with caplog.at_level("INFO"):
        result = separate_dead_money(
            contracts, {"isaacjo01": [ISAAC_DEAD]}, DISPLAY_TO_BBREF
        )
    # 10,449,421 (blended) - 8,000,000 (dead) = 2,449,421 (active @ORL).
    assert [c.team for c in result.kept] == ["ORL"]
    assert result.kept[0].amounts == {"2026-27": 2449421}
    assert result.dropped == []  # decomposed in place — nothing to drop
    assert result.flags == []
    # The blend line is logged exactly like the dual-team trio's.
    blend_logs = [r.message for r in caplog.records if "dead-money blend" in r.message]
    assert any(
        "10449421" in m and "8000000" in m and "2449421" in m for m in blend_logs
    )


def test_same_team_implausible_residual_is_not_separated(caplog):
    # False-positive lock: dead money present but the arithmetic does NOT
    # leave a plausible active remainder (8,500,000 - 8,000,000 = 500,000,
    # below MIN_PLAUSIBLE_ACTIVE_SALARY) — refuse loudly, keep the original.
    contracts = [_contract("isaacjo01", "ORL", {"2026-27": 8500000})]
    with caplog.at_level("WARNING"):
        result = separate_dead_money(
            contracts, {"isaacjo01": [ISAAC_DEAD]}, DISPLAY_TO_BBREF
        )
    assert result.kept[0].amounts == {"2026-27": 8500000}
    refusals = [r.message for r in caplog.records if "REFUSING" in r.message]
    assert refusals and "isaacjo01" in refusals[0]


# ---------------------------------------------------------------------------
# Pure-dead single rows (gap fix 2026-07-18): a player waived and signed
# nowhere gets ONE BBRef row equal to his stretch schedule — Micic/Rubio/
# McGee/Louzada class. Whole-schedule equality + Spotrac corroboration drops
# the row (the charge lives only in dead money); equality WITHOUT
# corroboration keeps + flags (the minimum-salary coincidence guard).
# ---------------------------------------------------------------------------

def test_pure_dead_single_row_with_spotrac_corroboration_is_dropped():
    # T1 (inverts the old cell == charge half of the guardian test): cell
    # equals the own-team charge for the whole schedule, and Spotrac's
    # actives map (provided, player absent) corroborates he is gone.
    contracts = [_contract("isaacjo01", "ORL", {"2026-27": 8000000})]
    result = separate_dead_money(
        contracts, {"isaacjo01": [ISAAC_DEAD]}, DISPLAY_TO_BBREF, spotrac_teams={}
    )
    assert result.kept == []
    assert result.flags == []
    assert result.dropped == [
        ("isaacjo01", "ORL", "pure-dead single row (Jonathan Isaac WAIVED)")
    ]


def test_same_team_cell_below_charge_is_not_separated():
    # RE-PINNED 2026-08-31 (Fix B ruling): with spotrac ACTIVE data loaded
    # and the player ABSENT from it while present as dead money, a single
    # row is pure dead regardless of cell-vs-charge arithmetic — the old
    # KEEP pin modeled a pre-ruling caution. The cell-below-charge KEEP
    # behavior survives where it belongs: a player Spotrac places ON the
    # team (real Isaac), or no Spotrac data at all.
    contracts = [_contract("isaacjo01", "ORL", {"2026-27": 5000000})]
    result = separate_dead_money(
        contracts, {"isaacjo01": [ISAAC_DEAD]}, DISPLAY_TO_BBREF, spotrac_teams={}
    )
    assert result.kept == []
    assert any("pure-dead" in why or "pure dead" in why for _, _, why in result.dropped)

    # Spotrac-active on the team: kept, same-team arithmetic untouched.
    result_active = separate_dead_money(
        contracts, {"isaacjo01": [ISAAC_DEAD]}, DISPLAY_TO_BBREF,
        spotrac_teams={"isaacjo01": "ORL"},
    )
    assert result_active.kept[0].amounts == {"2026-27": 5000000}

    # No Spotrac data: the old caution holds — keep.
    result_none = separate_dead_money(
        contracts, {"isaacjo01": [ISAAC_DEAD]}, DISPLAY_TO_BBREF, spotrac_teams=None
    )
    assert result_none.kept[0].amounts == {"2026-27": 5000000}


def test_pure_dead_equality_without_corroboration_is_kept_and_flagged():
    # T2: the minimum-salary coincidence guard, both non-corroborated shapes.
    # (a) no Spotrac data at all; (b) Spotrac says he IS an active on this
    # team. Either way: keep the row, flag for human review, never guess.
    for spotrac in (None, {"isaacjo01": "ORL"}):
        contracts = [_contract("isaacjo01", "ORL", {"2026-27": 8000000})]
        result = separate_dead_money(
            contracts, {"isaacjo01": [ISAAC_DEAD]}, DISPLAY_TO_BBREF, spotrac_teams=spotrac
        )
        assert result.kept[0].amounts == {"2026-27": 8000000}
        assert result.dropped == []
        assert len(result.flags) == 1
        assert result.flags[0].slug == "isaacjo01"
        assert result.flags[0].kept_team == "ORL"


def test_pure_dead_multi_season_schedule_is_dropped_within_rounding():
    # T3: the REAL Louzada shape (live CSVs 2026-07-18) — BBRef prints
    # 268,032 every season while the dead CSV rounds the stretch split as
    # 268,032 + 268,031 + 268,031 ($804,095 / 3). The ±$1
    # PURE_DEAD_ROUNDING_TOLERANCE absorbs exactly this.
    dead = DeadMoneyRow(
        player_raw="Didi Louzada WAIVED",
        player_name="Didi Louzada",
        team="POR",
        amounts={"2026-27": 268032, "2027-28": 268031, "2028-29": 268031},
    )
    contracts = [
        _contract(
            "louzama01",
            "POR",
            {"2026-27": 268032, "2027-28": 268032, "2028-29": 268032},
        )
    ]
    result = separate_dead_money(
        contracts, {"louzama01": [dead]}, DISPLAY_TO_BBREF, spotrac_teams={}
    )
    assert result.kept == []
    assert result.dropped == [
        ("louzama01", "POR", "pure-dead single row (Didi Louzada WAIVED)")
    ]


def test_pure_dead_superset_charge_still_explains_row():
    # The Little shape (live CSVs 2026-07-18): a 5-season stretch charge
    # where BBRef truncates its printed table at 3 seasons. Every VISIBLE
    # dollar is explained by the charge -> the row drops whole.
    dead = DeadMoneyRow(
        player_raw="Nassir Little WAIVED",
        player_name="Nassir Little",
        team="PHX",
        amounts={
            "2026-27": 3107143,
            "2027-28": 3107143,
            "2028-29": 3107143,
            "2029-30": 3107143,
            "2030-31": 3107143,
        },
    )
    contracts = [
        _contract(
            "littlna01",
            "PHO",
            {"2026-27": 3107143, "2027-28": 3107143, "2028-29": 3107143},
        )
    ]
    result = separate_dead_money(
        contracts, {"littlna01": [dead]}, DISPLAY_TO_BBREF, spotrac_teams={}
    )
    assert result.kept == []
    assert result.dropped == [
        ("littlna01", "PHO", "pure-dead single row (Nassir Little WAIVED)")
    ]


def test_charge_covering_fewer_seasons_than_row_is_not_pure_dead():
    # The EXACT pure-dead classifier still refuses a charge covering fewer
    # seasons than the row (its reason string must not appear). RE-PINNED
    # 2026-08-31: with spotrac actives loaded and the player absent, Fix B
    # drops the row as stretched pure dead instead — and with no spotrac
    # data the old keep-caution holds.
    contracts = [
        _contract("someoso01", "POR", {"2026-27": 268032, "2027-28": 5000000})
    ]
    dead = DeadMoneyRow(
        "Someone WAIVED", "Someone", "POR", {"2026-27": 268032}
    )
    result = separate_dead_money(
        contracts, {"someoso01": [dead]}, DISPLAY_TO_BBREF, spotrac_teams={}
    )
    assert not any(why.startswith("pure-dead single row") for _, _, why in result.dropped)
    assert any("stretched pure-dead" in why for _, _, why in result.dropped)

    result_none = separate_dead_money(
        contracts, {"someoso01": [dead]}, DISPLAY_TO_BBREF, spotrac_teams=None
    )
    assert result_none.kept[0].amounts["2027-28"] == 5000000


def test_partial_equality_is_not_a_pure_dead_candidate():
    # The EXACT classifier must not fire on partial equality (its reason is
    # absent). RE-PINNED 2026-08-31: spotrac-absent + dead-present is Fix
    # B's stretched-pure-dead — dropped with ITS reason; spotrac_teams=None
    # keeps the old refuse-and-keep caution.
    schedule = {"2026-27": 268032, "2027-28": 999999}
    dead = DeadMoneyRow(
        player_raw="Didi Louzada WAIVED",
        player_name="Didi Louzada",
        team="POR",
        amounts={"2026-27": 268032, "2027-28": 268031},
    )
    contracts = [_contract("louzadi01", "POR", dict(schedule))]
    result = separate_dead_money(
        contracts, {"louzadi01": [dead]}, DISPLAY_TO_BBREF, spotrac_teams={}
    )
    assert not any(why.startswith("pure-dead single row") for _, _, why in result.dropped)
    assert any("stretched pure-dead" in why for _, _, why in result.dropped)

    result_none = separate_dead_money(
        contracts, {"louzadi01": [dead]}, DISPLAY_TO_BBREF, spotrac_teams=None
    )
    assert result_none.kept[0].amounts == schedule


def test_single_row_cross_team_charge_is_never_subtracted():
    # The old-stretch coexistence shape: an active row on the NEW team next
    # to a former team's fresh charge is NOT a blend (when BBRef blends
    # across two teams it prints two rows — the duplicate machinery's
    # territory). Matching-looking arithmetic must not fire on a single row.
    contracts = [_contract("x01", "GSW", {"2026-27": 30000000})]
    dead = DeadMoneyRow("X WAIVED", "X", "MIL", {"2026-27": 10000000})
    result = separate_dead_money(contracts, {"x01": [dead]}, DISPLAY_TO_BBREF)
    assert result.kept[0].amounts == {"2026-27": 30000000}


def test_same_team_blend_only_touches_seasons_with_a_charge():
    # The vet-min's second season has no dead-money column — it must ride
    # through unchanged while the overlap season decomposes.
    contracts = [
        _contract("isaacjo01", "ORL", {"2026-27": 10449421, "2027-28": 2667947})
    ]
    result = separate_dead_money(
        contracts, {"isaacjo01": [ISAAC_DEAD]}, DISPLAY_TO_BBREF
    )
    assert result.kept[0].amounts == {"2026-27": 2449421, "2027-28": 2667947}


# ---------------------------------------------------------------------------
# Rollover-corrected blend decomposition (2026-07-11): the first correctly-
# labeled post-rollover schedules. The night BBRef rolled, the shifted labels
# produced exactly ONE blend line (Lillard's, attached to the wrong season);
# correctly parsed, all THREE decompose at the right seasons.
# ---------------------------------------------------------------------------


def test_rolled_lillard_decomposes_2026_27_and_2027_28():
    schedule = {
        "2026-27": 35915403,
        "2027-28": 36620603,
        "2028-29": 22516603,
        "2029-30": 22516603,
    }
    contracts = [
        _contract("lillada01", "MIL", dict(schedule)),
        _contract("lillada01", "POR", dict(schedule)),
    ]
    dead = DeadMoneyRow(
        "Lillard Damian WAIVED",
        "Lillard Damian",
        "MIL",
        {s: 22516603 for s in ("2026-27", "2027-28", "2028-29", "2029-30")},
    )
    result = separate_dead_money(contracts, {"lillada01": [dead]}, DISPLAY_TO_BBREF)
    assert [c.team for c in result.kept] == ["POR"]
    assert result.kept[0].amounts == {"2026-27": 13398800, "2027-28": 14104000}


def test_rolled_beal_decomposes_2026_27():
    schedule = {
        "2026-27": 25004710,
        "2027-28": 19383010,
        "2028-29": 19383010,
        "2029-30": 19383010,
    }
    contracts = [
        _contract("bealbr01", "PHO", dict(schedule)),
        _contract("bealbr01", "LAC", dict(schedule)),
    ]
    dead = DeadMoneyRow(
        "Bradley Beal",
        "Bradley Beal",
        "PHX",
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
        "Olivier-Maxence Prosper",
        "Olivier-Maxence Prosper",
        "DAL",
        {"2026-27": 1002360, "2027-28": 1002360},
    )
    result = separate_dead_money(
        contracts, {"prospol01": [dead]}, {**DISPLAY_TO_BBREF, "DAL": "DAL"}
    )
    assert [c.team for c in result.kept] == ["MEM"]
    assert result.kept[0].amounts == {"2026-27": 2497812}


# ---------------------------------------------------------------------------
# Fossil salary rows (fix/ingest-fossil-salary-rows, figures verified on
# BBRef by hand 2026-08-31): when a waived player signs ELSEWHERE, BBRef
# keeps the old team's pre-waive contract table beside the new team's. The
# old-team row is a FOSSIL — the dead-money frame is already that team's
# truth — and must drop before any summing/blending. Gated on Spotrac
# corroboration that the player is NOT active at the charge team, so the
# same-team waive-and-re-sign blend (Isaac) is untouched.
# ---------------------------------------------------------------------------

_FOSSIL_DISPLAY = {"PHX": "PHO", "DAL": "DAL", "MEM": "MEM"}


def test_klay_two_table_fossil_drops_dal_keeps_mia():
    # Klay Thompson: waived by DAL 08-21 (dead $7,660,317), signed MIA.
    # BBRef prints BOTH tables; the stored 2026-27 salary came out as
    # 23,060,317 = 17,460,317 (DAL pre-waive fossil) + 5,600,000 (MIA truth).
    contracts = [
        ContractSeasonAmounts(
            slug="thompkl01",
            player_name="Klay Thompson",
            team="DAL",
            amounts={"2026-27": 17_460_317},
        ),
        ContractSeasonAmounts(
            slug="thompkl01",
            player_name="Klay Thompson",
            team="MIA",
            amounts={"2026-27": 5_600_000, "2027-28": 5_880_000},
        ),
    ]
    dead = DeadMoneyRow(
        player_raw="Klay Thompson WAIVED",
        player_name="Klay Thompson",
        team="DAL",
        amounts={"2026-27": 7_660_317},
    )
    result = separate_dead_money(
        contracts,
        {"thompkl01": [dead]},
        _FOSSIL_DISPLAY,
        spotrac_teams={"thompkl01": "MIA"},
    )
    assert len(result.kept) == 1
    kept = result.kept[0]
    assert kept.team == "MIA"
    assert kept.amounts == {"2026-27": 5_600_000, "2027-28": 5_880_000}
    assert any(team == "DAL" for _, team, _ in result.dropped)
    # No DAL salary row survives anywhere.
    assert all(k.team != "DAL" for k in result.kept)


def test_kcp_legacy_mem_row_is_a_fossil_beside_the_phi_minimum():
    # Caldwell-Pope: legacy MEM table ($20,194,392) beside the real PHI
    # minimum ($2,449,421); MEM dead money $17,744,971. The MEM charge does
    # NOT equal the MEM row (so the exact-match dropper never fired) — the
    # fossil rule is what removes it.
    contracts = [
        ContractSeasonAmounts(
            slug="caldwke01",
            player_name="Kentavious Caldwell-Pope",
            team="MEM",
            amounts={"2026-27": 20_194_392},
        ),
        ContractSeasonAmounts(
            slug="caldwke01",
            player_name="Kentavious Caldwell-Pope",
            team="PHI",
            amounts={"2026-27": 2_449_421},
        ),
    ]
    dead = DeadMoneyRow(
        player_raw="Kentavious Caldwell-Pope WAIVED",
        player_name="Kentavious Caldwell-Pope",
        team="MEM",
        amounts={"2026-27": 17_744_971},
    )
    result = separate_dead_money(
        contracts,
        {"caldwke01": [dead]},
        _FOSSIL_DISPLAY,
        spotrac_teams={"caldwke01": "PHI"},
    )
    assert len(result.kept) == 1
    assert result.kept[0].team == "PHI"
    assert result.kept[0].amounts == {"2026-27": 2_449_421}
    assert all(k.team != "MEM" for k in result.kept)


def test_beal_real_shape_with_spotrac_is_byte_identical():
    # MUST-NOT-BREAK pin, on the FILE'S OWN real Beal fixtures and WITH
    # spotrac_teams passed (production always passes a dict — runner). The
    # first fossil implementation regressed exactly this: it fired on the
    # identical-schedule blended duplicate, pre-empted the season-level
    # split, and stranded pure-dead seasons on the kept row as phantom
    # salary (+$58.1M). The split's outcome must be byte-identical with the
    # fossil pass armed.
    contracts = [
        _contract("bealbr01", "PHO", dict(BEAL_SCHEDULE)),
        _contract("bealbr01", "LAC", dict(BEAL_SCHEDULE)),
    ]
    result = separate_dead_money(
        contracts,
        {"bealbr01": [BEAL_DEAD]},
        DISPLAY_TO_BBREF,
        spotrac_teams={"bealbr01": "LAC"},
    )
    assert len(result.kept) == 1
    kept = result.kept[0]
    assert kept.team == "LAC"
    assert kept.amounts == {"2025-26": 24737010, "2026-27": 5621700}
    assert result.flags == []


def test_lillard_real_shape_with_spotrac_is_byte_identical():
    # Same pin for the module's founding case: identical blended schedules,
    # MIL stretch charge, Spotrac=POR. The fossil pass must yield to the
    # season-level split — a fossil drop here kept 2028-29/2029-30 at
    # 22,516,603 each (+$45.0M phantom) in the first implementation.
    contracts = [
        _contract("lillada01", "MIL", dict(LILLARD_SCHEDULE)),
        _contract("lillada01", "POR", dict(LILLARD_SCHEDULE)),
    ]
    result = separate_dead_money(
        contracts,
        {"lillada01": [LILLARD_DEAD]},
        DISPLAY_TO_BBREF,
        spotrac_teams={"lillada01": "POR"},
    )
    kept = result.kept[0]
    assert kept.team == "POR"
    assert kept.amounts == {
        "2025-26": 36620603,
        "2026-27": 13398800,
        "2027-28": 14104000,
    }


def test_third_team_spotrac_is_not_fossil_corroboration():
    # Spotrac placing the player at a team on NEITHER row corroborates
    # nothing: old tier-3 behavior (keep file-first, flag) must hold — the
    # fossil pass requires Spotrac to endorse a SPECIFIC surviving row.
    contracts = [
        ContractSeasonAmounts(
            slug="thirdte01", player_name="Third Team", team="DAL",
            amounts={"2026-27": 17_460_317},
        ),
        ContractSeasonAmounts(
            slug="thirdte01", player_name="Third Team", team="MIA",
            amounts={"2026-27": 5_600_000},
        ),
    ]
    dead = DeadMoneyRow(
        player_raw="Third Team WAIVED", player_name="Third Team",
        team="DAL", amounts={"2026-27": 7_660_317},
    )
    result = separate_dead_money(
        contracts, {"thirdte01": [dead]}, _FOSSIL_DISPLAY,
        spotrac_teams={"thirdte01": "LAL"},
    )
    assert len(result.kept) == 1
    assert result.kept[0].team == "DAL"  # file-first, unchanged
    assert len(result.flags) == 1  # surfaced for human review
    assert not any("fossil" in why for _, _, why in result.dropped)


def test_both_teams_holding_dead_is_still_ambiguous():
    # Charges on BOTH rows' teams: the corroborated survivor's own team
    # holds an overlapping charge, so the fossil gate refuses and the old
    # tier-3 flag surfaces the ambiguity — never a silent resolution that
    # keeps a team's own dead amount as active salary.
    contracts = [
        ContractSeasonAmounts(
            slug="bothde01", player_name="Both Dead", team="MIL",
            amounts={"2026-27": 10_000_000},
        ),
        ContractSeasonAmounts(
            slug="bothde01", player_name="Both Dead", team="POR",
            amounts={"2026-27": 9_000_000},
        ),
    ]
    dead_mil = DeadMoneyRow("Both Dead WAIVED", "Both Dead", "MIL", {"2026-27": 4_000_000})
    dead_por = DeadMoneyRow("Both Dead WAIVED", "Both Dead", "POR", {"2026-27": 3_000_000})
    result = separate_dead_money(
        contracts, {"bothde01": [dead_mil, dead_por]}, DISPLAY_TO_BBREF,
        spotrac_teams={"bothde01": "POR"},
    )
    assert len(result.kept) == 1
    assert len(result.flags) == 1
    assert not any("fossil" in why for _, _, why in result.dropped)


def test_three_row_group_with_uncharged_third_is_not_fossil_resolved():
    # MIL charged, POR corroborated, BOS neither: not every non-corroborated
    # row is a fossil, so the pass refuses (all-or-nothing) and tier-3
    # keeps its flag — partial fossil resolution would be a guess about BOS.
    contracts = [
        ContractSeasonAmounts(
            slug="threer01", player_name="Three Rows", team="MIL",
            amounts={"2026-27": 10_000_000},
        ),
        ContractSeasonAmounts(
            slug="threer01", player_name="Three Rows", team="POR",
            amounts={"2026-27": 9_000_000},
        ),
        ContractSeasonAmounts(
            slug="threer01", player_name="Three Rows", team="BOS",
            amounts={"2026-27": 8_000_000},
        ),
    ]
    dead = DeadMoneyRow("Three Rows WAIVED", "Three Rows", "MIL", {"2026-27": 4_000_000})
    result = separate_dead_money(
        contracts, {"threer01": [dead]}, DISPLAY_TO_BBREF,
        spotrac_teams={"threer01": "POR"},
    )
    assert len(result.kept) == 1
    assert len(result.flags) == 1
    assert not any("fossil" in why for _, _, why in result.dropped)


def test_exact_pure_dead_keeps_its_reason_when_signed_elsewhere():
    # Ordering pin: a single row EXACTLY equal to its own team's charge with
    # Spotrac elsewhere is the pure-dead classifier's case — its reason
    # string and flag-free drop must survive the fossil pass (which runs
    # after it and only on NON-exact overlaps).
    contracts = [
        ContractSeasonAmounts(
            slug="puredx01", player_name="Pure Dead", team="DAL",
            amounts={"2026-27": 7_660_317},
        ),
    ]
    dead = DeadMoneyRow(
        "Pure Dead WAIVED", "Pure Dead", "DAL", {"2026-27": 7_660_317}
    )
    result = separate_dead_money(
        contracts, {"puredx01": [dead]}, _FOSSIL_DISPLAY,
        spotrac_teams={"puredx01": "MIA"},
    )
    assert result.kept == []
    assert result.flags == []
    assert any("pure-dead single row" in why for _, _, why in result.dropped)


def test_spotrac_absence_with_dead_money_is_stretched_pure_dead():
    # RE-PINNED 2026-08-31 (Fix B ruling): absent from Spotrac ACTIVES while
    # present in dead money = pure dead — the row drops (with Fix B's
    # reason, not the fossil's). The prior KEEP pin encoded the pre-ruling
    # Louzada caution, which now applies only when Spotrac data is absent
    # entirely (spotrac_teams=None, asserted below).
    contracts = [
        ContractSeasonAmounts(
            slug="absent01", player_name="Absent Guy", team="DAL",
            amounts={"2026-27": 17_460_317},
        ),
    ]
    dead = DeadMoneyRow(
        "Absent Guy WAIVED", "Absent Guy", "DAL", {"2026-27": 7_660_317}
    )
    result = separate_dead_money(
        contracts, {"absent01": [dead]}, _FOSSIL_DISPLAY, spotrac_teams={}
    )
    assert result.kept == []
    assert any("stretched pure-dead" in why for _, _, why in result.dropped)

    result_none = separate_dead_money(
        contracts, {"absent01": [dead]}, _FOSSIL_DISPLAY, spotrac_teams=None
    )
    assert len(result_none.kept) == 1


def test_fossil_row_printing_seasons_beyond_the_charge_drops_whole():
    # A fossil DAL row printing 2026-27 AND 2027-28 while the charge covers
    # only 2026-27: one overlapping season is enough — the whole pre-waive
    # table is the fossil, not just the charged season.
    contracts = [
        ContractSeasonAmounts(
            slug="widefo01", player_name="Wide Fossil", team="DAL",
            amounts={"2026-27": 17_460_317, "2027-28": 18_000_000},
        ),
        ContractSeasonAmounts(
            slug="widefo01", player_name="Wide Fossil", team="MIA",
            amounts={"2026-27": 5_600_000},
        ),
    ]
    dead = DeadMoneyRow(
        "Wide Fossil WAIVED", "Wide Fossil", "DAL", {"2026-27": 7_660_317}
    )
    result = separate_dead_money(
        contracts, {"widefo01": [dead]}, _FOSSIL_DISPLAY,
        spotrac_teams={"widefo01": "MIA"},
    )
    assert len(result.kept) == 1
    assert result.kept[0].team == "MIA"
    assert all(k.team != "DAL" for k in result.kept)


def test_fossil_drop_is_logged_at_info(caplog):
    import logging

    contracts = [
        ContractSeasonAmounts(
            slug="thompkl01", player_name="Klay Thompson", team="DAL",
            amounts={"2026-27": 17_460_317},
        ),
        ContractSeasonAmounts(
            slug="thompkl01", player_name="Klay Thompson", team="MIA",
            amounts={"2026-27": 5_600_000},
        ),
    ]
    dead = DeadMoneyRow(
        player_raw="Klay Thompson WAIVED", player_name="Klay Thompson",
        team="DAL", amounts={"2026-27": 7_660_317},
    )
    with caplog.at_level(logging.INFO, logger="nba_trade_analyzer.ingest.plans"):
        separate_dead_money(
            contracts, {"thompkl01": [dead]}, _FOSSIL_DISPLAY,
            spotrac_teams={"thompkl01": "MIA"},
        )
    hits = [r for r in caplog.records if "fossil" in r.message.lower()]
    assert hits, "fossil drop must log at INFO"
    joined = " ".join(r.getMessage() for r in hits)
    assert "Klay Thompson" in joined and "DAL" in joined and "17460317" in joined


def test_same_team_resign_is_never_a_fossil():
    # Isaac shape: waived and RE-SIGNED at the same team — Spotrac places
    # him ON the charge team, so the row is not a fossil and the same-team
    # blend decomposition still fires (10,449,421 − 8,000,000 = 2,449,421).
    contracts = [
        ContractSeasonAmounts(
            slug="isaacjo01", player_name="Jonathan Isaac", team="ORL",
            amounts={"2026-27": 10_449_421},
        ),
    ]
    dead = DeadMoneyRow(
        player_raw="Jonathan Isaac WAIVED", player_name="Jonathan Isaac",
        team="ORL", amounts={"2026-27": 8_000_000},
    )
    result = separate_dead_money(
        contracts, {"isaacjo01": [dead]}, _FOSSIL_DISPLAY,
        spotrac_teams={"isaacjo01": "ORL"},
    )
    assert len(result.kept) == 1
    assert result.kept[0].amounts == {"2026-27": 2_449_421}


def test_single_fossil_row_drops_entirely_no_phantom_blend():
    # The 08-25 Klay state: BBRef had ONLY the DAL table (MIA not yet
    # printed), and the same-team blend minted a $9,800,000 "active" phantom
    # (17,460,317 − 7,660,317). A waived team's row is the FULL pre-waive
    # salary, not dead+active — with Spotrac placing him elsewhere the row
    # is a fossil and drops entirely, leaving no salary row.
    contracts = [
        ContractSeasonAmounts(
            slug="thompkl01", player_name="Klay Thompson", team="DAL",
            amounts={"2026-27": 17_460_317},
        ),
    ]
    dead = DeadMoneyRow(
        player_raw="Klay Thompson WAIVED", player_name="Klay Thompson",
        team="DAL", amounts={"2026-27": 7_660_317},
    )
    result = separate_dead_money(
        contracts, {"thompkl01": [dead]}, _FOSSIL_DISPLAY,
        spotrac_teams={"thompkl01": "MIA"},
    )
    assert result.kept == []
    assert any(team == "DAL" for _, team, _ in result.dropped)


# ---------------------------------------------------------------------------
# Per-team RAW amounts (fix/per-team-raw-salary-amounts): fixtures are
# PRODUCTION-SHAPED — identical blended league cells on every stint row
# (today's real cache values) plus the real team-page figures fetched
# 2026-08-31. Raw is authoritative for multi-stint rows; the old blend
# arithmetic becomes a cross-check that flags disagreement, never overrides.
# ---------------------------------------------------------------------------


def _blended(slug, name, team, amounts, raw=None):
    return ContractSeasonAmounts(
        slug=slug, player_name=name, team=team, amounts=dict(amounts),
        raw_amounts=dict(raw) if raw is not None else None,
    )


KLAY_LEAGUE = {"2026-27": 23_060_317, "2027-28": 5_880_000}  # both rows, verbatim cache
KLAY_RAW_DAL = {"2026-27": 17_460_317}  # /contracts/DAL.html 2026-08-31
KLAY_RAW_MIA = {"2026-27": 5_600_000, "2027-28": 5_880_000}  # /contracts/MIA.html


def test_klay_production_shape_raw_replaces_no_subtraction():
    # THE ruled pin: MIA 5,600,000 / 5,880,000, DAL dropped with its own raw
    # amount. Subtraction can NOT produce this (23,060,317 − 7,660,317 =
    # 15,400,000 — the give-back buyout means the dead charge is only a
    # fraction of the pre-waive salary).
    contracts = [
        _blended("thompkl01", "Klay Thompson", "DAL", KLAY_LEAGUE, KLAY_RAW_DAL),
        _blended("thompkl01", "Klay Thompson", "MIA", KLAY_LEAGUE, KLAY_RAW_MIA),
    ]
    dead = DeadMoneyRow(
        "Klay Thompson WAIVED", "Klay Thompson", "DAL", {"2026-27": 7_660_317}
    )
    result = separate_dead_money(
        contracts, {"thompkl01": [dead]}, _FOSSIL_DISPLAY,
        spotrac_teams={"thompkl01": "MIA"},
    )
    assert len(result.kept) == 1
    kept = result.kept[0]
    assert kept.team == "MIA"
    assert kept.amounts == {"2026-27": 5_600_000, "2027-28": 5_880_000}
    assert any(t == "DAL" and "17460317" in why for _, t, why in result.dropped)


def test_lillard_production_shape_raw_wins_and_blend_cross_checks():
    # Identical blended schedules (the split's own founding shape) WITH raw:
    # POR = the POR page's 13,398,800 / 14,104,000, NOTHING in 28-29/29-30
    # (the 1,205,550-class artifacts die). The blend arithmetic agrees here,
    # so no disagreement flag.
    league = {
        "2026-27": 35_915_403, "2027-28": 36_620_603,
        "2028-29": 22_516_603, "2029-30": 22_516_603,
    }
    raw_mil = {s: 22_516_603 for s in league}
    raw_por = {"2026-27": 13_398_800, "2027-28": 14_104_000}
    contracts = [
        _blended("lillada01", "Damian Lillard", "MIL", league, raw_mil),
        _blended("lillada01", "Damian Lillard", "POR", league, raw_por),
    ]
    dead = DeadMoneyRow(
        "Lillard Damian WAIVED", "Lillard Damian", "MIL",
        {s: 22_516_603 for s in league},
    )
    result = separate_dead_money(
        contracts, {"lillada01": [dead]}, DISPLAY_TO_BBREF,
        spotrac_teams={"lillada01": "POR"},
    )
    assert len(result.kept) == 1
    kept = result.kept[0]
    assert kept.team == "POR"
    assert kept.amounts == {"2026-27": 13_398_800, "2027-28": 14_104_000}


def test_kcp_production_shape():
    # League prints the SUM 20,194,392 in both rows; the team pages decompose
    # it as MEM 17,744,971 (the dead-adjusted figure) + PHI 2,449,421.
    league = {"2026-27": 20_194_392}
    contracts = [
        _blended("caldwke01", "Kentavious Caldwell-Pope", "MEM", league,
                 {"2026-27": 17_744_971}),
        _blended("caldwke01", "Kentavious Caldwell-Pope", "PHI", league,
                 {"2026-27": 2_449_421}),
    ]
    dead = DeadMoneyRow(
        "Kentavious Caldwell-Pope WAIVED", "Kentavious Caldwell-Pope",
        "MEM", {"2026-27": 17_744_971},
    )
    result = separate_dead_money(
        contracts, {"caldwke01": [dead]}, _FOSSIL_DISPLAY,
        spotrac_teams={"caldwke01": "PHI"},
    )
    assert len(result.kept) == 1
    assert result.kept[0].team == "PHI"
    assert result.kept[0].amounts == {"2026-27": 2_449_421}


def test_beal_production_shape_matches_lac_page():
    # The LAC page figure (6,424,800 / 6,746,040) EQUALS the blend arithmetic
    # here — raw and cross-check agree; outcome pinned to the page.
    league = {
        "2026-27": 25_807_810, "2027-28": 26_129_050,
        "2028-29": 19_383_010, "2029-30": 19_383_010,
    }
    raw_pho = {s: 19_383_010 for s in league}
    raw_lac = {"2026-27": 6_424_800, "2027-28": 6_746_040}
    contracts = [
        _blended("bealbr01", "Bradley Beal", "PHO", league, raw_pho),
        _blended("bealbr01", "Bradley Beal", "LAC", league, raw_lac),
    ]
    dead = DeadMoneyRow(
        "Bradley Beal", "Bradley Beal", "PHX", {s: 19_383_010 for s in league}
    )
    result = separate_dead_money(
        contracts, {"bealbr01": [dead]}, DISPLAY_TO_BBREF,
        spotrac_teams={"bealbr01": "LAC"},
    )
    kept = result.kept[0]
    assert kept.team == "LAC"
    assert kept.amounts == {"2026-27": 6_424_800, "2027-28": 6_746_040}


def test_raw_absent_falls_back_to_todays_behavior():
    # The invariant-failure path upstream attaches NO raw — separation must
    # behave exactly as today (fossil rule + blend machinery), never worse.
    contracts = [
        _blended("thompkl01", "Klay Thompson", "DAL", KLAY_LEAGUE, None),
        _blended("thompkl01", "Klay Thompson", "MIA", KLAY_LEAGUE, None),
    ]
    dead = DeadMoneyRow(
        "Klay Thompson WAIVED", "Klay Thompson", "DAL", {"2026-27": 7_660_317}
    )
    result = separate_dead_money(
        contracts, {"thompkl01": [dead]}, _FOSSIL_DISPLAY,
        spotrac_teams={"thompkl01": "MIA"},
    )
    # Today's (pre-raw) machinery: identical schedules, split fails (no
    # season equals the charge), fossil pass keeps MIA and the blend
    # subtractor recovers what it can. The point pinned here is the SHAPE:
    # one kept row on MIA, DAL recorded, no crash — not the (known-wrong)
    # subtraction dollars, which raw exists to replace.
    assert len(result.kept) == 1
    assert result.kept[0].team == "MIA"


def test_whitmore_stretched_pure_dead_single_row_drops_to_zero(caplog):
    import logging

    # Fix B: single CLE row 5,458,310 (the cache's real cell); his stretch
    # charge spreads the money, so no season EQUALS the row and the exact
    # pure-dead classifier misses. He is absent from Spotrac's ACTIVE
    # salaries while present in dead money — active = 0, row drops, logged.
    contracts = [
        _blended("whitmca01", "Cam Whitmore", "CLE", {"2026-27": 5_458_310}, None),
    ]
    dead = DeadMoneyRow(
        "Cam Whitmore WAIVED", "Cam Whitmore", "CLE",
        {"2026-27": 1_091_662, "2027-28": 1_091_662, "2028-29": 1_091_662},
    )
    with caplog.at_level(logging.INFO, logger="nba_trade_analyzer.ingest.plans"):
        result = separate_dead_money(
            contracts, {"whitmca01": [dead]}, _FOSSIL_DISPLAY,
            spotrac_teams={"someoneelse01": "LAL"},
        )
    assert result.kept == []
    assert any(t == "CLE" for _, t, why in result.dropped)
    assert any("stretched" in r.message.lower() or "pure dead" in r.message.lower()
               for r in caplog.records)
