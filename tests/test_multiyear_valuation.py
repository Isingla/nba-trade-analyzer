"""Tests for the multi-year contract valuation path."""

from __future__ import annotations

import pandas as pd
import pytest

from nba_trade_analyzer.engine.constants import PROJECTION_DISCOUNT_RATE
from nba_trade_analyzer.engine.valuation import (
    evaluate_player,
    evaluate_player_multiyear,
)
from nba_trade_analyzer.models.player import Contract, Player


# ----- helpers -------------------------------------------------------------


def _empty_epm() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "player_name",
            "player_name_normalized",
            "team",
            "epm",
            "epm_off",
            "epm_def",
            "mpg",
            "position",
            "age",
        ]
    )


def _empty_darko() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "player_name",
            "player_name_normalized",
            "dpm",
            "dpm_off",
            "dpm_def",
            "box_dpm_off",
            "box_dpm_def",
            "onoff_dpm_off",
            "onoff_dpm_def",
            "position",
            "age",
        ]
    )


def _epm_with(name: str, epm: float) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "player_name": name,
                "player_name_normalized": name.casefold(),
                "team": "TST",
                "epm": epm,
                "epm_off": epm * 0.6,
                "epm_def": epm * 0.4,
                "mpg": 34.0,
                "position": "G",
                "age": 27,
            }
        ]
    )


def _darko_with(name: str, dpm: float) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "player_name": name,
                "player_name_normalized": name.casefold(),
                "dpm": dpm,
                "dpm_off": dpm * 0.5,
                "dpm_def": dpm * 0.5,
                "box_dpm_off": dpm * 0.5,
                "box_dpm_def": dpm * 0.5,
                "onoff_dpm_off": dpm * 0.5,
                "onoff_dpm_def": dpm * 0.5,
                "position": "G",
                "age": 27,
            }
        ]
    )


def _make_player(name: str, age: int, mpg: float = 34.0, gp: int = 75) -> Player:
    return Player(
        name=name,
        team="TST",
        age=age,
        stats={"NET_RATING": 0.0, "GP": gp, "MPG": mpg},
    )


# ----- core scenarios ------------------------------------------------------


def test_young_player_on_rookie_deal_total_surplus_strongly_positive() -> None:
    # 23, +3.5 EPM, 4 years at $10M/yr.
    player = _make_player("Rookie Star", age=23)
    contract = Contract(salary=10_000_000, years_remaining=4)
    mv = evaluate_player_multiyear(
        player,
        contract,
        epm_df=_epm_with("Rookie Star", 3.5),
        darko_df=_empty_darko(),
    )
    assert mv.total_contract_surplus > 0
    # All four years should be net-positive — cheap salary, growth curve.
    for yr in mv.year_by_year:
        assert yr.discounted_surplus > 0


def test_cade_growth_projection_lifts_per_year_value() -> None:
    # 23, +3.75 EPM, 4 years at $46.4M — the canonical Phase 5.5 example.
    # Cade is tanh-compressed near MAX_WINS_ADDED already, so aging growth
    # adds only a few wins of headroom — not enough to flip total surplus
    # positive at a $46M salary, but enough that per-year value is visibly
    # better than the single-season snapshot.
    player = _make_player("Cade Cunningham", age=23)
    contract = Contract(salary=46_400_000, years_remaining=4)

    single = evaluate_player(
        player,
        contract,
        epm_df=_epm_with("Cade Cunningham", 3.75),
        darko_df=_empty_darko(),
    )
    mv = evaluate_player_multiyear(
        player,
        contract,
        epm_df=_epm_with("Cade Cunningham", 3.75),
        darko_df=_empty_darko(),
    )

    assert single.surplus_value < 0  # single-season: underwater
    # Growth phase: projected EPM increases year-over-year.
    assert mv.year_by_year[-1].projected_epm > mv.year_by_year[0].projected_epm
    # Multi-year total is meaningfully better than naively multiplying the
    # single-season deficit by years_remaining — discount + aging lift help.
    naive_4x = single.surplus_value * mv.years_remaining
    assert mv.total_contract_surplus > naive_4x
    # Last-year discounted surplus is closer to zero than year 1 (improving
    # raw value + steeper discount both reduce the per-year drag).
    assert mv.year_by_year[-1].discounted_surplus > mv.year_by_year[0].discounted_surplus


def test_prime_max_player_strongly_positive() -> None:
    # 28, +6.0 EPM, 3 years at $45M.
    player = _make_player("Prime Star", age=28)
    contract = Contract(salary=45_000_000, years_remaining=3)
    mv = evaluate_player_multiyear(
        player,
        contract,
        epm_df=_epm_with("Prime Star", 6.0),
        darko_df=_empty_darko(),
    )
    assert mv.total_contract_surplus > 0


def test_aging_star_on_supermax_three_year_deeply_negative() -> None:
    # 35, +3.0 EPM, 3 years at $52M.
    player = _make_player("Aging Star", age=35)
    contract = Contract(salary=52_000_000, years_remaining=3)
    mv = evaluate_player_multiyear(
        player,
        contract,
        epm_df=_epm_with("Aging Star", 3.0),
        darko_df=_empty_darko(),
    )
    assert mv.total_contract_surplus < 0


