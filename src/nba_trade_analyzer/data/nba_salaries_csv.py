"""Verifier salary source — ``$SITE_DATA_ROOT/nba_salaries.csv`` (Phase 2A).

The Spotrac-derived per-season salary file, used ONLY by the layer-1 verifier
as the independent second opinion against the Basketball Reference scrape.
It is never an ingest source: databallr Phase 0 (Path 4c) found its
``Guaranteed`` column is a career-total contaminated by stray artifact
tokens, and its season cells carry the same artifacts.

Adjudicated parsing rules (implement exactly — Phase 2A spec):
  - Parse by HEADER NAME, never position (the real header interleaves season
    columns with ``Team``/``Pos``/``Age``/``Guaranteed``).
  - Drop artifact cells: non-zero values below ``ARTIFACT_MAX_DOLLARS``
    ($10,000) are scraper debris (e.g. the stray "4.0" years-count token that
    drifts into season columns — Kris Dunn ``2027-28=4.0``). Dropped cells are
    RECORDED on the row so the verifier can surface them, never silently
    summed or compared.
  - "0"/"0.0" = no contract that season (a fact, not an artifact).
"""

from __future__ import annotations

import csv
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

_SEASON_RE = re.compile(r"\d{4}-\d{2}")

FILENAME = "nba_salaries.csv"

# Non-zero cells below this are scraper artifacts, not salaries. No NBA salary
# (or dead-money charge, or cap hold on a real team) is under $10k.
ARTIFACT_MAX_DOLLARS = 10_000


@dataclass(frozen=True)
class NbaSalaryCsvRow:
    """One player's Spotrac-derived per-season salaries."""

    player_raw: str
    team: str
    amounts: dict[str, int] = field(default_factory=dict)  # season -> dollars
    guaranteed_total: int | None = None  # career total; known-contaminated (Phase 0 4c)
    artifacts: dict[str, int] = field(default_factory=dict)  # dropped season -> raw value


def default_path() -> str:
    root = os.environ.get("SITE_DATA_ROOT", os.path.expanduser("~/site_Data"))
    return os.path.join(root, FILENAME)


def _parse_dollars(cell: str) -> int | None:
    try:
        return int(round(float(cell)))
    except (TypeError, ValueError):
        return None


def nba_salaries_season_coverage(path: str | Path | None = None) -> set[str]:
    """The set of seasons the CSV has COLUMNS for (header-based).

    Coverage is a property of the file's shape, not its cells: the file
    currently has NO 2025-26 column at all (Phase 0, Path 4c header), so
    Spotrac has no opinion on 2025-26 — which is missing-from-source, not
    "no contract". The verifier only compares within this set.
    """
    path = Path(path) if path is not None else Path(default_path())
    if not path.exists():
        raise FileNotFoundError(f"nba_salaries source missing: {path}")
    with open(path, newline="", encoding="utf-8") as fh:
        header = next(csv.reader(fh), [])
    return {c.strip() for c in header if c and _SEASON_RE.fullmatch(c.strip())}


def load_nba_salaries(path: str | Path | None = None) -> list[NbaSalaryCsvRow]:
    """Parse verifier salary rows. Missing file raises (guard_blocked upstream)."""
    path = Path(path) if path is not None else Path(default_path())
    if not path.exists():
        raise FileNotFoundError(f"nba_salaries source missing: {path}")

    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        return []

    season_cols = [c for c in rows[0].keys() if c and _SEASON_RE.fullmatch(c)]

    out: list[NbaSalaryCsvRow] = []
    for r in rows:
        raw_name = (r.get("Player") or "").strip()
        team = (r.get("Team") or "").strip().upper()
        if not raw_name:
            logger.warning("nba_salaries_csv: skipping row with blank player")
            continue
        amounts: dict[str, int] = {}
        artifacts: dict[str, int] = {}
        for s in season_cols:
            cell = (r.get(s) or "").strip()
            if cell == "":
                continue
            amount = _parse_dollars(cell)
            if amount is None:
                logger.warning(
                    "nba_salaries_csv: skipping malformed cell player=%r season=%s value=%r",
                    raw_name,
                    s,
                    cell,
                )
                continue
            if amount == 0:
                continue  # "0"/"0.0" = no contract that season.
            if amount < ARTIFACT_MAX_DOLLARS:
                # Artifact (e.g. the stray "4.0"): record, never compare.
                artifacts[s] = amount
                continue
            amounts[s] = amount
        guaranteed = _parse_dollars((r.get("Guaranteed") or "").strip())
        out.append(
            NbaSalaryCsvRow(
                player_raw=raw_name,
                team=team,
                amounts=amounts,
                guaranteed_total=guaranteed,
                artifacts=artifacts,
            )
        )
    return out
