"""Clean valuation engine — docs/SPEC-clean-engine.md is the sole authority.

The chain (spec §1), exactly and with nothing else:

    WAR     = (EPM − R) × (minutes × pace/48) / 100 ÷ pointsPerWin
    dollars = WAR × dollarsPerWin
    surplus = dollars − salary

Deliberate absences (spec §3): no full-season pin, no 4.2, no damper, no
tanh, no output clamps. Extreme outputs are an input problem (spec §5) —
they are flagged with a loud warning and NEVER modified.

This module does not touch the existing engine files; the old engine stays
runnable for A/B comparison.
"""

from __future__ import annotations

import json
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from nba_trade_analyzer.data.cache import DEFAULT_CACHE_DIR

# Flag lines for the extreme-input policy (spec §5): outputs beyond the
# historical WAR range are warned about loudly, never clamped or squashed.
WAR_FLAG_HIGH = 20.0
WAR_FLAG_LOW = -5.0


@dataclass(frozen=True)
class CleanEngineConstants:
    """Ruled constants (spec §2), provenance in the spec's table.

    ``dollars_per_win_2025_26`` is a STORED season constant — derived
    2026-08-23 as matched payroll ÷ matched production (gross, the DARKO
    construction): $5.325B ÷ 677 WAR over 365 slug-joined players. The
    gauntlet's reproduction gate re-derives it from data and must land on
    this number; pricing calls read the constant, they never re-derive.
    """

    replacement_level: float = -2.1
    points_per_win: float = 36.7
    # Measured league pace per season (BBRef league per-game table, read
    # 2026-08-18). Seasons without a measured value use the LATEST measured
    # season's pace — never the 100 approximation (spec §2).
    pace_by_season: Mapping[str, float] = field(
        default_factory=lambda: {"2025-26": 99.4}
    )
    dollars_per_win_2025_26: float = 7_860_000.0


CLEAN_CONSTANTS = CleanEngineConstants()


def pace_for_season(
    season: str, constants: CleanEngineConstants = CLEAN_CONSTANTS
) -> float:
    """Measured pace for the season, else the LATEST measured season's pace.

    Season keys sort chronologically as strings ('2024-25' < '2025-26'), so
    max() over keys picks the latest measured season.
    """
    measured = constants.pace_by_season
    if season in measured:
        return measured[season]
    latest = max(measured)
    return measured[latest]


def calculate_war(
    epm: float,
    minutes: float,
    pace: float,
    constants: CleanEngineConstants = CLEAN_CONSTANTS,
    player_name: str | None = None,
) -> float:
    """WAR = (EPM − R) × (minutes × pace/48) / 100 ÷ pointsPerWin.

    Replacement enters at the rate (step 1), never after the wins step —
    subtracting replacement WINS at the end would scale the offset by each
    player's own exposure incorrectly (spec §1).

    Outputs outside [WAR_FLAG_LOW, WAR_FLAG_HIGH] emit a loud warning naming
    the player; the returned value is NEVER modified (spec §5).
    """
    possessions = minutes * pace / 48.0
    war = (epm - constants.replacement_level) * possessions / 100.0 / constants.points_per_win
    if war > WAR_FLAG_HIGH or war < WAR_FLAG_LOW:
        warnings.warn(
            f"clean engine WAR {war:.2f} for {player_name or '<unnamed>'} is "
            f"outside the historical range [{WAR_FLAG_LOW}, {WAR_FLAG_HIGH}] "
            "— absurd WAR comes from absurd inputs (projected minutes nobody "
            "will play); fix the minutes model, the output is not clamped",
            stacklevel=2,
        )
    return war


def calculate_value(war: float, dollars_per_win: float) -> float:
    """dollars = WAR × dollarsPerWin (spec §1 step 5)."""
    return war * dollars_per_win


def calculate_surplus(value: float, salary: float) -> float:
    """surplus = dollars − salary (spec §1)."""
    return value - salary


def derive_dollars_per_win(payroll: float, league_war: float) -> float:
    """The ruled construction (spec §2): payroll ÷ production, gross.

    This is the DARKO construction (darko.app/about/fair-salary) —
    re-derived annually from matched payroll and matched league WAR. The
    stored ``dollars_per_win_2025_26`` constant is what this derivation must
    REPRODUCE from data (gauntlet gate), not a number computed fresh on
    every pricing call.
    """
    if league_war <= 0:
        raise ValueError(
            f"league WAR must be positive to derive a win price; got {league_war}"
        )
    return payroll / league_war


def scale_dollars_per_win(rate: float, cap_from: float, cap_to: float) -> float:
    """Cap-scale a season's win price: rate × cap_to / cap_from."""
    if cap_from <= 0:
        raise ValueError(f"cap_from must be positive; got {cap_from}")
    return rate * cap_to / cap_from


# ----- EPM source (spec §6): Premium API cache ONLY ------------------------

# The scrape caches (epm_dunksandthrees.json, epm_dunksandthrees_<year>.json
# — no _api_ infix) serve Expected (modeled) values: 67 never-played players
# including 2026 draftees, no gp field, stars regressed toward the mean.
# Demoted 2026-08-18. This loader can only ever name the _api_ file, and it
# verifies the actuals shape before returning.
_API_CACHE_TEMPLATE = "epm_dunksandthrees_api_{season}.json"


def load_epm_api_cache(
    season: int = 2026, cache_dir: Path | None = None
) -> list[dict[str, Any]]:
    """Load the Premium API EPM cache — the source of record (actuals).

    Asserts the source at load: records MUST carry 'gp' and 'mp' (the
    scrape lacks both). A wrong-shaped file raises immediately, naming the
    file — there is no fallback to the scrape, by rule.
    """
    directory = Path(cache_dir) if cache_dir is not None else DEFAULT_CACHE_DIR
    path = directory / _API_CACHE_TEMPLATE.format(season=season)
    if not path.exists():
        raise FileNotFoundError(
            f"EPM API cache not found: {path}. The clean engine reads ONLY "
            "the _api_ cache (actuals, with gp/mp); the scrape caches serve "
            "Expected (modeled) values and are not a fallback."
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("value") if isinstance(payload, dict) else payload
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"EPM API cache {path} is empty or malformed")
    for row in rows:
        if "gp" not in row or "mp" not in row:
            raise ValueError(
                f"records in {path} lack 'gp'/'mp' — this is the scrape "
                "(Expected values) shape, not the API actuals; refusing to "
                "load, no fallback"
            )
    return rows
