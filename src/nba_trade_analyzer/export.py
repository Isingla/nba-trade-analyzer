"""Databallr cap-data export.

Builds the JSON payload that the databallr ``Control Runway`` and
``Trade Analyzer`` features consume — a frozen league snapshot of contracts
plus multi-year production projections. The databallr side (``scripts/
sync-cap-data.ts``) spawns ``nba-trade-analyzer export`` and renders the two
TypeScript snapshot files from this JSON.

The payload has three parts:

- ``metadata``    — row counts, the WAA formula, per-source season counts, and
                    the fixed projection-season window.
- ``salaries``    — one row per active contract (a near 1:1 of the Basketball
                    Reference scrape, with ``yearly_salaries`` as an int list).
- ``projections`` — keyed by BBRef slug; each player carries a fixed
                    5-season projection of ``{impact, mpg, waa, source}``.

Two databallr-specific transforms live here (and nowhere else), so the
valuation engine stays untouched:

1. **WAA** — databallr's average-impact team-quality metric. Unlike the
   engine's replacement-relative WAR, WAA has no replacement offset
   (clean chain, 2026-08-24)::

       waa = impact * (projected_minutes * pace/48) / 100 / pointsPerWin

   ``projected_minutes`` comes from the two-model minutes projection (issue
   2.2): ``projected_games * projected_mpg``, where games is a recency-weighted
   games-missed + age model and mpg is a recency-weighted prior MPG nudged by
   impact and salary. The same projected_minutes is exported (as projectedGames
   / projectedMpg) and is what databallr's TS WAR/surplus path re-prices, so
   WAA and WAR are unified on one availability-adjusted minutes figure.

2. **Source taxonomy** — the engine labels aged years ``aging_curve``;
   databallr distinguishes ``aging_epm`` vs ``aging_darko`` by the anchor the
   aging chain is built on. Players with no EPM/DARKO signal (the engine's
   ``net_rating`` fallback) are emitted as ``replacement`` to match databallr,
   which has no ``net_rating`` projection bucket.

A full 5-season horizon is projected for *every* player regardless of contract
length (``horizon_years`` on ``evaluate_player_multiyear``), because Control
Runway shows future team quality across the whole window, not just guaranteed
years.
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from nba_trade_analyzer.data.cap_holds import load_cap_holds
from nba_trade_analyzer.data.crosswalk import Crosswalk, load_crosswalk
from nba_trade_analyzer.data.darko import fetch_darko_data, get_player_darko
from nba_trade_analyzer.data.cache import JsonCache
from nba_trade_analyzer.data.epm import (
    api_cache_file,
    clear_epm_unmatched,
    epm_unmatched_report,
    get_player_epm,
    normalize_name,
)
from nba_trade_analyzer.data.guarantees import NonGuaranteeResolver
from nba_trade_analyzer.data.players import fetch_player_stats
from nba_trade_analyzer.data.salaries import build_contract, fetch_all_salaries
from nba_trade_analyzer.engine.constants import (
    CAP_THRESHOLDS_BY_SEASON,
    MAX_PROJECTION_YEARS,
    PROJECTED_GAMES_NO_HISTORY,
    SALARY_CAP,
)
from .engine.clean_engine import CLEAN_CONSTANTS, load_epm_api_cache, pace_for_season
from nba_trade_analyzer.engine.minutes import project_games, project_mpg, recency_weighted_mpg
from nba_trade_analyzer.engine.valuation import evaluate_player_multiyear
from nba_trade_analyzer.models.player import Player

# Completed seasons used as the GP/MPG history feeding the minutes models,
# ordered oldest -> latest. These are the seasons "available before" the
# projection window, so the projection never leaks future data.
_HISTORY_SEASONS = ("2022-23", "2023-24", "2024-25")

# First season of the projection window; subsequent seasons follow yearly.
# Rolled 2025 -> 2026 at the 2026-07-11 league-year rollover; with
# MAX_PROJECTION_YEARS=5 the window auto-extended to 2030-31 (closing the
# old ours_missing-2030-31 coverage gap). Must roll together with
# data.salaries._DEFAULT_SEASON every July.
_FIRST_SEASON_START = 2026

# The completed-actuals season the export prices from: the season ENDING the
# year the projection window starts (end-year 2026 = 2025-26 actuals).
# Derived, never hardcoded — a rollover bumps both together.
API_ACTUALS_SEASON = _FIRST_SEASON_START

# The export is a pure cache reader (ruled 2026-08-25: the nightly ingest
# owns fetching). A current cache older than this is refused, loudly.
EPM_CACHE_MAX_AGE_HOURS = 48.0


def _season_label(end_year: int) -> str:
    return f"{end_year - 1}-{end_year % 100:02d}"



_WAA_FORMULA = (
    "impact * (projected_minutes * pace/48) / 100 / pointsPerWin; "
    "no replacement-level offset, no compression (clean chain 2026-08-24). "
    "projected_minutes = projected_games * projected_mpg (issue 2.2 minutes models)"
)

_GENERATED_FROM = (
    "src/nba_trade_analyzer/data/salaries.py",
    "src/nba_trade_analyzer/data/epm.py",
    "src/nba_trade_analyzer/data/darko.py",
    "src/nba_trade_analyzer/data/players.py",
    "src/nba_trade_analyzer/data/cap_holds.py",
    "src/nba_trade_analyzer/engine/constants.py",
    "src/nba_trade_analyzer/engine/aging_curve.py",
    "src/nba_trade_analyzer/engine/minutes.py",
    "data/player_crosswalk.json",
)

# Own-FA cap holds are placeholder estimates, not exact Bird-rights holds — the
# page footnotes them from this note.
_CAP_HOLDS_NOTE = (
    "Own-FA cap holds are round-number ESTIMATES (placeholder tiers), not exact "
    "Bird-rights holds; team-season totals, future seasons only."
)

_CAP_THRESHOLDS_NOTE = (
    "League-wide TEAM-SALARY threshold levels per season (cap, floor, luxury "
    "tax, first apron, second apron). certified=True seasons are NBA-announced "
    "figures; certified=False seasons are projections (certified 2026-27 "
    "levels grown 8.0%/yr with the cap — past-5-season certified cap growth "
    "avg (NBA PR), ruled 2026-08-14) and must be labeled as estimates."
)

_DEAD_MONEY_NOTE = (
    "Waived/stretched dead-money charges on team books, SEPARATE from roster "
    "salaries (which are decomposed actives). Team-season totals + audit rows; "
    "name-variant duplicates collapsed by resolved player. Not yet consumed by "
    "the UI (display line is Phase 4)."
)

logger = logging.getLogger(__name__)


class _CamelModel(BaseModel):
    """Snake-case in Python, camelCase in JSON to match the databallr shape."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class DataballrSalaryRow(_CamelModel):
    player_name: str
    bbref_slug: str
    team: str
    salary: int
    years_remaining: int
    is_rookie_scale: bool
    has_player_option: bool
    has_team_option: bool
    yearly_salaries: list[int]
    # Non-guaranteed (NG) seasons (issue 3a, mark-only): season key -> that
    # season's salary. `yearly_salaries` is LEFT UNCHANGED — this only MARKS the
    # NG years (allowlisted + spread-NG-confirmed, future seasons only) for the
    # databallr side to handle the committed/cap%/surplus treatment.
    non_guaranteed_seasons: dict[str, int] = {}


