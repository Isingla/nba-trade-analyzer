"""Roster-side coverage report for the committed crosswalk.

``build_crosswalk.py`` reports the *contract* side (matched / total Basketball
Reference contracts). This companion reports the *roster* side: it fetches all
30 nba_api ``CommonTeamRoster`` rosters and lists every current roster player
whose nba_id has no crosswalk entry — i.e. players the runtime could see on a
team but couldn't join back to a salary/valuation record.

Note the realistic ceiling is well under 100%: two-way and 10-day players sit on
nba_api rosters but have NO standard contract on the Basketball Reference
contracts page, so they cannot have a crosswalk entry by construction (and can't
be normal trade inputs — they'd fail-loud on the salary side). With ~3 two-way
slots per team that floor is ~83%. The *contract* coverage gate in
``build_crosswalk.py`` (default 0.95) is the meaningful one; this roster gate is
set lower to catch a genuinely broken build (matched almost nothing) without
tripping on expected two-way gaps.

Exits non-zero if roster coverage is below ``--threshold`` (default 0.80).

Usage::

    python scripts/crosswalk_coverage.py
    python scripts/crosswalk_coverage.py --threshold 0.9
"""

from __future__ import annotations

import argparse
import sys

from nba_api.stats.static import teams as static_teams

from nba_trade_analyzer.data.crosswalk import load_crosswalk
from nba_trade_analyzer.data.players import fetch_team_roster


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--threshold", type=float, default=0.80)
    args = parser.parse_args()

    crosswalk = load_crosswalk()
    print(f"Loaded crosswalk with {len(crosswalk)} entries.")

    total = 0
    missing: list[tuple[str, int, str]] = []
    for t in static_teams.get_teams():
        abbr = t["abbreviation"]
        roster = fetch_team_roster(abbr)
        for rec in roster:
            total += 1
            if crosswalk.entry_for_nba_id(rec["nba_player_id"]) is None:
                missing.append((abbr, rec["nba_player_id"], rec["player_name"]))

    covered = total - len(missing)
    coverage = covered / total if total else 0.0

    print("\n=== ROSTER COVERAGE ===")
    print(
        f"  {covered}/{total} current roster players have a crosswalk entry "
        f"({coverage:.1%})"
    )
    if missing:
        print(f"  {len(missing)} roster players with NO crosswalk entry:")
        for abbr, nba_id, name in sorted(missing):
            print(f"    {abbr}  {nba_id:<10} {name}")

    if coverage < args.threshold:
        print(
            f"\nFAIL: roster coverage {coverage:.1%} below "
            f"threshold {args.threshold:.1%}.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    print(f"\nOK: roster coverage {coverage:.1%} >= threshold {args.threshold:.1%}.")


if __name__ == "__main__":
    main()
