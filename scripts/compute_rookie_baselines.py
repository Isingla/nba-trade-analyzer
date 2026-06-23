"""5-tier rookie production baselines (empty-roster-spots Piece 1, Step 2).

For every player drafted in a COMPLETE-window class (2014-2021 drafts, whose full
4-year rookie window has been played out through 2024-25), join:
  - draft slot      <- nba_api DraftHistory (PERSON_ID, OVERALL_PICK)
  - impact + minutes <- Dunks & Threes season-EPM API (/api/v1/season-epm), which
                        carries player_id, tot (EPM), gp and mpg per season.
The join is by ``player_id`` == DraftHistory ``PERSON_ID`` (no name-matching).

Group by draft tier and average the rookie-window production with WASHOUTS AS
ZERO (a drafted player absent from season-EPM that year contributes zeros —
expected value for a roster slot, not "if it hits"). EPM impact is run through
the SAME minutes-adjusted-WAR + $/win machinery the page prices on, so the
numbers are denominated like everyone else's. EPM-only by decision.

Requires the Dunks & Threes API key in $DUNKS_THREES_API_KEY (historical EPM is
Premium-gated). Run: ``uv run python scripts/compute_rookie_baselines.py``
"""

from __future__ import annotations

import json
import math
import os
from collections import defaultdict

from nba_api.stats.endpoints import DraftHistory

from nba_trade_analyzer.data.epm import fetch_epm_data
from nba_trade_analyzer.data.salaries import ROOKIE_SCALE_2025_26
from nba_trade_analyzer.engine.constants import (
    DOLLARS_PER_WIN,
    EPM_REPLACEMENT_LEVEL,
    EPM_TO_WINS_FACTOR,
    FULL_SEASON_MINUTES,
    MAX_WINS_ADDED,
)
from nba_trade_analyzer.export import compute_waa

# Where the generated databallr constant lands (sibling repo by default).
DEFAULT_OUT = os.path.expanduser("~/databallr_v3/lib/control-runway/rookie-baselines.ts")
OUT_PATH = os.environ.get("ROOKIE_BASELINES_OUT", DEFAULT_OUT)

# Complete-window draft classes: 2021 is the last class whose 4-year window
# (2021-22..2024-25) has finished. Partial recent classes are excluded — their
# only-early-career years bias the baseline downward.
FIRST_CLASS = 2014
LAST_CLASS = 2021  # inclusive
WINDOW_YEARS = 4

TIERS = ("top3", "lottery", "mid_late_1st", "early_2nd", "late_2nd")
TIER_LABEL = {
    "top3": "Top 3 (1-3)",
    "lottery": "Lottery (4-14)",
    "mid_late_1st": "Mid-late 1st (15-30)",
    "early_2nd": "Early 2nd (31-45)",
    "late_2nd": "Late 2nd (46-60)",
}
# Representative slot per FIRST-ROUND tier, for a rough surplus sanity-check
# against the year-1 rookie scale (the full 4-year table arrives in Step 3).
REP_SLOT = {"top3": 2, "lottery": 9, "mid_late_1st": 22}


def tier_of(pick: int) -> str | None:
    if pick <= 0:
        return None
    if pick <= 3:
        return "top3"
    if pick <= 14:
        return "lottery"
    if pick <= 30:
        return "mid_late_1st"
    if pick <= 45:
        return "early_2nd"
    if pick <= 60:
        return "late_2nd"
    return None


def war_from(impact: float, minutes: float) -> float:
    """Minutes-adjusted WAR — identical formula to the page's calculateWarFromProjection."""
    frac = minutes / FULL_SEASON_MINUTES
    raw = (impact - EPM_REPLACEMENT_LEVEL) * EPM_TO_WINS_FACTOR * frac
    return MAX_WINS_ADDED * math.tanh(raw / MAX_WINS_ADDED)


def _safe(value, default: float = 0.0) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    return default if math.isnan(f) else f


def load_draftees() -> list[dict]:
    """Drafted players in complete classes, with slot + tier (overall picks 1-60)."""
    df = DraftHistory().get_data_frames()[0]
    out = []
    for rec in df.to_dict(orient="records"):
        try:
            year = int(rec["SEASON"])
            pick = int(rec["OVERALL_PICK"])
            pid = int(rec["PERSON_ID"])
        except (TypeError, ValueError):
            continue
        if year < FIRST_CLASS or year > LAST_CLASS:
            continue
        tier = tier_of(pick)
        if tier is None:
            continue
        out.append({"pid": pid, "name": rec["PLAYER_NAME"], "year": year, "tier": tier})
    return out


def load_epm_by_season(years: list[int]) -> dict[int, dict[int, dict]]:
    """{season_ending_year: {player_id: {epm, gp, mpg}}} from the season-EPM API."""
    by_season: dict[int, dict[int, dict]] = {}
    for y in years:
        idx: dict[int, dict] = {}
        for rec in fetch_epm_data(season=y).to_dict(orient="records"):
            pid = rec.get("player_id")
            if pid is None or (isinstance(pid, float) and math.isnan(pid)):
                continue
            idx[int(pid)] = {
                "epm": _safe(rec.get("epm")),
                "gp": _safe(rec.get("gp")),
                "mpg": _safe(rec.get("mpg")),
            }
        by_season[y] = idx
    return by_season


