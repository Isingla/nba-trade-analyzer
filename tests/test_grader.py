"""Tests for the Phase 7 trade grader.

All data is mocked — no HTTP. Fixture players carry known EPM / salary / age
so every grade is deterministic. Two five-man rosters (NYK and MIN) live in a
shared synthetic world; trades move designated players between them.
"""

from __future__ import annotations

import pandas as pd
import pytest

from nba_trade_analyzer.engine.grader import (
    DUAL_NEGATIVE_DAMPING_THRESHOLD,
    _contract_tier,
    _dual_negative_damping,
    _epm_tier,
    _shed_sentence,
    _spacing_tier,
    _timeline_tier,
    _win_curve_tier,
    get_verdict,
    grade_trade,
)
from nba_trade_analyzer.models.draft_pick import DraftPick
from nba_trade_analyzer.engine.valuation import evaluate_player_multiyear
from nba_trade_analyzer.models.player import Contract, Player
from nba_trade_analyzer.models.team import CapStatus, RosterEntry, Team
from nba_trade_analyzer.models.trade import Trade, TradeAssets

# 2025-26 cap landmarks (mirrors engine/constants.py) — payrolls below pick the
# CBA tier the legality engine uses.
_OVER_CAP_PAYROLL = 170_000_000  # between cap and first apron → Expanded TPE
_FIRST_APRON_PAYROLL = 200_000_000  # between first and second apron → 100% match


# ---------------------------------------------------------------------------
# Fixture world
# ---------------------------------------------------------------------------


def _empty_darko() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "player_name",
            "player_name_normalized",
            "dpm",
            "dpm_off",
            "dpm_def",
            "position",
            "age",
        ]
    )


def _epm_df(specs: list[dict]) -> pd.DataFrame:
    rows = [
        {
            "player_name": s["name"],
            "player_name_normalized": s["name"].casefold(),
            "team": s["team"],
            "epm": s["epm"],
            "epm_off": s["epm"] * 0.6,
            "epm_def": s["epm"] * 0.4,
            "mpg": s["mpg"],
            "position": s["position"],
            "age": s["age"],
        }
        for s in specs
    ]
    return pd.DataFrame(rows)


def _stats_df(specs: list[dict]) -> pd.DataFrame:
    rows = [
        {
            "player_name": s["name"],
            "team": s["team"],
            "age": s["age"],
            "GP": s["gp"],
            "MPG": s["mpg"],
            "W": s["w"],
            "L": s["gp"] - s["w"],
            "FGA": s["fga"],
            "FG3A": s["fg3a"],
            "FG3_PCT": s["fg3_pct"],
            "FG3_RATE": (s["fg3a"] / s["fga"]) if s["fga"] else 0.0,
            "NET_RATING": 0.0,
            "position": s["position"],
        }
        for s in specs
    ]
    return pd.DataFrame(rows)


def _spec(
    name: str,
    team: str,
    epm: float,
    *,
    position: str = "F",
    age: int = 27,
    gp: int = 70,
    mpg: float = 34.0,
    w: int = 42,
    fga: float = 14.0,
    fg3a: float = 5.0,
    fg3_pct: float = 0.37,
) -> dict:
    return {
        "name": name,
        "team": team,
        "epm": epm,
        "position": position,
        "age": age,
        "gp": gp,
        "mpg": mpg,
        "w": w,
        "fga": fga,
        "fg3a": fg3a,
        "fg3_pct": fg3_pct,
    }


# NYK ~49 projected wins (W=42, GP=70), MIN ~47 (W=40, GP=70).
_NYK_ROSTER = [
    _spec("NYK Star", "NYK", 4.0, position="G", age=26, w=42),
    _spec("NYK Wing", "NYK", 2.0, position="F", age=27, w=42),
    _spec("NYK Big", "NYK", 1.5, position="C", age=28, w=42, fg3a=0.2, fg3_pct=0.20),
    _spec("NYK Guard2", "NYK", 0.5, position="G", age=25, w=42),
    _spec("NYK Vet", "NYK", -0.5, position="F", age=33, w=42),
]
_MIN_ROSTER = [
    _spec("MIN Star", "MIN", 3.5, position="F", age=25, w=40),
    _spec("MIN Wing", "MIN", 1.0, position="G", age=24, w=40),
    _spec("MIN Big", "MIN", 2.0, position="C", age=26, w=40, fg3a=0.1, fg3_pct=0.18),
    _spec("MIN Guard2", "MIN", 0.0, position="G", age=23, w=40),
    _spec("MIN Vet", "MIN", -1.0, position="F", age=31, w=40),
]


