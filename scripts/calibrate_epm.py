"""EPM_TO_WINS_FACTOR calibration helper.

Fetches live EPM and nba_api player stats, runs the full valuation pipeline
on the top 30 players by EPM, and prints a diagnostic table showing both
the pre-tanh raw wins and the post-tanh compressed wins. Compare the
``tanh_wins_added`` column against the "Estimated Wins" column on
dunksandthrees.com/epm — if our numbers diverge consistently from theirs
for the top ~20 players, adjust ``EPM_TO_WINS_FACTOR`` in
``engine/constants.py``.

Run from repo root: ``uv run python scripts/calibrate_epm.py``
"""

from __future__ import annotations

import math

from nba_trade_analyzer.data.epm import fetch_epm_data, get_player_epm, normalize_name
from nba_trade_analyzer.data.players import fetch_player_stats
from nba_trade_analyzer.data.salaries import fetch_all_salaries, get_player_salary
from nba_trade_analyzer.engine.constants import (
    DOLLARS_PER_WIN,
    EPM_TO_WINS_FACTOR,
    FULL_SEASON_MINUTES,
    MAX_WINS_ADDED,
)
from nba_trade_analyzer.engine.valuation import (
    evaluate_player,
    evaluate_player_multiyear,
)
from nba_trade_analyzer.models.player import Contract, Player

# ---------------------------------------------------------------------------
# FALLBACK 2025-26 SALARIES. The live source is now data/salaries.py (scraped
# from Basketball Reference, with a committed CSV offline fallback); this dict
# is only consulted for players the source doesn't return, and the placeholder
# below covers anyone in neither. Kept for backward-compatibility during the
# transition. Approximate figures from public trackers (Spotrac / HoopsHype).
# ---------------------------------------------------------------------------
SALARIES_2025_26: dict[str, int] = {
    # Supermax / 35% max
    "Nikola Jokic": 55_223_000,
    "Joel Embiid": 55_223_000,
    "Stephen Curry": 59_606_817,
    "Giannis Antetokounmpo": 54_126_380,
    "Jayson Tatum": 54_126_380,
    "Damian Lillard": 54_126_380,
    "Anthony Davis": 54_126_380,
    "Kevin Durant": 54_708_608,
    "LeBron James": 52_628_000,
    "Bradley Beal": 53_666_950,
    "Devin Booker": 53_142_264,
    "Karl-Anthony Towns": 53_142_264,
    "Jaylen Brown": 53_142_264,
    "Kawhi Leonard": 49_822_500,
    "Paul George": 49_205_800,
    # 30% max
    "Luka Doncic": 45_999_120,
    "Trae Young": 45_999_120,
    "Donovan Mitchell": 46_416_900,
    "Cade Cunningham": 46_416_900,
    "Tyrese Haliburton": 42_176_400,
    "Scottie Barnes": 46_416_900,
    "Evan Mobley": 46_416_900,
    "Anthony Edwards": 42_176_400,
    "Shai Gilgeous-Alexander": 38_333_988,
    "OG Anunoby": 39_553_840,
    "Franz Wagner": 38_667_000,
    # Mid/large veterans
    "Jalen Brunson": 34_897_959,
    "James Harden": 36_350_000,
    "Bam Adebayo": 37_096_400,
    "De'Aaron Fox": 37_096_400,
    "Jamal Murray": 36_016_200,
    "LaMelo Ball": 35_910_000,
    "Jrue Holiday": 32_400_000,
    "Isaiah Hartenstein": 28_500_000,
    "Jarrett Allen": 20_000_000,
    "Derrick White": 18_357_750,
    "Mitchell Robinson": 15_000_000,
    "Herb Jones": 4_500_000,  # per task spec — pre-extension figure
    # Rookie scale
    "Victor Wembanyama": 13_682_280,
    "Chet Holmgren": 13_637_400,
    "Ty Jerome": 2_500_000,
}