class DataballrSeasonProjection(_CamelModel):
    waa: float
    impact: float
    mpg: float
    source: str
    # Minutes-adjusted projection (issue 2.2). ``mpg`` stays the player's
    # current/anchor minutes; these two are the model OUTPUTS that price WAR:
    # projected_minutes = projected_games * projected_mpg. Kept separate so the
    # availability channel (games) and role channel (mpg) stay inspectable.
    projected_games: float
    projected_mpg: float


class DataballrPlayerProjection(_CamelModel):
    player_name: str
    nba_id: int | None
    age: int | None
    seasons: dict[str, DataballrSeasonProjection]
    # Which ACTUALS priced this player's EPM (ruled 2026-08-25): the current
    # season's, or — when he has no current row — the prior season's, said
    # explicitly so the site can tag it. None = no EPM row anywhere.
    epm_basis: str | None = None
    # The ACTUAL minutes/games of the CURRENT-season row epm_basis is stamped
    # from (ruled 2026-09-01): the site's "reduced mins" chip compares them
    # to projectedGames x projectedMpg. None for a fallback-basis player —
    # his row is the PRIOR season's and its minutes are not current actuals
    # — and for a no-data player. Missing is None, never a stand-in 0: a 0
    # here would read as "played none".
    source_minutes: float | None = None
    source_games: int | None = None


class DataballrExportMetadata(_CamelModel):
    repo: str
    salary_cap: int
    salary_rows: int
    epm_rows: int
    darko_rows: int
    stats_rows: int
    projection_seasons: list[str]
    generated_from: list[str]
    waa_formula: str
    source_counts: dict[str, int]
    # ISO timestamp of the EPM cache file this export read from — the actual
    # vintage of every est. EPM on the page. None when no cache file exists
    # (e.g. injected test frames). Read from the file, never invented.
    epm_vintage: str | None = None


class DataballrCapHolds(_CamelModel):
    """Own-FA cap holds (Tier 3c, Phase A), aggregated per team per future season.

    ``totals`` is ``{team: {season: summed_hold_dollars}}`` — team-level, kept
    SEPARATE from contract salaries so the page can render a distinct "cap holds"
    line and subtract it from Open Cap. ``estimated`` + ``note`` carry the
    placeholder-tier caveat through to the UI.
    """

    estimated: bool
    note: str
    team_seasons: int  # number of (team, season) totals — visibility on coverage.
    totals: dict[str, dict[str, int]]


class DataballrSeasonCapThresholds(_CamelModel):
    """One league year's official (or projected) cap-threshold levels.

    Every figure is a TEAM-SALARY dollar line — the level a team's total salary
    is measured against for that season (salary cap, minimum team salary /
    floor, luxury-tax level, first apron, second apron). These are the
    announced dollar amounts themselves, never percentages or exception values.

    ``certified`` distinguishes league-announced figures (True: 2025-26,
    2026-27) from growth-projected out-years (False) so the frontend can label
    projections honestly. ``source`` names the announcement or the projection
    method.
    """

    salary_cap: int
    minimum_team_salary: int
    luxury_tax: int
    first_apron: int
    second_apron: int
    certified: bool
    source: str


class DataballrCapThresholds(_CamelModel):
    """Per-season cap/tax/apron levels (Cap Sheet, Stage 1). League-wide, not
    per-team; keyed by season like ``projections[...].seasons``."""

    note: str
    seasons: dict[str, DataballrSeasonCapThresholds]


class DataballrDeadMoneyRow(_CamelModel):
    """One dead-money charge (audit grain). ``collapsed_variants`` lists raw
    source-name variants that resolved to the same player and were merged into
    this row (the Micic rule) — empty for normal rows."""

    team: str
    season: str
    player_name: str
    bbref_slug: str | None
    amount: int
    collapsed_variants: list[str] = []


class DataballrDeadMoney(_CamelModel):
    """Dead money per team-season (Phase 2 Day 2). ``totals`` is
    ``{display_team: {season: dollars}}`` — the same grammar as cap_holds —
    summed from the DEDUPED ``rows``, so the two views can never disagree."""

    note: str
    totals: dict[str, dict[str, int]]
    rows: list[DataballrDeadMoneyRow]


def _empty_dead_money() -> DataballrDeadMoney:
    return DataballrDeadMoney(note=_DEAD_MONEY_NOTE, totals={}, rows=[])


