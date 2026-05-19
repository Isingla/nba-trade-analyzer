"""Team-context adjustments for player valuation (Phase 5).

The base valuation pipeline (Phase 4 / 1.5) prices every player against a
fixed DOLLARS_PER_WIN — the same wins are worth the same to every team.
That ignores the obvious reality that a +4 win player is worth far more to
a 41-win bubble team than to a 22-win rebuilder, and a young point guard
slots into a young rebuild differently than into a championship core.

This module layers four adjustments on top of the base value:

1. **Win curve** — multiplicative. Sigmoid on the acquiring team's
   projected wins; peak marginal value sits near the playoff cutoff.
2. **Timeline** — additive. Penalize age gaps with the team's core.
3. **Positional fit** — additive. Bonus for filling thin positions,
   penalty for piling onto a logjam.
4. **Spacing** — additive. Shooters help spacing-poor teams; non-shooters
   compound the problem. Effect is muted on already-elite-spacing teams.

The win curve is multiplicative because it rescales the unit of value
itself (what a win is worth). The other three are additive and capped
percentages of context_value so they cannot compound into wild swings.
"""

from __future__ import annotations

import math

import pandas as pd

from nba_trade_analyzer.engine.constants import (
    DOLLARS_PER_WIN,
    LEAGUE_AVG_3PT_PCT,
    LEAGUE_AVG_3PT_RATE,
    POSITIONAL_MAX_ADJUSTMENT,
    POSITIONAL_MINUTES_THRESHOLD_HIGH,
    POSITIONAL_MINUTES_THRESHOLD_LOW,
    SPACING_MAX_ADJUSTMENT,
    TIMELINE_CORE_SIZE,
    TIMELINE_LAMBDA,
    TIMELINE_MAX_ADJUSTMENT,
    WIN_CURVE_MAX_MULTIPLIER,
    WIN_CURVE_MIDPOINT,
    WIN_CURVE_MIN_MULTIPLIER,
    WIN_CURVE_STEEPNESS,
)
from nba_trade_analyzer.models.player import Contract, Player
from nba_trade_analyzer.models.team_context import TeamContextValuation

# Two starter-plus-backup slots make up the ~96 game-minutes available at
# each of the five positions in a game (5 positions × 48 minutes).
MINUTES_PER_POSITION_SLOT = 48.0

# Roster entries with "G-F", "F-C", etc. split their minutes between the
# two listed positions. Anything else maps straight through.
_POSITION_SPLITS = {
    "G-F": ("G", "F"),
    "F-G": ("F", "G"),
    "F-C": ("F", "C"),
    "C-F": ("C", "F"),
    "PG-SG": ("PG", "SG"),
    "SG-SF": ("SG", "SF"),
    "SF-PF": ("SF", "PF"),
    "PF-C": ("PF", "C"),
}

# Map specific positions (PG/SG/SF/PF/C) down to the three coarse buckets
# (G / F / C) we use for fit calculations — positions are blurry in the
# modern NBA, three buckets is plenty of resolution.
_COARSE_POSITION = {
    "PG": "G",
    "SG": "G",
    "SF": "F",
    "PF": "F",
    "C": "C",
    "G": "G",
    "F": "F",
}


def _coarse_position(label: str | None) -> tuple[tuple[str, float], ...]:
    """Convert a position string into ``((bucket, weight), ...)`` pairs.

    Examples:
        ``"G"`` → ``(("G", 1.0),)``
        ``"G-F"`` → ``(("G", 0.5), ("F", 0.5))``
        ``"PF"`` → ``(("F", 1.0),)``
    """
    if not label:
        return ()
    label = label.strip().upper()
    if label in _POSITION_SPLITS:
        a, b = _POSITION_SPLITS[label]
        return (
            (_COARSE_POSITION.get(a, a), 0.5),
            (_COARSE_POSITION.get(b, b), 0.5),
        )
    return ((_COARSE_POSITION.get(label, label), 1.0),)


