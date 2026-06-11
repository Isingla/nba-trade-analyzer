"""CBA coverage + boundary audit (extends test_cba_constant_audit.py).

AUDIT ARTIFACT. These tests record three things and nothing else:

  1. Coverage gaps — reference entries (verified or not) for which the engine
     exposes NO constant at all. A failing/recording test here is a FINDING, not
     a number to silence. Do not "fix" by editing the engine or reference.

  2. The one remaining verified:true cap level the existing constant audit does
     NOT cover: ``minimum_team_salary`` (the salary floor). It is verified:true
     in the reference, so it MAY certify an engine constant — but the engine has
     none, so this records the absence.

  3. Boundary pins for the trade-matching brackets. Every bracket number is
     verified:false in the reference, so these pins assert the ENGINE'S CURRENT
     BEHAVIOR (not correctness) so that any silent drift in a cutoff surfaces in
     review. Correctness of these numbers is a "needs ratification" item against
     cbaguide.com — see the audit findings, not these asserts, for that.
"""

from __future__ import annotations

import re
from pathlib import Path

from nba_trade_analyzer.engine import constants
from nba_trade_analyzer.engine import salary_rules
from nba_trade_analyzer.engine.salary_rules import check_trade_legality
from nba_trade_analyzer.models.player import Contract, Player
from nba_trade_analyzer.models.team import CapStatus, RosterEntry, Team
from nba_trade_analyzer.models.trade import Trade, TradeAssets

REFERENCE_PATH = (
    Path(__file__).resolve().parents[1] / "reference" / "cba_reference_2025-26.yaml"
)
_YAML_TEXT = REFERENCE_PATH.read_text()


def _inline_entry(key: str) -> tuple[int, bool]:
    pattern = rf"^\s*{re.escape(key)}:\s*\{{(?P<body>[^}}]*)\}}"
    m = re.search(pattern, _YAML_TEXT, re.MULTILINE)
    assert m is not None, f"{key!r} not found as inline mapping"
    body = m.group("body")
    vm = re.search(r"value:\s*(\d+)", body)
    verm = re.search(r"verified:\s*(true|false)", body)
    assert vm and verm, f"missing value/verified in {key!r}"
    return int(vm.group(1)), verm.group(1) == "true"


def _block_value_verified(key: str) -> tuple[int, bool]:
    pattern = rf"^(?P<indent>\s*){re.escape(key)}:\s*$\n(?P<block>(?:^\s+.*\n?)+)"
    m = re.search(pattern, _YAML_TEXT, re.MULTILINE)
    assert m is not None, f"{key!r} not found as block mapping"
    block = m.group("block")
    vm = re.search(r"^\s*value:\s*(\d+)", block, re.MULTILINE)
    verm = re.search(r"^\s*verified:\s*(true|false)", block, re.MULTILINE)
    assert vm and verm, f"missing value/verified in {key!r}"
    return int(vm.group(1)), verm.group(1) == "true"


def _engine_has(*names: str) -> bool:
    return any(hasattr(constants, n) or hasattr(salary_rules, n) for n in names)


# --- Gap 1: minimum_team_salary is verified:true but uncovered by the engine ---
# The reference salary floor IS primary-sourced (verified:true) and COULD certify
# a constant — but the engine has none, and the floor rule (a team must spend to
# the floor) is not modeled. This records that gap.


def test_minimum_team_salary_is_verified_but_engine_has_no_constant():
    value, verified = _inline_entry("minimum_team_salary")
    assert verified, "reference minimum_team_salary must be verified:true"
    assert value == 139_182_000  # pin the recorded reference value
    assert not _engine_has(
        "MINIMUM_TEAM_SALARY", "MIN_TEAM_SALARY", "SALARY_FLOOR", "TEAM_SALARY_FLOOR"
    ), "engine now exposes a minimum-team-salary constant — update this audit"


# --- Gap 2: LUXURY_TAX constant exists but the rules engine never uses it -------
# constants.LUXURY_TAX is defined (and certified by the constant audit) but the
# salary-rules engine imports only SALARY_CAP/FIRST_APRON/SECOND_APRON/cushion.
# Records that the luxury-tax line is not a dispatch threshold in the engine.


def test_luxury_tax_constant_is_not_used_by_salary_rules_engine():
    src = Path(salary_rules.__file__).read_text()
    assert "LUXURY_TAX" not in src, (
        "salary_rules.py now references LUXURY_TAX — update this audit"
    )


# --- Gap 3: BAE / maximums / minimums are absent (all verified:false) ----------
# None of these may CERTIFY the engine (verified:false), but recording that the
# engine models none of them is a coverage finding regardless.


def test_bi_annual_exception_absent_and_unverified():
    value, verified = _block_value_verified("bi_annual_exception")
    assert value == 5_135_000
    assert verified is False  # cannot certify; needs ratification
    assert not _engine_has("BAE", "BI_ANNUAL_EXCEPTION", "BIANNUAL_EXCEPTION")


def test_maximum_salaries_absent_from_engine():
    assert not _engine_has(
        "MAX_SALARY",
        "MAX_SALARY_TIER_0_6",
        "MAX_SALARY_TIER_7_9",
        "MAX_SALARY_TIER_10_PLUS",
        "SUPERMAX",
    )