# Validation targets called out in the phase 1.5 task. Salaries match the
# numbers in the task description verbatim — do not adjust without updating
# the task spec too. ``years_remaining`` was added in Phase 5.5 so we can
# print multi-year contract surplus next to the single-season number.
VALIDATION_TARGETS: list[tuple[str, int, int, str]] = [
    ("Nikola Jokic", 55_200_000, 3, "should be positive surplus"),
    ("LeBron James", 52_600_000, 1, "should be less extreme than -$28.4M"),
    ("Cade Cunningham", 46_400_000, 4, "single-year negative, multi-year positive"),
    ("Jalen Brunson", 34_900_000, 3, "should be ~+$10M+ surplus"),
    ("Herb Jones", 4_500_000, 3, "should show meaningful positive value"),
    ("Derrick White", 18_000_000, 3, "should show positive value"),
]

PLACEHOLDER_SALARY = 5_000_000  # used for any top-30 player missing everywhere


def _resolve_salary(salary_df, name: str) -> tuple[int, bool]:
    """Return ``(salary, found)`` preferring the live source, then the
    hardcoded fallback dict. ``found`` is False only when both miss (the
    caller then uses ``PLACEHOLDER_SALARY`` and reports the gap)."""
    row = get_player_salary(salary_df, name)
    if row is not None:
        return int(row["salary"]), True
    fallback = SALARIES_2025_26.get(name)
    if fallback is not None:
        return fallback, True
    return PLACEHOLDER_SALARY, False


def _build_player(name: str, team: str, age: int, stats_row) -> Player:
    return Player(
        name=name,
        team=team,
        age=int(age) if age else 0,
        stats={
            "NET_RATING": float(stats_row.get("NET_RATING", 0.0) or 0.0),
            "GP": float(stats_row.get("GP", 0) or 0),
            "MPG": float(stats_row.get("MPG", 0.0) or 0.0),
        },
    )


def _format_row(values: list, widths: list[int]) -> str:
    return "  ".join(str(v).ljust(w) for v, w in zip(values, widths, strict=True))


def _money(amount: float) -> str:
    sign = "-" if amount < 0 else " "
    return f"{sign}${abs(amount) / 1_000_000:6.2f}M"


