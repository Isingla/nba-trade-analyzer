"""Fit the MPG aging curve: mean ΔMPG by age, from our own nba_api history.

Phase 1 of the future-minutes aging-curve work. Delta method:

  * For every player appearing in two CONSECUTIVE seasons, record
    (age_in_later_season, ΔMPG = mpg_later − mpg_earlier).
  * FIT FILTER: keep a delta pair only when BOTH endpoint seasons clear
    MIN_MPG / MIN_GP — low-minute role churn produces ΔMPG that's about roster
    chaos, not aging, and would distort the curve. The fitted curve is later
    APPLIED to all players regardless; the filter only governs the FIT.
  * Mean ΔMPG by age, then a light 3-point smooth.
  * TAIL GUARD: survivorship (cut players truncate decline) can flatten or tick
    the old-age curve UP. Force the delta NON-POSITIVE for age >= PRIME_END so a
    38/39/40-year-old never "gains" minutes.

Prints the raw, smoothed, and guarded curves side by side. Run:

    uv run python scripts/fit_minutes_aging.py
"""

from __future__ import annotations

import warnings

import pandas as pd

from nba_trade_analyzer.data.players import fetch_player_stats

warnings.filterwarnings("ignore")

# Approved fit window: 10 modern load-management-era seasons.
SEASONS = [
    "2013-14", "2014-15", "2015-16", "2016-17", "2017-18", "2018-19",
    "2019-20", "2020-21", "2021-22", "2022-23", "2023-24", "2024-25",
]
MIN_MPG = 15.0   # both endpoints must clear this...
MIN_GP = 20      # ...and this, to enter the FIT (not the application).
PRIME_END = 31   # age at/after which the delta is forced non-positive (tail guard).
AGE_LO, AGE_HI = 19, 40  # reported/stored age range.


def _season_start(season: str) -> int:
    return int(season[:4])


def load() -> dict[int, dict[int, tuple[float, float, int]]]:
    """nba_id -> {season_start_year: (age, mpg, gp)} for clean rows."""
    by_player: dict[int, dict[int, tuple[float, float, int]]] = {}
    for s in SEASONS:
        try:
            df = fetch_player_stats(s)
        except Exception as e:  # noqa: BLE001 — report and skip a flaky season.
            print(f"  WARN {s}: fetch failed ({type(e).__name__}); skipped")
            continue
        y = _season_start(s)
        for r in df.to_dict(orient="records"):
            pid, age, mpg, gp = r.get("nba_player_id"), r.get("age"), r.get("MPG"), r.get("GP")
            if pid is None or any(pd.isna(v) for v in (pid, age, mpg, gp)):
                continue
            by_player.setdefault(int(pid), {})[y] = (float(age), float(mpg), int(gp))
    return by_player


def deltas(by_player) -> dict[int, list[float]]:
    """age_in_later_season -> [ΔMPG, ...] over consecutive, filtered pairs."""
    out: dict[int, list[float]] = {}
    n_pairs = 0
    for seasons in by_player.values():
        for y in sorted(seasons):
            if y + 1 not in seasons:
                continue  # not consecutive
            age0, mpg0, gp0 = seasons[y]
            age1, mpg1, gp1 = seasons[y + 1]
            # FIT FILTER: both endpoints must be real rotation seasons.
            if mpg0 < MIN_MPG or mpg1 < MIN_MPG or gp0 < MIN_GP or gp1 < MIN_GP:
                continue
            out.setdefault(int(round(age1)), []).append(mpg1 - mpg0)
            n_pairs += 1
    print(f"  fit pairs after filter (>= {MIN_MPG} MPG & >= {MIN_GP} GP both ends): {n_pairs}")
    return out


def smooth3(raw: dict[int, float], ages: list[int]) -> dict[int, float]:
    """Centered 3-point moving average over the age axis."""
    sm: dict[int, float] = {}
    for a in ages:
        window = [raw[x] for x in (a - 1, a, a + 1) if x in raw]
        sm[a] = sum(window) / len(window)
    return sm


def main() -> None:
    print("Loading seasons...")
    by_player = load()
    print(f"  players with >=1 clean season: {len(by_player)}")
    d = deltas(by_player)

    ages = [a for a in range(AGE_LO, AGE_HI + 1) if a in d]
    raw = {a: sum(d[a]) / len(d[a]) for a in ages}
    counts = {a: len(d[a]) for a in ages}
    smoothed = smooth3(raw, ages)

    # TAIL GUARD (two rules, past prime only):
    #   1. NON-POSITIVE: floor any positive late-age value to 0 (survivorship
    #      up-blip, e.g. raw age 40).
    #   2. NON-INCREASING (monotonic-ish): the delta may not drift back toward
    #      zero as age rises past the prime — carry the most-negative value
    #      forward. This fixes the survivorship FLATTENING at 39-40 and means a
    #      future noisy season can never re-create the "old players gain minutes"
    #      shape. Before the prime the young-bump shape is left untouched.
    guarded: dict[int, float] = {}
    running_min = 0.0
    for a in ages:
        v = smoothed[a]
        if a >= PRIME_END:
            if v > 0:
                v = 0.0
            running_min = min(running_min, v)
            v = running_min
        guarded[a] = round(v, 3)

    print(f"\n{'age':>4} {'n':>5} {'raw':>8} {'smooth':>8} {'GUARDED':>8}  guard?")
    for a in ages:
        changed = "<-- guard" if guarded[a] != round(smoothed[a], 3) else ""
        print(f"{a:>4} {counts[a]:>5} {raw[a]:>8.3f} {smoothed[a]:>8.3f} {guarded[a]:>8.3f}  {changed}")

    # Cumulative-from-prime sanity: total mpg lost aging 30 -> 40.
    cum = sum(guarded[a] for a in range(31, 41) if a in guarded)
    print(f"\ncumulative ΔMPG aging 30->40 (guarded): {cum:.1f}")
    print("python dict (guarded), for the constant table:")
    print("{" + ", ".join(f"{a}: {guarded[a]}" for a in ages) + "}")


if __name__ == "__main__":
    main()