class DataballrExport(_CamelModel):
    metadata: DataballrExportMetadata
    salaries: list[DataballrSalaryRow]
    # Keyed by BBRef slug, matching CONTROL_RUNWAY_PROJECTIONS in databallr.
    projections: dict[str, DataballrPlayerProjection]
    # Per-team-per-season own-FA cap-hold totals (Tier 3c, Phase A). Team-level,
    # NOT folded into `salaries`.
    cap_holds: DataballrCapHolds
    # Per-season tax/apron threshold levels (Cap Sheet, Stage 1). ADDITIVE:
    # consumers that predate this field simply ignore it.
    cap_thresholds: DataballrCapThresholds
    # Dead-money charges (Phase 2 Day 2). ADDITIVE: pre-Day-2 consumers ignore
    # it; the databallr sync treats it as optional.
    dead_money: DataballrDeadMoney = Field(default_factory=_empty_dead_money)
    # Staleness marker (ADDITIVE, serializes as `sourceNote`): set when the
    # salary frame came from the committed-CSV fallback instead of a live
    # BBRef fetch, so a degraded export is self-describing (never silent).
    # None on a normal live-scrape export.
    source_note: str | None = None


def season_keys() -> list[str]:
    """The fixed projection window, ``["2026-27", ..., "2030-31"]``.

    Rolled at the 2026-07-11 league-year rollover (the previously deferred
    decision). The window tracks ``_FIRST_SEASON_START`` and must roll
    together with ``data.salaries._DEFAULT_SEASON``: the salary parser
    anchors on that label and ``_contract_rows`` maps yearly values onto
    the SALARY window (``salary_season_keys``) positionally — rolling one
    without the other mislabels every salary by a season.
    """
    return [
        f"{_FIRST_SEASON_START + i}-{(_FIRST_SEASON_START + i + 1) % 100:02d}"
        for i in range(MAX_PROJECTION_YEARS)
    ]


# The BBRef contracts table exposes one year column beyond the projection
# window (y6): real salary can live there — wembavi01's 2031-32 extension
# year ($57,420,000, surfaced 2026-07-15) — even though projections and
# CAP_THRESHOLDS_BY_SEASON deliberately stop at MAX_PROJECTION_YEARS.
# Derived from the same _FIRST_SEASON_START as season_keys(), so the July
# roll (see that docstring: roll together with data.salaries._DEFAULT_SEASON)
# moves BOTH windows in lockstep — never hardcode a season label here.
SALARY_WINDOW_EXTRA_YEARS = 1


def salary_season_keys() -> list[str]:
    """The salary-row window, ``["2026-27", ..., "2031-32"]``.

    SALARY ingest/read shaping only (``_contract_rows``, the db_source
    reader, verifier coverage). The projection window (``season_keys``) and
    the cap-thresholds fail-loud check stay on MAX_PROJECTION_YEARS —
    extending THIS window must never require a thresholds-table entry.
    """
    return [
        f"{_FIRST_SEASON_START + i}-{(_FIRST_SEASON_START + i + 1) % 100:02d}"
        for i in range(MAX_PROJECTION_YEARS + SALARY_WINDOW_EXTRA_YEARS)
    ]


# ---------------------------------------------------------------------------
# Dead money (Phase 2 Day 2) — one builder, two sources.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DeadMoneyCharge:
    """One (team, season, player) dead-money charge, source-agnostic.

    db mode fills these from v3_dead_money (slug via the player join); scrape
    mode from the SAME CSV loader the ingest uses (data/dead_money.py), so the
    two modes agree by construction.
    """

    team: str  # display code (PHX/MIL style — the CSV's and DB's grammar)
    season: str
    player_name: str  # raw source name (may carry a WAIVED marker)
    bbref_slug: str | None
    amount: int


def _dead_money_dedupe_key(charge: DeadMoneyCharge) -> tuple[str, str]:
    """Resolved-player identity for the Micic rule.

    Prefer the slug; else a WAIVED-stripped, order-insensitive token
    normalization so "Vasilije Micic" and "Micic Vasilije WAIVED" collapse
    without a resolver in the loop.
    """
    if charge.bbref_slug:
        return ("slug", charge.bbref_slug)
    tokens = sorted(
        t for t in re.findall(r"[a-z0-9]+", charge.player_name.lower()) if t != "waived"
    )
    return ("name", " ".join(tokens))


def build_dead_money(charges: list[DeadMoneyCharge]) -> DataballrDeadMoney:
    """Dedupe by resolved player, then sum team-season totals from the rows.

    Name-variant duplicates (same team+season+resolved player, SAME amount)
    are one charge listed twice upstream — merged into a single row with the
    variants noted, and counted ONCE. Same-key charges with DIFFERENT amounts
    are NOT merged (variant evidence requires an identical figure); both stay,
    loudly, for human eyes.
    """
    groups: dict[tuple[str, str, tuple[str, str]], list[DeadMoneyCharge]] = {}
    order: list[tuple[str, str, tuple[str, str]]] = []
    for charge in charges:
        key = (charge.team, charge.season, _dead_money_dedupe_key(charge))
        if key not in groups:
            order.append(key)
        groups.setdefault(key, []).append(charge)

    rows: list[DataballrDeadMoneyRow] = []
    for key in order:
        group = groups[key]
        # Prefer the cleanest display name: unmarked over WAIVED-marked.
        display = min(group, key=lambda c: ("waived" in c.player_name.lower(), c.player_name))
        slug = next((c.bbref_slug for c in group if c.bbref_slug), None)
        amounts = {c.amount for c in group}
        if len(group) > 1 and len(amounts) == 1:
            variants = sorted({c.player_name for c in group} - {display.player_name})
            logger.info(
                "dead money: collapsed %d name variant(s) of %s (%s %s): %s",
                len(group) - 1,
                display.player_name,
                display.team,
                display.season,
                variants,
            )
            rows.append(
                DataballrDeadMoneyRow(
                    team=display.team,
                    season=display.season,
                    player_name=display.player_name,
                    bbref_slug=slug,
                    amount=display.amount,
                    collapsed_variants=variants,
                )
            )
            continue
        if len(group) > 1:
            logger.warning(
                "dead money: NOT collapsing %s (%s %s) — same resolved player "
                "but DIFFERENT amounts %s; keeping all rows for review",
                display.player_name,
                display.team,
                display.season,
                sorted(amounts),
            )
        for charge in group:
            rows.append(
                DataballrDeadMoneyRow(
                    team=charge.team,
                    season=charge.season,
                    player_name=charge.player_name,
                    bbref_slug=charge.bbref_slug,
                    amount=charge.amount,
                    collapsed_variants=[],
                )
            )

    totals: dict[str, dict[str, int]] = {}
    for row in rows:
        team_map = totals.setdefault(row.team, {})
        team_map[row.season] = team_map.get(row.season, 0) + row.amount

    rows.sort(key=lambda r: (r.team, r.season, r.player_name))
    return DataballrDeadMoney(note=_DEAD_MONEY_NOTE, totals=totals, rows=rows)


