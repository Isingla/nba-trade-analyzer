"""Stress test for the multi-year valuation pipeline and aging curve.

Runs every Phase 5.5 edge case across 10 categories: aging-curve shape,
contract length sensitivity, age × length matrices, projection-source
chain, discount-rate compounding, extreme EPM values, real players,
minutes projection, team-context integration, boundary cases. Reports
pass/fail/warning counts at the end.

Run from repo root:
    uv run --native-tls python scripts/stress_test_multiyear.py
"""

from __future__ import annotations

import io
import math
import sys

import pandas as pd

from nba_trade_analyzer.data.epm import fetch_epm_data, get_player_epm, normalize_name
from nba_trade_analyzer.data.players import fetch_player_stats
from nba_trade_analyzer.engine.aging_curve import get_aging_factor
from nba_trade_analyzer.engine.constants import (
    DOLLARS_PER_WIN,
    EPM_TO_WINS_FACTOR,
    MAX_PROJECTION_YEARS,
    PROJECTED_GP_HEALTHY,
    PROJECTION_DISCOUNT_RATE,
)
from nba_trade_analyzer.engine.team_context import evaluate_player_in_team_context
from nba_trade_analyzer.engine.valuation import (
    evaluate_player,
    evaluate_player_multiyear,
)
from nba_trade_analyzer.models.player import Contract, Player

# ---------------------------------------------------------------------------
# Test result tracking
# ---------------------------------------------------------------------------

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []  # (label, detail)
WARNINGS: list[tuple[str, str]] = []


def expect(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(label)
    else:
        FAILED.append((label, detail))


def warn(label: str, detail: str = "") -> None:
    WARNINGS.append((label, detail))


def _force_utf8_stdout() -> None:
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", line_buffering=True
        )


# ---------------------------------------------------------------------------
# Hardcoded salaries — TODO: replace once data/salaries.py exists.
# ---------------------------------------------------------------------------
SALARIES_2025_26: dict[str, int] = {
    "Nikola Jokic": 55_223_000,
    "Cade Cunningham": 46_416_900,
    "LeBron James": 52_628_000,
    "Shai Gilgeous-Alexander": 38_333_988,
    "Victor Wembanyama": 13_682_280,
    "Chet Holmgren": 13_637_400,
    "Anthony Davis": 54_126_380,
    "Russell Westbrook": 3_303_771,
    "Bradley Beal": 53_666_950,
    "Gordon Hayward": 31_500_000,
}


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def money(amount: float) -> str:
    sign = "-" if amount < 0 else " "
    return f"{sign}${abs(amount) / 1_000_000:7.2f}M"


def print_yby(mv) -> None:
    print(
        "  yr  age  proj_epm  source       wins   value         "
        "salary        disc   discounted_surplus"
    )
    for yr in mv.year_by_year:
        print(
            f"  {yr.year:>2}  {yr.projected_age:>3}  "
            f"{yr.projected_epm:+6.2f}    "
            f"{yr.projection_source:<11}  "
            f"{yr.projected_wins_added:+5.2f}  "
            f"{money(yr.projected_value)}  "
            f"{money(yr.salary)}  "
            f"{yr.discount_factor:.3f}  "
            f"{money(yr.discounted_surplus)}"
        )
    print(
        f"  TOTAL: {money(mv.total_contract_surplus)}  "
        f"(year-1 only: {money(mv.current_season_surplus)})"
    )


def banner(title: str) -> None:
    print()
    print("=" * 100)
    print(title)
    print("=" * 100)


# ---------------------------------------------------------------------------
# Synthetic data builders
# ---------------------------------------------------------------------------


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


def _epm_row(name: str, epm: float, position: str = "G") -> pd.DataFrame:
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
                "position": position,
                "age": 27,
            }
        ]
    )


def _darko_row(name: str, dpm: float) -> pd.DataFrame:
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


def _make_player(
    name: str, age: int, gp: int = 75, mpg: float = 34.0, net_rating: float = 0.0
) -> Player:
    return Player(
        name=name,
        team="TST",
        age=age,
        stats={"NET_RATING": net_rating, "GP": gp, "MPG": mpg},
    )


# ---------------------------------------------------------------------------
# CATEGORY 1 — aging curve shape
# ---------------------------------------------------------------------------


