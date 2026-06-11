from __future__ import annotations


from nba_trade_analyzer.engine.constants import (
    EXPANDED_TPE_CUSHION,
    FIRST_APRON,
    SALARY_CAP,
    SECOND_APRON,
)
from nba_trade_analyzer.engine.salary_rules import check_trade_legality
from nba_trade_analyzer.models.player import Contract, Player
from nba_trade_analyzer.models.team import CapStatus, RosterEntry, Team
from nba_trade_analyzer.models.trade import Trade, TradeAssets


def _entry(name: str, team: str, salary: int) -> RosterEntry:
    return RosterEntry(
        player=Player(name=name, team=team, age=28),
        contract=Contract(salary=salary, years_remaining=1),
    )


def _team(abbr: str, payroll: int, status: CapStatus) -> Team:
    return Team(
        name=f"{abbr} team",
        abbreviation=abbr,
        total_payroll=payroll,
        cap_status=status,
    )


def _trade(
    *,
    team_a_payroll: int,
    team_a_status: CapStatus,
    team_a_out: list[RosterEntry],
    team_b_payroll: int,
    team_b_status: CapStatus,
    team_b_out: list[RosterEntry],
) -> Trade:
    return Trade(
        team_a=_team("AAA", team_a_payroll, team_a_status),
        team_b=_team("BBB", team_b_payroll, team_b_status),
        team_a_sends=TradeAssets(players=team_a_out),
        team_b_sends=TradeAssets(players=team_b_out),
    )


# ----- Room TPE (under cap) -----------------------------------------------


def test_room_tpe_absorbs_with_cap_space_plus_outgoing_plus_cushion():
    # Cap space = 4.647M, outgoing = 10M, cushion = 250K → limit = 14.897M.
    trade = _trade(
        team_a_payroll=150_000_000,
        team_a_status=CapStatus.UNDER_CAP,
        team_a_out=[_entry("Out A", "AAA", 10_000_000)],
        team_b_payroll=170_000_000,
        team_b_status=CapStatus.OVER_CAP,
        team_b_out=[_entry("In A", "BBB", 14_897_000)],
    )
    result = check_trade_legality(trade)
    assert result.legal, result.error_reason


def test_room_tpe_rejects_when_incoming_exceeds_cap_space_plus_outgoing_plus_cushion():
    trade = _trade(
        team_a_payroll=150_000_000,
        team_a_status=CapStatus.UNDER_CAP,
        team_a_out=[_entry("Out A", "AAA", 10_000_000)],
        team_b_payroll=170_000_000,
        team_b_status=CapStatus.OVER_CAP,
        team_b_out=[_entry("In A", "BBB", 14_898_000)],
    )
    result = check_trade_legality(trade)
    assert not result.legal
    assert result.error_reason is not None
    assert "Room TPE" in result.error_reason


# ----- Expanded TPE: tier 1 (outgoing < $7.25M) ---------------------------


def test_expanded_tpe_tier1_allows_200pct_plus_cushion():
    # Outgoing 5M → limit = 10M + 250K.
    trade = _trade(
        team_a_payroll=170_000_000,
        team_a_status=CapStatus.OVER_CAP,
        team_a_out=[_entry("Out A", "AAA", 5_000_000)],
        team_b_payroll=170_000_000,
        team_b_status=CapStatus.OVER_CAP,
        team_b_out=[_entry("In A", "BBB", 10_250_000)],
    )
    assert check_trade_legality(trade).legal


def test_expanded_tpe_tier1_rejects_above_limit():
    trade = _trade(
        team_a_payroll=170_000_000,
        team_a_status=CapStatus.OVER_CAP,
        team_a_out=[_entry("Out A", "AAA", 5_000_000)],
        team_b_payroll=170_000_000,
        team_b_status=CapStatus.OVER_CAP,
        team_b_out=[_entry("In A", "BBB", 10_251_000)],
    )
    result = check_trade_legality(trade)
    assert not result.legal
    assert "Expanded TPE" in (result.error_reason or "")