def _world(extra: list[dict] | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    specs = _NYK_ROSTER + _MIN_ROSTER + (extra or [])
    return _epm_df(specs), _stats_df(specs)


def _entry(spec: dict, salary: int, years: int = 3) -> RosterEntry:
    player = Player(
        name=spec["name"],
        team=spec["team"],
        age=spec["age"],
        stats={
            "NET_RATING": 0.0,
            "GP": float(spec["gp"]),
            "MPG": float(spec["mpg"]),
        },
    )
    return RosterEntry(
        player=player, contract=Contract(salary=salary, years_remaining=years)
    )


def _team(abbr: str, name: str, payroll: int = _OVER_CAP_PAYROLL) -> Team:
    if payroll < 154_647_000:
        status = CapStatus.UNDER_CAP
    elif payroll < 195_945_000:
        status = CapStatus.OVER_CAP
    elif payroll < 207_824_000:
        status = CapStatus.FIRST_APRON
    else:
        status = CapStatus.SECOND_APRON
    return Team(name=name, abbreviation=abbr, total_payroll=payroll, cap_status=status)


def _grade(trade: Trade, extra: list[dict] | None = None):
    epm_df, stats_df = _world(extra)
    return grade_trade(
        trade,
        player_stats_df=stats_df,
        epm_df=epm_df,
        darko_df=_empty_darko(),
    )


# ---------------------------------------------------------------------------
# Trade builders
# ---------------------------------------------------------------------------


def _fair_trade() -> Trade:
    """A near-even swap of two similar mid-tier starters (~$25M each)."""
    a_sends = TradeAssets(players=[_entry(_NYK_ROSTER[1], 25_000_000)])  # NYK Wing
    b_sends = TradeAssets(players=[_entry(_MIN_ROSTER[2], 25_000_000)])  # MIN Big
    return Trade(
        team_a=_team("NYK", "New York Knicks"),
        team_b=_team("MIN", "Minnesota Timberwolves"),
        team_a_sends=a_sends,
        team_b_sends=b_sends,
    )


def _lopsided_trade() -> Trade:
    """NYK fleeces MIN: a productive star for a negative-value contract.

    Salaries are matched (~$30M each) so the deal is CBA-legal; the gap is
    entirely in production, not money.
    """
    star = _spec("Cheap Star", "MIN", 6.5, position="F", age=25, w=40)
    scrub = _spec("Bad Deal", "NYK", -1.0, position="F", age=31, w=42)
    a_sends = TradeAssets(players=[_entry(scrub, 30_000_000)])  # NYK sends bad deal
    b_sends = TradeAssets(players=[_entry(star, 30_000_000)])  # MIN sends cheap star
    trade = Trade(
        team_a=_team("NYK", "New York Knicks"),
        team_b=_team("MIN", "Minnesota Timberwolves"),
        team_a_sends=a_sends,
        team_b_sends=b_sends,
    )
    return trade


def _trade_with_picks() -> Trade:
    """MIN sends a player plus a first-round pick to NYK."""
    a_sends = TradeAssets(players=[_entry(_NYK_ROSTER[1], 22_000_000)])
    b_sends = TradeAssets(
        players=[_entry(_MIN_ROSTER[0], 24_000_000)],
        picks=[DraftPick(team="MIN", year=2027, round=1)],
    )
    return Trade(
        team_a=_team("NYK", "New York Knicks"),
        team_b=_team("MIN", "Minnesota Timberwolves"),
        team_a_sends=a_sends,
        team_b_sends=b_sends,
    )


def _illegal_trade() -> Trade:
    """A first-apron team takes back far more salary than it sends out."""
    scrub = _spec("Min Salary", "NYK", 0.0, position="G", age=24, w=42)
    star = _spec("Max Salary", "MIN", 5.0, position="F", age=27, w=40)
    a_sends = TradeAssets(players=[_entry(scrub, 3_000_000)])
    b_sends = TradeAssets(players=[_entry(star, 35_000_000)])
    return Trade(
        team_a=_team("NYK", "New York Knicks", payroll=_FIRST_APRON_PAYROLL),
        team_b=_team("MIN", "Minnesota Timberwolves"),
        team_a_sends=a_sends,
        team_b_sends=b_sends,
    )


# ---------------------------------------------------------------------------
# 1. Illegal trade short-circuits
# ---------------------------------------------------------------------------


def test_illegal_trade_short_circuits():
    star = _spec("Max Salary", "MIN", 5.0, position="F", age=27, w=40)
    scrub = _spec("Min Salary", "NYK", 0.0, position="G", age=24, w=42)
    grade = _grade(_illegal_trade(), extra=[star, scrub])
    assert grade.is_legal is False
    assert grade.illegal_reason is not None
    assert len(grade.illegal_reason) > 0
    assert grade.team_a_grade is None
    assert grade.team_b_grade is None


# ---------------------------------------------------------------------------
# 2. Legal trade produces both grades with in-range scores
# ---------------------------------------------------------------------------


def test_legal_trade_populates_both_grades():
    grade = _grade(_fair_trade())
    assert grade.is_legal is True
    assert grade.illegal_reason is None
    assert grade.team_a_grade is not None
    assert grade.team_b_grade is not None
    for tg in (grade.team_a_grade, grade.team_b_grade):
        assert 0 <= tg.score <= 100


# ---------------------------------------------------------------------------
# 3. Score symmetry (zero-sum-ish)
# ---------------------------------------------------------------------------


def test_score_symmetry_is_zero_sum():
    grade = _grade(
        _lopsided_trade(),
        extra=[
            _spec("Cheap Star", "MIN", 6.5, position="F", age=25, w=40),
            _spec("Bad Deal", "NYK", -1.0, position="F", age=31, w=42),
        ],
    )
    a = grade.team_a_grade.score
    b = grade.team_b_grade.score
    # Scores are computed from the same surplus delta with opposite sign.
    assert abs((a + b) - 100) <= 1
    # If one side clears 50, the other must be below it.
    if a > 50:
        assert b < 50
    if b > 50:
        assert a < 50


# ---------------------------------------------------------------------------
# 4. Verdict label mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("score", "verdict"),
    [
        (90, "Highway Robbery"),
        (85, "Highway Robbery"),
        (75, "Clear Win"),
        (70, "Clear Win"),
        (60, "Smart Deal"),
        (50, "Fair Trade"),
        (45, "Fair Trade"),
        (40, "Slight Overpay"),
        (25, "Overpay"),
        (10, "Fleeced"),
    ],
)
def test_verdict_label_mapping(score: int, verdict: str):
    assert get_verdict(score) == verdict