def main() -> None:
    print("Fetching EPM table from dunksandthrees.com...")
    epm_df = fetch_epm_data()
    print(f"  {len(epm_df)} EPM rows loaded")

    print("Fetching player stats from nba_api...")
    stats_df = fetch_player_stats()
    print(f"  {len(stats_df)} player-stat rows loaded")

    print("Fetching salaries from Basketball Reference...")
    salary_df = fetch_all_salaries()
    print(f"  {len(salary_df)} contracts loaded\n")

    # Build a normalized-name lookup into the nba_api stats frame so we can
    # match EPM rows (which may have diacritics) to their GP/MPG.
    stats_df = stats_df.copy()
    stats_df["player_name_normalized"] = stats_df["player_name"].map(normalize_name)
    stats_lookup = {
        row["player_name_normalized"]: row for _, row in stats_df.iterrows()
    }

    top30 = epm_df.sort_values("epm", ascending=False).head(30)

    headers = [
        "player_name",
        "team",
        "EPM",
        "GP",
        "MPG",
        "min_frac",
        "raw_wins",
        "tanh_wins",
        "player_value",
        "salary",
        "surplus_value",
        "metric_source",
    ]
    widths = [24, 4, 6, 4, 5, 7, 8, 9, 12, 11, 13, 13]

    rows = []
    missing_salaries: list[str] = []

    for _, epm_row in top30.iterrows():
        name = epm_row["player_name"]
        team = epm_row["team"]
        epm = float(epm_row["epm"])

        stats_row = stats_lookup.get(normalize_name(name))
        if stats_row is None:
            gp, mpg, age = 0, 0.0, 0
        else:
            gp = int(stats_row.get("GP", 0) or 0)
            mpg = float(stats_row.get("MPG", 0.0) or 0.0)
            age = int(stats_row.get("age", 0) or 0)

        minutes_played = gp * mpg
        minutes_fraction = minutes_played / FULL_SEASON_MINUTES
        raw_wins = epm * minutes_fraction * EPM_TO_WINS_FACTOR
        # tanh_wins should equal evaluate_player(...).wins_added — we compute it
        # here too just to surface both in the same row, but the value used for
        # player_value and surplus comes from the real pipeline below.
        tanh_wins_expected = MAX_WINS_ADDED * math.tanh(raw_wins / MAX_WINS_ADDED)

        salary, found = _resolve_salary(salary_df, name)
        if not found:
            missing_salaries.append(name)

        player = _build_player(
            name, team, age, stats_row if stats_row is not None else {}
        )
        contract = Contract(salary=salary, years_remaining=1)

        valuation = evaluate_player(
            player,
            contract,
            epm_df=epm_df,
            darko_df=None,  # we want the EPM path; DARKO never queried since EPM hits
        )

        # Sanity check that our inline tanh math matches the pipeline output.
        assert abs(valuation.wins_added - tanh_wins_expected) < 1e-6, (
            f"tanh divergence for {name}"
        )

        rows.append(
            {
                "player_name": name,
                "team": team,
                "EPM": f"{epm:+.2f}",
                "GP": gp,
                "MPG": f"{mpg:.1f}",
                "min_frac": f"{minutes_fraction:.3f}",
                "raw_wins": f"{raw_wins:+.2f}",
                "tanh_wins": f"{valuation.wins_added:+.2f}",
                "player_value": _money(valuation.player_value),
                "salary": _money(salary),
                "surplus_value": _money(valuation.surplus_value),
                "metric_source": valuation.metric_source,
                "_sort": valuation.wins_added,
            }
        )

    rows.sort(key=lambda r: r["_sort"], reverse=True)

    print("=" * 130)
    print("TOP 30 PLAYERS BY EPM — FULL VALUATION PIPELINE (EPM PATH)")
    print("=" * 130)
    print(_format_row(headers, widths))
    print("-" * 130)
    for r in rows:
        print(_format_row([r[h] for h in headers], widths))

    print()
    if missing_salaries:
        print(
            f"NOTE: {len(missing_salaries)} player(s) used the ${PLACEHOLDER_SALARY / 1e6:.1f}M "
            "placeholder salary (not in SALARIES_2025_26 dict): "
            f"{', '.join(missing_salaries)}"
        )
        print()

    # ----- Validation targets ---------------------------------------------
    print("=" * 130)
    print("VALIDATION TARGETS")
    print("=" * 130)
    v_headers = [
        "player_name",
        "age",
        "EPM",
        "yrs",
        "1yr_surplus",
        "total_surplus",
        "salary",
        "expected",
    ]
    v_widths = [22, 4, 6, 4, 13, 14, 11, 45]
    print(_format_row(v_headers, v_widths))
    print("-" * 140)

    for name, salary, years_remaining, note in VALIDATION_TARGETS:
        epm_row = get_player_epm(epm_df, name)
        if epm_row is None:
            print(f"{name}: NOT FOUND in EPM data")
            continue

        stats_row = stats_lookup.get(normalize_name(name))
        if stats_row is None:
            gp, mpg, age = 0, 0.0, 0
        else:
            gp = int(stats_row.get("GP", 0) or 0)
            mpg = float(stats_row.get("MPG", 0.0) or 0.0)
            age = int(stats_row.get("age", 0) or 0)

        player = _build_player(
            name, epm_row["team"], age, stats_row if stats_row is not None else {}
        )
        contract = Contract(salary=salary, years_remaining=years_remaining)
        single = evaluate_player(
            player,
            contract,
            epm_df=epm_df,
            darko_df=None,
        )
        multi = evaluate_player_multiyear(
            player,
            contract,
            epm_df=epm_df,
            darko_df=None,
        )

        print(
            _format_row(
                [
                    name,
                    age,
                    f"{float(epm_row['epm']):+.2f}",
                    years_remaining,
                    _money(single.surplus_value),
                    _money(multi.total_contract_surplus),
                    _money(salary),
                    note,
                ],
                v_widths,
            )
        )

    print()
    print("=" * 130)
    print(f"CURRENT EPM_TO_WINS_FACTOR = {EPM_TO_WINS_FACTOR}")
    print(f"DOLLARS_PER_WIN           = ${DOLLARS_PER_WIN / 1_000_000:.1f}M")
    print(f"MAX_WINS_ADDED            = {MAX_WINS_ADDED}")
    print(f"FULL_SEASON_MINUTES       = {FULL_SEASON_MINUTES}")
    print("=" * 130)


if __name__ == "__main__":
    main()