def test_expanded_tpe_tier1_uses_250k_cushion_when_doubled_match_short():
    # Outgoing 5M, incoming exactly 200% (no cushion needed) → legal.
    trade = _trade(
        team_a_payroll=170_000_000,
        team_a_status=CapStatus.OVER_CAP,
        team_a_out=[_entry("Out A", "AAA", 5_000_000)],
        team_b_payroll=170_000_000,
        team_b_status=CapStatus.OVER_CAP,
        team_b_out=[_entry("In A", "BBB", 10_000_000)],
    )
    assert check_trade_legality(trade).legal


# ----- Expanded TPE: tier 2 ($7.25M ≤ outgoing ≤ $29M) ---------------------


def test_expanded_tpe_tier2_at_lower_boundary_uses_cushion():
    # Outgoing 7.25M → limit = 7.25M + 8.527M = 15.777M.
    out = 7_250_000  # tier-2 lower boundary
    incoming = out + EXPANDED_TPE_CUSHION
    trade = _trade(
        team_a_payroll=170_000_000,
        team_a_status=CapStatus.OVER_CAP,
        team_a_out=[_entry("Out A", "AAA", out)],
        team_b_payroll=170_000_000,
        team_b_status=CapStatus.OVER_CAP,
        team_b_out=[_entry("In A", "BBB", incoming)],
    )
    assert check_trade_legality(trade).legal


def test_expanded_tpe_tier2_rejects_above_cushion_limit():
    out = 7_250_000  # tier-2 lower boundary
    incoming = out + EXPANDED_TPE_CUSHION + 1
    trade = _trade(
        team_a_payroll=170_000_000,
        team_a_status=CapStatus.OVER_CAP,
        team_a_out=[_entry("Out A", "AAA", out)],
        team_b_payroll=170_000_000,
        team_b_status=CapStatus.OVER_CAP,
        team_b_out=[_entry("In A", "BBB", incoming)],
    )
    result = check_trade_legality(trade)
    assert not result.legal
    assert "Expanded TPE" in (result.error_reason or "")


def test_expanded_tpe_tier2_at_upper_boundary_uses_cushion_not_125pct():
    # Outgoing 29M is still tier 2 (≤ 29M). Limit = 29M + 8.527M = 37.527M,
    # NOT 125% × 29M = 36.25M.
    out = 29_000_000
    incoming = out + EXPANDED_TPE_CUSHION
    trade = _trade(
        team_a_payroll=170_000_000,
        team_a_status=CapStatus.OVER_CAP,
        team_a_out=[_entry("Out A", "AAA", out)],
        team_b_payroll=170_000_000,
        team_b_status=CapStatus.OVER_CAP,
        team_b_out=[_entry("In A", "BBB", incoming)],
    )
    assert check_trade_legality(trade).legal


# ----- Expanded TPE: tier 3 (outgoing > $29M, 125%, NO $250K) -------------


def test_expanded_tpe_tier3_allows_125pct_exact():
    out = 30_000_000
    incoming = (5 * out) // 4  # 37.5M
    trade = _trade(
        team_a_payroll=170_000_000,
        team_a_status=CapStatus.OVER_CAP,
        team_a_out=[_entry("Out A", "AAA", out)],
        team_b_payroll=170_000_000,
        team_b_status=CapStatus.OVER_CAP,
        team_b_out=[_entry("In A", "BBB", incoming)],
    )
    assert check_trade_legality(trade).legal


def test_expanded_tpe_tier3_rejects_when_250k_cushion_is_assumed():
    # Tier 3 has NO $250K cushion. 125% × 30M = 37.5M. 37.5M + 1 must fail.
    out = 30_000_000
    incoming = (5 * out) // 4 + 1
    trade = _trade(
        team_a_payroll=170_000_000,
        team_a_status=CapStatus.OVER_CAP,
        team_a_out=[_entry("Out A", "AAA", out)],
        team_b_payroll=170_000_000,
        team_b_status=CapStatus.OVER_CAP,
        team_b_out=[_entry("In A", "BBB", incoming)],
    )
    result = check_trade_legality(trade)
    assert not result.legal
    assert "Expanded TPE" in (result.error_reason or "")
    assert "no $250K" in (result.error_reason or "")