# --- Win curve ------------------------------------------------------------


def calculate_win_curve_multiplier(team_projected_wins: float) -> float:
    """Sigmoid multiplier on DOLLARS_PER_WIN driven by team win projection.

    Bubble teams get the steepest rescale (a marginal win flips them into
    the postseason); tanking teams get a floor multiplier; juggernauts
    approach the ceiling. Inverse of the sigmoid's tail behavior so output
    is bounded in ``[WIN_CURVE_MIN_MULTIPLIER, WIN_CURVE_MAX_MULTIPLIER]``.
    """
    span = WIN_CURVE_MAX_MULTIPLIER - WIN_CURVE_MIN_MULTIPLIER
    exponent = -WIN_CURVE_STEEPNESS * (team_projected_wins - WIN_CURVE_MIDPOINT)
    # Clamp the exponent to keep math.exp away from overflow at extreme inputs.
    exponent = max(-50.0, min(50.0, exponent))
    return WIN_CURVE_MIN_MULTIPLIER + span / (1.0 + math.exp(exponent))


# --- Timeline -------------------------------------------------------------


def _team_core_ages(roster: list[dict]) -> list[int]:
    """Return ages of the top ``TIMELINE_CORE_SIZE`` players by total minutes."""
    if not roster:
        return []
    enriched = []
    for entry in roster:
        gp = float(entry.get("GP", 0) or 0)
        mpg = float(entry.get("MPG", 0) or 0)
        minutes = gp * mpg
        age = entry.get("age")
        if age is None:
            continue
        enriched.append((minutes, int(age)))
    enriched.sort(key=lambda x: x[0], reverse=True)
    return [age for _, age in enriched[:TIMELINE_CORE_SIZE]]


def calculate_timeline_modifier(player_age: int, team_core_ages: list[int]) -> float:
    """Exponential decay on age gap, mapped to ``[-MAX, +MAX]``.

    A perfect age match returns ``+TIMELINE_MAX_ADJUSTMENT`` (boost); a
    large gap returns nearly ``-TIMELINE_MAX_ADJUSTMENT`` (penalty). If the
    team has no identifiable core, returns 0 (no signal either direction).
    """
    if not team_core_ages:
        return 0.0
    core_age = sum(team_core_ages) / len(team_core_ages)
    age_gap = abs(player_age - core_age)
    alignment = math.exp(-TIMELINE_LAMBDA * age_gap)
    return TIMELINE_MAX_ADJUSTMENT * (2.0 * alignment - 1.0)


# --- Positional fit -------------------------------------------------------


def _minutes_by_position(roster: list[dict]) -> dict[str, float]:
    """Sum minutes-per-game by coarse position bucket (G/F/C)."""
    totals: dict[str, float] = {"G": 0.0, "F": 0.0, "C": 0.0}
    for entry in roster:
        mpg = float(entry.get("MPG", 0) or 0)
        for bucket, weight in _coarse_position(entry.get("position")):
            if bucket in totals:
                totals[bucket] += mpg * weight
    return totals


def calculate_positional_modifier(
    player_position: str | None, team_roster: list[dict]
) -> float:
    """Bonus when filling a thin position, penalty when joining a logjam.

    Roster entries should be dicts with ``MPG`` and ``position`` keys.
    Without a position label on the incoming player we return 0 (no signal).
    """
    if not player_position or not team_roster:
        return 0.0
    minutes_by_pos = _minutes_by_position(team_roster)
    buckets = _coarse_position(player_position)
    if not buckets:
        return 0.0

    # If the player splits across two positions, weight by the split.
    modifier = 0.0
    high = POSITIONAL_MINUTES_THRESHOLD_HIGH
    low = POSITIONAL_MINUTES_THRESHOLD_LOW
    span = max(high - low, 1e-6)
    for bucket, weight in buckets:
        minutes_filled = minutes_by_pos.get(bucket, 0.0)
        if minutes_filled >= high:
            # Full logjam — saturate at -MAX.
            bucket_modifier = -POSITIONAL_MAX_ADJUSTMENT
        elif minutes_filled <= low:
            # Real positional need — saturate at +MAX.
            bucket_modifier = POSITIONAL_MAX_ADJUSTMENT
        else:
            # Linear interpolation across the band.
            t = (minutes_filled - low) / span
            bucket_modifier = POSITIONAL_MAX_ADJUSTMENT * (1.0 - 2.0 * t)
        modifier += bucket_modifier * weight
    return modifier