def _load_dead_money_charges(crosswalk: Crosswalk) -> list[DeadMoneyCharge]:
    """Scrape-mode charges: the ingest's CSV loader + name resolution.

    Reuses data/dead_money.py (same parser, same numbers as the ingest by
    construction). A missing CSV degrades to an empty block with a warning —
    the export must not hard-fail on an absent side file (cap-holds precedent),
    unlike the ingest where a missing source is guard_blocked.
    """
    from nba_trade_analyzer.data.dead_money import load_dead_money
    from nba_trade_analyzer.ingest.names import NameResolver

    try:
        dead_rows = load_dead_money()
    except FileNotFoundError as exc:
        logger.warning("dead money: source missing (%s) — exporting empty block", exc)
        return []

    resolver = NameResolver(crosswalk)
    charges: list[DeadMoneyCharge] = []
    for row in dead_rows:
        slug = resolver.resolve(row.player_name)
        for season, amount in sorted(row.amounts.items()):
            charges.append(
                DeadMoneyCharge(
                    team=row.team,
                    season=season,
                    player_name=row.player_raw,
                    bbref_slug=slug,
                    amount=amount,
                )
            )
    return charges


def compute_waa(impact: float, minutes: float) -> float:
    """databallr WAA for one projected season from total projected minutes.

    Clean chain (spec + gauntlet gate 6 definition): impact vs league
    average, actual-possessions exposure, no compression. Pace resolves to
    the latest measured season (99.4) for unplayed projection years.
    """
    pace = pace_for_season("2026-27")
    return impact * (minutes * pace / 48.0) / 100.0 / CLEAN_CONSTANTS.points_per_win


def map_source(year_source: str, anchor_source: str) -> str:
    """Map an engine year-source to databallr's projection taxonomy.

    ``epm``/``darko`` pass through. The engine's ``aging_curve`` becomes
    ``aging_darko`` or ``aging_epm`` depending on the anchor the aging chain is
    built on. Anything else (the ``net_rating`` fallback) becomes
    ``replacement`` — databallr has no ``net_rating`` projection bucket.
    """
    if year_source in ("epm", "darko"):
        return year_source
    if year_source == "aging_curve":
        return "aging_darko" if anchor_source == "darko" else "aging_epm"
    return "replacement"


def _is_nan(value: object) -> bool:
    return isinstance(value, float) and math.isnan(value)


def _stats_index(stats_df: pd.DataFrame) -> tuple[dict[int, dict], dict[str, dict]]:
    """Build ``(by nba_player_id, by normalized name)`` lookups into stats."""
    by_id: dict[int, dict] = {}
    by_name: dict[str, dict] = {}
    for record in stats_df.to_dict(orient="records"):
        pid = record.get("nba_player_id")
        if pid is not None and not _is_nan(pid):
            by_id[int(pid)] = record
        name = record.get("player_name")
        if isinstance(name, str) and name:
            by_name[normalize_name(name)] = record
    return by_id, by_name


def _stats_for(
    player_name: str,
    nba_id: int | None,
    by_id: dict[int, dict],
    by_name: dict[str, dict],
) -> dict | None:
    """Find a stats row by nba_id (preferred) then normalized name."""
    if nba_id is not None and nba_id in by_id:
        return by_id[nba_id]
    return by_name.get(normalize_name(player_name))


def _age_from_stats(stats_row: dict | None) -> int | None:
    if stats_row is None:
        return None
    age = stats_row.get("age")
    if age is None or _is_nan(age):
        return None
    return int(age)


def _mpg_from_stats(stats_row: dict | None) -> float:
    if stats_row is None:
        return 0.0
    mpg = stats_row.get("MPG")
    if mpg is None or _is_nan(mpg):
        return 0.0
    return float(mpg)


def _fill_age_mpg_from_epm(
    epm_row: pd.Series | None,
    age: int | None,
    mpg: float,
) -> tuple[int | None, float]:
    """Backfill age/MPG from EPM when nba_api has no current-season stats row.

    nba_api stats are primary (they match the committed snapshot), but a
    rostered player who is injured or hasn't played this season has no stats
    row — yet still appears in the EPM table with an age and a minutes figure.
    Using those keeps high-value injured stars (e.g. a $45M point guard out for
    the season) as real future-quality projections instead of zeroed-out
    replacements.
    """
    if age is not None and mpg > 0.0:
        return age, mpg
    if epm_row is None:
        return age, mpg
    if age is None:
        epm_age = epm_row.get("age")
        if epm_age is not None and not _is_nan(epm_age):
            age = int(epm_age)
    if mpg == 0.0:
        epm_mpg = epm_row.get("mpg")
        if epm_mpg is not None and not _is_nan(epm_mpg):
            mpg = float(epm_mpg)
    return age, mpg


def _salary_share_for_offset(contract, offset: int) -> float:
    """That season's salary as a share of the cap (mpg-model input)."""
    yearly = list(getattr(contract, "yearly_salaries", []) or [])
    if yearly:
        salary = yearly[offset] if offset < len(yearly) else yearly[-1]
    else:
        salary = getattr(contract, "salary", 0)
    return float(salary) / SALARY_CAP if SALARY_CAP else 0.0


