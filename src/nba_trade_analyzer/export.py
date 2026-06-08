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
   engine's replacement-relative WAR, WAA has no replacement offset and is
   scaled by ``WAA_IMPACT_WEIGHT``::

       waa = tanh_compress(
           impact * projected_minutes / FULL_SEASON_MINUTES
           * (EPM_TO_WINS_FACTOR * WAA_IMPACT_WEIGHT)
       )

   ``projected_minutes`` uses the same aging-availability haircut the engine's
   multi-year projection applies, so WAA and the engine's per-year minutes
   agree season-for-season.

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

import math

import pandas as pd
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from nba_trade_analyzer.data.crosswalk import Crosswalk, load_crosswalk
from nba_trade_analyzer.data.darko import fetch_darko_data
from nba_trade_analyzer.data.epm import (
    fetch_epm_data,
    get_player_epm,
    normalize_name,
)
from nba_trade_analyzer.data.players import fetch_player_stats
from nba_trade_analyzer.data.salaries import build_contract, fetch_all_salaries
from nba_trade_analyzer.engine.aging_curve import get_aging_factor
from nba_trade_analyzer.engine.constants import (
    EPM_TO_WINS_FACTOR,
    FULL_SEASON_MINUTES,
    MAX_PROJECTION_YEARS,
    MAX_WINS_ADDED,
    PROJECTED_GP_CAP,
    PROJECTED_GP_HEALTHY,
    SALARY_CAP,
)
from nba_trade_analyzer.engine.valuation import evaluate_player_multiyear
from nba_trade_analyzer.models.player import Player

# First season of the projection window; subsequent seasons follow yearly.
_FIRST_SEASON_START = 2025

# databallr's average-impact weighting on the EPM->wins factor. WAA answers
# "how much team quality" (centered on average), not "how much surplus over
# replacement", so it drops the replacement offset and scales down.
WAA_IMPACT_WEIGHT = 0.40

_WAA_FORMULA = (
    "impact * projected_minutes / FULL_SEASON_MINUTES "
    "* (EPM_TO_WINS_FACTOR * 0.40), tanh compressed; no replacement-level offset"
)

_GENERATED_FROM = (
    "src/nba_trade_analyzer/data/salaries.py",
    "src/nba_trade_analyzer/data/epm.py",
    "src/nba_trade_analyzer/data/darko.py",
    "src/nba_trade_analyzer/data/players.py",
    "src/nba_trade_analyzer/engine/constants.py",
    "src/nba_trade_analyzer/engine/aging_curve.py",
    "data/player_crosswalk.json",
)


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


class DataballrSeasonProjection(_CamelModel):
    waa: float
    impact: float
    mpg: float
    source: str


class DataballrPlayerProjection(_CamelModel):
    player_name: str
    nba_id: int | None
    age: int | None
    seasons: dict[str, DataballrSeasonProjection]


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


class DataballrExport(_CamelModel):
    metadata: DataballrExportMetadata
    salaries: list[DataballrSalaryRow]
    # Keyed by BBRef slug, matching CONTROL_RUNWAY_PROJECTIONS in databallr.
    projections: dict[str, DataballrPlayerProjection]


def season_keys() -> list[str]:
    """The fixed projection window, e.g. ``["2025-26", ..., "2029-30"]``."""
    return [
        f"{_FIRST_SEASON_START + i}-{(_FIRST_SEASON_START + i + 1) % 100:02d}"
        for i in range(MAX_PROJECTION_YEARS)
    ]


def _compress(raw_wins: float) -> float:
    return MAX_WINS_ADDED * math.tanh(raw_wins / MAX_WINS_ADDED)


def _projected_minutes(mpg: float, age: int, year_offset: int) -> float:
    """Minutes for a projected season, with the engine's availability haircut.

    Mirrors ``evaluate_player_multiyear``: hold current MPG flat, haircut games
    played by the aging availability proxy (capped at the healthy ceiling), so
    WAA's minutes agree with the engine's per-year minutes season-for-season.
    """
    availability_factor = min(1.0, get_aging_factor(age, year_offset))
    projected_gp = min(PROJECTED_GP_CAP, PROJECTED_GP_HEALTHY * availability_factor)
    return mpg * projected_gp


def compute_waa(impact: float, mpg: float, age: int, year_offset: int) -> float:
    """databallr WAA for one projected season (see module docstring)."""
    minutes = _projected_minutes(mpg, age, year_offset)
    raw_wins = (
        impact
        * minutes
        / FULL_SEASON_MINUTES
        * (EPM_TO_WINS_FACTOR * WAA_IMPACT_WEIGHT)
    )
    return _compress(raw_wins)


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
    epm_df: pd.DataFrame, player_name: str, age: int | None, mpg: float
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
    epm_row = get_player_epm(epm_df, player_name)
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


