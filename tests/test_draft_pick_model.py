"""Tests for the structured DraftPick model and team-aware pick valuation."""

from __future__ import annotations

import pandas as pd
import pytest
from pydantic import ValidationError

from nba_trade_analyzer.engine.constants import (
    CURRENT_SEASON_START,
    LEAGUE_MEAN_WINS,
    PICK_SWAP_VALUE_FRACTION,
    PICK_YEAR_DISCOUNT_RATE,
)
from nba_trade_analyzer.engine.draft_picks import (
    PROTECTION_DISCOUNT,
    calculate_pick_value,
    estimate_pick_position,
    evaluate_draft_pick,
)
from nba_trade_analyzer.models.draft_pick import DraftPick
from nba_trade_analyzer.models.trade import TradeAssets

# A few reusable win projections.
TANKING_WINS = 20.0
CONTENDER_WINS = 60.0


# --------------------------------------------------------------------------- #
# 1. Construction & validation
# --------------------------------------------------------------------------- #
def test_draftpick_basic_construction():
    pick = DraftPick(team="LAL", year=2027, round=1, protections="top-4 protected")
    assert pick.team == "LAL"
    assert pick.year == 2027
    assert pick.round == 1
    assert pick.protections == "top-4 protected"
    assert pick.swap is False
    assert pick.via_team is None


def test_draftpick_rejects_bad_round():
    with pytest.raises(ValidationError):
        DraftPick(team="LAL", year=2027, round=3)


def test_draftpick_rejects_out_of_range_year():
    with pytest.raises(ValidationError):
        DraftPick(team="LAL", year=2024, round=1)
    with pytest.raises(ValidationError):
        DraftPick(team="LAL", year=2040, round=1)


@pytest.mark.parametrize("bad_team", ["lal", "LA", "LALX", "L4L"])
def test_draftpick_rejects_bad_team(bad_team: str):
    with pytest.raises(ValidationError):
        DraftPick(team=bad_team, year=2027, round=1)


def test_draftpick_rejects_bad_via_team():
    with pytest.raises(ValidationError):
        DraftPick(team="LAL", year=2027, round=1, via_team="nop")


# --------------------------------------------------------------------------- #
# 2. Label generation matches the old string format
# --------------------------------------------------------------------------- #
def test_label_first_round_protected():
    pick = DraftPick(team="LAL", year=2027, round=1, protections="top-4 protected")
    assert pick.label == "2027 LAL 1st (top-4 protected)"
    assert str(pick) == "2027 LAL 1st (top-4 protected)"


def test_label_second_round_unprotected_default():
    pick = DraftPick(team="WAS", year=2026, round=2)
    assert pick.label == "2026 WAS 2nd (unprotected)"


def test_label_swap():
    pick = DraftPick(team="OKC", year=2028, round=1, swap=True)
    assert pick.label == "2028 OKC 1st swap"


def test_label_includes_via_team():
    pick = DraftPick(team="LAL", year=2027, round=1, via_team="NOP")
    assert pick.label == "2027 LAL 1st (unprotected) via NOP"


# --------------------------------------------------------------------------- #
# 3. estimate_pick_position — record drives slot
# --------------------------------------------------------------------------- #
def test_estimate_position_bad_team_picks_earlier_than_good_team():
    bad = estimate_pick_position(TANKING_WINS)
    good = estimate_pick_position(CONTENDER_WINS)
    # A tanking team picks near the top of the draft (low slot number); a
    # contender picks late (high slot number).
    assert bad < good
    assert 1.0 <= bad <= 30.0
    assert 1.0 <= good <= 30.0


def test_estimate_position_monotonic_in_wins():
    positions = [estimate_pick_position(w) for w in range(10, 70, 5)]
    assert positions == sorted(positions)


def test_contender_maps_to_late_first_round():
    # A 60-win top seed should land in the high 20s, not the low 20s — the slot
    # map spans the realistic win range, not 0-82 (which made good teams' picks
    # too early). This is the BUG-3 regression: 60 wins must be ~28-30.
    assert 28.0 <= estimate_pick_position(CONTENDER_WINS, year_offset=0) <= 30.0


def test_record_extremes_map_to_slot_extremes():
    # Worst realistic record picks 1st; best picks 30th.
    assert estimate_pick_position(15.0, year_offset=0) == pytest.approx(1.0)
    assert estimate_pick_position(62.0, year_offset=0) == pytest.approx(30.0)
    # The league-average team lands mid-first-round.
    assert 14.0 <= estimate_pick_position(LEAGUE_MEAN_WINS, year_offset=0) <= 18.0


# --------------------------------------------------------------------------- #
# 4. estimate_pick_position — future regression toward the mean
# --------------------------------------------------------------------------- #
def test_future_picks_regress_toward_mean():
    # A contender regresses down toward 41 wins → earlier (lower) future slot.
    assert estimate_pick_position(
        CONTENDER_WINS, year_offset=3
    ) < estimate_pick_position(CONTENDER_WINS, year_offset=0)
    # A tanking team regresses up → later (higher) future slot.
    assert estimate_pick_position(TANKING_WINS, year_offset=3) > estimate_pick_position(
        TANKING_WINS, year_offset=0
    )


def test_league_average_team_does_not_regress():
    # (mean - mean) * factor == 0, so position is identical across years.
    now = estimate_pick_position(LEAGUE_MEAN_WINS, year_offset=0)
    later = estimate_pick_position(LEAGUE_MEAN_WINS, year_offset=4)
    assert now == pytest.approx(later)