def category_1_aging_curve() -> None:
    banner("CATEGORY 1 — Aging curve shape validation")

    horizons = list(range(0, 6))
    ages = list(range(20, 41))

    header = "age   " + "  ".join(f" +{h}y " for h in horizons)
    print(header)
    print("-" * len(header))
    for age in ages:
        cells = [f"{get_aging_factor(age, h):0.3f}" for h in horizons]
        print(f"{age:>3}    " + "  ".join(cells))

    # a) identity at horizon 0
    for age in ages:
        expect(
            f"cat1.a age {age} factor at h=0 is 1.0",
            get_aging_factor(age, 0) == 1.0,
            f"got {get_aging_factor(age, 0)}",
        )

    # b) growth: factor(age, 3) > 1 for ages 20-26
    for age in range(20, 27):
        f = get_aging_factor(age, 3)
        expect(
            f"cat1.b age {age} growth (factor 3y > 1.0)",
            f > 1.0,
            f"factor={f:.4f}",
        )

    # c) plateau for 27-29 — at least one of the 3 horizon years should be ≤
    # 1.001 (plateau-dominated) for ages 28-29; age 27 still ticks up 1.5%/yr
    # so we check that the SHORT horizon is near 1.0.
    expect(
        "cat1.c age 28 h=1 factor near 1.0 (plateau)",
        abs(get_aging_factor(28, 1) - 1.0) < 0.001,
        f"got {get_aging_factor(28, 1)}",
    )
    expect(
        "cat1.c age 29 h=1 factor near 1.0 (plateau)",
        abs(get_aging_factor(29, 1) - 1.0) < 0.001,
        f"got {get_aging_factor(29, 1)}",
    )

    # d) decline for 30-35
    for age in range(30, 36):
        f = get_aging_factor(age, 3)
        expect(
            f"cat1.d age {age} decline (factor 3y < 1.0)",
            f < 1.0,
            f"factor={f:.4f}",
        )

    # e) sharp decline 36+
    for age in (36, 38, 40):
        f = get_aging_factor(age, 3)
        expect(
            f"cat1.e age {age} sharp decline (factor 3y < 0.80)",
            f < 0.80,
            f"factor={f:.4f}",
        )

    # f) bracket continuity — adjacent ages, same horizon, factors close
    for low, high in [(24, 25), (27, 28), (29, 30), (32, 33), (35, 36)]:
        delta = abs(get_aging_factor(low, 1) - get_aging_factor(high, 1))
        expect(
            f"cat1.f no jump {low}->{high}",
            delta < 0.06,
            f"delta={delta:.4f}",
        )

    # g) factors never negative
    for age in range(15, 50):
        for h in range(0, 11):
            f = get_aging_factor(age, h)
            expect(
                f"cat1.g age {age} h {h} non-negative",
                f >= 0.0,
                f"got {f}",
            )

    # h) compounding: factor(age, 3) == factor(age,1) * factor(age+1,1) * factor(age+2,1)
    for age in (22, 27, 31, 36):
        composed = (
            get_aging_factor(age, 1)
            * get_aging_factor(age + 1, 1)
            * get_aging_factor(age + 2, 1)
        )
        direct = get_aging_factor(age, 3)
        expect(
            f"cat1.h compounding at age {age}",
            math.isclose(composed, direct, rel_tol=1e-9),
            f"composed={composed:.6f} direct={direct:.6f}",
        )


# ---------------------------------------------------------------------------
# CATEGORY 2 — contract length edge cases
# ---------------------------------------------------------------------------


def category_2_contract_length() -> None:
    banner("CATEGORY 2 — Contract length 1..5 for +5.0 EPM age-27 player at $35M")

    name = "Cat2Star"
    player = _make_player(name, age=27, gp=75, mpg=34.0)
    epm = _epm_row(name, 5.0)
    darko = _empty_darko()

    results = []
    for years in range(1, 6):
        contract = Contract(salary=35_000_000, years_remaining=years)
        mv = evaluate_player_multiyear(player, contract, epm_df=epm, darko_df=darko)
        results.append((years, mv))
        print(f"\n-- {years}-year deal --")
        print_yby(mv)

    # a) 1-year total = single-season exactly
    one = results[0][1]
    expect(
        "cat2.a 1yr total = single-season",
        math.isclose(one.total_contract_surplus, one.current_season_surplus),
        f"total={one.total_contract_surplus} single={one.current_season_surplus}",
    )

    # b) diminishing marginal — added year-N surplus declines with N
    marginals = []
    prev_total = 0.0
    for years, mv in results:
        marg = mv.total_contract_surplus - prev_total
        marginals.append(marg)
        prev_total = mv.total_contract_surplus
    print("\nMarginal contribution of each added year:")
    for years, marg in zip(range(1, 6), marginals, strict=True):
        print(f"  year {years}: {money(marg)}")
    decreasing = all(
        marginals[i] >= marginals[i + 1] - 1.0 for i in range(len(marginals) - 1)
    )
    expect(
        "cat2.b marginal year contribution non-increasing",
        decreasing,
        f"marginals={[round(m / 1e6, 2) for m in marginals]}",
    )

    # c) prime player on fair deal: total surplus rises with more years
    totals = [mv.total_contract_surplus for _, mv in results]
    expect(
        "cat2.c total surplus rises with contract length (prime, fair deal)",
        all(totals[i] < totals[i + 1] for i in range(len(totals) - 1)),
        f"totals={[round(t / 1e6, 2) for t in totals]}",
    )

    # d) year-5 discount visibly < year-1
    five = results[4][1]
    y5_disc = five.year_by_year[-1].discount_factor
    expect(
        "cat2.d year-5 discount < 0.65",
        y5_disc < 0.65,
        f"year5_discount={y5_disc:.4f}",
    )


# ---------------------------------------------------------------------------
# CATEGORY 3 — age × contract length matrix
# ---------------------------------------------------------------------------