# ----- First apron: 100% match, no $250K ----------------------------------


def test_first_apron_allows_exact_match():
    payroll = FIRST_APRON + 5_000_000  # safely between first and second apron
    trade = _trade(
        team_a_payroll=payroll,
        team_a_status=CapStatus.FIRST_APRON,
        team_a_out=[_entry("Out A", "AAA", 20_000_000)],
        team_b_payroll=170_000_000,
        team_b_status=CapStatus.OVER_CAP,
        team_b_out=[_entry("In A", "BBB", 20_000_000)],
    )
    assert check_trade_legality(trade).legal


def test_first_apron_rejects_250k_cushion():
    payroll = FIRST_APRON + 5_000_000
    trade = _trade(
        team_a_payroll=payroll,
        team_a_status=CapStatus.FIRST_APRON,
        team_a_out=[_entry("Out A", "AAA", 20_000_000)],
        team_b_payroll=170_000_000,
        team_b_status=CapStatus.OVER_CAP,
        team_b_out=[_entry("In A", "BBB", 20_000_001)],
    )
    result = check_trade_legality(trade)
    assert not result.legal
    assert "first apron" in (result.error_reason or "")


def test_first_apron_allows_aggregation_when_incoming_does_not_exceed():
    payroll = FIRST_APRON + 5_000_000
    trade = _trade(
        team_a_payroll=payroll,
        team_a_status=CapStatus.FIRST_APRON,
        team_a_out=[
            _entry("Out 1", "AAA", 10_000_000),
            _entry("Out 2", "AAA", 12_000_000),
        ],
        team_b_payroll=170_000_000,
        team_b_status=CapStatus.OVER_CAP,
        team_b_out=[_entry("In A", "BBB", 22_000_000)],
    )
    assert check_trade_legality(trade).legal


# ----- Second apron: 100% match, aggregation blocked ----------------------


def test_second_apron_allows_exact_match_single_outgoing():
    # Pre and post are both over the second apron (incoming == outgoing).
    payroll = SECOND_APRON + 10_000_000
    trade = _trade(
        team_a_payroll=payroll,
        team_a_status=CapStatus.SECOND_APRON,
        team_a_out=[_entry("Out A", "AAA", 25_000_000)],
        team_b_payroll=170_000_000,
        team_b_status=CapStatus.OVER_CAP,
        team_b_out=[_entry("In A", "BBB", 25_000_000)],
    )
    assert check_trade_legality(trade).legal


def test_second_apron_rejects_250k_cushion():
    payroll = SECOND_APRON + 10_000_000
    trade = _trade(
        team_a_payroll=payroll,
        team_a_status=CapStatus.SECOND_APRON,
        team_a_out=[_entry("Out A", "AAA", 25_000_000)],
        team_b_payroll=170_000_000,
        team_b_status=CapStatus.OVER_CAP,
        team_b_out=[_entry("In A", "BBB", 25_000_001)],
    )
    result = check_trade_legality(trade)
    assert not result.legal
    assert "second apron" in (result.error_reason or "")


def test_second_apron_blocks_aggregation_even_for_smaller_incoming():
    # Two outgoing salaries → aggregation. Even if incoming is LESS than the
    # sum of outgoing, the second-apron rule blocks the combine.
    payroll = SECOND_APRON + 10_000_000
    trade = _trade(
        team_a_payroll=payroll,
        team_a_status=CapStatus.SECOND_APRON,
        team_a_out=[
            _entry("Out 1", "AAA", 15_000_000),
            _entry("Out 2", "AAA", 12_000_000),
        ],
        team_b_payroll=170_000_000,
        team_b_status=CapStatus.OVER_CAP,
        team_b_out=[_entry("In A", "BBB", 20_000_000)],  # less than 27M outgoing
    )
    result = check_trade_legality(trade)
    assert not result.legal
    assert "aggregation blocked" in (result.error_reason or "")


