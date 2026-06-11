"""Certification audit for the 2026-06-11 ratification pass.

AUDIT ARTIFACT (verification-only — never edits engine/constants/reference).

After the ratification pass, `trade_matching`, `apron_status_timing`, the
MLE-adjacent caps, and `minimum_team_salary` are `verified: true` in the
reference. Per the source-of-truth hierarchy, engine values that match a
`verified: true` entry may now be CERTIFIED correct (the older coverage tests
only PINNED current behavior because those blocks were `verified: false` when
they were written).

These tests:
  1. Confirm the relevant reference blocks are genuinely `verified: true`
     (otherwise certification is not licensed and the test must fail, not pass).
  2. Certify the engine band constants/operators against the now-verified
     `trade_matching` boundaries (Q4 7.25M `<`, 29M `<=`, 8,527,000 cushion,
     200%+250k tier-1, 125%-no-cushion tier-3).
  3. Certify Gate B (`post_trade > FIRST_APRON`) and the second-apron carve-out
     (`post_trade <= SECOND_APRON`) implement the strict "exceeds" timing from
     `apron_status_timing` (Q2).
"""

from __future__ import annotations

import re
from pathlib import Path

from nba_trade_analyzer.engine import constants, salary_rules
from nba_trade_analyzer.engine.salary_rules import check_trade_legality
from nba_trade_analyzer.models.player import Contract, Player
from nba_trade_analyzer.models.team import CapStatus, RosterEntry, Team
from nba_trade_analyzer.models.trade import Trade, TradeAssets

REFERENCE_PATH = (
    Path(__file__).resolve().parents[1] / "reference" / "cba_reference_2025-26.yaml"
)
_YAML_TEXT = REFERENCE_PATH.read_text()


def _block(key: str, *, top_level: bool = False) -> str:
    anchor = "^" if top_level else r"^\s*"
    pattern = rf"{anchor}{re.escape(key)}:\s*$\n(?P<block>(?:^\s+.*\n?)+)"
    m = re.search(pattern, _YAML_TEXT, re.MULTILINE)
    assert m is not None, f"{key!r} block not found in reference"
    return m.group("block")


def _block_is_verified(key: str, *, top_level: bool = False) -> bool:
    block = _block(key, top_level=top_level)
    # First verified: line inside the block (the block's own flag).
    m = re.search(r"^\s*verified:\s*(true|false)", block, re.MULTILINE)
    assert m is not None, f"no verified: flag in {key!r}"
    return m.group(1) == "true"


# ---------------------------------------------------------------------------
# (1) The blocks we are about to certify against must actually be verified:true.
# ---------------------------------------------------------------------------


def test_trade_matching_block_is_verified_true():
    assert _block_is_verified("trade_matching", top_level=True), (
        "trade_matching must be verified:true to certify the engine bands"
    )


def test_apron_status_timing_block_is_verified_true():
    assert _block_is_verified("apron_status_timing"), (
        "apron_status_timing must be verified:true to certify Gate B / carve-out"
    )


# ---------------------------------------------------------------------------
# (2) Trade-matching bands CERTIFIED against the now-verified trade_matching.
# ---------------------------------------------------------------------------


def test_band_constants_certify_against_verified_trade_matching():
    block = _block("trade_matching", top_level=True)
    # The verified reference names these exact boundary values.
    assert "7250000" in block
    assert "29000000" in block
    assert "8527000" in block
    assert "250000" in block
    # Engine encodes the same numbers.
    assert salary_rules.EXPANDED_TPE_LOW == 7_250_000
    assert salary_rules.EXPANDED_TPE_HIGH == 29_000_000
    assert constants.EXPANDED_TPE_CUSHION == 8_527_000
    assert salary_rules.TRADE_MATCH_CUSHION == 250_000