def _projected_games_for(
    gp_history: list[float], age: int | None, offset: int
) -> float:
    """Games projection for a season, or the no-history baseline if age unknown."""
    if age is None:
        return round(PROJECTED_GAMES_NO_HISTORY, 1)
    return round(project_games(gp_history, age, year_offset=offset), 1)


def _replacement_seasons(
    mpg: float,
    keys: list[str],
    *,
    gp_history: list[float] | None = None,
    age: int | None = None,
) -> dict[str, DataballrSeasonProjection]:
    rounded_mpg = round(mpg, 1)
    gp_history = gp_history or []
    return {
        key: DataballrSeasonProjection(
            waa=0.0,
            impact=0.0,
            mpg=rounded_mpg,
            source="replacement",
            projected_games=_projected_games_for(gp_history, age, offset),
            projected_mpg=rounded_mpg,
        )
        for offset, key in enumerate(keys)
    }


def _project_player(
    player_name: str,
    team: str,
    age: int | None,
    mpg: float,
    contract,
    epm_df: pd.DataFrame,
    darko_df: pd.DataFrame,
    keys: list[str],
    *,
    gp_history: list[float] | None = None,
    mpg_history: list[float] | None = None,
    engine_name: str | None = None,
) -> dict[str, DataballrSeasonProjection]:
    """Project a fixed 5-season window for one player.

    Minutes for each season come from the two-model projection (issue 2.2):
    ``project_games`` (recency-weighted games-missed % + age) times
    ``project_mpg`` (recency-weighted prior MPG nudged by impact + salary). When
    GP/MPG history is unavailable the models fall back to the player's current
    MPG and a healthy-minus-age games baseline, so the export still produces a
    sensible projection. Returns replacement seasons when the player has no
    usable EPM/DARKO signal or no stats row.
    """
    gp_history = gp_history or []
    mpg_history = mpg_history or []

    if age is None:
        logger.warning(
            "projection demoted to replacement: %s (%s) — unknown age "
            "(no stats row and no EPM backfill); all seasons zeroed",
            player_name,
            team,
        )
        return _replacement_seasons(mpg, keys, gp_history=gp_history, age=age)

    # The engine joins EPM/DARKO by name. When the slug-first lookup already
    # resolved this player's canonical source name, hand the engine THAT name
    # so its join hits the same row (payload keeps the salary-frame name).
    player = Player(
        name=engine_name or player_name,
        team=team,
        age=age,
        stats={"MPG": mpg, "GP": 0.0, "NET_RATING": 0.0},
    )
    multi = evaluate_player_multiyear(
        player,
        contract,
        epm_df=epm_df,
        darko_df=darko_df,
        horizon_years=MAX_PROJECTION_YEARS,
    )

    if multi.primary_metric_source not in ("epm", "darko"):
        logger.warning(
            "projection demoted to replacement: %s (%s) — no usable EPM/DARKO "
            "signal (engine fell through to %s); all seasons zeroed",
            player_name,
            team,
            multi.primary_metric_source,
        )
        return _replacement_seasons(mpg, keys, gp_history=gp_history, age=age)

    # The aging chain anchors on DARKO whenever any projected year used it
    # (year-1 DARKO, or the year-2 DARKO override); otherwise it anchors on EPM.
    anchor_source = (
        "darko"
        if any(yr.projection_source == "darko" for yr in multi.year_by_year)
        else "epm"
    )

    # Prior MPG anchor for the mpg model: recency-weighted completed-season MPG,
    # falling back to the player's current MPG when there is no history.
    prior_mpg = recency_weighted_mpg(mpg_history)
    if prior_mpg is None:
        prior_mpg = mpg

    rounded_mpg = round(mpg, 1)
    seasons: dict[str, DataballrSeasonProjection] = {}
    for offset, key in enumerate(keys):
        proj_games = _projected_games_for(gp_history, age, offset)
        if offset >= len(multi.year_by_year):
            seasons[key] = DataballrSeasonProjection(
                waa=0.0,
                impact=0.0,
                mpg=rounded_mpg,
                source="replacement",
                projected_games=proj_games,
                projected_mpg=rounded_mpg,
            )
            continue
        year = multi.year_by_year[offset]
        impact = year.projected_epm
        salary_share = _salary_share_for_offset(contract, offset)
        proj_mpg = round(
            project_mpg(prior_mpg, impact, salary_share, age=age, year_offset=offset),
            1,
        )
        minutes = proj_games * proj_mpg
        seasons[key] = DataballrSeasonProjection(
            waa=round(compute_waa(impact, minutes), 1),
            impact=round(impact, 2),
            mpg=rounded_mpg,
            source=map_source(year.projection_source, anchor_source),
            projected_games=proj_games,
            projected_mpg=proj_mpg,
        )
    return seasons


def _source_actuals(epm_row: pd.Series) -> tuple[float | None, int | None]:
    """The (minutes, games) actually played on a resolved EPM row.

    Read only for a CURRENT-basis row (the caller gates on epm_basis), so
    these are current-season actuals by construction — which holds only for
    a frame from ``default_epm_frame``; an injected ``epm_df`` has no prior
    ids, so every row there reads as current. ``.get`` + NaN guard because
    injected test frames may omit gp/mp; either missing yields None for
    BOTH — half an actuals line is not an actuals line.
    """
    mp = epm_row.get("mp")
    gp = epm_row.get("gp")
    if mp is None or gp is None or pd.isna(mp) or pd.isna(gp):
        return None, None
    # gp is a float in the API cache (16.0); round-then-int also survives a
    # numeric string, where a bare int("16.0") would kill the export.
    return float(mp), int(round(float(gp)))