# ---------------------------------------------------------------------------
# 5. Metric breakdown completeness
# ---------------------------------------------------------------------------


def test_all_seven_categories_populated():
    grade = _grade(_trade_with_picks())
    for tg in (grade.team_a_grade, grade.team_b_grade):
        assert tg.impact.category == "Impact"
        assert tg.contract.category == "Contract"
        assert tg.win_curve.category == "Win Curve"
        assert tg.timeline.category == "Timeline"
        assert tg.positional_fit.category == "Positional Fit"
        assert tg.spacing.category == "Spacing"
        assert tg.draft_capital is not None
        for mb in (
            tg.impact,
            tg.contract,
            tg.win_curve,
            tg.timeline,
            tg.positional_fit,
            tg.spacing,
        ):
            assert mb.tier
            assert mb.raw_label


# ---------------------------------------------------------------------------
# 6. Tier labels match expected ranges
# ---------------------------------------------------------------------------


def test_tier_thresholds():
    assert _epm_tier(5.0) == "Elite (top 5%)"
    assert _epm_tier(3.0) == "All-Star Level (top 15%)"
    assert _epm_tier(1.5) == "Above Average Starter (top 30%)"
    assert _epm_tier(0.0) == "Average (top 50%)"
    assert _epm_tier(-2.0) == "Replacement Level"
    assert _contract_tier(16_000_000) == "Elite Value"
    assert _contract_tier(-20_000_000) == "Bad Contract"
    assert _win_curve_tier(1.85) == "Championship Contender"
    assert _win_curve_tier(0.5) == "Rebuilding"
    assert _timeline_tier(0.12) == "Strong Fit"
    assert _timeline_tier(-0.12) == "Timeline Mismatch"
    assert _spacing_tier(0.05) == "Significant Boost"
    assert _spacing_tier(-0.05) == "Hurts Spacing"


