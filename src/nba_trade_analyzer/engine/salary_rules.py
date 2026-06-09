"""CBA trade-legality engine.

Implements salary-matching and aggregation rules per the 2023 CBA.

Tier selection is keyed on **pre-trade** payroll: an under-cap team uses the
Room TPE, a team between the cap and the first apron uses the Expanded TPE
(graded by outgoing size), and so on. Post-trade payroll matters for the
second-apron aggregation block — including the carve-out where the trade
itself drops the team below the second apron, which re-enables aggregation
via the Aggregated TPE.
"""

from __future__ import annotations

from nba_trade_analyzer.engine.constants import (
    EXPANDED_TPE_CUSHION,
    FIRST_APRON,
    SALARY_CAP,
    SECOND_APRON,
)
from nba_trade_analyzer.models.trade import Trade, TradeAssets, TradeResult

TRADE_MATCH_CUSHION = 250_000
EXPANDED_TPE_LOW = 7_250_000
EXPANDED_TPE_HIGH = 29_000_000


def check_trade_legality(trade: Trade) -> TradeResult:
    """Evaluate trade legality for both teams; short-circuit on first failure."""
    error = _evaluate_team(
        team_name=trade.team_a.abbreviation,
        pre_trade_payroll=trade.team_a.total_payroll,
        outgoing=trade.team_a_sends,
        incoming=trade.team_b_sends,
    )
    if error is None:
        error = _evaluate_team(
            team_name=trade.team_b.abbreviation,
            pre_trade_payroll=trade.team_b.total_payroll,
            outgoing=trade.team_b_sends,
            incoming=trade.team_a_sends,
        )

    return TradeResult(
        legal=error is None,
        error_reason=error,
        grade_a="N/A",
        grade_b="N/A",
        surplus_delta_a=0.0,
        surplus_delta_b=0.0,
    )


def _evaluate_team(
    team_name: str,
    pre_trade_payroll: int,
    outgoing: TradeAssets,
    incoming: TradeAssets,
) -> str | None:
    """Return None if legal, else a descriptive failure string."""
    outgoing_salary = outgoing.total_salary
    incoming_salary = incoming.total_salary
    post_trade = pre_trade_payroll - outgoing_salary + incoming_salary
    num_outgoing = len(outgoing.players)
    aggregating = num_outgoing > 1

    if pre_trade_payroll < SALARY_CAP:
        return _check_room_tpe(
            team_name, pre_trade_payroll, outgoing_salary, incoming_salary
        )

    if pre_trade_payroll < FIRST_APRON:
        return _check_expanded_tpe(
            team_name, outgoing_salary, incoming_salary, post_trade
        )

    if pre_trade_payroll < SECOND_APRON:
        return _check_first_apron(team_name, outgoing_salary, incoming_salary)

    return _check_second_apron(
        team_name,
        outgoing_salary,
        incoming_salary,
        post_trade,
        aggregating,
        num_outgoing,
    )


def _check_room_tpe(
    team_name: str, pre_trade_payroll: int, outgoing: int, incoming: int
) -> str | None:
    cap_space = SALARY_CAP - pre_trade_payroll
    limit = cap_space + outgoing + TRADE_MATCH_CUSHION
    if incoming > limit:
        return (
            f"{team_name}: Room TPE absorption failed — incoming ${incoming:,} "
            f"exceeds cap space ${cap_space:,} + outgoing ${outgoing:,} + $250K "
            f"(limit ${limit:,})."
        )
    return None


def _check_expanded_tpe(
    team_name: str, outgoing: int, incoming: int, post_trade: int
) -> str | None:
    # Gate B — apron eligibility is judged on where the team lands AFTER the
    # trade, mirroring _check_second_apron's post-trade test. cbaguide
    # (/thresholds/apron/): a team is prohibited only if its Apron Team Salary
    # "exceeds the applicable Apron Threshold after executing the transaction".
    # "Exceeds" is strict, so post_trade <= FIRST_APRON is legal and only
    # strictly over is not. The Expanded TPE (a >100% takeback) is therefore
    # unavailable when taking back more than 100% would land the team over the
    # first apron. (Tier selection itself — Gate A — stays keyed on pre-trade
    # payroll in _evaluate_team: which exception a team may reach for.)
    if incoming > outgoing and post_trade > FIRST_APRON:
        return (
            f"{team_name}: Expanded TPE unavailable — taking back more than 100% "
            f"(incoming ${incoming:,} > outgoing ${outgoing:,}) lands the team at "
            f"${post_trade:,}, over the first apron (${FIRST_APRON:,})."
        )

    if outgoing < EXPANDED_TPE_LOW:
        limit = 2 * outgoing + TRADE_MATCH_CUSHION
        tier_desc = f"outgoing < ${EXPANDED_TPE_LOW:,} → 200% × outgoing + $250K"
    elif outgoing <= EXPANDED_TPE_HIGH:
        limit = outgoing + EXPANDED_TPE_CUSHION
        tier_desc = (
            f"outgoing in [${EXPANDED_TPE_LOW:,}, ${EXPANDED_TPE_HIGH:,}] → "
            f"outgoing + ${EXPANDED_TPE_CUSHION:,}"
        )
    else:
        # 125% with no $250K cushion in this band.
        limit = (5 * outgoing) // 4
        tier_desc = f"outgoing > ${EXPANDED_TPE_HIGH:,} → 125% × outgoing (no $250K)"

    if incoming > limit:
        return (
            f"{team_name}: Expanded TPE matching failed ({tier_desc}); "
            f"incoming ${incoming:,} > limit ${limit:,}."
        )
    return None


def _check_first_apron(team_name: str, outgoing: int, incoming: int) -> str | None:
    if incoming > outgoing:
        return (
            f"{team_name}: over first apron requires 100% matching "
            f"(no $250K cushion); incoming ${incoming:,} > outgoing ${outgoing:,}."
        )
    return None


def _check_second_apron(
    team_name: str,
    outgoing: int,
    incoming: int,
    post_trade: int,
    aggregating: bool,
    num_outgoing: int,
) -> str | None:
    # Exception: trade drops team to at-or-below the second apron → Aggregated
    # TPE applies. cbaguide: a team is barred only if it stays "over the Second
    # Apron after the Trade" (strict), so landing exactly on the apron is legal.
    if post_trade <= SECOND_APRON:
        if incoming > outgoing:
            return (
                f"{team_name}: trade drops below second apron but matching fails — "
                f"incoming ${incoming:,} > outgoing ${outgoing:,}."
            )
        return None

    if aggregating:
        return (
            f"{team_name}: salary aggregation blocked — team stays over the second "
            f"apron post-trade (${post_trade:,}) while combining {num_outgoing} "
            f"outgoing salaries."
        )
    if incoming > outgoing:
        return (
            f"{team_name}: over second apron requires 100% matching "
            f"(no $250K cushion); incoming ${incoming:,} > outgoing ${outgoing:,}."
        )
    return None