def test_second_apron_aggregation_allowed_when_trade_drops_team_below_apron():
    # Pre-trade just over the second apron. Outgoing two players totaling a
    # large amount, incoming smaller — post-trade lands below the apron, so
    # Aggregated TPE re-enables aggregation.
    payroll = SECOND_APRON + 1_000_000  # 208.824M
    trade = _trade(
        team_a_payroll=payroll,
        team_a_status=CapStatus.SECOND_APRON,
        team_a_out=[
            _entry("Out 1", "AAA", 20_000_000),
            _entry("Out 2", "AAA", 10_000_000),
        ],
        team_b_payroll=170_000_000,
        team_b_status=CapStatus.OVER_CAP,
        team_b_out=[_entry("In A", "BBB", 25_000_000)],
    )
    # Post-trade payroll for A: 208.824M - 30M + 25M = 203.824M < SECOND_APRON.
    result = check_trade_legality(trade)
    assert result.legal, result.error_reason


def test_second_apron_aggregation_allowed_when_trade_lands_exactly_at_apron():
    # LEGAL — A aggregates two outgoing salaries, sheds $10M, and lands
    # POST-trade at exactly $207,824,000 (== SECOND_APRON). cbaguide
    # (/transactions/trades/tpe/): "even if a Second Apron Team wants to aggregate
    # players ... they can't do so if they still are over the Second Apron after
    # the Trade" and "it only matters where the Team lands after the trade."
    # "Over" is strict, so landing exactly ON the apron is not over → aggregation
    # is re-enabled → legal. The current `<` at salary_rules.py:163 treats
    # at-exactly as still over and blocks it, so this XFAILs against current code.
    payroll = SECOND_APRON + 10_000_000  # 217.824M, over the second apron pre-trade
    trade = _trade(
        team_a_payroll=payroll,
        team_a_status=CapStatus.SECOND_APRON,
        team_a_out=[
            _entry("Out 1", "AAA", 15_000_000),
            _entry("Out 2", "AAA", 12_000_000),  # aggregating: 2 outgoing = 27M
        ],
        team_b_payroll=140_000_000,
        team_b_status=CapStatus.UNDER_CAP,
        team_b_out=[_entry("In A", "BBB", 17_000_000)],  # sheds 10M, <=100% match
    )
    # Post-trade A lands exactly on the line:
    assert 217_824_000 - 27_000_000 + 17_000_000 == SECOND_APRON
    result = check_trade_legality(trade)
    assert result.legal, result.error_reason


def test_second_apron_drops_below_but_still_fails_100pct_match():
    # The exception re-enables aggregation but still requires 100% matching.
    payroll = SECOND_APRON + 1_000_000
    trade = _trade(
        team_a_payroll=payroll,
        team_a_status=CapStatus.SECOND_APRON,
        team_a_out=[
            _entry("Out 1", "AAA", 10_000_000),
            _entry("Out 2", "AAA", 8_000_000),
        ],
        team_b_payroll=170_000_000,
        team_b_status=CapStatus.OVER_CAP,
        team_b_out=[
            # Post-trade: 208.824 - 18 + 18.5 = 209.324 → still over apron,
            # so aggregation is blocked outright (not the matching path).
            _entry("In A", "BBB", 18_500_000),
        ],
    )
    result = check_trade_legality(trade)
    assert not result.legal
    assert "aggregation blocked" in (result.error_reason or "")


# ----- Both teams evaluated; short-circuit on first failure ----------------


def test_short_circuits_when_first_team_fails():
    # team_a fails first-apron 100% rule; team_b is fine.
    payroll = FIRST_APRON + 5_000_000
    trade = _trade(
        team_a_payroll=payroll,
        team_a_status=CapStatus.FIRST_APRON,
        team_a_out=[_entry("Out A", "AAA", 10_000_000)],
        team_b_payroll=140_000_000,
        team_b_status=CapStatus.UNDER_CAP,
        team_b_out=[_entry("In A", "BBB", 15_000_000)],
    )
    result = check_trade_legality(trade)
    assert not result.legal
    assert (result.error_reason or "").startswith("AAA:")