def test_elite_player_impact_tier_in_pipeline():
    elite = _spec("Elite Guy", "MIN", 5.5, position="F", age=26, w=40)
    a_sends = TradeAssets(players=[_entry(_NYK_ROSTER[1], 30_000_000)])
    b_sends = TradeAssets(players=[_entry(elite, 30_000_000)])
    trade = Trade(
        team_a=_team("NYK", "New York Knicks"),
        team_b=_team("MIN", "Minnesota Timberwolves"),
        team_a_sends=a_sends,
        team_b_sends=b_sends,
    )
    grade = _grade(trade, extra=[elite])
    # NYK acquires the elite player.
    assert grade.team_a_grade.impact.tier == "Elite (top 5%)"
    assert grade.team_a_grade.impact.raw_value == pytest.approx(5.5)


# ---------------------------------------------------------------------------
# 7. Explanations are non-empty and name the player / team
# ---------------------------------------------------------------------------


def test_explanations_reference_names():
    grade = _grade(_fair_trade())
    tg = grade.team_a_grade  # NYK acquires "MIN Big"
    for mb in (
        tg.impact,
        tg.contract,
        tg.win_curve,
        tg.timeline,
        tg.positional_fit,
        tg.spacing,
    ):
        assert mb.explanation.strip()
    assert "MIN Big" in tg.impact.explanation
    assert "Knicks" in tg.win_curve.explanation


# ---------------------------------------------------------------------------
# 8. Prose verdict
# ---------------------------------------------------------------------------


def test_prose_is_substantial():
    grade = _grade(_fair_trade())
    for tg in (grade.team_a_grade, grade.team_b_grade):
        assert len(tg.prose) >= 50
        assert _short(tg.team_name) in tg.prose


def _short(name: str) -> str:
    return name.split()[-1]


# ---------------------------------------------------------------------------
# 9 & 10. Draft capital breakdown
# ---------------------------------------------------------------------------


def test_draft_capital_no_picks():
    grade = _grade(_fair_trade())
    for tg in (grade.team_a_grade, grade.team_b_grade):
        assert tg.draft_capital.picks_description == []
        assert tg.draft_capital.total_pick_value == 0.0
        assert tg.draft_capital.explanation == "No draft picks involved."


def test_draft_capital_with_picks():
    grade = _grade(_trade_with_picks())
    nyk = grade.team_a_grade  # NYK receives MIN's pick
    assert len(nyk.draft_capital.picks_description) == 1
    assert "2027 MIN 1st" in nyk.draft_capital.picks_description[0]
    assert nyk.draft_capital.total_pick_value > 0
    # MIN receives no picks.
    assert grade.team_b_grade.draft_capital.picks_description == []


# ---------------------------------------------------------------------------
# 11 & 12. Score calibration: lopsided vs fair
# ---------------------------------------------------------------------------


def test_lopsided_trade_winner_and_loser():
    grade = _grade(
        _lopsided_trade(),
        extra=[
            _spec("Cheap Star", "MIN", 6.5, position="F", age=25, w=40),
            _spec("Bad Deal", "NYK", -1.0, position="F", age=31, w=42),
        ],
    )
    nyk = grade.team_a_grade  # acquires the cheap star
    minn = grade.team_b_grade  # acquires the bad contract
    assert nyk.score >= 70
    assert minn.score <= 30