def _replacement_seasons(
    mpg: float, keys: list[str]
) -> dict[str, DataballrSeasonProjection]:
    rounded_mpg = round(mpg, 1)
    return {
        key: DataballrSeasonProjection(
            waa=0.0, impact=0.0, mpg=rounded_mpg, source="replacement"
        )
        for key in keys
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
) -> dict[str, DataballrSeasonProjection]:
    """Project a fixed 5-season window for one player.

    Returns replacement seasons when the player has no usable EPM/DARKO signal
    or no stats row (so age/minutes are unknown).
    """
    if age is None:
        return _replacement_seasons(mpg, keys)

    player = Player(
        name=player_name,
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
        return _replacement_seasons(mpg, keys)

    # The aging chain anchors on DARKO whenever any projected year used it
    # (year-1 DARKO, or the year-2 DARKO override); otherwise it anchors on EPM.
    anchor_source = (
        "darko"
        if any(yr.projection_source == "darko" for yr in multi.year_by_year)
        else "epm"
    )

    rounded_mpg = round(mpg, 1)
    seasons: dict[str, DataballrSeasonProjection] = {}
    for offset, key in enumerate(keys):
        if offset >= len(multi.year_by_year):
            seasons[key] = DataballrSeasonProjection(
                waa=0.0, impact=0.0, mpg=rounded_mpg, source="replacement"
            )
            continue
        year = multi.year_by_year[offset]
        impact = year.projected_epm
        seasons[key] = DataballrSeasonProjection(
            waa=round(compute_waa(impact, mpg, age, offset), 1),
            impact=round(impact, 2),
            mpg=rounded_mpg,
            source=map_source(year.projection_source, anchor_source),
        )
    return seasons


def build_export(
    *,
    salary_df: pd.DataFrame | None = None,
    epm_df: pd.DataFrame | None = None,
    darko_df: pd.DataFrame | None = None,
    stats_df: pd.DataFrame | None = None,
    crosswalk: Crosswalk | None = None,
) -> DataballrExport:
    """Assemble the full databallr cap-data payload.

    Every data source is injectable so the export is unit-testable with stub
    frames and never touches the network in tests. In normal use each source is
    fetched once (cached 24h, with the salaries CSV offline fallback).
    """
    salary_df = fetch_all_salaries() if salary_df is None else salary_df
    epm_df = fetch_epm_data() if epm_df is None else epm_df
    darko_df = fetch_darko_data() if darko_df is None else darko_df
    stats_df = fetch_player_stats() if stats_df is None else stats_df
    crosswalk = load_crosswalk() if crosswalk is None else crosswalk

    keys = season_keys()
    by_id, by_name = _stats_index(stats_df)

    salaries: list[DataballrSalaryRow] = []
    projections: dict[str, DataballrPlayerProjection] = {}
    source_counts: dict[str, int] = {}

    for record in salary_df.to_dict(orient="records"):
        slug = str(record.get("bbref_slug") or "").strip()
        player_name = str(record.get("player_name") or "").strip()
        if not player_name:
            continue

        contract = build_contract(record)
        salaries.append(
            DataballrSalaryRow(
                player_name=player_name,
                bbref_slug=slug,
                team=str(record.get("team") or ""),
                salary=int(record["salary"]),
                years_remaining=int(record["years_remaining"]),
                is_rookie_scale=bool(record.get("is_rookie_scale", False)),
                has_player_option=bool(record.get("has_player_option", False)),
                has_team_option=bool(record.get("has_team_option", False)),
                yearly_salaries=list(contract.yearly_salaries),
            )
        )

        # Projections are keyed by slug; a contract row without one can't be
        # joined to a projection, so skip it (it still appears in salaries).
        if not slug:
            continue

        nba_id = crosswalk.nba_id_for_slug(slug)
        stats_row = _stats_for(player_name, nba_id, by_id, by_name)
        age = _age_from_stats(stats_row)
        mpg = _mpg_from_stats(stats_row)
        age, mpg = _fill_age_mpg_from_epm(epm_df, player_name, age, mpg)

        seasons = _project_player(
            player_name,
            str(record.get("team") or ""),
            age,
            mpg,
            contract,
            epm_df,
            darko_df,
            keys,
        )
        for season in seasons.values():
            source_counts[season.source] = source_counts.get(season.source, 0) + 1

        projections[slug] = DataballrPlayerProjection(
            player_name=player_name,
            nba_id=nba_id,
            age=age,
            seasons=seasons,
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
    )

    return DataballrExport(
        metadata=metadata, salaries=salaries, projections=projections
    )
