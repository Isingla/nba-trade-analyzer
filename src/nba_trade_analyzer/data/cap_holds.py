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
from dataclasses import dataclass
from pathlib import Path

from nba_trade_analyzer.ingest.site_data import site_data_root

# Reuse the single source of truth for the league year, so a rollover bump in
# guarantees.py moves NG marks and cap holds together — but the two consumers
# DELIBERATELY compare against it differently (fix/cap-holds-current-year-gate,
# 2026-07-11):
#   * NG marks gate out seasons AT OR BEFORE the current year: an NG year that
#     has arrived is settled money, no longer a future commitment.
#   * Cap holds KEEP the current year and drop only strictly-elapsed seasons:
#     a hold FOR the current league year is the operative charge sitting on a
#     team's books right now (Trae Young's 2026-27 hold counted against WAS the
#     day the league year rolled). The first post-rollover ingest plan proved
#     the difference: an after-current-only gate here silently dropped ~235
#     live 2026-27 holds (~$1.16B).
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

    CURRENT-and-future seasons (``season >= current_league_year``) are kept;
    only strictly-elapsed seasons are dropped. This deliberately DIVERGES from
    the NG resolver's at-or-before gate — see the import comment above: a hold
    for the current league year is an operative charge, not settled history.
    A missing file returns ``{}``.
    """
    if path is None:
        path = str(site_data_root() / "nba_cap_holds.csv")
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
            # Keep current-and-future; drop strictly-elapsed seasons only.
            if start is None or (current is not None and start < current):
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


# ---------------------------------------------------------------------------
# Player-grain loader (Phase 2A ingest). The team-summed loader above serves
# the legacy export; ingest keeps player identity (databallr Phase 0 Path 2b/2c:
# the parser used to discard it) and classifies sentinel data as data.
# ---------------------------------------------------------------------------

# Non-zero hold cells below this are sentinel/placeholder debris, not holds —
# Phase 0 Path 2d: 25 of 30 teams carry single-digit sentinel rows (BOS all
# 4.0, summing to a $24 "team total"). No real cap hold is under $10k.
SENTINEL_MAX_DOLLARS = 10_000


@dataclass(frozen=True)
class CapHoldRow:
    """One player's cap hold on one team for one season."""

    team: str
    season: str
    player_name: str
    amount: int


def load_cap_holds_rows(
    path: str | Path | None = None,
    current_league_year: str = CURRENT_LEAGUE_YEAR,
) -> list[CapHoldRow]:
    """Player-grain cap holds, current-and-future seasons.

    Same header-driven parsing and elapsed-season gate as :func:`load_cap_holds`,
    but rows keep (team, season, player, amount) instead of summing per team.
    Sentinel-vs-real classification is the caller's job (see
    :func:`classify_cap_hold_teams`) so the loader stays a faithful reader.

    Missing file raises ``FileNotFoundError`` — ingest turns that into
    guard_blocked, never an empty write (Phase 0 fact #5). The legacy loader's
    empty-dict default is unchanged for the export path.
    """
    if path is None:
        path = str(site_data_root() / "nba_cap_holds.csv")
    if not os.path.exists(path):
        raise FileNotFoundError(f"cap-holds source missing: {path}")

    current = _season_start_year(current_league_year)
    out: list[CapHoldRow] = []

    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        return []

    season_cols = [c for c in rows[0].keys() if c and _SEASON_RE.fullmatch(c)]

    for r in rows:
        team = _norm_team(r.get("Team"))
        player = (r.get("Player") or "").strip()
        if not team:
            logger.warning(
                "cap_holds: skipping row with blank team (player=%r)", r.get("Player")
            )
            continue
        for s in season_cols:
            start = _season_start_year(s)
            # Keep current-and-future; drop strictly-elapsed seasons only.
            if start is None or (current is not None and start < current):
                continue
            raw = (r.get(s) or "").strip()
            amount = _to_amount(raw)
            if amount is None:
                if raw != "":
                    logger.warning(
                        "cap_holds: skipping malformed cell team=%s season=%s value=%r",
                        team,
                        s,
                        raw,
                    )
                continue
            if amount <= 0:
                continue
            out.append(
                CapHoldRow(team=team, season=s, player_name=player, amount=amount)
            )
    return out


def classify_cap_hold_teams(rows: list[CapHoldRow]) -> dict[str, str]:
    """Per-team quality: 'real' iff the team has at least one plausible hold.

    A team whose every non-zero cell is under ``SENTINEL_MAX_DOLLARS`` is
    sentinel (the 25-team placeholder problem, Phase 0 Path 2d). Real teams'
    rows may still individually be small only if the team also carries
    plausible rows — quality is a TEAM-level statement about the source, so
    all of a team's rows share its classification.
    """
    quality: dict[str, str] = {}
    for row in rows:
        if row.amount >= SENTINEL_MAX_DOLLARS:
            quality[row.team] = "real"
        else:
            quality.setdefault(row.team, "sentinel")
    return quality