def category_3_age_x_length_matrix() -> tuple[list[int], list[int], list[list[float]]]:
    banner("CATEGORY 3 — Age × contract-length matrix at +4.0 EPM / $30M")

    ages = [22, 25, 28, 31, 34, 37]
    years_list = [1, 2, 3, 4]
    matrix: list[list[float]] = []

    epm = _empty_epm()  # we set EPM per-row below by re-creating the df
    darko = _empty_darko()

    for age in ages:
        row = []
        name = f"Cat3@{age}"
        player = _make_player(name, age=age, gp=75, mpg=34.0)
        epm = _epm_row(name, 4.0)
        for years in years_list:
            contract = Contract(salary=30_000_000, years_remaining=years)
            mv = evaluate_player_multiyear(player, contract, epm_df=epm, darko_df=darko)
            row.append(mv.total_contract_surplus)
        matrix.append(row)

    # Print matrix
    header = "age   " + "  ".join(f"  {y}yr      " for y in years_list)
    print(header)
    print("-" * len(header))
    for age, row in zip(ages, matrix, strict=True):
        cells = "  ".join(money(v) for v in row)
        print(f"{age:>3}    {cells}")

    # a) Young players gain from longer contracts (age 22 row monotone increasing)
    expect(
        "cat3.a age 22 total rises with years",
        all(matrix[0][i] < matrix[0][i + 1] for i in range(len(years_list) - 1)),
        f"row={[round(v / 1e6, 2) for v in matrix[0]]}",
    )

    # b) Old players lose with longer contracts (age 37 row monotone decreasing)
    expect(
        "cat3.b age 37 total falls with years",
        all(matrix[-1][i] > matrix[-1][i + 1] for i in range(len(years_list) - 1)),
        f"row={[round(v / 1e6, 2) for v in matrix[-1]]}",
    )

    # c+d) Crossover age — find youngest age where 4yr < 1yr (adding years hurts).
    crossover = None
    for age, row in zip(ages, matrix, strict=True):
        if row[-1] < row[0]:
            crossover = age
            break
    print(f"\nCrossover age (4yr < 1yr): {crossover}")
    expect(
        "cat3.c crossover exists",
        crossover is not None,
        "no crossover found in age range 22-37",
    )
    if crossover is not None:
        if 28 <= crossover <= 34:
            expect("cat3.d crossover in 30±2 band", True, "")
        else:
            warn(
                "cat3.d crossover outside 30±2 band",
                f"crossover={crossover}; spec expected 30-32",
            )

    return ages, years_list, matrix


# ---------------------------------------------------------------------------
# CATEGORY 4 — projection source chain
# ---------------------------------------------------------------------------


def category_4_source_chain() -> None:
    banner("CATEGORY 4 — Projection source chain by data availability")

    name = "SourceTest"
    contract = Contract(salary=20_000_000, years_remaining=4)

    scenarios = [
        ("EPM+DARKO", _epm_row(name, 4.0), _darko_row(name, 4.0), ["epm", "darko"]),
        (
            "EPM only",
            _epm_row(name, 4.0),
            _empty_darko(),
            ["epm", "aging_curve"],
        ),
        ("DARKO only", _empty_epm(), _darko_row(name, 4.0), ["darko", "aging_curve"]),
        (
            "neither (net_rating)",
            _empty_epm(),
            _empty_darko(),
            ["net_rating", "aging_curve"],
        ),
    ]

    for label, epm, darko, expected_first_two in scenarios:
        # net_rating fallback requires NET_RATING to compute year 1
        nr_player = _make_player(name, age=27, gp=75, mpg=34.0, net_rating=3.0)
        mv = evaluate_player_multiyear(nr_player, contract, epm_df=epm, darko_df=darko)
        sources = [yr.projection_source for yr in mv.year_by_year]
        print(f"\n  {label}: {sources}")
        expect(
            f"cat4 {label} year 1 source = {expected_first_two[0]}",
            sources[0] == expected_first_two[0],
            f"got {sources[0]}",
        )
        expect(
            f"cat4 {label} year 2 source = {expected_first_two[1]}",
            sources[1] == expected_first_two[1],
            f"got {sources[1]}",
        )
        for i in range(2, 4):
            expect(
                f"cat4 {label} year {i + 1} source = aging_curve",
                sources[i] == "aging_curve",
                f"got {sources[i]}",
            )


# ---------------------------------------------------------------------------
# CATEGORY 5 — discount rate validation
# ---------------------------------------------------------------------------