def _fetch_minutes_history(
    seasons: tuple[str, ...] = _HISTORY_SEASONS,
) -> dict[int, dict[str, list[float]]]:
    """Build per-player GP/MPG history (oldest -> latest) from recent seasons.

    Keyed by nba_id. Only seasons in which the player actually has GP/MPG rows
    contribute, so a player who entered the league mid-window still gets a
    correctly-ordered partial history. Each season is fetched once (cached 24h).
    """
    history: dict[int, dict[str, list[float]]] = {}
    for season in seasons:  # oldest -> latest
        df = fetch_player_stats(season)
        for record in df.to_dict(orient="records"):
            pid = record.get("nba_player_id")
            if pid is None or (isinstance(pid, float) and math.isnan(pid)):
                continue
            gp = record.get("GP")
            mpg = record.get("MPG")
            if gp is None or mpg is None or _is_nan(gp) or _is_nan(mpg):
                continue
            entry = history.setdefault(int(pid), {"gp": [], "mpg": []})
            entry["gp"].append(float(gp))
            entry["mpg"].append(float(mpg))
    return history


def _epm_cache_vintage(cache_dir: Path | None = None) -> str | None:
    """The API actuals cache file's modification time, as a UTC ISO timestamp.

    This is the true vintage of every impact number in the export: the
    CURRENT-season Premium-API cache (``epm_dunksandthrees_api_{season}``) —
    the file the export actually reads, never the demoted scrape cache. A
    missing file yields ``None`` and the snapshot metadata says so.
    """
    cache = JsonCache(cache_dir) if cache_dir is not None else None
    path = api_cache_file(API_ACTUALS_SEASON, cache)
    if not path.exists():
        return None
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()


# Every column the export pipeline reads off the EPM frame — validated at
# load so a wrong-shaped file fails naming itself, not as a KeyError deep in
# the build loop (review finding 6).
_CONSUMED_EPM_COLUMNS = (
    "player_id",
    "player_name",
    "player_name_normalized",
    "epm",
    "mpg",
    "gp",
    "mp",
    "age",
)


def default_epm_frame(
    cache_dir: Path | None = None,
) -> tuple[pd.DataFrame, set[int]]:
    """The export's EPM frame: current-season API ACTUALS, with the ruled
    one-season fallback (2026-08-25).

    - CURRENT cache (``API_ACTUALS_SEASON``) is mandatory: missing or older
      than ``EPM_CACHE_MAX_AGE_HOURS`` fails loud with the file and the fix
      (the export is a pure reader; the nightly ingest owns fetching).
    - PRIOR season: players absent from the current cache are served their
      prior-season actuals — never further back. Their ids are returned so
      the payload can carry an explicit ``epmBasis``.
    - A player in neither cache stays absent: missing stays missing (dash),
      never invented, and the scrape's Expected values feed nothing.

    Reuses clean_engine.load_epm_api_cache (the _api_-only path and gp/mp
    actuals assertion) rather than re-parsing.
    """
    jc = JsonCache(cache_dir) if cache_dir is not None else JsonCache()
    current_path = api_cache_file(API_ACTUALS_SEASON, jc)
    fix = (
        "the nightly ingest owns this cache — run `uv run nba-trade-analyzer "
        "ingest` (or a manual fetch_epm_data(season="
        f"{API_ACTUALS_SEASON})) to (re)write it"
    )
    if not current_path.exists():
        raise FileNotFoundError(
            f"EPM API cache missing: {current_path}. The export is a pure "
            f"cache reader and never fetches; {fix}."
        )
    age_hours = (
        datetime.now(timezone.utc)
        - datetime.fromtimestamp(current_path.stat().st_mtime, tz=timezone.utc)
    ).total_seconds() / 3600
    if age_hours > EPM_CACHE_MAX_AGE_HOURS:
        raise RuntimeError(
            f"EPM API cache is stale: {current_path} is {age_hours:.0f}h old "
            f"(limit {EPM_CACHE_MAX_AGE_HOURS:.0f}h); {fix}."
        )

    current = load_epm_api_cache(season=API_ACTUALS_SEASON, cache_dir=cache_dir)
    missing_current = [
        c for c in _CONSUMED_EPM_COLUMNS if any(c not in r for r in current)
    ]
    if missing_current:
        raise ValueError(
            f"EPM API cache {current_path} lacks column(s) {missing_current} "
            "that the export consumes — wrong-shaped file, refusing to price "
            "from it."
        )
    try:
        prior = load_epm_api_cache(
            season=API_ACTUALS_SEASON - 1, cache_dir=cache_dir
        )
    except FileNotFoundError:
        # First season of API history: no fallback pool, current-only.
        prior = []
    except (ValueError, json.JSONDecodeError) as exc:
        # The prior cache is OPTIONAL: a malformed/truncated file (non-atomic
        # cache writes make this a real state) must not kill the export —
        # degrade to current-only, loudly, naming the file.
        logger.warning(
            "prior-season EPM cache %s is unreadable (%s) — fallback pool "
            "disabled this run, pricing current-season rows only",
            api_cache_file(API_ACTUALS_SEASON - 1, jc),
            exc,
        )
        prior = []

    current_ids = {int(r["player_id"]) for r in current}
    fallback_rows = [r for r in prior if int(r["player_id"]) not in current_ids]
    prior_ids = {int(r["player_id"]) for r in fallback_rows}
    # Age correction on fallback rows (ruled 2026-08-25): a prior-season row
    # carries the player's PRIOR-season age, which would feed the aging curve
    # and minutes model a year young. Increment by exactly 1 at the merge
    # point — the single seam every downstream consumer reads through.
    fallback_rows = [
        {**r, "age": int(r["age"]) + 1} if r.get("age") is not None else r
        for r in fallback_rows
    ]
    frame = pd.DataFrame(current + fallback_rows)

    missing = [c for c in _CONSUMED_EPM_COLUMNS if c not in frame.columns]
    if missing:
        raise ValueError(
            f"EPM API cache {current_path} lacks column(s) {missing} that the "
            "export consumes — wrong-shaped file, refusing to price from it."
        )
    return frame, prior_ids


