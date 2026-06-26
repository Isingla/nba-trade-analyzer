"""Own-FA cap holds (Tier 3c, Phase A) — per-team, per-season placeholder charges.

site_Data's ``nba_cap_holds.csv`` lists each team's own pending free agents with a
per-season hold amount. Control Runway subtracts these **team-season totals** from
Open Cap so available room isn't overstated by ignoring holds.

Two framing caveats carried downstream:

  * **Team-level, not player-level.** Holds are summed per (team, season) and
    rendered as their own "cap holds" line — never folded into a player's contract
    row.
  * **Estimated, not exact.** The future-season figures are round-number
    placeholder tiers (e.g. 2.1M / 2.3M), NOT precise Bird-rights holds, and the
    source rows include implausibly old/retired names — so the totals are rough.
    The export marks them ``estimated`` so the page can footnote them.

Read from ``$SITE_DATA_ROOT/nba_cap_holds.csv``. A missing file yields an empty
result so NOTHING is subtracted (safe default, same as the NG resolver). Junk is
skipped and logged, never silently summed.
"""

from __future__ import annotations

import csv
import logging
import os
import re
from pathlib import Path

# Reuse the single source of truth for the league-year gate, so a rollover bump
# in guarantees.py moves NG marks and cap holds together.
from nba_trade_analyzer.data.guarantees import CURRENT_LEAGUE_YEAR

logger = logging.getLogger(__name__)

# Same season-key shape as the NG resolver, e.g. "2026-27".
_SEASON_RE = re.compile(r"\d{4}-\d{2}")


def _season_start_year(season: str | None) -> int | None:
    """Start year of a season key, e.g. '2026-27' -> 2026; None if malformed."""
    if season and _SEASON_RE.fullmatch(season):
        return int(season[:4])
    return None


def _norm_team(value: str | None) -> str:
    return (value or "").strip().upper()


def _to_amount(value) -> int | None:
    """Parse a hold cell to whole dollars; None for blank/non-numeric."""
    if value in (None, ""):
        return None
    s = str(value).strip()
    if s == "":
        return None
    try:
        return int(round(float(s)))
    except (TypeError, ValueError):
        return None


def load_cap_holds(
    path: str | Path | None = None,
    current_league_year: str = CURRENT_LEAGUE_YEAR,
) -> dict[str, dict[str, int]]:
    """Sum own-FA cap holds into ``{team: {season: total_dollars}}``.

    Only FUTURE seasons (strictly after ``current_league_year``) are kept —
    settled/elapsed seasons don't need projected holds, consistent with how the
    NG resolver gates the current league year. A missing file returns ``{}``.
    """
    if path is None:
        root = os.environ.get("SITE_DATA_ROOT", os.path.expanduser("~/site_Data"))
        path = os.path.join(root, "nba_cap_holds.csv")
    if not os.path.exists(path):
        return {}

    current = _season_start_year(current_league_year)
    totals: dict[str, dict[str, int]] = {}

    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        return {}

    # Header-driven season columns (same detection as the spread reader).
    season_cols = [c for c in rows[0].keys() if c and _SEASON_RE.fullmatch(c)]

    for r in rows:
        team = _norm_team(r.get("Team"))
        if not team:
            logger.warning(
                "cap_holds: skipping row with blank team (player=%r)", r.get("Player")
            )
            continue
        for s in season_cols:
            start = _season_start_year(s)
            # Future seasons only — gate out the current/elapsed league year.
            if start is None or (current is not None and start <= current):
                continue
            raw = (r.get(s) or "").strip()
            amount = _to_amount(raw)
            if amount is None:
                if raw != "":  # a non-empty cell we couldn't parse is junk — flag it.
                    logger.warning(
                        "cap_holds: skipping malformed cell team=%s season=%s value=%r",
                        team,
                        s,
                        raw,
                    )
                continue
            if amount <= 0:
                continue
            team_map = totals.setdefault(team, {})
            team_map[s] = team_map.get(s, 0) + amount

    return totals
