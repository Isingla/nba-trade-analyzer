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


# ----- Sanity check on the constants used in tests ------------------------


def test_cap_constants_match_expected_values():
    # Guards against unintentionally editing constants and silently changing
    # what every other test in this module means.
    assert SALARY_CAP == 154_647_000
    assert FIRST_APRON == 195_945_000
    assert SECOND_APRON == 207_824_000
    assert EXPANDED_TPE_CUSHION == 8_527_000