def build_export(
    *,
    salary_df: pd.DataFrame | None = None,
    epm_df: pd.DataFrame | None = None,
    darko_df: pd.DataFrame | None = None,
    stats_df: pd.DataFrame | None = None,
    crosswalk: Crosswalk | None = None,
    minutes_history: dict[int, dict[str, list[float]]] | None = None,
    guarantee_resolver: NonGuaranteeResolver | None = None,
    cap_holds: dict[str, dict[str, int]] | None = None,
    dead_money: list[DeadMoneyCharge] | None = None,
) -> DataballrExport:
    """Assemble the full databallr cap-data payload.

    Every data source is injectable so the export is unit-testable with stub
    frames and never touches the network in tests. In normal use each source is
    fetched once (cached 24h, with the salaries CSV offline fallback).

    ``minutes_history`` feeds the games/MPG models (issue 2.2): a mapping of
    nba_id -> ``{"gp": [...], "mpg": [...]}`` over recent completed seasons
    (oldest first). It is fetched live only on the production path (when
    ``stats_df`` is not injected); tests inject frames and omit history, so the
    minutes models fall back to current MPG + a healthy-minus-age games baseline.
    """
    fetch_live = stats_df is None
    salary_df = fetch_all_salaries() if salary_df is None else salary_df
    epm_was_fetched = epm_df is None
    # SOURCE FLIP (feat/epm-source-flip, ruled 2026-08-18) + one-season
    # fallback (ruled 2026-08-25): the export's EPM input is the D&T Premium
    # API cache — season ACTUALS, gp/mp-asserted by clean_engine's loader —
    # with players absent from the current season served their PRIOR-season
    # actuals (tagged via epmBasis). The scrape path served Expected
    # (modeled) values and feeds nothing.
    epm_prior_ids: set[int] = set()
    if epm_df is None:
        epm_df, epm_prior_ids = default_epm_frame()
    darko_df = fetch_darko_data() if darko_df is None else darko_df
    stats_df = fetch_player_stats() if stats_df is None else stats_df
    crosswalk = load_crosswalk() if crosswalk is None else crosswalk
    if minutes_history is None and fetch_live:
        minutes_history = _fetch_minutes_history()
    minutes_history = minutes_history or {}
    # NG resolver (issue 3a). `load()` reads the allowlist (always present) + the
    # site_Data spread; a missing spread yields an empty NG set so nothing fires.
    if guarantee_resolver is None:
        guarantee_resolver = NonGuaranteeResolver.load()
    # Own-FA cap holds (Tier 3c, Phase A). A missing file yields {} so nothing is
    # subtracted from Open Cap. The loader gates to future seasons only.
    if cap_holds is None:
        cap_holds = load_cap_holds()
    # Dead money (Phase 2 Day 2): scrape mode loads the ingest's CSV loader;
    # db mode injects charges from v3_dead_money. Missing CSV -> empty block.
    if dead_money is None:
        dead_money = _load_dead_money_charges(crosswalk)

    keys = season_keys()
    by_id, by_name = _stats_index(stats_df)
    # Fresh unmatched-player report per export run.
    clear_epm_unmatched()

    salaries: list[DataballrSalaryRow] = []
    projections: dict[str, DataballrPlayerProjection] = {}
    source_counts: dict[str, int] = {}

    for record in salary_df.to_dict(orient="records"):
        slug = str(record.get("bbref_slug") or "").strip()
        player_name = str(record.get("player_name") or "").strip()
        if not player_name:
            continue

        contract = build_contract(record)
        team = str(record.get("team") or "")
        nba_id = crosswalk.nba_id_for_slug(slug)

        # MARK confirmed non-guaranteed years (issue 3a, mark-only): record them
        # but leave yearly_salaries untouched — the committed/cap%/surplus math
        # stays as-is for the databallr side to handle. yearly_salaries is
        # current-season-first, so index i maps to season keys[i]. NG can sit on
        # any year (non-contiguous); each fires only on allowlist + spread-NG and
        # only for future seasons (the resolver gates out the current league year).
        yearly = list(contract.yearly_salaries)
        non_guaranteed: dict[str, int] = {}
        for i, season in enumerate(keys):
            if i >= len(yearly):
                break
            # `yearly[i] > 0` is the $0-NG guard: some NG-coded seasons carry no
            # snapshot salary (e.g. Jalen Wilson, Jordan Miller) — nothing to mark.
            if yearly[i] > 0 and guarantee_resolver.is_non_guaranteed(
                season, nba_id=nba_id, player=player_name, team=team
            ):
                non_guaranteed[season] = yearly[i]

        salaries.append(
            DataballrSalaryRow(
                player_name=player_name,
                bbref_slug=slug,
                team=team,
                salary=int(record["salary"]),
                years_remaining=int(record["years_remaining"]),
                is_rookie_scale=bool(record.get("is_rookie_scale", False)),
                has_player_option=bool(record.get("has_player_option", False)),
                has_team_option=bool(record.get("has_team_option", False)),
                yearly_salaries=yearly,
                non_guaranteed_seasons=non_guaranteed,
            )
        )

        # Projections are keyed by slug; a contract row without one can't be
        # joined to a projection, so skip it (it still appears in salaries).
        if not slug:
            continue

        stats_row = _stats_for(player_name, nba_id, by_id, by_name)
        age = _age_from_stats(stats_row)
        mpg = _mpg_from_stats(stats_row)

        # Resolve the player's EPM identity ONCE, slug-first, with the miss
        # recorded (this is the one lookup the unmatched report counts). The
        # resolved row feeds the age/MPG backfill AND — because the engine
        # joins by name — its exact source name becomes the engine identity.
        # An unresolved player keeps the salary-frame name; the engine will
        # miss the same way, and the miss is already on the report.
        epm_identity = get_player_epm(
            epm_df, player_name, slug=slug, crosswalk=crosswalk, record_miss=True
        )
        age, mpg = _fill_age_mpg_from_epm(epm_identity, age, mpg)

        # Which actuals priced him (ruled 2026-08-25). Fallback applications
        # are logged separately from the unmatched report so genuine join
        # bugs ("no row anywhere") stay visible instead of drowning in
        # legitimate injured/rookie absences.
        epm_basis: str | None = None
        source_minutes: float | None = None
        source_games: int | None = None
        if epm_identity is not None:
            # .get: injected test frames may omit player_id; a row without
            # one can never be a fallback row (prior ids are always real).
            row_pid = epm_identity.get("player_id")
            if row_pid is not None and pd.notna(row_pid) and int(row_pid) in epm_prior_ids:
                epm_basis = (
                    f"{_season_label(API_ACTUALS_SEASON - 1)} actuals "
                    f"(no {_season_label(API_ACTUALS_SEASON)} season)"
                )
                logger.info(
                    "EPM fallback applied (prior season): %s priced off "
                    "%s",
                    player_name,
                    epm_basis,
                )
            else:
                epm_basis = f"{_season_label(API_ACTUALS_SEASON)} actuals"
                source_minutes, source_games = _source_actuals(epm_identity)

        engine_name = None
        if epm_identity is not None:
            engine_name = str(epm_identity["player_name"])
            if (
                engine_name != player_name
                and get_player_darko(darko_df, player_name) is not None
                and get_player_darko(darko_df, engine_name) is None
            ):
                # The EPM identity wins (it anchors year 1), but never
                # silently: this player's salary-frame name matched DARKO and
                # the canonical EPM name does not, so the year-2 DARKO forward
                # look will miss and fall back to the aging curve.
                logger.warning(
                    "engine identity %r (EPM canonical) loses the DARKO match "
                    "that %r had — year-2 forward look falls back to the "
                    "aging curve for this player",
                    engine_name,
                    player_name,
                )

        hist = minutes_history.get(nba_id) if nba_id is not None else None
        gp_history = list(hist["gp"]) if hist else []
        mpg_history = list(hist["mpg"]) if hist else []

        seasons = _project_player(
            player_name,
            str(record.get("team") or ""),
            age,
            mpg,
            contract,
            epm_df,
            darko_df,
            keys,
            gp_history=gp_history,
            mpg_history=mpg_history,
            engine_name=engine_name,
        )
        for season in seasons.values():
            source_counts[season.source] = source_counts.get(season.source, 0) + 1

        projections[slug] = DataballrPlayerProjection(
            player_name=player_name,
            nba_id=nba_id,
            age=age,
            seasons=seasons,
            epm_basis=epm_basis,
            source_minutes=source_minutes,
            source_games=source_games,
        )

    # Unmatched-player report: the export's identity join is the only recorded
    # lookup, so each player appears at most once. "No EPM row" is the precise
    # claim — some of these still price via DARKO; the rest fall to
    # replacement (each demotion is separately warned above).
    unmatched = epm_unmatched_report()
    if unmatched:
        # The claim must match what was actually searched: on the default
        # path, say whether a prior-season fallback pool even existed (an
        # operator debugging a miss must not rule out a missing prior cache
        # the report never consulted). Injected frames get the generic label.
        if epm_was_fetched and not api_cache_file(API_ACTUALS_SEASON - 1).exists():
            searched = "current season only; NO prior-season cache was available"
        else:
            searched = "current or prior season"
        logger.warning(
            "EPM join report: no row in the searched actuals (" + searched + ") for "
            "%d player(s) — legitimate absences or join bugs, check names: %s",
            len(unmatched),
            "; ".join(
                f"{e['name']} (slug={e['slug'] or '-'})"
                for e in sorted(unmatched, key=lambda e: (e["name"], e["slug"] or ""))
            ),
        )

    metadata = DataballrExportMetadata(
        repo="Isingla/nba-trade-analyzer",
        salary_cap=SALARY_CAP,
        salary_rows=len(salaries),
        epm_rows=len(epm_df),
        darko_rows=len(darko_df),
        stats_rows=len(stats_df),
        projection_seasons=keys,
        generated_from=list(_GENERATED_FROM),
        waa_formula=_WAA_FORMULA,
        source_counts=dict(sorted(source_counts.items())),
        # Only a fetched frame came from the cache file; an injected frame's
        # vintage is unknowable and must not borrow the file's date.
        epm_vintage=_epm_cache_vintage() if epm_was_fetched else None,
    )

    # Per-season tax/apron levels for the export window. Loud failure if the
    # window ever outgrows the constants table — silently missing thresholds
    # would read as "no Cap Sheet" downstream instead of a data bug.
    missing = [s for s in keys if s not in CAP_THRESHOLDS_BY_SEASON]
    if missing:
        raise ValueError(
            f"CAP_THRESHOLDS_BY_SEASON lacks season(s) {missing}; extend the "
            "table in engine/constants.py to cover the export window."
        )
    cap_thresholds = DataballrCapThresholds(
        note=_CAP_THRESHOLDS_NOTE,
        seasons={
            season: DataballrSeasonCapThresholds(**CAP_THRESHOLDS_BY_SEASON[season])
            for season in keys
        },
    )

    # Staleness pass-through: fetch_all_salaries stamps df.attrs on the
    # committed-CSV fallback path (loud-warn degradation, never silent).
    fallback = getattr(salary_df, "attrs", {}).get("bbref_fallback")
    source_note = (
        "BBREF FETCH FAILED — salaries exported from the committed CSV fallback "
        f"dated {fallback['csv_mtime']}; DATA MAY BE STALE (reason: {fallback['reason']})"
        if fallback
        else None
    )

    return DataballrExport(
        metadata=metadata,
        salaries=salaries,
        projections=projections,
        cap_holds=DataballrCapHolds(
            estimated=True,
            note=_CAP_HOLDS_NOTE,
            team_seasons=sum(len(seasons) for seasons in cap_holds.values()),
            totals=cap_holds,
        ),
        cap_thresholds=cap_thresholds,
        dead_money=build_dead_money(dead_money),
        source_note=source_note,
    )
