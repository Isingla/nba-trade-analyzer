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

import math

import pandas as pd
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from nba_trade_analyzer.data.cap_holds import load_cap_holds
from nba_trade_analyzer.data.crosswalk import Crosswalk, load_crosswalk
from nba_trade_analyzer.data.darko import fetch_darko_data
from nba_trade_analyzer.data.epm import (
    fetch_epm_data,
    get_player_epm,
    normalize_name,
)
from nba_trade_analyzer.data.guarantees import NonGuaranteeResolver
from nba_trade_analyzer.data.players import fetch_player_stats
from nba_trade_analyzer.data.salaries import build_contract, fetch_all_salaries
from nba_trade_analyzer.engine.constants import (
    EPM_TO_WINS_FACTOR,
    FULL_SEASON_MINUTES,
    MAX_PROJECTION_YEARS,
    MAX_WINS_ADDED,
    PROJECTED_GAMES_NO_HISTORY,
    SALARY_CAP,
)
from nba_trade_analyzer.engine.minutes import project_games, project_mpg, recency_weighted_mpg
from nba_trade_analyzer.engine.valuation import evaluate_player_multiyear
from nba_trade_analyzer.models.player import Player

# Completed seasons used as the GP/MPG history feeding the minutes models,
# ordered oldest -> latest. These are the seasons "available before" the
# 2025-26 projection window, so the projection never leaks future data.
_HISTORY_SEASONS = ("2022-23", "2023-24", "2024-25")

# First season of the projection window; subsequent seasons follow yearly.
_FIRST_SEASON_START = 2025

# databallr's average-impact weighting on the EPM->wins factor. WAA answers
# "how much team quality" (centered on average), not "how much surplus over
# replacement", so it drops the replacement offset and scales down.
WAA_IMPACT_WEIGHT = 0.40

_WAA_FORMULA = (
    "impact * projected_minutes / FULL_SEASON_MINUTES "
    "* (EPM_TO_WINS_FACTOR * 0.40), tanh compressed; no replacement-level offset. "
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


class DataballrExport(_CamelModel):
    metadata: DataballrExportMetadata
    salaries: list[DataballrSalaryRow]
    # Keyed by BBRef slug, matching CONTROL_RUNWAY_PROJECTIONS in databallr.
    projections: dict[str, DataballrPlayerProjection]
    # Per-team-per-season own-FA cap-hold totals (Tier 3c, Phase A). Team-level,
    # NOT folded into `salaries`.
    cap_holds: DataballrCapHolds


def season_keys() -> list[str]:
    """The fixed projection window, e.g. ``["2025-26", ..., "2029-30"]``."""
    return [
        f"{_FIRST_SEASON_START + i}-{(_FIRST_SEASON_START + i + 1) % 100:02d}"
        for i in range(MAX_PROJECTION_YEARS)
    ]


def _compress(raw_wins: float) -> float:
    return MAX_WINS_ADDED * math.tanh(raw_wins / MAX_WINS_ADDED)


def compute_waa(impact: float, minutes: float) -> float:
    """databallr WAA for one projected season from total projected minutes.

    Minutes now come from the two-model minutes projection (games x mpg, issue
    2.2) instead of a flat-72 assumption, so WAA and the TS WAR/surplus path
    are priced on the SAME availability-adjusted minutes.
    """
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
        return _replacement_seasons(mpg, keys, gp_history=gp_history, age=age)

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
        proj_mpg = round(project_mpg(prior_mpg, impact, salary_share), 1)
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
    epm_df = fetch_epm_data() if epm_df is None else epm_df
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
        age, mpg = _fill_age_mpg_from_epm(epm_df, player_name, age, mpg)

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
        metadata=metadata,
        salaries=salaries,
        projections=projections,
        cap_holds=DataballrCapHolds(
            estimated=True,
            note=_CAP_HOLDS_NOTE,
            team_seasons=sum(len(seasons) for seasons in cap_holds.values()),
            totals=cap_holds,
        ),
    )