def test_evaluates_both_teams_when_team_a_passes_team_b_fails():
    # team_a (under cap) absorbs fine; team_b (second apron) fails aggregation.
    trade = _trade(
        team_a_payroll=140_000_000,
        team_a_status=CapStatus.UNDER_CAP,
        team_a_out=[
            _entry("Out 1", "AAA", 5_000_000),
            _entry("Out 2", "AAA", 5_000_000),
        ],
        team_b_payroll=SECOND_APRON + 10_000_000,
        team_b_status=CapStatus.SECOND_APRON,
        team_b_out=[
            _entry("In 1", "BBB", 6_000_000),
            _entry("In 2", "BBB", 4_500_000),
        ],
    )
    result = check_trade_legality(trade)
    assert not result.legal
    assert (result.error_reason or "").startswith("BBB:")


# ----- Expanded-TPE eligibility judged on post-trade landing (Gate B) -------
# Ratified rule (cbaguide /thresholds/apron/): a team is prohibited only if its
# Apron Team Salary "exceeds the applicable Apron Threshold after executing the
# transaction". "Exceeds" is strict, so a >100% (expanded) takeback is legal
# when POST-trade salary stays AT OR BELOW the first apron ($195,945,000) and
# illegal only when it lands strictly over. The engine historically keyed the
# tier on PRE-trade payroll alone (_evaluate_team) and never re-checked the
# landing, so a team under the apron pre-trade could take back >100% and balloon
# over the apron and the engine wrongly allowed it. The remaining xfail below
# pins the one genuine bug case (lands strictly over) until the Gate B fix lands.


def test_expanded_tpe_over_100pct_landing_over_first_apron_is_illegal():
    # ILLEGAL — an expanded >100% takeback is legal only if post-trade stays at
    # or below the first apron; here A lands at $202,527,000, strictly over it.
    # Now enforced by the Gate B check in _check_expanded_tpe.
    incoming = (
        10_000_000 + EXPANDED_TPE_CUSHION
    )  # 18,527,000 (max the buggy engine allows)
    trade = _trade(
        team_a_payroll=194_000_000,  # under the $195.945M first apron pre-trade
        team_a_status=CapStatus.OVER_CAP,
        team_a_out=[_entry("Out A", "AAA", 10_000_000)],
        team_b_payroll=170_000_000,
        team_b_status=CapStatus.OVER_CAP,
        team_b_out=[_entry("In A", "BBB", incoming)],
    )
    # Post-trade A: 194M - 10M + 18.527M = 202.527M > FIRST_APRON.
    result = check_trade_legality(trade)
    assert not result.legal
    assert result.error_reason is not None


def test_match_within_100pct_over_first_apron_stays_legal():
    # Expected LEGAL — matching <=100% is fine even over the first apron; proves
    # the rule targets the >100% portion, not merely crossing the apron. Starts
    # over the apron because a <=100% match from $194M can never land above it.
    payroll = FIRST_APRON + 5_000_000  # 200.945M, over the first apron pre-trade
    trade = _trade(
        team_a_payroll=payroll,
        team_a_status=CapStatus.FIRST_APRON,
        team_a_out=[_entry("Out A", "AAA", 10_000_000)],
        team_b_payroll=170_000_000,
        team_b_status=CapStatus.OVER_CAP,
        team_b_out=[_entry("In A", "BBB", 10_000_000)],  # exact 100% match
    )
    # Post-trade A: 200.945M - 10M + 10M = 200.945M, still over apron, <=100%.
    result = check_trade_legality(trade)
    assert result.legal, result.error_reason