# --------------------------------------------------------------------------- #
# 5-9. evaluate_draft_pick
# --------------------------------------------------------------------------- #
def test_unprotected_current_year_pick_matches_curve():
    pick = DraftPick(team="WAS", year=CURRENT_SEASON_START, round=1)
    expected_slot = round(estimate_pick_position(TANKING_WINS, year_offset=0))
    assert evaluate_draft_pick(pick, TANKING_WINS) == pytest.approx(
        calculate_pick_value(expected_slot)
    )


def test_protection_discount_applied():
    unprotected = DraftPick(team="WAS", year=CURRENT_SEASON_START, round=1)
    protected = DraftPick(
        team="WAS",
        year=CURRENT_SEASON_START,
        round=1,
        protections="top-5 protected",
    )
    assert evaluate_draft_pick(protected, TANKING_WINS) == pytest.approx(
        evaluate_draft_pick(unprotected, TANKING_WINS)
        * PROTECTION_DISCOUNT["top-5 protected"]
    )


def test_unknown_protection_uses_conservative_default():
    weird = DraftPick(
        team="WAS", year=CURRENT_SEASON_START, round=1, protections="lightly protected"
    )
    unprotected = DraftPick(team="WAS", year=CURRENT_SEASON_START, round=1)
    assert evaluate_draft_pick(weird, TANKING_WINS) == pytest.approx(
        evaluate_draft_pick(unprotected, TANKING_WINS) * 0.70
    )


def test_future_year_discount_applied():
    # Use a league-average team so regression doesn't shift the slot — then the
    # only difference between years is the year discount itself.
    now = DraftPick(team="WAS", year=CURRENT_SEASON_START, round=1)
    future = DraftPick(team="WAS", year=CURRENT_SEASON_START + 3, round=1)
    expected = evaluate_draft_pick(now, LEAGUE_MEAN_WINS) * (
        1.0 / (1.0 + PICK_YEAR_DISCOUNT_RATE) ** 3
    )
    assert evaluate_draft_pick(future, LEAGUE_MEAN_WINS) == pytest.approx(expected)


def test_swap_valued_at_fraction():
    outright = DraftPick(team="OKC", year=CURRENT_SEASON_START, round=1)
    swap = DraftPick(team="OKC", year=CURRENT_SEASON_START, round=1, swap=True)
    assert evaluate_draft_pick(swap, CONTENDER_WINS) == pytest.approx(
        evaluate_draft_pick(outright, CONTENDER_WINS) * PICK_SWAP_VALUE_FRACTION
    )


def test_second_round_penalty_applies():
    first = DraftPick(team="WAS", year=CURRENT_SEASON_START, round=1)
    second = DraftPick(team="WAS", year=CURRENT_SEASON_START, round=2)
    first_value = evaluate_draft_pick(first, TANKING_WINS)
    second_value = evaluate_draft_pick(second, TANKING_WINS)
    assert second_value > 0
    # Second-round picks land in slots 31-60 with the 40% penalty — a steep drop.
    assert second_value < first_value * 0.3


# --------------------------------------------------------------------------- #
# 10. End-to-end: a tanking team's pick is worth far more than a contender's
# --------------------------------------------------------------------------- #
def test_tanking_team_pick_worth_more_than_contender_pick():
    was_pick = DraftPick(team="WAS", year=2027, round=1)
    okc_pick = DraftPick(team="OKC", year=2027, round=1)
    was_value = evaluate_draft_pick(was_pick, TANKING_WINS)
    okc_value = evaluate_draft_pick(okc_pick, CONTENDER_WINS)
    assert was_value > okc_value * 1.3


# --------------------------------------------------------------------------- #
# Integration: picks flow through evaluate_trade_assets
# --------------------------------------------------------------------------- #
def test_evaluate_trade_assets_counts_picks():
    from nba_trade_analyzer.engine.valuation import evaluate_trade_assets

    pick = DraftPick(team="WAS", year=2027, round=1)
    assets = TradeAssets(picks=[pick])
    # No stats supplied → pick falls back to a league-average team.
    total = evaluate_trade_assets(
        assets, epm_df=pd.DataFrame(), darko_df=pd.DataFrame()
    )
    assert total == pytest.approx(evaluate_draft_pick(pick, LEAGUE_MEAN_WINS))


def test_evaluate_trade_assets_pick_uses_team_record():
    from nba_trade_analyzer.engine.valuation import evaluate_trade_assets

    stats_df = pd.DataFrame(
        [
            {"team": "WAS", "GP": 50, "W": 12, "NET_RATING": -6.0},
            {"team": "OKC", "GP": 50, "W": 38, "NET_RATING": 9.0},
        ]
    )
    was_total = evaluate_trade_assets(
        TradeAssets(picks=[DraftPick(team="WAS", year=2027, round=1)]),
        epm_df=pd.DataFrame(),
        darko_df=pd.DataFrame(),
        player_stats_df=stats_df,
    )
    okc_total = evaluate_trade_assets(
        TradeAssets(picks=[DraftPick(team="OKC", year=2027, round=1)]),
        epm_df=pd.DataFrame(),
        darko_df=pd.DataFrame(),
        player_stats_df=stats_df,
    )
    assert was_total > okc_total