def test_minimum_salaries_and_reimbursement_absent_from_engine():
    assert not _engine_has(
        "MIN_SALARY",
        "MINIMUM_SALARY_SCALE",
        "REIMBURSEMENT_CAP_HIT",
        "MIN_CAP_HIT_BASIS",
    )


def test_apron_hard_cap_triggers_not_implemented_in_engine():
    # hard_cap_effect lives only as YAML data; the engine has no trigger logic.
    src = Path(salary_rules.__file__).read_text()
    assert "hard_cap" not in src.lower()
    assert not _engine_has("HARD_CAP_TRIGGERS", "apply_hard_cap", "hard_cap_for")


# --- Boundary PINS for trade-matching brackets (all verified:false) ------------
# These assert the ENGINE'S CURRENT behavior so a silent cutoff change is caught.
# They do NOT assert correctness — that is a ratification item.


def _trade(*, payroll: int, status: CapStatus, out: int, incoming: int) -> Trade:
    return Trade(
        team_a=Team(
            name="A", abbreviation="AAA", total_payroll=payroll, cap_status=status
        ),
        team_b=Team(
            name="B",
            abbreviation="BBB",
            total_payroll=170_000_000,
            cap_status=CapStatus.OVER_CAP,
        ),
        team_a_sends=TradeAssets(
            players=[
                RosterEntry(
                    player=Player(name="O", team="AAA", age=28),
                    contract=Contract(salary=out, years_remaining=1),
                )
            ]
        ),
        team_b_sends=TradeAssets(
            players=[
                RosterEntry(
                    player=Player(name="I", team="BBB", age=28),
                    contract=Contract(salary=incoming, years_remaining=1),
                )
            ]
        ),
    )


def test_pin_engine_tier_band_cutoffs():
    # Pins the literal cutoffs the engine encodes. cbaguide bands are
    # "<$7.25MM" / "$7.25MM-$29MM" / ">$29MM" — engine LOW/HIGH must equal those.
    assert salary_rules.EXPANDED_TPE_LOW == 7_250_000
    assert salary_rules.EXPANDED_TPE_HIGH == 29_000_000
    assert salary_rules.TRADE_MATCH_CUSHION == 250_000
    assert constants.EXPANDED_TPE_CUSHION == 8_527_000


def test_pin_tier1_band_is_strict_less_than_low_cutoff():
    # outgoing == 7.25M must NOT be tier-1 (200%+250k); it must be tier-2
    # (100%+8.527M). Proven by a takeback that tier-1 would allow but tier-2
    # rejects: 200%*7.25M+250k = 14.75M vs tier-2 limit 7.25M+8.527M = 15.777M.
    # Pick incoming = 15.0M: legal under both, so instead probe the rejection.
    # tier-2 limit (15.777M) > tier-1 limit (14.75M); incoming 15.5M is legal
    # only if the engine is on tier-2 at exactly 7.25M outgoing.
    res = check_trade_legality(
        _trade(
            payroll=170_000_000,
            status=CapStatus.OVER_CAP,
            out=7_250_000,
            incoming=15_500_000,
        )
    )
    assert res.legal, res.error_reason  # tier-2 band active at the LOW boundary


def test_pin_tier3_band_is_strict_greater_than_high_cutoff():
    # outgoing == 29M must still be tier-2 (gets the 8.527M cushion), NOT tier-3
    # (125% only). tier-2 limit 37.527M vs tier-3 limit 36.25M; incoming 37M is
    # legal only if engine is still on tier-2 at exactly 29M outgoing.
    res = check_trade_legality(
        _trade(
            payroll=170_000_000,
            status=CapStatus.OVER_CAP,
            out=29_000_000,
            incoming=37_000_000,
        )
    )
    assert res.legal, res.error_reason  # tier-2 active at the HIGH boundary


def test_pin_first_apron_dispatch_is_strict_below_second_apron():
    # _evaluate_team dispatches to _check_first_apron only when pre_trade is
    # STRICTLY below SECOND_APRON (salary_rules.py:77). A team sitting exactly at
    # SECOND_APRON pre-trade goes to the second-apron path. Pin that split:
    # at SECOND_APRON pre-trade, aggregation (2 outgoing) is blocked if still over.
    trade = Trade(
        team_a=Team(
            name="A",
            abbreviation="AAA",
            total_payroll=constants.SECOND_APRON,
            cap_status=CapStatus.SECOND_APRON,
        ),
        team_b=Team(
            name="B",
            abbreviation="BBB",
            total_payroll=170_000_000,
            cap_status=CapStatus.OVER_CAP,
        ),
        team_a_sends=TradeAssets(
            players=[
                RosterEntry(
                    player=Player(name="O1", team="AAA", age=28),
                    contract=Contract(salary=10_000_000, years_remaining=1),
                ),
                RosterEntry(
                    player=Player(name="O2", team="AAA", age=28),
                    contract=Contract(salary=10_000_000, years_remaining=1),
                ),
            ]
        ),
        team_b_sends=TradeAssets(
            players=[
                RosterEntry(
                    player=Player(name="I", team="BBB", age=28),
                    contract=Contract(salary=20_500_000, years_remaining=1),
                )
            ]
        ),
    )
    # post-trade = SECOND_APRON - 20M + 20.5M = SECOND_APRON + 0.5M > apron →
    # aggregation blocked. Confirms dispatch routed to the second-apron path.
    res = check_trade_legality(trade)
    assert not res.legal
    assert "aggregation blocked" in (res.error_reason or "")