def test_expanded_tpe_over_100pct_landing_just_under_first_apron_is_legal():
    # Expected LEGAL — >100% matching is fine as long as post-trade stays below
    # $195,945,000; this lands at $195,944,999 (one dollar under). Passes now.
    incoming = 11_944_999  # post-trade A: 194M - 10M + 11,944,999 = 195,944,999
    trade = _trade(
        team_a_payroll=194_000_000,
        team_a_status=CapStatus.OVER_CAP,
        team_a_out=[_entry("Out A", "AAA", 10_000_000)],
        team_b_payroll=170_000_000,
        team_b_status=CapStatus.OVER_CAP,
        team_b_out=[_entry("In A", "BBB", incoming)],
    )
    assert FIRST_APRON == 195_945_000  # boundary this case sits one dollar below
    result = check_trade_legality(trade)
    assert result.legal, result.error_reason


def test_expanded_tpe_over_100pct_landing_at_first_apron_is_legal():
    # Expected LEGAL — post-trade lands exactly on $195,945,000. cbaguide
    # (/thresholds/apron/): a team is prohibited only if its Apron Team Salary
    # "exceeds the applicable Apron Threshold after executing the transaction" —
    # "exceeds" is strict, so landing AT the apron (<= FIRST_APRON) is legal;
    # only going over is not. A >100% takeback that lands exactly on the line
    # is allowed.
    incoming = 11_945_000  # post-trade A: 194M - 10M + 11,945,000 = 195,945,000
    trade = _trade(
        team_a_payroll=194_000_000,
        team_a_status=CapStatus.OVER_CAP,
        team_a_out=[_entry("Out A", "AAA", 10_000_000)],
        team_b_payroll=170_000_000,
        team_b_status=CapStatus.OVER_CAP,
        team_b_out=[_entry("In A", "BBB", incoming)],
    )
    assert (
        194_000_000 - 10_000_000 + incoming == FIRST_APRON
    )  # lands exactly at the apron
    result = check_trade_legality(trade)
    assert result.legal, result.error_reason


# ----- RATIFIED EQUIVALENCE PINS (cbaguide.com, authority of record 2026-06-11) -
# These pin the proof that the engine's pre-trade-dispatch + flat-100% over-apron
# matching is equivalent to cbaguide's post-transaction, landing-keyed, cushion-
# eliminated rule (Q1 + Q2). A refactor that "helpfully" re-adds a $250k cushion to
# the over-apron paths, or flips Gate B's strict ">", will break one of these.


def test_equiv_a_over_first_apron_takeback_above_100pct_is_illegal():
    # pre = FIRST_APRON + 1 (over the apron) → _check_first_apron; incoming =
    # outgoing + 1 (>100%) → illegal. Per Q1 no cushion survives over the apron.
    trade = _trade(
        team_a_payroll=FIRST_APRON + 1,
        team_a_status=CapStatus.FIRST_APRON,
        team_a_out=[_entry("Out A", "AAA", 20_000_000)],
        team_b_payroll=170_000_000,
        team_b_status=CapStatus.OVER_CAP,
        team_b_out=[_entry("In A", "BBB", 20_000_001)],
    )
    result = check_trade_legality(trade)
    assert not result.legal
    assert "first apron" in (result.error_reason or "")


def test_equiv_b_over_first_apron_exact_100pct_match_is_legal():
    # Same over-apron team, incoming == outgoing (exactly 100%, no cushion) → legal.
    trade = _trade(
        team_a_payroll=FIRST_APRON + 1,
        team_a_status=CapStatus.FIRST_APRON,
        team_a_out=[_entry("Out A", "AAA", 20_000_000)],
        team_b_payroll=170_000_000,
        team_b_status=CapStatus.OVER_CAP,
        team_b_out=[_entry("In A", "BBB", 20_000_000)],
    )
    result = check_trade_legality(trade)
    assert result.legal, result.error_reason