def test_aging_star_one_year_much_less_negative_than_three_year() -> None:
    # The key Phase 5.5 distinction: contract length matters.
    player = _make_player("Aging Star", age=35)
    three_year = evaluate_player_multiyear(
        player,
        Contract(salary=52_000_000, years_remaining=3),
        epm_df=_epm_with("Aging Star", 3.0),
        darko_df=_empty_darko(),
    )
    one_year = evaluate_player_multiyear(
        player,
        Contract(salary=52_000_000, years_remaining=1),
        epm_df=_epm_with("Aging Star", 3.0),
        darko_df=_empty_darko(),
    )
    assert three_year.total_contract_surplus < one_year.total_contract_surplus
    assert (
        one_year.total_contract_surplus > three_year.total_contract_surplus + 5_000_000
    )


def test_lebron_one_year_damage_capped() -> None:
    # 41, low EPM, 1 year at $52.6M — bounded to one bad year.
    player = _make_player("LeBron James", age=41)
    contract = Contract(salary=52_600_000, years_remaining=1)
    mv = evaluate_player_multiyear(
        player,
        contract,
        epm_df=_epm_with("LeBron James", 1.5),
        darko_df=_empty_darko(),
    )
    # Only one year projected.
    assert mv.years_remaining == 1
    assert len(mv.year_by_year) == 1
    # Total surplus matches single-season surplus exactly when years=1.
    assert mv.total_contract_surplus == pytest.approx(mv.current_season_surplus)


def test_young_star_new_max_growth_visible() -> None:
    # 24, +3.75 EPM (Cade-level), 4 years at $46M/yr. Like Cade, tanh
    # compression near MAX_WINS_ADDED limits how much the aging curve can
    # lift wins, so the contract doesn't fully flip positive. What we do
    # want to see: out-year EPM > year-1 EPM (model recognizes growth).
    player = _make_player("Young Star", age=24)
    contract = Contract(salary=46_000_000, years_remaining=4)
    mv = evaluate_player_multiyear(
        player,
        contract,
        epm_df=_epm_with("Young Star", 3.75),
        darko_df=_empty_darko(),
    )
    assert mv.year_by_year[-1].projected_epm > mv.year_by_year[0].projected_epm
    # Per-year average surplus is less negative than single-season alone,
    # showing the aging-lift + discount don't compound the salary deficit
    # naively across 4 years.
    avg_per_year = mv.total_contract_surplus / mv.years_remaining
    assert avg_per_year > mv.current_season_surplus


# ----- discount rate visibility -------------------------------------------


def test_discount_rate_visibly_devalues_later_years() -> None:
    player = _make_player("Test", age=27)  # plateau — no aging confound

    one_year = evaluate_player_multiyear(
        player,
        Contract(salary=20_000_000, years_remaining=1),
        epm_df=_epm_with("Test", 3.0),
        darko_df=_empty_darko(),
    )
    four_year = evaluate_player_multiyear(
        player,
        Contract(salary=20_000_000, years_remaining=4),
        epm_df=_epm_with("Test", 3.0),
        darko_df=_empty_darko(),
    )

    # The marginal year-4 contribution must be visibly less than year-1.
    # Year 4 offset = 3, compounding 1/(1+r)^3 ≈ 0.712 at r=0.12.
    assert four_year.year_by_year[3].discount_factor < 0.75
    expected_year4_discount = 1.0 / (1.0 + PROJECTION_DISCOUNT_RATE) ** 3
    assert four_year.year_by_year[3].discount_factor == pytest.approx(
        expected_year4_discount
    )
    # And the four-year total is roughly four years of similar surplus,
    # but each subsequent year is worth less per dollar.
    assert four_year.total_contract_surplus > one_year.total_contract_surplus


# ----- DARKO precedence ----------------------------------------------------


def test_year_2_uses_darko_when_available() -> None:
    player = _make_player("DarkoTest", age=27)
    epm = _epm_with("DarkoTest", 3.0)
    darko = _darko_with("DarkoTest", 4.5)  # DARKO projects upward
    mv = evaluate_player_multiyear(
        player,
        Contract(salary=20_000_000, years_remaining=3),
        epm_df=epm,
        darko_df=darko,
    )
    # Year 1 sourced from EPM, year 2 from DARKO (raw DPM — see comment in
    # evaluate_player_multiyear), year 3 aging curve.
    assert mv.year_by_year[0].projection_source == "epm"
    assert mv.year_by_year[1].projection_source == "darko"
    assert mv.year_by_year[1].projected_epm == pytest.approx(4.5)
    assert mv.year_by_year[2].projection_source == "aging_curve"


def test_year_2_falls_back_to_aging_curve_when_no_darko() -> None:
    player = _make_player("NoDarko", age=27)
    mv = evaluate_player_multiyear(
        player,
        Contract(salary=20_000_000, years_remaining=3),
        epm_df=_epm_with("NoDarko", 3.0),
        darko_df=_empty_darko(),
    )
    assert mv.year_by_year[0].projection_source == "epm"
    assert mv.year_by_year[1].projection_source == "aging_curve"
    assert mv.year_by_year[2].projection_source == "aging_curve"