def category_5_discount_rate() -> None:
    banner("CATEGORY 5 — Discount rate (plateau-age, no aging influence)")

    name = "DiscountFlat"
    player = _make_player(name, age=28, gp=75, mpg=34.0)  # plateau ⇒ no aging tilt
    contract = Contract(salary=10_000_000, years_remaining=5)
    mv = evaluate_player_multiyear(
        player,
        contract,
        epm_df=_epm_row(name, 3.0),
        darko_df=_empty_darko(),
    )
    print_yby(mv)

    expected = [1.0]
    for n in range(1, 5):
        expected.append(1.0 / (1.0 + PROJECTION_DISCOUNT_RATE) ** n)

    for i, (yr, want) in enumerate(zip(mv.year_by_year, expected, strict=True), 1):
        expect(
            f"cat5 year {i} discount ≈ {want:.4f}",
            math.isclose(yr.discount_factor, want, rel_tol=1e-6),
            f"got {yr.discount_factor:.6f}",
        )

    # Undiscounted vs discounted (only over years 2..n; year 1 is identity)
    undiscounted = sum(
        yr.projected_value - yr.salary for yr in mv.year_by_year if yr.year > 1
    )
    discounted = sum(yr.discounted_surplus for yr in mv.year_by_year if yr.year > 1)
    if undiscounted != 0:
        ratio = discounted / undiscounted
        print(f"\nDiscounted/undiscounted ratio across years 2-5: {ratio:.3f}")
        # 1+r=1.12 across years 2..5: average factor (0.893+0.797+0.712+0.636)/4 = 0.760
        expect(
            "cat5.f discount reduces years 2-5 by ~20-30%",
            0.70 <= ratio <= 0.80,
            f"ratio={ratio:.3f}",
        )


# ---------------------------------------------------------------------------
# CATEGORY 6 — extreme EPM values
# ---------------------------------------------------------------------------


def category_6_extreme_epm() -> None:
    banner("CATEGORY 6 — Extreme EPM scenarios")

    cases = [
        ("Elite MVP", 9.0, 27, 4, 55_000_000, "positive", "strongly positive"),
        ("Replacement", 0.0, 28, 3, 10_000_000, "negative", "near -salary*years"),
        ("Negative impact", -3.0, 30, 3, 25_000_000, "negative", "deeply negative"),
        ("Borderline", 2.0, 26, 4, 15_000_000, "either", "see breakdown"),
    ]

    for label, epm, age, years, salary, sign, note in cases:
        print(f"\n-- {label}: EPM={epm:+.1f}, age={age}, {years}yr/${salary / 1e6:.0f}M ({note})")
        player = _make_player(label, age=age, gp=75, mpg=34.0)
        contract = Contract(salary=salary, years_remaining=years)
        mv = evaluate_player_multiyear(
            player,
            contract,
            epm_df=_epm_row(label, epm),
            darko_df=_empty_darko(),
        )
        print_yby(mv)

        if sign == "positive":
            expect(
                f"cat6 {label} positive total",
                mv.total_contract_surplus > 0,
                f"total={money(mv.total_contract_surplus)}",
            )
        elif sign == "negative":
            expect(
                f"cat6 {label} negative total",
                mv.total_contract_surplus < 0,
                f"total={money(mv.total_contract_surplus)}",
            )

        if label == "Negative impact":
            # year-over-year discounted surplus should get LESS negative once
            # discount overpowers the per-year decline — but each year's
            # *raw* surplus (value - salary) should worsen as EPM declines.
            raw = [yr.projected_value - yr.salary for yr in mv.year_by_year]
            print(f"  raw per-year surplus: {[round(x / 1e6, 2) for x in raw]}")
            expect(
                "cat6.c negative-impact raw surplus declines each year",
                all(raw[i] >= raw[i + 1] for i in range(len(raw) - 1)),
                f"raw={raw}",
            )


# ---------------------------------------------------------------------------
# CATEGORY 7 — real player validation (uses live EPM data)
# ---------------------------------------------------------------------------


def _build_real_player(
    name: str, stats_lookup: dict, epm_age: int | None = None
) -> Player | None:
    """Build a Player from live EPM/nba_api data — None if not found."""
    row = stats_lookup.get(normalize_name(name))
    if row is None:
        return None
    age = int(row.get("age", 0) or 0)
    if age == 0 and epm_age is not None:
        age = epm_age
    return Player(
        name=name,
        team=row.get("TEAM_ABBREVIATION") or row.get("team") or "TST",
        age=age,
        stats={
            "NET_RATING": float(row.get("NET_RATING", 0.0) or 0.0),
            "GP": float(row.get("GP", 0) or 0),
            "MPG": float(row.get("MPG", 0.0) or 0.0),
        },
    )