# --- Spacing --------------------------------------------------------------


def _player_spacing_score(rate: float, pct: float) -> float:
    """Volume-weighted shooting score, centered on league average.

    Returns positive for above-average shooters, negative for below-average
    or low-volume non-shooters. Magnitudes are intentionally small (~0-1
    range typical) so the modifier downstream stays modest.
    """
    # A player who doesn't shoot threes can't be a "spacer", regardless of
    # the percentage on a tiny sample.
    rate_signal = rate - LEAGUE_AVG_3PT_RATE
    pct_signal = pct - LEAGUE_AVG_3PT_PCT
    # Volume gates the percentage — a 50% shooter on 0.5 attempts/game is
    # still not stretching the floor.
    return rate_signal * 2.0 + pct_signal * (rate / max(LEAGUE_AVG_3PT_RATE, 1e-6))


def calculate_spacing_modifier(
    player_3pt_rate: float,
    player_3pt_pct: float,
    team_3pt_rate: float,
    team_3pt_pct: float,
) -> float:
    """Match player spacing profile against team spacing need.

    - Spacing-poor team + spacer → positive (need filled)
    - Spacing-poor team + non-spacer → negative (compounding problem)
    - Elite-spacing team → modifier shrinks toward zero in both directions

    Direction is set by the player (spacer vs. non-spacer); magnitude is set
    by team scarcity (poor team = large effect, elite team = muted). Output
    is bounded in ``[-SPACING_MAX_ADJUSTMENT, +SPACING_MAX_ADJUSTMENT]``.
    """
    player_score = _player_spacing_score(player_3pt_rate, player_3pt_pct)
    team_score = _player_spacing_score(team_3pt_rate, team_3pt_pct)

    # Compress both scores onto ~[-1, 1] so neither can dominate.
    contribution = math.tanh(player_score)
    team_pressure = math.tanh(team_score)  # positive = elite, negative = poor

    # Scarcity is 1.0 when the team is maximally spacing-poor and 0.0 when
    # they're already elite — spacing is "less of a constraint" then.
    scarcity = (1.0 - team_pressure) / 2.0
    scarcity = max(0.0, min(1.0, scarcity))

    return SPACING_MAX_ADJUSTMENT * contribution * scarcity


# --- Top-level entry point -----------------------------------------------


def _row_for(df: pd.DataFrame | None, player_name: str) -> pd.Series | None:
    if df is None or df.empty or "player_name" not in df.columns:
        return None
    match = df[df["player_name"] == player_name]
    if match.empty:
        return None
    return match.iloc[0]


def _player_3pt_profile(
    player: Player,
    player_stats_df: pd.DataFrame | None,
) -> tuple[float, float]:
    """Pull (rate, pct) for a player from stats, falling back to league avg."""
    row = _row_for(player_stats_df, player.name)
    if row is None:
        return LEAGUE_AVG_3PT_RATE, LEAGUE_AVG_3PT_PCT
    rate = row.get("FG3_RATE")
    pct = row.get("FG3_PCT")
    if rate is None or pd.isna(rate):
        rate = LEAGUE_AVG_3PT_RATE
    if pct is None or pd.isna(pct):
        pct = LEAGUE_AVG_3PT_PCT
    return float(rate), float(pct)


