"""Player valuation engine.

Three impact-metric paths, picked in order of preference per player:

1. **EPM** (Estimated Plus-Minus from dunksandthrees.com) — primary.
   Already isolates individual impact via RAPM, so no team adjustment is
   applied; the partial-team-adjustment hack from the NET_RATING era is
   bypassed entirely on this path.
2. **DARKO** (DPM projection from Kostya Medvedovsky's sheet) — secondary.
   Treated the same way as EPM mechanically: per-100-possessions impact
   scaled by minutes fraction, no team adjustment.
3. **NET_RATING** (team-adjusted on/off from nba_api) — fallback only.
   Kept for players who appear in neither EPM nor DARKO data (mostly
   end-of-bench, two-way, and very-low-sample players).

All three paths share the same downstream pipeline:
``raw_wins → tanh compression → DOLLARS_PER_WIN → surplus_value``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd

from nba_trade_analyzer.data.darko import fetch_darko_data, get_player_darko
from nba_trade_analyzer.data.epm import fetch_epm_data, get_player_epm
from nba_trade_analyzer.data.players import get_team_projected_wins
from nba_trade_analyzer.engine.aging_curve import get_aging_factor
from nba_trade_analyzer.engine.constants import (
    DOLLARS_PER_WIN,
    EPM_REPLACEMENT_LEVEL,
    EPM_TO_WINS_FACTOR,
    FULL_SEASON_MINUTES,
    LEAGUE_MEAN_WINS,
    MAX_PROJECTION_YEARS,
    MAX_WINS_ADDED,
    NET_RATING_TO_WINS_FACTOR,
    PROJECTED_GP_CAP,
    PROJECTED_GP_HEALTHY,
    PROJECTION_DISCOUNT_RATE,
    REPLACEMENT_LEVEL_NET_RATING,
    TEAM_ADJUSTMENT_WEIGHT,
)
from nba_trade_analyzer.engine.draft_picks import evaluate_draft_pick
from nba_trade_analyzer.engine.team_context import evaluate_player_in_team_context
from nba_trade_analyzer.models.draft_pick import DraftPick
from nba_trade_analyzer.models.player import Contract, Player
from nba_trade_analyzer.models.team_context import TeamContextValuation
from nba_trade_analyzer.models.trade import TradeAssets
from nba_trade_analyzer.models.valuation import (
    MultiYearValuation,
    PlayerValuation,
    YearProjection,
)


@dataclass(frozen=True)
class TeamContext:
    """Bundle of acquiring-team info needed for Phase 5 adjustments.

    Pass to ``evaluate_trade_assets`` to switch from base surplus to
    team-context-adjusted surplus. ``roster`` is a list of dicts (typically
    rows of ``fetch_player_stats``) for the acquiring team.
    """

    projected_wins: float
    roster: list[dict]
    player_stats_df: pd.DataFrame | None = None


FULL_CONFIDENCE_MINUTES = 2000.0
MIN_CONFIDENCE = 0.1


def calculate_adjusted_net_rating(
    player_net_rating: float, team_net_rating: float
) -> float:
    """Partially strip out team context, weighted by TEAM_ADJUSTMENT_WEIGHT."""
    return player_net_rating - (team_net_rating * TEAM_ADJUSTMENT_WEIGHT)


def calculate_wins_added(
    adjusted_net_rating: float, minutes_played: float, games_played: int
) -> float:
    """NET_RATING-path wins. Subtracts a replacement floor before scaling.

    `minutes_played` is total minutes for the season (MPG × GP). `games_played`
    is kept in the signature for forward compatibility with availability-aware
    adjustments but is not used in the current formula — scaling is purely
    minutes-based.
    """
    del games_played
    value_above_replacement = adjusted_net_rating - REPLACEMENT_LEVEL_NET_RATING
    minutes_fraction = minutes_played / FULL_SEASON_MINUTES
    raw_wins = (value_above_replacement * NET_RATING_TO_WINS_FACTOR) * minutes_fraction
    return MAX_WINS_ADDED * math.tanh(raw_wins / MAX_WINS_ADDED)


def calculate_wins_added_from_impact(impact: float, minutes_played: float) -> float:
    """EPM/DARKO-path wins, measured above replacement level.

    EPM centers on league *average* (0.0), not replacement level, so we shift
    by ``EPM_REPLACEMENT_LEVEL`` before scaling: a 0.0-EPM average starter is
    ``-EPM_REPLACEMENT_LEVEL`` (≈ +1.0) above the minimum-salary body you could
    sign to replace him, which is what actually generates surplus over a
    contract. The shift is a constant offset, so it preserves the relative
    ordering of players while pricing the median starter near fair value
    instead of as a large overpay.
    """
    impact_above_replacement = impact - EPM_REPLACEMENT_LEVEL
    minutes_fraction = minutes_played / FULL_SEASON_MINUTES
    raw_wins = impact_above_replacement * minutes_fraction * EPM_TO_WINS_FACTOR
    return MAX_WINS_ADDED * math.tanh(raw_wins / MAX_WINS_ADDED)


def calculate_player_value(wins_added: float) -> float:
    return wins_added * DOLLARS_PER_WIN


def calculate_surplus_value(player_value: float, salary: int) -> float:
    return player_value - salary


def _confidence_from_minutes(minutes_played: float) -> float:
    raw = minutes_played / FULL_CONFIDENCE_MINUTES
    return max(MIN_CONFIDENCE, min(1.0, raw))


def _evaluate_with_epm(
    player: Player,
    contract: Contract,
    epm_row: pd.Series,
    minutes_played: float,
) -> PlayerValuation:
    impact = float(epm_row["epm"])
    wins_added = calculate_wins_added_from_impact(impact, minutes_played)
    player_value = calculate_player_value(wins_added)
    surplus_value = calculate_surplus_value(player_value, contract.salary)
    return PlayerValuation(
        player_name=player.name,
        team=player.team,
        adjusted_net_rating=impact,
        wins_added=wins_added,
        player_value=player_value,
        surplus_value=surplus_value,
        salary=contract.salary,
        confidence=_confidence_from_minutes(minutes_played),
        metric_source="epm",
    )


def _evaluate_with_darko(
    player: Player,
    contract: Contract,
    darko_row: pd.Series,
    minutes_played: float,
) -> PlayerValuation:
    impact = float(darko_row["dpm"])
    wins_added = calculate_wins_added_from_impact(impact, minutes_played)
    player_value = calculate_player_value(wins_added)
    surplus_value = calculate_surplus_value(player_value, contract.salary)
    return PlayerValuation(
        player_name=player.name,
        team=player.team,
        adjusted_net_rating=impact,
        wins_added=wins_added,
        player_value=player_value,
        surplus_value=surplus_value,
        salary=contract.salary,
        confidence=_confidence_from_minutes(minutes_played),
        metric_source="darko",
    )


def _evaluate_with_net_rating(
    player: Player,
    contract: Contract,
    team_net_rating: float,
    minutes_played: float,
    games_played: int,
) -> PlayerValuation:
    net_rating = float(player.stats.get("NET_RATING", 0.0))
    adjusted = calculate_adjusted_net_rating(net_rating, team_net_rating)
    wins_added = calculate_wins_added(adjusted, minutes_played, games_played)
    player_value = calculate_player_value(wins_added)
    surplus_value = calculate_surplus_value(player_value, contract.salary)
    return PlayerValuation(
        player_name=player.name,
        team=player.team,
        adjusted_net_rating=adjusted,
        wins_added=wins_added,
        player_value=player_value,
        surplus_value=surplus_value,
        salary=contract.salary,
        confidence=_confidence_from_minutes(minutes_played),
        metric_source="net_rating",
    )


def evaluate_player(
    player: Player,
    contract: Contract,
    team_net_rating: float = 0.0,
    epm_df: pd.DataFrame | None = None,
    darko_df: pd.DataFrame | None = None,
) -> PlayerValuation:
    """Evaluate a single player, picking the best available impact source.

    Pre-fetch ``epm_df`` and ``darko_df`` once when scoring many players —
    each one is a network call the first time. If left unset, this function
    will fetch them on demand (cached for 24h).
    """
    games_played = int(player.stats.get("GP", 0))
    mpg = float(player.stats.get("MPG", 0.0))
    minutes_played = mpg * games_played

    if epm_df is None:
        epm_df = fetch_epm_data()
    epm_row = get_player_epm(epm_df, player.name) if epm_df is not None else None
    if epm_row is not None:
        return _evaluate_with_epm(player, contract, epm_row, minutes_played)

    if darko_df is None:
        darko_df = fetch_darko_data()
    darko_row = (
        get_player_darko(darko_df, player.name) if darko_df is not None else None
    )
    if darko_row is not None:
        return _evaluate_with_darko(player, contract, darko_row, minutes_played)

    return _evaluate_with_net_rating(
        player, contract, team_net_rating, minutes_played, games_played
    )


def evaluate_player_multiyear(
    player: Player,
    contract: Contract,
    team_net_rating: float = 0.0,
    epm_df: pd.DataFrame | None = None,
    darko_df: pd.DataFrame | None = None,
) -> MultiYearValuation:
    """Project surplus across every remaining contract year.

    Year 1 mirrors ``evaluate_player`` exactly. Year 2 prefers DARKO's
    forward projection when the year-1 source was EPM (so DARKO is used
    as a true projection, not redundantly with itself); otherwise it
    falls back to the aging curve applied to the year-1 impact. Years 3+
    anchor on the year-2 EPM and apply the aging curve compounding for
    each additional year. Each year is discounted by
    ``PROJECTION_DISCOUNT_RATE`` to reflect projection uncertainty, and
    the horizon is capped at ``MAX_PROJECTION_YEARS``.

    Salary is held flat across years until a salary data source exists
    that exposes per-year salary escalation.
    """
    if epm_df is None:
        epm_df = fetch_epm_data()
    if darko_df is None:
        darko_df = fetch_darko_data()

    base = evaluate_player(
        player, contract, team_net_rating, epm_df=epm_df, darko_df=darko_df
    )

    horizon = min(contract.years_remaining, MAX_PROJECTION_YEARS)
    if horizon == 0:
        return MultiYearValuation(
            player_name=player.name,
            current_age=player.age,
            years_remaining=0,
            current_season_surplus=0.0,
            total_contract_surplus=0.0,
            year_by_year=[],
            primary_metric_source=base.metric_source,
        )

    # adjusted_net_rating holds EPM / DPM / adjusted NR depending on the
    # year-1 source — it's our impact anchor in the metric's native units.
    current_impact = base.adjusted_net_rating
    current_mpg = float(player.stats.get("MPG", 0.0))

    # Only override year 2 with DARKO when the year-1 source was EPM. If
    # DARKO was already year 1, projecting it again as a "forward look"
    # would be circular.
    darko_row = None
    if base.metric_source == "epm" and darko_df is not None:
        darko_row = get_player_darko(darko_df, player.name)

    year_results: list[YearProjection] = []
    year_2_anchor: float = current_impact  # placeholder; set in year_offset == 1

    for year_offset in range(horizon):
        if year_offset == 0:
            year_results.append(
                YearProjection(
                    year=1,
                    projected_age=player.age,
                    projected_epm=current_impact,
                    projected_wins_added=base.wins_added,
                    projected_value=base.player_value,
                    salary=float(contract.salary),
                    discount_factor=1.0,
                    discounted_surplus=float(base.surplus_value),
                    projection_source=base.metric_source,
                )
            )
            continue

        if year_offset == 1:
            if darko_row is not None:
                # DARKO DPM is on a ~40%-compressed scale vs EPM (see
                # DARKO_TO_EPM_SLOPE in constants.py). We intentionally use
                # the raw DPM value — its regression-to-mean is a feature
                # of DARKO's Kalman filter that we want to preserve as a
                # conservative year-2 projection.
                projected_impact = float(darko_row["dpm"])
                source = "darko"
            else:
                projected_impact = current_impact * get_aging_factor(player.age, 1)
                source = "aging_curve"
            year_2_anchor = projected_impact
        else:
            projected_impact = year_2_anchor * get_aging_factor(
                player.age + 1, year_offset - 1
            )
            source = "aging_curve"

        # Project minutes: hold current MPG flat, haircut GP by the aging
        # availability proxy (older players miss more games), cap at the
        # league-healthy ceiling so growth-phase aging factors > 1.0 don't
        # inflate a player into an 80+ GP season.
        availability_factor = min(1.0, get_aging_factor(player.age, year_offset))
        projected_gp = min(PROJECTED_GP_CAP, PROJECTED_GP_HEALTHY * availability_factor)
        projected_minutes = current_mpg * projected_gp

        wins_added = calculate_wins_added_from_impact(
            projected_impact, projected_minutes
        )
        player_value = calculate_player_value(wins_added)

        year_salary = float(contract.salary)
        discount_factor = 1.0 / (1.0 + PROJECTION_DISCOUNT_RATE) ** year_offset
        year_surplus = (player_value - year_salary) * discount_factor

        year_results.append(
            YearProjection(
                year=year_offset + 1,
                projected_age=player.age + year_offset,
                projected_epm=projected_impact,
                projected_wins_added=wins_added,
                projected_value=player_value,
                salary=year_salary,
                discount_factor=discount_factor,
                discounted_surplus=year_surplus,
                projection_source=source,
            )
        )

    total_surplus = sum(yr.discounted_surplus for yr in year_results)

    return MultiYearValuation(
        player_name=player.name,
        current_age=player.age,
        years_remaining=horizon,
        current_season_surplus=float(base.surplus_value),
        total_contract_surplus=total_surplus,
        year_by_year=year_results,
        primary_metric_source=base.metric_source,
    )


def _draft_pick_team_wins(
    pick: DraftPick, player_stats_df: pd.DataFrame | None
) -> float:
    """Projected wins of the team whose record sets ``pick``'s position.

    Falls back to the league mean when no stats are supplied, or when the
    originating team isn't found in the data (``get_team_projected_wins``
    returns 0.0 in that case, which would otherwise read as a 0-win team and
    wrongly value the pick as a number-one slot).
    """
    if player_stats_df is None:
        return LEAGUE_MEAN_WINS
    wins = get_team_projected_wins(player_stats_df, pick.team)
    return wins if wins > 0 else LEAGUE_MEAN_WINS


def evaluate_trade_assets(
    trade_assets: TradeAssets,
    team_net_rating: float = 0.0,
    epm_df: pd.DataFrame | None = None,
    darko_df: pd.DataFrame | None = None,
    team_context: TeamContext | None = None,
    player_stats_df: pd.DataFrame | None = None,
) -> float:
    """Sum total-contract surplus across the player package, plus draft picks.

    Multi-year by default: each player's full contract is projected
    via ``evaluate_player_multiyear`` and the discounted surplus across
    all remaining years is summed. Single-season surplus is still
    available per-player via the detailed breakdown.

    When ``team_context`` is provided, year 1 surplus is replaced with
    the team-context-adjusted year-1 surplus from
    ``evaluate_player_in_team_context``; years 2+ are still projected
    via the multi-year pipeline. This avoids re-running the (expensive)
    team-context computation for every projected year while still
    letting team fit shape the immediate trade impact.

    Draft picks are valued team-aware via ``evaluate_draft_pick``: each pick's
    position is estimated from its originating team's projected wins, read from
    ``player_stats_df`` (or ``team_context.player_stats_df`` when omitted).
    Without any stats, picks fall back to a league-average team.
    """
    total = 0.0
    for entry in trade_assets.players:
        multi = evaluate_player_multiyear(
            entry.player,
            entry.contract,
            team_net_rating,
            epm_df=epm_df,
            darko_df=darko_df,
        )

        if team_context is None:
            total += multi.total_contract_surplus
            continue

        # Replace year-1 surplus with team-adjusted year-1 surplus
        base = evaluate_player(
            entry.player,
            entry.contract,
            team_net_rating,
            epm_df=epm_df,
            darko_df=darko_df,
        )
        adjusted = evaluate_player_in_team_context(
            player_value=base.player_value,
            wins_added=base.wins_added,
            player=entry.player,
            contract=entry.contract,
            acquiring_team_wins=team_context.projected_wins,
            acquiring_team_roster=team_context.roster,
            epm_df=epm_df,
            player_stats_df=team_context.player_stats_df,
        )
        total += adjusted.team_surplus + (
            multi.total_contract_surplus - multi.current_season_surplus
        )

    pick_stats_df = player_stats_df
    if pick_stats_df is None and team_context is not None:
        pick_stats_df = team_context.player_stats_df
    for pick in trade_assets.picks:
        total += evaluate_draft_pick(pick, _draft_pick_team_wins(pick, pick_stats_df))

    return total


def evaluate_trade_assets_detailed(
    trade_assets: TradeAssets,
    team_context: TeamContext,
    team_net_rating: float = 0.0,
    epm_df: pd.DataFrame | None = None,
    darko_df: pd.DataFrame | None = None,
) -> list[tuple[PlayerValuation, TeamContextValuation, MultiYearValuation]]:
    """Per-player breakdown: single-season base, team-adjusted, multi-year.

    Returned triples expose all three views so callers can show the
    single-season number (transparency), the team-context-adjusted year-1
    number (fit), and the full-contract surplus (trade-decisive metric).
    """
    out: list[tuple[PlayerValuation, TeamContextValuation, MultiYearValuation]] = []
    for entry in trade_assets.players:
        base = evaluate_player(
            entry.player,
            entry.contract,
            team_net_rating,
            epm_df=epm_df,
            darko_df=darko_df,
        )
        adjusted = evaluate_player_in_team_context(
            player_value=base.player_value,
            wins_added=base.wins_added,
            player=entry.player,
            contract=entry.contract,
            acquiring_team_wins=team_context.projected_wins,
            acquiring_team_roster=team_context.roster,
            epm_df=epm_df,
            player_stats_df=team_context.player_stats_df,
        )
        multi = evaluate_player_multiyear(
            entry.player,
            entry.contract,
            team_net_rating,
            epm_df=epm_df,
            darko_df=darko_df,
        )
        out.append((base, adjusted, multi))
    return out