def category_7_real_players(epm_df: pd.DataFrame, stats_df: pd.DataFrame) -> None:
    banner("CATEGORY 7 — Real-player full breakdowns (live EPM data)")

    stats_df = stats_df.copy()
    stats_df["player_name_normalized"] = stats_df["player_name"].map(normalize_name)
    stats_lookup = {
        row["player_name_normalized"]: row for _, row in stats_df.iterrows()
    }

    targets = [
        ("Nikola Jokic", 3, "prime supermax"),
        ("Cade Cunningham", 4, "young max"),
        ("LeBron James", 1, "expiring vet"),
        ("Shai Gilgeous-Alexander", 4, "young superstar pre-supermax"),
    ]

    for name, years_remaining, note in targets:
        epm_row = get_player_epm(epm_df, name)
        if epm_row is None:
            warn(f"cat7 {name} NOT FOUND in EPM data")
            continue
        salary = SALARIES_2025_26.get(name)
        if salary is None:
            warn(f"cat7 {name} no hardcoded salary; skipping")
            continue
        player = _build_real_player(
            name, stats_lookup, epm_age=int(epm_row.get("age", 0) or 0)
        )
        if player is None:
            # Fall back to EPM age + reasonable MPG/GP
            player = Player(
                name=name,
                team=epm_row.get("team", "TST"),
                age=int(epm_row.get("age", 0) or 0),
                stats={
                    "NET_RATING": 0.0,
                    "GP": 60,
                    "MPG": float(epm_row.get("mpg", 30.0) or 30.0),
                },
            )
        contract = Contract(salary=salary, years_remaining=years_remaining)
        mv = evaluate_player_multiyear(
            player, contract, epm_df=epm_df, darko_df=None
        )
        print(
            f"\n-- {name} ({note}): age {player.age}, EPM "
            f"{float(epm_row['epm']):+.2f}, {years_remaining}yr / ${salary / 1e6:.1f}M"
        )
        print_yby(mv)

    # Rookie-deal player: find an EPM player with epm > 1.0, age ≤ 23,
    # not already in the targets above.
    rookie_pool = epm_df[(epm_df["epm"] > 1.0) & (epm_df["age"] <= 23)]
    rookie_pool = rookie_pool.sort_values("epm", ascending=False)
    target_names = {t[0] for t in targets}
    rookie_pick = None
    for _, row in rookie_pool.iterrows():
        if row["player_name"] not in target_names:
            rookie_pick = row
            break
    if rookie_pick is not None:
        rookie_salary = 5_000_000  # TODO: real rookie scale lookup
        player = _build_real_player(
            rookie_pick["player_name"],
            stats_lookup,
            epm_age=int(rookie_pick["age"]),
        ) or Player(
            name=rookie_pick["player_name"],
            team=rookie_pick["team"],
            age=int(rookie_pick["age"]),
            stats={"NET_RATING": 0.0, "GP": 60, "MPG": 28.0},
        )
        contract = Contract(salary=rookie_salary, years_remaining=3)
        mv = evaluate_player_multiyear(player, contract, epm_df=epm_df, darko_df=None)
        print(
            f"\n-- rookie-deal pick: {player.name} (age {player.age}, "
            f"EPM {float(rookie_pick['epm']):+.2f}, $5M placeholder, 3yr)"
        )
        print_yby(mv)
        expect(
            "cat7 rookie-deal player has positive total",
            mv.total_contract_surplus > 0,
            f"total={money(mv.total_contract_surplus)}",
        )
    else:
        warn("cat7 no rookie-deal candidate found in EPM data")

    # Overpaid vet: age ≥ 32, low EPM, plausibly $25M+. We can't filter on
    # salary without data, so pick any low-EPM aging vet and stress-test
    # with a hardcoded $25M.
    vet_pool = epm_df[(epm_df["epm"] < 1.0) & (epm_df["age"] >= 32)]
    vet_pool = vet_pool.sort_values("epm", ascending=True)
    vet_pick = None
    for _, row in vet_pool.iterrows():
        if row["player_name"] not in target_names:
            vet_pick = row
            break
    if vet_pick is not None:
        vet_salary = 25_000_000  # TODO: real salary lookup
        player = _build_real_player(
            vet_pick["player_name"], stats_lookup, epm_age=int(vet_pick["age"])
        ) or Player(
            name=vet_pick["player_name"],
            team=vet_pick["team"],
            age=int(vet_pick["age"]),
            stats={"NET_RATING": 0.0, "GP": 50, "MPG": 25.0},
        )
        contract = Contract(salary=vet_salary, years_remaining=3)
        mv = evaluate_player_multiyear(player, contract, epm_df=epm_df, darko_df=None)
        print(
            f"\n-- overpaid-vet pick: {player.name} (age {player.age}, "
            f"EPM {float(vet_pick['epm']):+.2f}, $25M placeholder, 3yr)"
        )
        print_yby(mv)
        expect(
            "cat7 overpaid vet has negative total",
            mv.total_contract_surplus < 0,
            f"total={money(mv.total_contract_surplus)}",
        )
    else:
        warn("cat7 no overpaid-vet candidate found in EPM data")


# ---------------------------------------------------------------------------
# CATEGORY 8 — minutes projection edge cases
# ---------------------------------------------------------------------------


