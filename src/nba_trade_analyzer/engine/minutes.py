"""Minutes projection: two independent models multiplied (issue 2.2).

WAR is priced on minutes, and minutes are projected as::

    projected_minutes = projected_games * projected_mpg

The two models stay separable on purpose so an "injury-prone" signal (games)
and a "reduced role" signal (mpg) never collapse into one number:

- ``project_games`` — recency-weighted games-missed % + an age durability term.
- ``project_mpg`` — a recency-weighted prior MPG nudged by impact and salary.

v1 applies a **flat per-minute impact** across the projected minutes. Within-game
effectiveness degradation (fatigue curves, minute-restriction reduced
effectiveness) is a deliberate future TODO — see ``docs`` backlog — and is NOT
modeled here.

Every function is pure: it takes already-fetched history arrays, so the models
are unit-testable offline and reusable by both the export pipeline and the
backtest harness.
"""

from __future__ import annotations

from dataclasses import dataclass

from nba_trade_analyzer.engine.constants import (
    GAMES_AGE_DURABILITY_PIVOT,
    GAMES_AGE_MISSED_PER_YEAR,
    GAMES_RECENCY_DECAY,
    MPG_CEILING,
    MPG_FLOOR,
    MPG_IMPACT_COEF,
    MPG_IMPACT_REF,
    MPG_RECENCY_DECAY,
    MPG_SALARY_COEF,
    MPG_SALARY_REF,
    PROJECTED_GAMES_CEILING,
    PROJECTED_GAMES_FLOOR,
    PROJECTED_GAMES_NO_HISTORY,
    REGULAR_SEASON_GAMES,
)


@dataclass(frozen=True)
class MinutesProjection:
    """Inspectable output: games and MPG kept separate, plus their product."""

    projected_games: float
    projected_mpg: float

    @property
    def projected_minutes(self) -> float:
        return self.projected_games * self.projected_mpg


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _recency_weights(n: int, decay: float) -> list[float]:
    """Geometric weights for a history ordered oldest -> latest.

    The latest season (last element) gets weight 1.0; each season further back
    is multiplied by ``decay`` again, so the most recent season dominates.
    """
    return [decay ** (n - 1 - i) for i in range(n)]


def _games_missed_pct(gp: float) -> float:
    """Share of an 82-game season missed, clamped to [0, 1]."""
    played = _clamp(gp, 0.0, float(REGULAR_SEASON_GAMES))
    return _clamp(1.0 - played / REGULAR_SEASON_GAMES, 0.0, 1.0)


def recency_weighted_games_missed(
    gp_history: list[float],
    *,
    decay: float = GAMES_RECENCY_DECAY,
    flat_average: bool = False,
) -> float | None:
    """Recency-weighted games-missed fraction from a GP history (oldest first).

    Returns ``None`` when there is no history. ``flat_average=True`` swaps the
    geometric recency weighting for an equal-weight average (the documented
    fallback flag).
    """
    if not gp_history:
        return None
    n = len(gp_history)
    weights = [1.0] * n if flat_average else _recency_weights(n, decay)
    missed = [_games_missed_pct(gp) for gp in gp_history]
    total_weight = sum(weights)
    if total_weight == 0:
        return None
    return sum(w * m for w, m in zip(weights, missed)) / total_weight


def _age_missed_pct(age: int, year_offset: int) -> float:
    """Extra games-missed fraction from age, at the projected season's age."""
    projected_age = age + year_offset
    years_past_pivot = max(0, projected_age - GAMES_AGE_DURABILITY_PIVOT)
    return years_past_pivot * GAMES_AGE_MISSED_PER_YEAR


def project_games(
    gp_history: list[float],
    age: int,
    *,
    year_offset: int = 0,
    decay: float = GAMES_RECENCY_DECAY,
    flat_average: bool = False,
) -> float:
    """Project games played for the season ``year_offset`` years out.

    Combines the recency-weighted games-missed rate with an age durability
    penalty, then maps the surviving availability onto an 82-game season,
    bounded by ``[PROJECTED_GAMES_FLOOR, PROJECTED_GAMES_CEILING]``.
    """
    base_missed = recency_weighted_games_missed(
        gp_history, decay=decay, flat_average=flat_average
    )
    if base_missed is None:
        # No usable history: anchor on a healthy baseline, then age-haircut it.
        base_games = PROJECTED_GAMES_NO_HISTORY
        base_missed = 1.0 - base_games / REGULAR_SEASON_GAMES
    total_missed = _clamp(base_missed + _age_missed_pct(age, year_offset), 0.0, 1.0)
    games = REGULAR_SEASON_GAMES * (1.0 - total_missed)
    return _clamp(games, PROJECTED_GAMES_FLOOR, PROJECTED_GAMES_CEILING)


def recency_weighted_mpg(
    mpg_history: list[float],
    *,
    decay: float = MPG_RECENCY_DECAY,
    flat_average: bool = False,
) -> float | None:
    """Recency-weighted prior MPG from a history ordered oldest -> latest."""
    if not mpg_history:
        return None
    n = len(mpg_history)
    weights = [1.0] * n if flat_average else _recency_weights(n, decay)
    total_weight = sum(weights)
    if total_weight == 0:
        return None
    return sum(w * m for w, m in zip(weights, mpg_history)) / total_weight


def project_mpg(
    prior_mpg: float,
    impact: float,
    salary_share: float,
    *,
    flat: bool = False,
) -> float:
    """Project MPG from a prior MPG anchor nudged by impact and salary.

    ``salary_share`` is salary as a fraction of the cap (e.g. 0.35 for a max).
    ``flat=True`` returns the bounded prior MPG with no impact/salary nudge
    (the documented fallback). Both nudges are small so prior role dominates.
    """
    if flat:
        return _clamp(prior_mpg, MPG_FLOOR, MPG_CEILING)
    nudged = (
        prior_mpg
        + MPG_IMPACT_COEF * (impact - MPG_IMPACT_REF)
        + MPG_SALARY_COEF * (salary_share - MPG_SALARY_REF)
    )
    return _clamp(nudged, MPG_FLOOR, MPG_CEILING)


def project_minutes(
    gp_history: list[float],
    mpg_history: list[float],
    age: int,
    impact: float,
    salary_share: float,
    *,
    year_offset: int = 0,
    flat_average: bool = False,
    flat_mpg: bool = False,
) -> MinutesProjection:
    """Full minutes projection: games x mpg, both kept inspectable."""
    games = project_games(
        gp_history, age, year_offset=year_offset, flat_average=flat_average
    )
    prior_mpg = recency_weighted_mpg(mpg_history, flat_average=flat_average) or 0.0
    mpg = project_mpg(prior_mpg, impact, salary_share, flat=flat_mpg)
    return MinutesProjection(projected_games=games, projected_mpg=mpg)