def test_fair_trade_scores_balanced():
    grade = _grade(_fair_trade())
    assert 40 <= grade.team_a_grade.score <= 60
    assert 40 <= grade.team_b_grade.score <= 60


# ---------------------------------------------------------------------------
# 13. Score scaling normalized by trade magnitude (Issue 3)
# ---------------------------------------------------------------------------


def _swap(
    a_spec: dict,
    a_salary: int,
    a_years: int,
    b_spec: dict,
    b_salary: int,
    b_years: int,
) -> tuple[Trade, list[dict]]:
    """One-for-one swap; returns the trade plus the extra specs for the world."""
    a_sends = TradeAssets(players=[_entry(a_spec, a_salary, a_years)])
    b_sends = TradeAssets(players=[_entry(b_spec, b_salary, b_years)])
    trade = Trade(
        team_a=_team("NYK", "New York Knicks"),
        team_b=_team("MIN", "Minnesota Timberwolves"),
        team_a_sends=a_sends,
        team_b_sends=b_sends,
    )
    return trade, [a_spec, b_spec]


def test_identical_players_score_exactly_fifty():
    # Same EPM, salary, age, length on both sides → zero surplus delta → 50/50.
    a = _spec("Twin A", "NYK", 2.0, age=27)
    b = _spec("Twin B", "MIN", 2.0, age=27)
    trade, extra = _swap(a, 25_000_000, 3, b, 25_000_000, 3)
    grade = _grade(trade, extra=extra)
    assert grade.team_a_grade.score == 50
    assert grade.team_b_grade.score == 50


def test_similar_value_different_contract_lengths_stay_balanced():
    # Same quality and salary, but 4 years vs 1 year. The longer commitment
    # tilts the score, but normalizing by the multi-year deal magnitude keeps
    # both sides inside the fair-trade band instead of producing an extreme
    # split off contract length alone.
    a = _spec("Long Deal", "NYK", 2.0, age=27)
    b = _spec("Expiring", "MIN", 2.0, age=27)
    trade, extra = _swap(a, 25_000_000, 4, b, 25_000_000, 1)
    grade = _grade(trade, extra=extra)
    assert 40 <= grade.team_a_grade.score <= 60
    assert 40 <= grade.team_b_grade.score <= 60


def test_similar_negative_value_players_stay_balanced():
    # The Klay/Aldama case: two similar, modestly-negative-value players. A
    # swap of equals should land near even, not at the extremes.
    a = _spec("Vet A", "NYK", -0.3, age=34)
    b = _spec("Vet B", "MIN", -0.1, age=24)
    trade, extra = _swap(a, 14_000_000, 1, b, 13_000_000, 1)
    grade = _grade(trade, extra=extra)
    assert 40 <= grade.team_a_grade.score <= 60
    assert 40 <= grade.team_b_grade.score <= 60


def test_star_for_scrub_is_decisive():
    # A productive star for a negative-value contract (salaries matched so the
    # gap is all production) should be a clear fleecing.
    star = _spec("Bargain Star", "MIN", 6.5, position="F", age=25)
    scrub = _spec("Dead Weight", "NYK", -1.0, position="F", age=31)
    trade, extra = _swap(scrub, 30_000_000, 3, star, 30_000_000, 3)
    grade = _grade(trade, extra=extra)
    assert grade.team_a_grade.score >= 75  # NYK acquires the star
    assert grade.team_b_grade.score <= 25  # MIN acquires the scrub


# ---------------------------------------------------------------------------
# Prose clarity / display regressions
# ---------------------------------------------------------------------------


_LOPSIDED_EXTRA = [
    _spec("Cheap Star", "MIN", 6.5, position="F", age=25, w=40),
    _spec("Bad Deal", "NYK", -1.0, position="F", age=31, w=42),
]


def test_prose_does_not_contradict_a_losing_grade():
    # Issue 1: a positive metric must never read as a "win" when the overall
    # verdict is an overpay.
    grade = _grade(_lopsided_trade(), extra=_LOPSIDED_EXTRA)
    loser = grade.team_b_grade  # MIN acquires the bad contract → low score
    assert loser.score < 45
    lowered = loser.prose.lower()
    assert "clear win" not in lowered
    assert "strong win" not in lowered