def build_baselines() -> tuple[dict, dict]:
    """Returns (baselines, meta). baselines[tier] = [yr1..yr4] cells of
    {impact, gp, mpg, waa}; washouts contribute zeros (incl. age-out absences)."""
    draftees = load_draftees()
    window_end_years = list(range(FIRST_CLASS + 1, LAST_CLASS + WINDOW_YEARS + 1))
    epm = load_epm_by_season(window_end_years)

    cells: dict[tuple[str, int], list[dict]] = defaultdict(list)
    washouts: dict[str, int] = defaultdict(int)
    tier_n: dict[str, int] = defaultdict(int)
    for d in draftees:
        tier_n[d["tier"]] += 1
        played_any = False
        for k in range(WINDOW_YEARS):
            erow = epm.get(d["year"] + 1 + k, {}).get(d["pid"])
            if erow and erow["gp"] > 0:
                impact, games, mpg = erow["epm"], erow["gp"], erow["mpg"]
                played_any = True
            else:
                impact = games = mpg = 0.0
            cells[(d["tier"], k)].append({"impact": impact, "gp": games, "mpg": mpg})
        washouts[d["tier"]] += 0 if played_any else 1

    def avg(rows, field):
        return sum(r[field] for r in rows) / len(rows) if rows else 0.0

    baselines: dict[str, list[dict]] = {}
    for tier in TIERS:
        if tier_n[tier] == 0:
            continue
        years = []
        for k in range(WINDOW_YEARS):
            rows = cells[(tier, k)]
            impact, gp, mpg = avg(rows, "impact"), avg(rows, "gp"), avg(rows, "mpg")
            waa = compute_waa(impact, gp * mpg)
            years.append(
                {
                    "impact": round(impact, 3),
                    "gp": round(gp, 1),
                    "mpg": round(mpg, 1),
                    "waa": round(waa, 2),
                }
            )
        baselines[tier] = years

    meta = {
        "source": "Dunks & Threes season-EPM (Premium API) + nba_api DraftHistory",
        "metric": "epm",
        "classes": f"{FIRST_CLASS}-{LAST_CLASS} (complete 4-year windows only)",
        "washouts": "counted as zero (impact/gp/mpg = 0 for absent window years)",
        "tierCounts": {t: tier_n[t] for t in TIERS if tier_n[t]},
        "washoutPct": {t: round(washouts[t] / tier_n[t], 3) for t in TIERS if tier_n[t]},
    }
    return baselines, meta


def render_ts(baselines: dict, meta: dict) -> str:
    tier_keys = [t for t in TIERS if t in baselines]
    return f"""// GENERATED by nba-trade-analyzer/scripts/compute_rookie_baselines.py — do not edit by hand.
// Rookie production baselines by draft tier, per rookie-contract window year
// (yr1..yr4), on EPM impact with washouts as zero, over complete draft classes.
// Regenerate with the Dunks & Threes API key set: `uv run python
// scripts/compute_rookie_baselines.py` (writes this file in the databallr repo).

export type RookieTier = {" | ".join(f"'{t}'" for t in tier_keys)};

export interface RookieBaselineYear {{
  /** Average EPM impact for this tier in this rookie-window year. */
  impact: number;
  /** Average games played (washouts pull this toward zero). */
  gp: number;
  /** Average minutes per game. */
  mpg: number;
  /** Average WAA (display), same formula as the page's projection snapshot. */
  waa: number;
}}

export const ROOKIE_BASELINE_METADATA = {json.dumps(meta, indent=2)} as const;

/** tier -> [yr1, yr2, yr3, yr4] production baselines. */
export const ROOKIE_BASELINES: Record<RookieTier, RookieBaselineYear[]> = {json.dumps(baselines, indent=2)};

/** Pick slot (1-60) -> tier. Only the first three tiers auto-fill rosters. */
export function rookieTierForSlot(slot: number): RookieTier | null {{
  if (slot <= 0) return null;
  if (slot <= 3) return 'top3';
  if (slot <= 14) return 'lottery';
  if (slot <= 30) return 'mid_late_1st';
  if (slot <= 45) return 'early_2nd';
  if (slot <= 60) return 'late_2nd';
  return null;
}}
"""


def print_report(baselines: dict, meta: dict) -> None:
    print("\n========= PER-TIER ROOKIE BASELINES (EPM, washouts = zero) =========")
    print(f"($/win=${DOLLARS_PER_WIN / 1e6:.1f}M; rep-slot year-1 rookie-scale surplus)\n")
    for tier, years in baselines.items():
        n = meta["tierCounts"][tier]
        wp = meta["washoutPct"][tier]
        impact = sum(y["impact"] for y in years) / len(years)
        minutes = sum(y["gp"] * y["mpg"] for y in years) / len(years)
        war = war_from(impact, minutes)
        slot = REP_SLOT.get(tier)
        surplus = (
            f"{(war * DOLLARS_PER_WIN - ROOKIE_SCALE_2025_26[slot - 1]) / 1e6:+.1f}M"
            if slot
            else "n/a (2nd rd)"
        )
        print(
            f"{TIER_LABEL[tier]:<22} N={n:<3} washout={wp:4.0%} "
            f"impact {impact:+5.2f} | WAR {war:+5.2f} | surplus {surplus}  "
            f"yrWAR={[round(war_from(y['impact'], y['gp'] * y['mpg']), 2) for y in years]}"
        )


def main() -> None:
    import sys

    baselines, meta = build_baselines()
    print_report(baselines, meta)
    if "--write" in sys.argv:
        with open(OUT_PATH, "w", encoding="utf-8") as fh:
            fh.write(render_ts(baselines, meta))
        print(f"\nWrote {OUT_PATH}")
    else:
        print("\n(dry run — pass --write to generate the databallr rookie-baselines.ts)")


if __name__ == "__main__":
    main()
