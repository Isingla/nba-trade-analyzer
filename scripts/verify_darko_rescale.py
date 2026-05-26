"""Verify the DARKO→EPM rescale applied in the year-2 projection path.

Prints top-20 players showing current EPM, raw DPM, rescaled DPM
(EPM-equivalent), and the year-2 projected EPM that
``evaluate_player_multiyear`` actually consumes. Also reruns Cade and
SGA year-by-year breakdowns to confirm year 2 no longer reads as a
fake regression.

Run from repo root:
    uv run --native-tls python scripts/verify_darko_rescale.py
"""

from __future__ import annotations

import io
import sys

from nba_trade_analyzer.data.darko import (
    dpm_to_epm_equivalent,
    fetch_darko_data,
    get_player_darko,
)
from nba_trade_analyzer.data.epm import fetch_epm_data
from nba_trade_analyzer.data.players import fetch_player_stats
from nba_trade_analyzer.data.epm import normalize_name
from nba_trade_analyzer.engine.valuation import evaluate_player_multiyear
from nba_trade_analyzer.models.player import Contract, Player


def _force_utf8_stdout() -> None:
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", line_buffering=True
        )


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


def main() -> None:
    _force_utf8_stdout()

    print("Fetching EPM, DARKO, and player stats...")
    epm = fetch_epm_data()
    darko = fetch_darko_data()
    stats = fetch_player_stats()
    print(f"  EPM={len(epm)} rows, DARKO={len(darko)} rows, stats={len(stats)} rows")
    print()

    # Build a name → stats lookup so multi-year has real GP/MPG.
    stats = stats.copy()
    stats["player_name_normalized"] = stats["player_name"].map(normalize_name)
    stats_lookup = {row["player_name_normalized"]: row for _, row in stats.iterrows()}

    # ----- Top-20 EPM with raw and rescaled DPM ---------------------------
    top20 = epm.sort_values("epm", ascending=False).head(20)
    print(
        f"{'rank':>4}  {'player':<28}  {'team':<4}  {'age':>3}  "
        f"{'EPM':>7}  {'DPM_raw':>8}  {'DPM_resc':>9}  {'yr2_EPM':>8}  "
        f"{'Δ(yr2-yr1)':>11}"
    )
    print("-" * 100)

    for rank, (_, epm_row) in enumerate(top20.iterrows(), 1):
        name = epm_row["player_name"]
        team = epm_row["team"]
        age = int(epm_row["age"])
        epm_val = float(epm_row["epm"])

        darko_row = get_player_darko(darko, name)
        dpm_raw = float(darko_row["dpm"]) if darko_row is not None else None
        dpm_resc = dpm_to_epm_equivalent(dpm_raw) if dpm_raw is not None else None

        # Build a real Player with live GP/MPG and run the multi-year pipeline
        # to capture what year-2 projected_epm ACTUALLY ends up at.
        stats_row = stats_lookup.get(normalize_name(name))
        if stats_row is None:
            gp, mpg = 60, 30.0
        else:
            gp = int(stats_row.get("GP", 0) or 0)
            mpg = float(stats_row.get("MPG", 0.0) or 0.0)
        player = Player(
            name=name,
            team=team,
            age=age,
            stats={"NET_RATING": 0.0, "GP": gp, "MPG": mpg},
        )
        contract = Contract(salary=20_000_000, years_remaining=2)
        mv = evaluate_player_multiyear(player, contract, epm_df=epm, darko_df=darko)
        yr2_epm = mv.year_by_year[1].projected_epm
        delta = yr2_epm - epm_val

        dpm_raw_s = f"{dpm_raw:+8.2f}" if dpm_raw is not None else f"{'—':>8}"
        dpm_resc_s = f"{dpm_resc:+9.2f}" if dpm_resc is not None else f"{'—':>9}"

        print(
            f"{rank:>4}  {name:<28}  {team:<4}  {age:>3}  "
            f"{epm_val:+7.2f}  {dpm_raw_s}  {dpm_resc_s}  "
            f"{yr2_epm:+8.2f}  {delta:+11.2f}"
        )

    print()
    print("Legend:")
    print("  EPM       — current-season EPM from dunksandthrees")
    print("  DPM_raw   — raw DARKO DPM from public sheet")
    print("  DPM_resc  — DPM rescaled to EPM-equivalent via (dpm-0.026)/0.608")
    print("  yr2_EPM   — projected_epm consumed by evaluate_player_multiyear's year 2")
    print("  Δ(yr2-yr1)— net change vs current EPM; small for most, larger when")
    print("              DARKO genuinely projects an up or down move")
    print()

    # ----- Cade and SGA year-by-year breakdowns ---------------------------
    targets = [
        ("Cade Cunningham", 46_416_900, 4),
        ("Shai Gilgeous-Alexander", 38_333_988, 4),
    ]
    for name, salary, years_remaining in targets:
        epm_row = epm[epm["player_name"] == name]
        if epm_row.empty:
            print(f"{name}: NOT FOUND in EPM data; skipping.")
            continue
        epm_row = epm_row.iloc[0]
        stats_row = stats_lookup.get(normalize_name(name))
        if stats_row is None:
            gp, mpg = 60, 30.0
        else:
            gp = int(stats_row.get("GP", 0) or 0)
            mpg = float(stats_row.get("MPG", 0.0) or 0.0)
        player = Player(
            name=name,
            team=epm_row["team"],
            age=int(epm_row["age"]),
            stats={"NET_RATING": 0.0, "GP": gp, "MPG": mpg},
        )
        contract = Contract(salary=salary, years_remaining=years_remaining)
        mv = evaluate_player_multiyear(player, contract, epm_df=epm, darko_df=darko)
        print(
            f"\n-- {name}: age {player.age}, current EPM "
            f"{float(epm_row['epm']):+.2f}, {years_remaining}yr / ${salary / 1e6:.1f}M"
        )
        print_yby(mv)


if __name__ == "__main__":
    main()