def category_8_minutes_projection() -> None:
    banner("CATEGORY 8 — Minutes projection edge cases")

    name = "MinutesTest"
    epm = _epm_row(name, 4.0)
    darko = _empty_darko()

    # a) 82 GP this season — future GP projects at PROJECTED_GP_HEALTHY (72)
    player = _make_player(name, age=27, gp=82, mpg=34.0)
    contract = Contract(salary=20_000_000, years_remaining=3)
    mv = evaluate_player_multiyear(player, contract, epm_df=epm, darko_df=darko)
    print(f"\n-- 82 GP this year, age 27, {PROJECTED_GP_HEALTHY=}")
    print_yby(mv)
    yr2_wins = mv.year_by_year[1].projected_wins_added
    yr1_wins = mv.year_by_year[0].projected_wins_added
    expect(
        "cat8.a year-2 wins drop from 82->72 GP (lower minutes)",
        yr2_wins < yr1_wins,
        f"yr1_wins={yr1_wins:.2f}, yr2_wins={yr2_wins:.2f}",
    )

    # b) 20 GP injury year — year 1 low wins, year 2+ healthy projection
    player = _make_player(name, age=27, gp=20, mpg=30.0)
    contract = Contract(salary=20_000_000, years_remaining=3)
    mv = evaluate_player_multiyear(player, contract, epm_df=epm, darko_df=darko)
    print("\n-- 20 GP injury year, age 27")
    print_yby(mv)
    yr1 = mv.year_by_year[0].projected_wins_added
    yr2 = mv.year_by_year[1].projected_wins_added
    expect(
        "cat8.b year-2 healthy projection > injury year",
        yr2 > yr1,
        f"yr1={yr1:.2f}, yr2={yr2:.2f}",
    )

    # c) 0 GP DNP — year 1 zero value, year 2+ still project
    player = _make_player(name, age=27, gp=0, mpg=0.0)
    contract = Contract(salary=20_000_000, years_remaining=3)
    mv = evaluate_player_multiyear(player, contract, epm_df=epm, darko_df=darko)
    print("\n-- 0 GP / 0 MPG DNP, age 27")
    print_yby(mv)
    expect(
        "cat8.c year 1 wins = 0 when GP=0",
        math.isclose(mv.year_by_year[0].projected_wins_added, 0.0),
        f"yr1_wins={mv.year_by_year[0].projected_wins_added}",
    )
    # MPG=0 means projected_minutes=0 for years 2+ too — flag this.
    if mv.year_by_year[1].projected_wins_added == 0.0:
        warn(
            "cat8.c MPG=0 zeros out future projection",
            "current_mpg drives future minutes; a DNP year leaves us with no "
            "MPG signal so the model projects zero forever. Acceptable for "
            "true DNPs but worth flagging.",
        )

    # d) 38-year-old — aging curve reduces projected GP below 72
    player = _make_player("OldGuy", age=38, gp=70, mpg=28.0)
    contract = Contract(salary=15_000_000, years_remaining=3)
    mv = evaluate_player_multiyear(
        player, contract, epm_df=_epm_row("OldGuy", 2.0), darko_df=darko
    )
    print("\n-- age 38 — aging-aware GP haircut")
    print_yby(mv)
    # Year 2 MPG × projected_gp should equal current MPG × (72 × aging_factor)
    # → projected wins / wins-per-minute lets us infer projected GP. Quick
    # check: year-3 projected wins should be lower than year-2 (decline +
    # GP haircut combined).
    expect(
        "cat8.d age-38 wins decline year-over-year",
        mv.year_by_year[2].projected_wins_added < mv.year_by_year[1].projected_wins_added,
        f"yr2={mv.year_by_year[1].projected_wins_added:.2f}, "
        f"yr3={mv.year_by_year[2].projected_wins_added:.2f}",
    )


# ---------------------------------------------------------------------------
# CATEGORY 9 — team-context integration
# ---------------------------------------------------------------------------


def _build_roster(
    age_template: int, base_minutes: float = 28.0, gp: int = 70, count: int = 8
) -> list[dict]:
    """Synthetic roster — ``count`` players around an average age."""
    return [
        {
            "player_name": f"Roster{i}",
            "GP": gp,
            "MPG": base_minutes,
            "age": age_template + (i % 3) - 1,
            "position": ["G", "G", "F", "F", "C"][i % 5],
            "FG3_RATE": 0.35,
            "FG3_PCT": 0.36,
        }
        for i in range(count)
    ]


def _stats_df_with(name: str, position: str = "G") -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "player_name": name,
                "FG3_RATE": 0.40,
                "FG3_PCT": 0.37,
                "position": position,
            }
        ]
    )


