"""A/B harness for the engine swap — run BEFORE and AFTER the swap edits.

Prices every exported player-season through BOTH conversion chains on
IDENTICAL inputs (the export's projection impacts + projected minutes),
so the diff isolates the conversion swap from every other axis (EPM
source, minutes model, pool composition — all held fixed).

OLD chain = databallr_v3/lib/control-runway/valuation.ts transcribed
verbatim (constants confirmed 2026-08-23: epmReplacementLevel -1.0,
epmToWinsFactor 4.2, fullSeasonMinutes 2952, netRating fallback -2.0 x
2.75, tanh at 20, $3.5M/win cap-scaled).

NEW chain = nba_trade_analyzer.engine.clean_engine (the shipped module),
$/win $8.38M for 2026-27 (25-26 $7.86M cap-scaled), WAA per the gauntlet
definition (impact vs 0, no replacement offset, no compression).

Usage:
    uv run python scripts/ab_swap_harness.py /path/to/export.json

Read-only. Prints anchors, sigma-WAA both ways (judging the on-record
prediction, PREDICTION-2026-08-24), value distribution, biggest movers.
"""

from __future__ import annotations

import json
import math
import sys

from nba_trade_analyzer.engine.clean_engine import (
    CLEAN_CONSTANTS,
    calculate_war,
    pace_for_season,
    scale_dollars_per_win,
)

# ---- OLD chain: valuation.ts verbatim -------------------------------------

OLD = {
    "dollars_per_win": 3_500_000,
    "epm_replacement": -1.0,
    "epm_to_wins": 4.2,
    "full_season_minutes": 82 * 36,  # 2952
    "net_rating_replacement": -2.0,
    "net_rating_to_wins": 2.75,
    "max_wins_added": 20.0,
    "base_cap": 154_647_000,  # 2025-26 (valuation.ts dollarsPerWinForSeason base)
}

# Certified/projected caps the site uses for the scaling ratio.
CAPS = {"2025-26": 154_647_000, "2026-27": 164_961_000}


def old_compress(raw: float) -> float:
    m = OLD["max_wins_added"]
    return m * math.tanh(raw / m)


def old_war(impact: float, minutes: float, source: str) -> float:
    frac = minutes / OLD["full_season_minutes"]
    if source == "replacement":
        return 0.0
    if source == "net_rating":
        return old_compress(
            (impact - OLD["net_rating_replacement"]) * OLD["net_rating_to_wins"] * frac
        )
    return old_compress(
        (impact - OLD["epm_replacement"]) * OLD["epm_to_wins"] * frac
    )


def old_dpw(season: str) -> float:
    return OLD["dollars_per_win"] * CAPS.get(season, OLD["base_cap"]) / OLD["base_cap"]


# ---- NEW chain: clean_engine ----------------------------------------------

NEW_DPW_2526 = 7_860_000


def new_dpw(season: str) -> float:
    return scale_dollars_per_win(NEW_DPW_2526, CAPS["2025-26"], CAPS.get(season, CAPS["2025-26"]))


def new_war(impact: float, minutes: float, source: str, pace: float) -> float:
    if source == "replacement":
        return 0.0
    # net_rating fallback: same chain, R aligned to the ruled -2.1 (the
    # dropped-stash alignment arrives via this swap, per the 08-18 ruling).
    return calculate_war(impact, minutes, pace)


def new_waa(impact: float, minutes: float, source: str, pace: float) -> float:
    if source == "replacement":
        return 0.0
    poss = minutes * pace / 48.0
    return impact * poss / 100.0 / CLEAN_CONSTANTS.points_per_win


def old_waa(impact: float, minutes: float, source: str) -> float:
    # export.py compute_waa verbatim: damper 0.4214, tanh, no offset.
    if source == "replacement":
        return 0.0
    raw = impact * minutes / OLD["full_season_minutes"] * (OLD["epm_to_wins"] * 0.4214)
    return old_compress(raw)