def _team_3pt_profile(
    acquiring_team_roster: list[dict],
) -> tuple[float, float]:
    """Minutes-weighted team 3PT rate and percentage from the roster dicts."""
    total_minutes = 0.0
    rate_weighted = 0.0
    pct_weighted = 0.0
    for entry in acquiring_team_roster:
        gp = float(entry.get("GP", 0) or 0)
        mpg = float(entry.get("MPG", 0) or 0)
        minutes = gp * mpg
        if minutes <= 0:
            continue
        rate = entry.get("FG3_RATE")
        pct = entry.get("FG3_PCT")
        if rate is None or (isinstance(rate, float) and math.isnan(rate)):
            rate = LEAGUE_AVG_3PT_RATE
        if pct is None or (isinstance(pct, float) and math.isnan(pct)):
            pct = LEAGUE_AVG_3PT_PCT
        rate_weighted += float(rate) * minutes
        pct_weighted += float(pct) * minutes
        total_minutes += minutes
    if total_minutes <= 0:
        return LEAGUE_AVG_3PT_RATE, LEAGUE_AVG_3PT_PCT
    return rate_weighted / total_minutes, pct_weighted / total_minutes


def _player_position(
    player: Player,
    epm_df: pd.DataFrame | None,
    player_stats_df: pd.DataFrame | None,
) -> str | None:
    """Find a position label, preferring EPM, then player_stats, then None."""
    for df in (epm_df, player_stats_df):
        row = _row_for(df, player.name)
        if row is None:
            continue
        pos = row.get("position") if "position" in row.index else None
        if pos is not None and not (isinstance(pos, float) and math.isnan(pos)):
            return str(pos)
    return None


def evaluate_player_in_team_context(
    player_value: float,
    wins_added: float,
    player: Player,
    contract: Contract,
    acquiring_team_wins: float,
    acquiring_team_roster: list[dict],
    epm_df: pd.DataFrame | None = None,
    player_stats_df: pd.DataFrame | None = None,
) -> TeamContextValuation:
    """Apply the four Phase 5 adjustments to a base player valuation.

    ``acquiring_team_roster`` is a list of dicts (typically rows from
    ``fetch_player_stats``) for the team the player would be joining.
    Required keys: ``MPG``, ``GP``, ``age``; optional: ``position``,
    ``FG3_RATE``, ``FG3_PCT``. Missing optional fields fall back to league
    averages so the function never crashes on partial data.
    """
    win_curve = calculate_win_curve_multiplier(acquiring_team_wins)

    # Rescale base value through the win curve. context_value is the new
    # "starting point" against which the three additive adjustments apply.
    context_value = wins_added * (DOLLARS_PER_WIN * win_curve)

    core_ages = _team_core_ages(acquiring_team_roster)
    timeline_mod = calculate_timeline_modifier(player.age, core_ages)

    position = _player_position(player, epm_df, player_stats_df)
    positional_mod = calculate_positional_modifier(position, acquiring_team_roster)

    player_rate, player_pct = _player_3pt_profile(player, player_stats_df)
    team_rate, team_pct = _team_3pt_profile(acquiring_team_roster)
    spacing_mod = calculate_spacing_modifier(
        player_rate, player_pct, team_rate, team_pct
    )

    timeline_adj = context_value * timeline_mod
    positional_adj = context_value * positional_mod
    spacing_adj = context_value * spacing_mod
    team_adjusted_value = context_value + timeline_adj + positional_adj + spacing_adj
    team_surplus = team_adjusted_value - contract.salary

    return TeamContextValuation(
        base_value=player_value,
        win_curve_multiplier=win_curve,
        timeline_modifier=timeline_mod,
        positional_modifier=positional_mod,
        spacing_modifier=spacing_mod,
        team_adjusted_value=team_adjusted_value,
        team_surplus=team_surplus,
    )