def test_prose_references_outgoing_players():
    # Issue 2: the verdict must reflect the delta — what was sent, not just
    # what was acquired.
    grade = _grade(_lopsided_trade(), extra=_LOPSIDED_EXTRA)
    loser = grade.team_b_grade  # MIN sent "Cheap Star" to get "Bad Deal"
    assert "Cheap Star" in loser.prose
    assert "Bad Deal" in loser.prose


def test_losing_prose_lists_sent_picks():
    # Issue 2: a pick sent out should appear in the losing side's verdict.
    grade = _grade(_trade_with_picks())
    # MIN sends a player + a 2027 pick to NYK and comes out behind.
    minn = grade.team_b_grade
    assert minn.score < 45
    assert "2027 MIN 1st" in minn.prose


def test_contract_explanation_omits_deficit_dollars():
    # Issue 3: the plain-English sentence must not quote the raw deficit; that
    # number lives only in raw_label.
    grade = _grade(_lopsided_trade(), extra=_LOPSIDED_EXTRA)
    contract = grade.team_b_grade.contract  # acquired the bad contract
    assert "overpaying by $" not in contract.explanation
    assert "/yr" not in contract.explanation
    assert "deficit" in contract.raw_label  # ...still surfaced for analytics


def test_win_curve_multiplier_not_leaked_into_prose():
    # Issue 4: the "x" multiplier belongs in the Win Curve breakdown only.
    grade = _grade(_lopsided_trade(), extra=_LOPSIDED_EXTRA)
    for tg in (grade.team_a_grade, grade.team_b_grade):
        assert tg.win_curve.raw_label not in tg.prose


# ---------------------------------------------------------------------------
# 14. Capped dollar amounts in salary-dump prose (Issue 4)
# ---------------------------------------------------------------------------


def test_shed_sentence_small_amount_omits_dollar_figure():
    # Under $10M: no specific number, just "negative value".
    s = _shed_sentence(6.0, "Filler Guy")
    assert "$" not in s
    assert "negative value" in s
    assert "Filler Guy" in s


def test_shed_sentence_mid_amount_calls_it_significant():
    # $10-30M: "significant negative value (roughly $XM)".
    s = _shed_sentence(20.0, "Filler Guy")
    assert "significant negative value" in s
    assert "$20M" in s


def test_shed_sentence_large_amount_is_major_improvement():
    # Over $30M: quotes the figure and frames it as a major cap win.
    s = _shed_sentence(45.0, None)
    assert "$45M" in s
    assert "major cap improvement" in s


def test_shed_sentence_never_uses_old_inflated_phrasing():
    for amount in (5.0, 18.0, 56.0):
        s = _shed_sentence(amount, "X")
        assert "shedding roughly $" not in s


def _salary_dump_trade() -> Trade:
    """NYK dumps a bad contract for a cheap, weak filler player.

    NYK takes back far less salary than it sends (always CBA-legal), the
    incoming player is weak, and NYK comes out ahead — exactly the conditions
    that trigger the salary-dump prose branch.
    """
    bad = _spec("Bad Contract", "NYK", -1.0, position="F", age=31)
    filler = _spec("Cheap Filler", "MIN", 0.0, position="F", age=24)
    a_sends = TradeAssets(players=[_entry(bad, 30_000_000, years=3)])
    b_sends = TradeAssets(players=[_entry(filler, 5_000_000, years=1)])
    return Trade(
        team_a=_team("NYK", "New York Knicks"),
        # MIN is under the cap with room to absorb the dumped contract.
        team_b=_team("MIN", "Minnesota Timberwolves", payroll=120_000_000),
        team_a_sends=a_sends,
        team_b_sends=b_sends,
    )