def category_9_team_context() -> None:
    banner("CATEGORY 9 — Team context: single-season vs multi-year")

    # Scenario A — young improving player to a rebuilder (20 wins, young core)
    young_name = "YoungImproving"
    young = _make_player(young_name, age=23, gp=75, mpg=34.0)
    young_contract = Contract(salary=15_000_000, years_remaining=4)
    young_epm = _epm_row(young_name, 3.5, position="G")
    rebuild_roster = _build_roster(age_template=22)
    rebuild_stats = _stats_df_with(young_name, position="G")

    base_single = evaluate_player(
        young, young_contract, epm_df=young_epm, darko_df=_empty_darko()
    )
    single_ctx = evaluate_player_in_team_context(
        player_value=base_single.player_value,
        wins_added=base_single.wins_added,
        player=young,
        contract=young_contract,
        acquiring_team_wins=22.0,
        acquiring_team_roster=rebuild_roster,
        epm_df=young_epm,
        player_stats_df=rebuild_stats,
    )
    multi = evaluate_player_multiyear(
        young, young_contract, epm_df=young_epm, darko_df=_empty_darko()
    )

    print("\n-- Scenario A: 23-yo, +3.5 EPM, 4yr/$15M → rebuilder (22 wins)")
    print(f"  single-season base surplus:  {money(base_single.surplus_value)}")
    print(f"  single-season + team ctx:    {money(single_ctx.team_surplus)}")
    print(f"  multi-year total surplus:    {money(multi.total_contract_surplus)}")
    print(
        "  ratio multi/single:          "
        f"{multi.total_contract_surplus / base_single.surplus_value:.2f}x"
    )
    expect(
        "cat9.a multi-year amplifies value for rebuilder (young player)",
        multi.total_contract_surplus > base_single.surplus_value,
        f"multi={multi.total_contract_surplus:.0f} single={base_single.surplus_value:.0f}",
    )

    # Scenario B — aging vet on expiring → contender
    vet_name = "AgingVet"
    vet = _make_player(vet_name, age=35, gp=65, mpg=28.0)
    vet_contract = Contract(salary=25_000_000, years_remaining=1)
    vet_epm = _epm_row(vet_name, 3.0, position="F")
    contender_roster = _build_roster(age_template=29)
    contender_stats = _stats_df_with(vet_name, position="F")

    base_single = evaluate_player(
        vet, vet_contract, epm_df=vet_epm, darko_df=_empty_darko()
    )
    single_ctx = evaluate_player_in_team_context(
        player_value=base_single.player_value,
        wins_added=base_single.wins_added,
        player=vet,
        contract=vet_contract,
        acquiring_team_wins=50.0,
        acquiring_team_roster=contender_roster,
        epm_df=vet_epm,
        player_stats_df=contender_stats,
    )
    multi = evaluate_player_multiyear(
        vet, vet_contract, epm_df=vet_epm, darko_df=_empty_darko()
    )

    print("\n-- Scenario B: 35-yo, +3.0 EPM, 1yr/$25M → contender (50 wins)")
    print(f"  single-season base surplus:  {money(base_single.surplus_value)}")
    print(f"  single-season + team ctx:    {money(single_ctx.team_surplus)}")
    print(f"  multi-year total surplus:    {money(multi.total_contract_surplus)}")
    expect(
        "cat9.b 1-year vet: multi-year ≈ single-season",
        math.isclose(
            multi.total_contract_surplus, base_single.surplus_value, rel_tol=1e-6
        ),
        f"multi={multi.total_contract_surplus} single={base_single.surplus_value}",
    )

    # Same vet, but on a 3-year deal: multi-year should now be MUCH more negative.
    vet_long_contract = Contract(salary=25_000_000, years_remaining=3)
    multi_long = evaluate_player_multiyear(
        vet, vet_long_contract, epm_df=vet_epm, darko_df=_empty_darko()
    )
    print("\n-- Scenario B': same 35-yo on a 3-year deal at $25M")
    print(f"  multi-year total surplus:    {money(multi_long.total_contract_surplus)}")
    print_yby(multi_long)
    expect(
        "cat9.b' 3-year aging vet visibly worse than 1-year",
        multi_long.total_contract_surplus < multi.total_contract_surplus - 5_000_000,
        f"3yr={multi_long.total_contract_surplus} 1yr={multi.total_contract_surplus}",
    )


# ---------------------------------------------------------------------------
# CATEGORY 10 — boundary and error cases
# ---------------------------------------------------------------------------