# ---- run -------------------------------------------------------------------

ANCHOR_NAMES = {
    "Victor Wembanyama", "Kyle Kuzma", "Shai Gilgeous-Alexander",
    "Anthony Davis", "Jayson Tatum", "Bub Carrington",
}
SEASON = "2026-27"  # the current-year card; the season the swap renders first


def main(path: str) -> None:
    data = json.load(open(path))
    projections = data["projections"]  # dict: slug -> {playerName, seasons}
    # salary for SEASON: yearlySalaries[0] is the first projection season (2026-27)
    salary_by_slug = {
        s["bbrefSlug"]: (s["yearlySalaries"][0] if s.get("yearlySalaries") else s.get("salary"))
        for s in data["salaries"]
    }

    pace = pace_for_season(SEASON)
    rows = []
    for slug, p in projections.items():
        proj = (p.get("seasons") or {}).get(SEASON)
        if not proj:
            continue
        impact = proj.get("impact")
        source = proj.get("source", "epm")
        pg, pm = proj.get("projectedGames"), proj.get("projectedMpg")
        if impact is None or pg is None or pm is None:
            continue
        minutes = pg * pm
        ow = old_war(impact, minutes, source)
        nw = new_war(impact, minutes, source, pace)
        rows.append({
            "name": p.get("playerName", slug), "source": source,
            "impact": impact, "minutes": minutes,
            "salary": salary_by_slug.get(slug),
            "old_war": ow, "new_war": nw,
            "old_val": ow * old_dpw(SEASON), "new_val": nw * new_dpw(SEASON),
            "old_waa": old_waa(impact, minutes, source),
            "new_waa": new_waa(impact, minutes, source, pace),
            "export_waa": proj.get("waa"),
        })

    n = len(rows)
    print(f"priced {n} players for {SEASON} (pace {pace}, old dpw ${old_dpw(SEASON)/1e6:.2f}M, new dpw ${new_dpw(SEASON)/1e6:.2f}M)\n")

    print("ANCHORS (old -> new):")
    for r in rows:
        if r["name"] in ANCHOR_NAMES:
            s = f"  {r['name']:<26} WAR {r['old_war']:6.2f} -> {r['new_war']:6.2f}   value ${r['old_val']/1e6:6.1f}M -> ${r['new_val']/1e6:6.1f}M"
            if r["salary"]:
                s += f"   surplus ${(r['old_val']-r['salary'])/1e6:+6.1f}M -> ${(r['new_val']-r['salary'])/1e6:+6.1f}M"
            print(s)

    so, sn = sum(r["old_waa"] for r in rows), sum(r["new_waa"] for r in rows)
    se = sum(r["export_waa"] for r in rows if r["export_waa"] is not None)
    print(f"\nSIGMA-WAA (projected minutes, contracted pool): old {so:+.1f} -> new {sn:+.1f}   (export's own waa field sums {se:+.1f})")
    print("  [prediction on record: new lands +80..+110 ~ unchanged from old]")

    wo, wn = sum(r["old_war"] for r in rows), sum(r["new_war"] for r in rows)
    vo, vn = sum(r["old_val"] for r in rows), sum(r["new_val"] for r in rows)
    print(f"SIGMA-WAR: old {wo:.0f} -> new {wn:.0f}   SIGMA-VALUE: ${vo/1e9:.2f}B -> ${vn/1e9:.2f}B")

    movers = sorted(rows, key=lambda r: abs(r["new_war"] - r["old_war"]), reverse=True)[:8]
    print("\nBIGGEST WAR MOVERS (|new-old|):")
    for r in movers:
        print(f"  {r['name']:<26} {r['old_war']:6.2f} -> {r['new_war']:6.2f}   ({r['source']}, {r['minutes']:.0f} proj min, impact {r['impact']:+.2f})")

if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "export.json")