def test_equiv_c_expanded_landing_exactly_at_first_apron_is_legal():
    # Expanded-TPE team (pre < FIRST_APRON) taking back >100% and landing EXACTLY
    # on the apron. Q2 "exceeds … after executing" is strict, so at-exactly is
    # legal — Gate B fires only on post_trade > FIRST_APRON.
    incoming = 11_945_000  # 194M - 10M + 11.945M = 195,945,000 == FIRST_APRON
    trade = _trade(
        team_a_payroll=194_000_000,
        team_a_status=CapStatus.OVER_CAP,
        team_a_out=[_entry("Out A", "AAA", 10_000_000)],
        team_b_payroll=170_000_000,
        team_b_status=CapStatus.OVER_CAP,
        team_b_out=[_entry("In A", "BBB", incoming)],
    )
    assert 194_000_000 - 10_000_000 + incoming == FIRST_APRON
    result = check_trade_legality(trade)
    assert result.legal, result.error_reason


def test_equiv_d_expanded_landing_one_over_first_apron_is_illegal():
    # Same team, one dollar higher → lands FIRST_APRON + 1 with incoming > outgoing
    # → Gate B rejects (strict ">").
    incoming = 11_945_001  # 194M - 10M + 11,945,001 = 195,945,001
    trade = _trade(
        team_a_payroll=194_000_000,
        team_a_status=CapStatus.OVER_CAP,
        team_a_out=[_entry("Out A", "AAA", 10_000_000)],
        team_b_payroll=170_000_000,
        team_b_status=CapStatus.OVER_CAP,
        team_b_out=[_entry("In A", "BBB", incoming)],
    )
    assert 194_000_000 - 10_000_000 + incoming == FIRST_APRON + 1
    result = check_trade_legality(trade)
    assert not result.legal
    assert "first apron" in (result.error_reason or "")


def test_equiv_e_band_boundaries_7_25m_and_29m_are_middle_band():
    # Q4 semantics: "Less than $7.25MM" vs "$7.25MM – $29MM" vs "More than $29MM".
    # outgoing == 7.25M is the MIDDLE band (limit = outgoing + 8.527M = 15.777M),
    # NOT tier-1 (200%+250k = 14.75M). Take back 15.777M (== middle limit) → legal;
    # one dollar more → illegal.
    low = check_trade_legality(
        _trade(
            team_a_payroll=170_000_000,
            team_a_status=CapStatus.OVER_CAP,
            team_a_out=[_entry("Out A", "AAA", 7_250_000)],
            team_b_payroll=170_000_000,
            team_b_status=CapStatus.OVER_CAP,
            team_b_out=[_entry("In A", "BBB", 7_250_000 + EXPANDED_TPE_CUSHION)],
        )
    )
    assert low.legal, low.error_reason  # 15,777,000 allowed only on the middle band
    low_over = check_trade_legality(
        _trade(
            team_a_payroll=170_000_000,
            team_a_status=CapStatus.OVER_CAP,
            team_a_out=[_entry("Out A", "AAA", 7_250_000)],
            team_b_payroll=170_000_000,
            team_b_status=CapStatus.OVER_CAP,
            team_b_out=[_entry("In A", "BBB", 7_250_000 + EXPANDED_TPE_CUSHION + 1)],
        )
    )
    assert not low_over.legal

    # outgoing == 29M is STILL the middle band (limit = 29M + 8.527M = 37.527M),
    # NOT tier-3 (125% = 36.25M). 37M exceeds 125% but is under the cushion limit,
    # so it is legal only if the engine is on the middle band at exactly 29M.
    high = check_trade_legality(
        _trade(
            team_a_payroll=170_000_000,
            team_a_status=CapStatus.OVER_CAP,
            team_a_out=[_entry("Out A", "AAA", 29_000_000)],
            team_b_payroll=170_000_000,
            team_b_status=CapStatus.OVER_CAP,
            team_b_out=[_entry("In A", "BBB", 37_000_000)],
        )
    )
    assert high.legal, high.error_reason  # > 125%(=36.25M) but under cushion limit


# ----- Sanity check on the constants used in tests ------------------------


def test_cap_constants_match_expected_values():
    # Guards against unintentionally editing constants and silently changing
    # what every other test in this module means.
    assert SALARY_CAP == 154_647_000
    assert FIRST_APRON == 195_945_000
    assert SECOND_APRON == 207_824_000
    assert EXPANDED_TPE_CUSHION == 8_527_000