def test_salary_dump_prose_does_not_quote_inflated_delta():
    grade = _grade(
        _salary_dump_trade(),
        extra=[
            _spec("Bad Contract", "NYK", -1.0, position="F", age=31),
            _spec("Cheap Filler", "MIN", 0.0, position="F", age=24),
        ],
    )
    nyk = grade.team_a_grade  # the side shedding the bad contract
    assert nyk.score >= 55
    assert "shedding roughly $" not in nyk.prose
    # The shed value is framed (cap relief), not quoted as a raw deficit.
    assert "negative value" in nyk.prose or "cap" in nyk.prose


def test_shedding_prose_quotes_delta_not_raw_total():
    # BUG 1: the shed figure must be the improvement (negative surplus sent out
    # minus negative surplus taken back), not the outgoing contract's raw total.
    dumped = _spec("Dumped Max", "NYK", -1.5, position="F", age=32)
    weak = _spec("Weak Return", "MIN", -0.5, position="F", age=27, gp=60, mpg=20.0)
    e_sent = _entry(dumped, 30_000_000, years=3)
    e_recv = _entry(weak, 12_000_000, years=2)
    trade = Trade(
        team_a=_team("NYK", "New York Knicks"),
        team_b=_team("MIN", "Minnesota Timberwolves", payroll=120_000_000),
        team_a_sends=TradeAssets(players=[e_sent]),
        team_b_sends=TradeAssets(players=[e_recv]),
    )
    grade = _grade(trade, extra=[dumped, weak])
    nyk = grade.team_a_grade
    assert nyk.score >= 55

    epm_df, _ = _world([dumped, weak])
    sent = evaluate_player_multiyear(
        e_sent.player, e_sent.contract, epm_df=epm_df, darko_df=_empty_darko()
    ).total_contract_surplus
    recv = evaluate_player_multiyear(
        e_recv.player, e_recv.contract, epm_df=epm_df, darko_df=_empty_darko()
    ).total_contract_surplus
    delta_m = round((abs(sent) - abs(recv)) / 1_000_000)
    raw_m = round(abs(sent) / 1_000_000)
    assert delta_m != raw_m  # the two would coincide only if nothing came back
    assert f"${delta_m}M" in nyk.prose  # the improvement
    assert f"${raw_m}M" not in nyk.prose  # not the raw outgoing total


# ---------------------------------------------------------------------------
# 15. Dual-negative-surplus score damping (Issue 5)
# ---------------------------------------------------------------------------


def test_damping_inactive_unless_both_sides_negative():
    # No damping when either received package is non-negative.
    assert _dual_negative_damping(10_000_000, 5_000_000) == 1.0
    assert _dual_negative_damping(-30_000_000, 5_000_000) == 1.0
    assert _dual_negative_damping(5_000_000, -30_000_000) == 1.0


def test_damping_compresses_when_both_negative():
    # Both negative → multiplier strictly inside (0, 1).
    d = _dual_negative_damping(-28_000_000, -73_000_000)
    assert 0.0 < d < 1.0


def test_damping_deeper_better_deal_compresses_more():
    # The more negative the *better* (less-bad) package, the smaller the
    # multiplier — a deeper "pick your poison" collapses harder toward even.
    mild = _dual_negative_damping(-8_000_000, -40_000_000)
    deep = _dual_negative_damping(-28_000_000, -60_000_000)
    assert deep < mild


def test_damping_floors_at_zero():
    # Once the better package is past the threshold, the swing vanishes.
    assert (
        _dual_negative_damping(
            -DUAL_NEGATIVE_DAMPING_THRESHOLD - 1, -DUAL_NEGATIVE_DAMPING_THRESHOLD - 5
        )
        == 0.0
    )


# Real 2025-26 values (EPM / GP / MPG / salary / years) for the reported
# regression cases. Two below-replacement players on different-length deals.
_KLAY = _spec("Klay Thompson", "NYK", -0.7843, gp=69, mpg=21.7, age=35)
_KISPERT = _spec("Corey Kispert", "MIN", -2.1878, gp=58, mpg=18.6, age=26)
_ALDAMA = _spec("Santi Aldama", "MIN", -0.3166, gp=43, mpg=27.9, age=24)