def _otrade(*, payroll: int, out: int, incoming: int) -> Trade:
    return Trade(
        team_a=Team(
            name="A",
            abbreviation="AAA",
            total_payroll=payroll,
            cap_status=CapStatus.OVER_CAP,
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


def test_tier1_is_strict_below_7_25m_per_q4():
    # Q4: "Less than $7.25MM -> 200% + $250k". At exactly 7.25M the engine must be
    # on tier-2 (cushion), not tier-1. 200%*7.25M+250k = 14.75M < 7.25M+8.527M.
    # 15.5M is legal only on tier-2.
    assert check_trade_legality(
        _otrade(payroll=170_000_000, out=7_250_000, incoming=15_500_000)
    ).legal
    # Just below the boundary IS tier-1: out 7,249,999 -> 200%+250k ~= 14,749,998;
    # 15.5M must be rejected.
    assert not check_trade_legality(
        _otrade(payroll=170_000_000, out=7_249_999, incoming=15_500_000)
    ).legal


def test_tier3_is_strict_above_29m_per_q4():
    # Q4: "More than $29MM -> 125% (no $250k)". At exactly 29M still tier-2.
    # 37M is under 29M+8.527M(=37.527M) but over 125%*29M(=36.25M): legal => tier-2.
    assert check_trade_legality(
        _otrade(payroll=170_000_000, out=29_000_000, incoming=37_000_000)
    ).legal
    # Just above (29,000,001) is tier-3: 125% = 36,250,001; 37M must be rejected.
    res = check_trade_legality(
        _otrade(payroll=170_000_000, out=29_000_001, incoming=37_000_000)
    )
    assert not res.legal
    assert "no $250K" in (res.error_reason or "")


def test_tier1_200pct_plus_250k_certified():
    # out 5M -> limit 10.25M legal; 10,250,001 illegal.
    assert check_trade_legality(
        _otrade(payroll=170_000_000, out=5_000_000, incoming=10_250_000)
    ).legal
    assert not check_trade_legality(
        _otrade(payroll=170_000_000, out=5_000_000, incoming=10_250_001)
    ).legal


def test_tier3_125pct_no_cushion_certified():
    # out 30M -> 125% = 37.5M legal exactly; +1 illegal (no 250k cushion).
    assert check_trade_legality(
        _otrade(payroll=170_000_000, out=30_000_000, incoming=37_500_000)
    ).legal
    assert not check_trade_legality(
        _otrade(payroll=170_000_000, out=30_000_000, incoming=37_500_001)
    ).legal


# ---------------------------------------------------------------------------
# (3) Gate B + second-apron carve-out CERTIFIED against verified apron timing.
#     Q2: prohibited only if salary "exceeds ... after executing the
#     transaction" -> strict ">"; landing exactly at the line is legal.
# ---------------------------------------------------------------------------


def test_gate_b_strict_exceeds_first_apron():
    # Lands exactly at FIRST_APRON via a >100% takeback -> legal (strict exceeds).
    incoming = 11_945_000  # 194M - 10M + 11.945M = 195,945,000 == FIRST_APRON
    assert 194_000_000 - 10_000_000 + incoming == constants.FIRST_APRON
    assert check_trade_legality(
        _otrade(payroll=194_000_000, out=10_000_000, incoming=incoming)
    ).legal
    # One dollar over -> illegal.
    assert not check_trade_legality(
        _otrade(payroll=194_000_000, out=10_000_000, incoming=incoming + 1)
    ).legal


def test_second_apron_carveout_strict_exceeds():
    # Aggregating team that lands EXACTLY at SECOND_APRON post-trade -> carve-out
    # re-enables aggregation (strict "exceeds"). Engine uses post_trade <= SECOND.
    payroll = constants.SECOND_APRON + 10_000_000  # 217.824M
    trade = Trade(
        team_a=Team(
            name="A",
            abbreviation="AAA",
            total_payroll=payroll,
            cap_status=CapStatus.SECOND_APRON,
        ),
        team_b=Team(
            name="B",
            abbreviation="BBB",
            total_payroll=140_000_000,
            cap_status=CapStatus.UNDER_CAP,
        ),
        team_a_sends=TradeAssets(
            players=[
                RosterEntry(
                    player=Player(name="O1", team="AAA", age=28),
                    contract=Contract(salary=15_000_000, years_remaining=1),
                ),
                RosterEntry(
                    player=Player(name="O2", team="AAA", age=28),
                    contract=Contract(salary=12_000_000, years_remaining=1),
                ),
            ]
        ),
        team_b_sends=TradeAssets(
            players=[
                RosterEntry(
                    player=Player(name="I", team="BBB", age=28),
                    contract=Contract(salary=17_000_000, years_remaining=1),
                )
            ]
        ),
    )
    # 217.824M - 27M + 17M = 207.824M == SECOND_APRON -> not "over" -> legal.
    assert payroll - 27_000_000 + 17_000_000 == constants.SECOND_APRON
    assert check_trade_legality(trade).legal


def test_engine_uses_strict_operators_in_source():
    # Pin the strict comparison operators the verified timing rule requires, so a
    # refactor to `>=` / `<` cannot silently flip the boundary semantics.
    src = Path(salary_rules.__file__).read_text()
    assert "post_trade > FIRST_APRON" in src  # Gate B strict exceeds
    assert "post_trade <= SECOND_APRON" in src  # carve-out: at-or-below is not over