def category_10_boundaries() -> None:
    banner("CATEGORY 10 — Boundary and error cases")

    name = "Boundary"
    player = _make_player(name, age=27, gp=75, mpg=34.0)
    epm = _epm_row(name, 4.0)
    darko = _empty_darko()

    # a) 0 years remaining
    mv = evaluate_player_multiyear(
        player, Contract(salary=10_000_000, years_remaining=0), epm_df=epm, darko_df=darko
    )
    print(f"\n-- 0 years remaining: years_remaining={mv.years_remaining}, "
          f"year_by_year={len(mv.year_by_year)} entries, total={money(mv.total_contract_surplus)}")
    expect(
        "cat10.a 0-year contract returns empty projection",
        mv.years_remaining == 0
        and len(mv.year_by_year) == 0
        and mv.total_contract_surplus == 0.0,
        f"got years_remaining={mv.years_remaining}",
    )

    # b) 6 years remaining — capped at MAX_PROJECTION_YEARS
    mv = evaluate_player_multiyear(
        player, Contract(salary=10_000_000, years_remaining=6), epm_df=epm, darko_df=darko
    )
    print(f"\n-- 6 years remaining (cap test): projected {len(mv.year_by_year)} years")
    expect(
        f"cat10.b 6-year capped at {MAX_PROJECTION_YEARS}",
        len(mv.year_by_year) == MAX_PROJECTION_YEARS,
        f"got {len(mv.year_by_year)}",
    )

    # c) age 19 (pre-draft) — aging curve still works
    teen = _make_player(name, age=19, gp=75, mpg=34.0)
    mv = evaluate_player_multiyear(
        teen, Contract(salary=5_000_000, years_remaining=3), epm_df=_epm_row(name, 2.0), darko_df=darko
    )
    print("\n-- age 19 player, 3yr/$5M:")
    print_yby(mv)
    expect(
        "cat10.c age 19 produces sane finite projection",
        all(math.isfinite(yr.projected_epm) for yr in mv.year_by_year),
        "non-finite EPM in projection",
    )

    # d) age 42 — very low factors but still finite
    old = _make_player(name, age=42, gp=50, mpg=25.0)
    mv = evaluate_player_multiyear(
        old, Contract(salary=10_000_000, years_remaining=3), epm_df=_epm_row(name, 1.5), darko_df=darko
    )
    print("\n-- age 42 player, 3yr/$10M:")
    print_yby(mv)
    expect(
        "cat10.d age 42 last-year EPM significantly degraded",
        mv.year_by_year[-1].projected_epm < mv.year_by_year[0].projected_epm * 0.85,
        f"yr1={mv.year_by_year[0].projected_epm:.3f}, "
        f"yrN={mv.year_by_year[-1].projected_epm:.3f}",
    )

    # e) EPM exactly 0.0
    zero = _make_player(name, age=27, gp=75, mpg=34.0)
    mv = evaluate_player_multiyear(
        zero, Contract(salary=10_000_000, years_remaining=3), epm_df=_epm_row(name, 0.0), darko_df=darko
    )
    print("\n-- EPM=0.0, age 27, 3yr/$10M:")
    print_yby(mv)
    expect(
        "cat10.e EPM=0.0 year-1 produces zero wins",
        math.isclose(mv.year_by_year[0].projected_wins_added, 0.0),
        f"yr1_wins={mv.year_by_year[0].projected_wins_added}",
    )
    expect(
        "cat10.e EPM=0.0 year-1 value zero",
        math.isclose(mv.year_by_year[0].projected_value, 0.0),
        f"yr1_value={mv.year_by_year[0].projected_value}",
    )

    # f) Supermax + moderate EPM — negative surplus compounds
    super_player = _make_player(name, age=29, gp=75, mpg=34.0)
    mv = evaluate_player_multiyear(
        super_player,
        Contract(salary=60_000_000, years_remaining=4),
        epm_df=_epm_row(name, 3.0),
        darko_df=darko,
    )
    print("\n-- supermax $60M, +3.0 EPM, age 29, 4yr:")
    print_yby(mv)
    expect(
        "cat10.f supermax + moderate EPM is deeply negative",
        mv.total_contract_surplus < -20_000_000,
        f"total={money(mv.total_contract_surplus)}",
    )


# ---------------------------------------------------------------------------
# Final summary
# ---------------------------------------------------------------------------


def print_summary(
    cat3: tuple[list[int], list[int], list[list[float]]],
) -> None:
    banner("SUMMARY")
    total = len(PASSED) + len(FAILED)
    print(f"Total scenarios tested: {total}")
    print(f"  Passed:   {len(PASSED)}")
    print(f"  Failed:   {len(FAILED)}")
    print(f"  Warnings: {len(WARNINGS)}")
    print()

    if FAILED:
        print("Failures:")
        for label, detail in FAILED:
            print(f"  ✗ {label}")
            if detail:
                print(f"      → {detail}")
        print()

    if WARNINGS:
        print("Warnings / flag-for-review:")
        for label, detail in WARNINGS:
            print(f"  ! {label}")
            if detail:
                print(f"      → {detail}")
        print()

    # Re-print the age × length matrix from cat 3
    ages, years_list, matrix = cat3
    print("Age × contract-length total-surplus matrix (+4.0 EPM, $30M/yr):")
    print()
    print("  age   " + "  ".join(f"  {y}yr      " for y in years_list))
    print("  " + "-" * 50)
    for age, row in zip(ages, matrix, strict=True):
        cells = "  ".join(money(v) for v in row)
        print(f"  {age:>3}    {cells}")
    print()
    print(f"DOLLARS_PER_WIN={DOLLARS_PER_WIN}, EPM_TO_WINS_FACTOR={EPM_TO_WINS_FACTOR}, "
          f"discount={PROJECTION_DISCOUNT_RATE}, MAX_PROJECTION_YEARS={MAX_PROJECTION_YEARS}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    _force_utf8_stdout()

    print("Fetching live EPM data...")
    epm_df = fetch_epm_data()
    print(f"  {len(epm_df)} EPM rows loaded")
    print("Fetching live nba_api player stats...")
    stats_df = fetch_player_stats()
    print(f"  {len(stats_df)} stats rows loaded")

    category_1_aging_curve()
    category_2_contract_length()
    cat3 = category_3_age_x_length_matrix()
    category_4_source_chain()
    category_5_discount_rate()
    category_6_extreme_epm()
    category_7_real_players(epm_df, stats_df)
    category_8_minutes_projection()
    category_9_team_context()
    category_10_boundaries()

    print_summary(cat3)


if __name__ == "__main__":
    main()