# ----- structural sanity ---------------------------------------------------


def test_year_1_matches_single_season_evaluate_player() -> None:
    player = _make_player("Match", age=26)
    contract = Contract(salary=15_000_000, years_remaining=3)
    epm = _epm_with("Match", 4.0)
    darko = _empty_darko()

    single = evaluate_player(player, contract, epm_df=epm, darko_df=darko)
    mv = evaluate_player_multiyear(player, contract, epm_df=epm, darko_df=darko)

    yr1 = mv.year_by_year[0]
    assert yr1.year == 1
    assert yr1.projected_epm == pytest.approx(single.adjusted_net_rating)
    assert yr1.projected_wins_added == pytest.approx(single.wins_added)
    assert yr1.projected_value == pytest.approx(single.player_value)
    assert yr1.discounted_surplus == pytest.approx(single.surplus_value)
    assert mv.current_season_surplus == pytest.approx(single.surplus_value)


def test_zero_years_remaining_returns_empty_projection() -> None:
    player = _make_player("Expired", age=30)
    contract = Contract(salary=10_000_000, years_remaining=0)
    mv = evaluate_player_multiyear(
        player,
        contract,
        epm_df=_epm_with("Expired", 2.0),
        darko_df=_empty_darko(),
    )
    assert mv.years_remaining == 0
    assert mv.year_by_year == []
    assert mv.total_contract_surplus == 0.0


def test_horizon_capped_at_max_projection_years() -> None:
    # A 7-year contract should still only project MAX_PROJECTION_YEARS=5.
    player = _make_player("LongDeal", age=24)
    contract = Contract(salary=20_000_000, years_remaining=7)
    mv = evaluate_player_multiyear(
        player,
        contract,
        epm_df=_epm_with("LongDeal", 3.5),
        darko_df=_empty_darko(),
    )
    assert mv.years_remaining == 5
    assert len(mv.year_by_year) == 5


# ----- diagnostic breakdowns ----------------------------------------------


def test_print_cade_breakdown(capsys: pytest.CaptureFixture[str]) -> None:
    """Eyeball Cade's year-by-year trajectory."""
    player = _make_player("Cade Cunningham", age=23)
    contract = Contract(salary=46_400_000, years_remaining=4)
    mv = evaluate_player_multiyear(
        player,
        contract,
        epm_df=_epm_with("Cade Cunningham", 3.75),
        darko_df=_empty_darko(),
    )
    print()
    print(
        f"Cade Cunningham — age {mv.current_age}, {mv.years_remaining}yr / "
        f"${contract.salary / 1e6:.1f}M"
    )
    print(
        "year  age  proj_epm  proj_wins  proj_value     "
        "salary         disc    discounted_surplus  source"
    )
    for yr in mv.year_by_year:
        print(
            f"  {yr.year:>2}  {yr.projected_age:>3}  {yr.projected_epm:+.2f}     "
            f"{yr.projected_wins_added:+.2f}      "
            f"${yr.projected_value / 1e6:>6.2f}M     "
            f"${yr.salary / 1e6:>6.2f}M     "
            f"{yr.discount_factor:.3f}    "
            f"${yr.discounted_surplus / 1e6:+.2f}M           "
            f"{yr.projection_source}"
        )
    print(f"  TOTAL: ${mv.total_contract_surplus / 1e6:+.2f}M")
    captured = capsys.readouterr()
    assert "Cade Cunningham" in captured.out


def test_print_lebron_breakdown(capsys: pytest.CaptureFixture[str]) -> None:
    """Eyeball LeBron — should be a single bad year, bounded."""
    player = _make_player("LeBron James", age=41)
    contract = Contract(salary=52_600_000, years_remaining=1)
    mv = evaluate_player_multiyear(
        player,
        contract,
        epm_df=_epm_with("LeBron James", 1.5),
        darko_df=_empty_darko(),
    )
    print()
    print(
        f"LeBron James — age {mv.current_age}, {mv.years_remaining}yr / "
        f"${contract.salary / 1e6:.1f}M"
    )
    print(
        "year  age  proj_epm  proj_wins  proj_value     "
        "salary         disc    discounted_surplus  source"
    )
    for yr in mv.year_by_year:
        print(
            f"  {yr.year:>2}  {yr.projected_age:>3}  {yr.projected_epm:+.2f}     "
            f"{yr.projected_wins_added:+.2f}      "
            f"${yr.projected_value / 1e6:>6.2f}M     "
            f"${yr.salary / 1e6:>6.2f}M     "
            f"{yr.discount_factor:.3f}    "
            f"${yr.discounted_surplus / 1e6:+.2f}M           "
            f"{yr.projection_source}"
        )
    print(f"  TOTAL: ${mv.total_contract_surplus / 1e6:+.2f}M")
    captured = capsys.readouterr()
    assert "LeBron James" in captured.out
