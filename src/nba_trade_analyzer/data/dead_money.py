"""Dead-money loader — ``$SITE_DATA_ROOT/nba_dead_money.csv`` (Phase 2A).

First-class dead money is the schema fix for the waive-and-stretch charges
that Basketball Reference lists as duplicate ACTIVE contract rows on two
teams (Lillard MIL+POR, Beal PHO+LAC — databallr Phase 0, Path 1d/5). The
ingest command writes these rows to ``v3_dead_money`` and uses them to decide
which duplicated BBRef salary rows are phantoms.

Pattern follows ``cap_holds.py``: header-name-driven season detection, junk
skipped and logged, missing file handled by the CALLER (ingest treats a
missing source as guard_blocked, never as an empty write — Phase 0 fact #5).

Names come in two shapes: plain ("Bradley Beal") and Spotrac waived-marker
style ("Lillard Damian WAIVED" — surname first). :func:`clean_player_name`
strips the marker; actual resolution to a bbref slug happens in
``ingest.names`` via the crosswalk, which tries both word orders.
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
_WAIVED_RE = re.compile(r"\s+WAIVED\s*$", re.IGNORECASE)

FILENAME = "nba_dead_money.csv"


@dataclass(frozen=True)
class DeadMoneyRow:
    """One player's dead-money schedule on one team."""

    player_raw: str  # verbatim CSV cell, e.g. "Lillard Damian WAIVED"
    player_name: str  # marker-stripped, e.g. "Lillard Damian"
    team: str  # CSV team code (display style, e.g. PHX)
    amounts: dict[str, int] = field(default_factory=dict)  # season -> dollars (>0 only)

    @property
    def was_waived_marker(self) -> bool:
        return bool(_WAIVED_RE.search(self.player_raw))


def clean_player_name(raw: str | None) -> str:
    """Strip the trailing WAIVED marker and collapse whitespace.

    Word order is NOT flipped here — "Lillard Damian" stays surname-first.
    The crosswalk resolver tries both orders, so cleaning stays lossless.
    """
    s = _WAIVED_RE.sub("", (raw or "").strip())
    return re.sub(r"\s+", " ", s)


def default_path() -> str:
    root = os.environ.get("SITE_DATA_ROOT", os.path.expanduser("~/site_Data"))
    return os.path.join(root, FILENAME)


def load_dead_money(path: str | Path | None = None) -> list[DeadMoneyRow]:
    """Parse dead-money rows; header-name-driven, junk logged never summed.

    Raises ``FileNotFoundError`` for a missing file — the ingest caller turns
    that into guard_blocked. (Contrast with ``load_cap_holds``'s empty-dict
    default, which exists for the legacy export path's safe-subtraction
    semantics; ingest must fail loud instead.)
    """
    path = Path(path) if path is not None else Path(default_path())
    if not path.exists():
        raise FileNotFoundError(f"dead-money source missing: {path}")

    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        return []

    season_cols = [c for c in rows[0].keys() if c and _SEASON_RE.fullmatch(c)]

    out: list[DeadMoneyRow] = []
    for r in rows:
        raw_name = (r.get("Player") or "").strip()
        team = (r.get("Team") or "").strip().upper()
        if not raw_name or not team:
            logger.warning(
                "dead_money: skipping row with blank player/team (player=%r team=%r)",
                raw_name,
                team,
            )
            continue
        amounts: dict[str, int] = {}
        for s in season_cols:
            cell = (r.get(s) or "").strip()
            if cell == "":
                continue
            try:
                amount = int(round(float(cell)))
            except (TypeError, ValueError):
                logger.warning(
                    "dead_money: skipping malformed cell player=%r season=%s value=%r",
                    raw_name,
                    s,
                    cell,
                )
                continue
            if amount <= 0:
                continue
            amounts[s] = amount
        if amounts:
            out.append(
                DeadMoneyRow(
                    player_raw=raw_name,
                    player_name=clean_player_name(raw_name),
                    team=team,
                    amounts=amounts,
                )
            )
    return out
