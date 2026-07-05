"""Option-code loader — ``$SITE_DATA_ROOT/nba_options.csv`` (Phase 2A).

The living Spotrac-derived option file: per-season marker codes per player.
databallr Phase 0 (Path 3c) established this file carries June-2026 option
OUTCOMES *implicitly* (a marker clears or a row disappears when an option is
decided) and was consumed by NOTHING. Ingest reads it as the option source
and diffs against the DB state — it never guesses what a cleared marker
means (see ``ingest.plans.plan_option_transitions``).

Schema quirks handled here (verified in Phase 0, Path 3c):
  - header ``Player,Team,2026-27,...,2031-32,Pos,Age,2025-26`` — trailing
    ``Pos``/``Age``/``2025-26`` columns are scraper-misaligned junk. Parsing
    is header-NAME driven; junk columns only matter if their cells carry a
    valid code, which they don't ("0"/blank).
  - cell vocabulary: 0 (nothing), P, T, EE, NG, D, UFA, RFA. Anything else is
    logged and skipped, never silently imported.
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

FILENAME = "nba_options.csv"

# The full marker vocabulary (matches the v3_contract_options CHECK constraint).
VALID_OPTION_CODES = frozenset({"P", "T", "EE", "NG", "D", "UFA", "RFA"})

# Cells that mean "no marker this season".
_EMPTY_CELLS = frozenset({"", "0", "0.0"})


@dataclass(frozen=True)
class OptionCsvRow:
    """One player's per-season option/marker codes on one team."""

    player_raw: str
    team: str
    codes: dict[str, str] = field(default_factory=dict)  # season -> code


def default_path() -> str:
    root = os.environ.get("SITE_DATA_ROOT", os.path.expanduser("~/site_Data"))
    return os.path.join(root, FILENAME)


def load_options(path: str | Path | None = None) -> list[OptionCsvRow]:
    """Parse option rows. Missing file raises (ingest turns it into guard_blocked)."""
    path = Path(path) if path is not None else Path(default_path())
    if not path.exists():
        raise FileNotFoundError(f"options source missing: {path}")

    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        return []

    season_cols = [c for c in rows[0].keys() if c and _SEASON_RE.fullmatch(c)]

    out: list[OptionCsvRow] = []
    for r in rows:
        raw_name = (r.get("Player") or "").strip()
        team = (r.get("Team") or "").strip().upper()
        if not raw_name or not team:
            logger.warning(
                "options_csv: skipping row with blank player/team (player=%r team=%r)",
                raw_name,
                team,
            )
            continue
        codes: dict[str, str] = {}
        for s in season_cols:
            cell = (r.get(s) or "").strip()
            if cell in _EMPTY_CELLS:
                continue
            code = cell.upper()
            if code not in VALID_OPTION_CODES:
                logger.warning(
                    "options_csv: skipping unknown code player=%r season=%s value=%r",
                    raw_name,
                    s,
                    cell,
                )
                continue
            codes[s] = code
        if codes:
            out.append(OptionCsvRow(player_raw=raw_name, team=team, codes=codes))
    return out