def test_two_bad_contracts_different_lengths_not_a_robbery():
    # Klay (2yr) for Kispert (4yr): both deeply negative value. The team
    # shedding the longer deal wins, but it is a "Smart Deal" nudge, not a
    # fleecing — without damping this graded ~96/4.
    a_sends = TradeAssets(players=[_entry(_KISPERT, 13_975_000, years=4)])
    b_sends = TradeAssets(players=[_entry(_KLAY, 16_666_667, years=2)])
    trade = Trade(
        team_a=_team("NYK", "New York Knicks"),
        team_b=_team("MIN", "Minnesota Timberwolves"),
        team_a_sends=a_sends,
        team_b_sends=b_sends,
    )
    grade = _grade(trade, extra=[_KLAY, _KISPERT])
    a, b = grade.team_a_grade.score, grade.team_b_grade.score
    assert 35 <= a <= 65
    assert 35 <= b <= 65
    assert grade.team_a_grade.verdict != "Highway Robbery"
    assert grade.team_b_grade.verdict != "Highway Robbery"


def test_outgoing_player_trimmed_from_positional_fit():
    # BUG 2: a player being sent out vacates his minutes, so he must not count
    # toward the team's positional load nor be named in the incoming player's
    # crunch. Without trimming, the highest-minute departing forward would be
    # named first.
    leaving_f = _spec("Leaving Forward", "NYK", 1.0, position="F", age=29, mpg=36.0)
    arriving_f = _spec("Arriving Forward", "MIN", 2.0, position="F", age=26)
    trade = Trade(
        team_a=_team("NYK", "New York Knicks"),
        team_b=_team("MIN", "Minnesota Timberwolves"),
        team_a_sends=TradeAssets(players=[_entry(leaving_f, 22_000_000)]),
        team_b_sends=TradeAssets(players=[_entry(arriving_f, 22_000_000)]),
    )
    grade = _grade(trade, extra=[leaving_f, arriving_f])
    pos = grade.team_a_grade.positional_fit
    # The departing forward is trimmed out of the crunch...
    assert "Leaving Forward" not in pos.explanation
    # ...while the forwards who actually stay still define the fit.
    assert ("NYK Wing" in pos.explanation) or ("NYK Vet" in pos.explanation)


def test_luka_not_named_in_forward_crunch():
    # BUG 3: Luka (listed "F-G" in the feed) is overridden to guard, so a team
    # acquiring a forward must not see him in the forward minutes crunch.
    luka = _spec("Luka Dončić", "NYK", 6.0, position="F-G", age=26, mpg=36.0)
    new_f = _spec("New Forward", "MIN", 2.0, position="F", age=27)
    trade = Trade(
        team_a=_team("NYK", "New York Knicks"),
        team_b=_team("MIN", "Minnesota Timberwolves"),
        team_a_sends=TradeAssets(players=[_entry(_NYK_ROSTER[3], 20_000_000)]),
        team_b_sends=TradeAssets(players=[_entry(new_f, 20_000_000)]),
    )
    grade = _grade(trade, extra=[luka, new_f])
    explanation = grade.team_a_grade.positional_fit.explanation
    # It is a genuine forward logjam (the real forwards fill it)...
    assert grade.team_a_grade.positional_fit.raw_value < 0
    # ...but Luka must not be the one named for it.
    assert "Luka" not in explanation
    assert "Dončić" not in explanation


def test_two_bad_contracts_small_length_gap_stays_balanced():
    # Klay (2yr) for Aldama (3yr): a tighter pairing should sit near even.
    a_sends = TradeAssets(players=[_entry(_ALDAMA, 18_485_916, years=3)])
    b_sends = TradeAssets(players=[_entry(_KLAY, 16_666_667, years=2)])
    trade = Trade(
        team_a=_team("NYK", "New York Knicks"),
        team_b=_team("MIN", "Minnesota Timberwolves"),
        team_a_sends=a_sends,
        team_b_sends=b_sends,
    )
    grade = _grade(trade, extra=[_KLAY, _ALDAMA])
    assert 45 <= grade.team_a_grade.score <= 55
    assert 45 <= grade.team_b_grade.score <= 55
