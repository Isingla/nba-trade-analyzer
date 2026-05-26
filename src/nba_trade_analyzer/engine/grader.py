"""Trade grader (Phase 7).

A pure *consumer* layer on top of the existing engines — it does not modify
any of them. The pipeline:

1. **Legality** (`check_trade_legality`). Illegal trades short-circuit: the
   returned :class:`TradeGrade` has ``is_legal=False`` and both team grades
   ``None``. No surplus analysis is run on an illegal trade.
2. **Valuation** (`evaluate_trade_assets_detailed`, `evaluate_player_multiyear`,
   `evaluate_draft_pick`). Produces per-player base/team-context/multi-year
   views plus team-aware pick values for both sides.
3. **Translation.** Each side gets a 0-100 score, a verdict label, seven
   :class:`MetricBreakdown`\\s (impact, contract, win curve, timeline,
   positional fit, spacing) and a draft-capital summary, plus a short
   basketball-prose write-up.

The score is zero-sum by construction: it is driven by team-agnostic
multi-year surplus plus team-agnostic pick values, so what one side gains in
surplus the other gives up. Team context (win curve, fit, spacing) shapes the
*breakdowns and prose* — the texture of the deal — rather than the headline
number, which keeps the score interpretable and aligned with the documented
surplus calibration.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

import pandas as pd

from nba_trade_analyzer.data.darko import fetch_darko_data
from nba_trade_analyzer.data.epm import fetch_epm_data, get_player_epm, normalize_name
from nba_trade_analyzer.data.players import get_team_3pt_stats, get_team_projected_wins
from nba_trade_analyzer.engine.constants import (
    CURRENT_SEASON_START,
    LEAGUE_AVG_3PT_PCT,
    LEAGUE_MEAN_WINS,
)
from nba_trade_analyzer.engine.draft_picks import (
    FIRST_PICK,
    FIRST_ROUND_PICKS,
    LAST_PICK,
    estimate_pick_position,
    evaluate_draft_pick,
)
from nba_trade_analyzer.engine.salary_rules import check_trade_legality
from nba_trade_analyzer.engine.team_context import (
    _coarse_position,
    _minutes_by_position,
    _team_core_ages,
    calculate_win_curve_multiplier,
)
from nba_trade_analyzer.engine.valuation import (
    TeamContext,
    evaluate_player,
    evaluate_player_multiyear,
    evaluate_trade_assets_detailed,
)
from nba_trade_analyzer.models.grade import (
    DraftCapitalBreakdown,
    MetricBreakdown,
    TeamGrade,
    TradeGrade,
)
from nba_trade_analyzer.models.draft_pick import DraftPick
from nba_trade_analyzer.models.team import Team
from nba_trade_analyzer.models.trade import Trade, TradeAssets

# Calibrated against the documented surplus targets: a star-on-a-bargain-deal
# haul (ratio ≈ 1) lands ~80, a fleecing tips past it, and a fair swap (ratio
# ≈ 0) sits at 50. See module docstring for why the score is surplus-driven.
SCORE_SCALING_FACTOR = 30.0

_MILLION = 1_000_000.0


# ---------------------------------------------------------------------------
# Verdict labels
# ---------------------------------------------------------------------------


def get_verdict(score: int) -> str:
    """Map a 0-100 score to a fan-readable verdict label."""
    if score >= 85:
        return "Highway Robbery"
    if score >= 70:
        return "Clear Win"
    if score >= 55:
        return "Smart Deal"
    if score >= 45:
        return "Fair Trade"
    if score >= 35:
        return "Slight Overpay"
    if score >= 20:
        return "Overpay"
    return "Fleeced"


# ---------------------------------------------------------------------------
# Tier maps (thresholds copied verbatim from the Phase 7 spec)
# ---------------------------------------------------------------------------


def _epm_tier(epm: float) -> str:
    if epm >= 5.0:
        return "Elite (top 5%)"
    if epm >= 3.0:
        return "All-Star Level (top 15%)"
    if epm >= 1.5:
        return "Above Average Starter (top 30%)"
    if epm >= 0.0:
        return "Average (top 50%)"
    if epm >= -1.5:
        return "Below Average"
    return "Replacement Level"


def _contract_tier(per_year_surplus: float) -> str:
    if per_year_surplus > 15 * _MILLION:
        return "Elite Value"
    if per_year_surplus > 5 * _MILLION:
        return "Good Value"
    if per_year_surplus > 0:
        return "Fair Value"
    if per_year_surplus > -5 * _MILLION:
        return "Slight Overpay"
    if per_year_surplus > -15 * _MILLION:
        return "Overpay"
    return "Bad Contract"


def _win_curve_tier(multiplier: float) -> str:
    if multiplier >= 1.8:
        return "Championship Contender"
    if multiplier >= 1.4:
        return "Contender"
    if multiplier >= 1.0:
        return "Playoff Team"
    if multiplier >= 0.7:
        return "Play-In Team"
    return "Rebuilding"


def _timeline_tier(modifier: float) -> str:
    if modifier >= 0.10:
        return "Strong Fit"
    if modifier >= 0.03:
        return "Good Fit"
    if modifier >= -0.03:
        return "Neutral"
    if modifier >= -0.10:
        return "Slight Mismatch"
    return "Timeline Mismatch"


def _positional_tier(modifier: float) -> str:
    if modifier >= 0.05:
        return "Fills a Need"
    if modifier >= -0.02:
        return "Neutral Fit"
    return "Roster Overlap"


def _spacing_tier(modifier: float) -> str:
    if modifier >= 0.04:
        return "Significant Boost"
    if modifier >= 0.01:
        return "Mild Boost"
    if modifier >= -0.01:
        return "Neutral"
    return "Hurts Spacing"


# ---------------------------------------------------------------------------
# Small formatting / lookup helpers
# ---------------------------------------------------------------------------


def _short_label(team_name: str) -> str:
    """'New York Knicks' -> 'Knicks', 'Oklahoma City Thunder' -> 'Thunder'."""
    parts = team_name.split()
    return parts[-1] if parts else team_name


def _ordinal(n: int | None) -> str:
    if n is None:
        return ""
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _natural_join(items: list[str]) -> str:
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + f", and {items[-1]}"


def _surplus_label(per_year: float) -> str:
    millions = per_year / _MILLION
    if per_year >= 0:
        return f"+${millions:.1f}M/yr surplus"
    return f"-${abs(millions):.1f}M/yr deficit"


def _pct_label(fraction: float) -> str:
    """Format a modifier as a signed percentage, avoiding a '-0%' artifact."""
    pct = round(fraction * 100)
    return "+0%" if pct == 0 else f"{pct:+d}%"


def _possessive(label: str) -> str:
    """'Knicks' -> \"Knicks'\", 'Heat' -> \"Heat's\"."""
    return f"{label}'" if label.endswith("s") else f"{label}'s"


def _team_wins(stats_df: pd.DataFrame | None, abbr: str) -> float:
    if stats_df is None or len(stats_df) == 0:
        return LEAGUE_MEAN_WINS
    wins = get_team_projected_wins(stats_df, abbr)
    return wins if wins > 0 else LEAGUE_MEAN_WINS


def _roster_dicts(stats_df: pd.DataFrame | None, abbr: str) -> list[dict]:
    if stats_df is None or len(stats_df) == 0 or "team" not in stats_df.columns:
        return []
    return stats_df[stats_df["team"] == abbr].to_dict(orient="records")


def _stats_row(stats_df: pd.DataFrame | None, name: str) -> pd.Series | None:
    if stats_df is None or len(stats_df) == 0 or "player_name" not in stats_df.columns:
        return None
    key = normalize_name(name)
    norm = stats_df["player_name"].astype(str).map(normalize_name)
    match = stats_df[norm == key]
    if match.empty:
        return None
    return match.iloc[0]


def _pick_team_wins(pick: DraftPick, stats_df: pd.DataFrame | None) -> float:
    return _team_wins(stats_df, pick.team)


def _projected_pick_number(pick: DraftPick, team_wins: float) -> int:
    year_offset = max(0, pick.year - CURRENT_SEASON_START)
    position = estimate_pick_position(team_wins, year_offset)
    if pick.round == 2:
        position += FIRST_ROUND_PICKS
    return min(LAST_PICK, max(FIRST_PICK, round(position)))


def _team_spacing(stats_df: pd.DataFrame | None, abbr: str) -> tuple[int | None, str]:
    """Return ``(league_spacing_rank, label)`` where label is good/average/poor.

    Rank is 1 = best team 3P%. When only one team's rows are present (e.g. a
    unit-test fixture), rank is ``None`` and the label is derived from the
    team's 3P% versus the league average.
    """
    if stats_df is None or len(stats_df) == 0 or "team" not in stats_df.columns:
        return None, "average"
    teams = [t for t in stats_df["team"].dropna().unique()]
    if len(teams) <= 1:
        _, pct = get_team_3pt_stats(stats_df, abbr)
        if pct >= LEAGUE_AVG_3PT_PCT + 0.005:
            return None, "good"
        if pct <= LEAGUE_AVG_3PT_PCT - 0.005:
            return None, "poor"
        return None, "average"
    scored = sorted(
        ((get_team_3pt_stats(stats_df, t)[1], t) for t in teams), reverse=True
    )
    rank = next((i + 1 for i, (_, t) in enumerate(scored) if t == abbr), None)
    n = len(scored)
    if rank is None:
        label = "average"
    elif rank <= n / 3:
        label = "good"
    elif rank <= 2 * n / 3:
        label = "average"
    else:
        label = "poor"
    return rank, label


# ---------------------------------------------------------------------------
# Acquired-player enrichment
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Acquired:
    """Everything the metric builders need about one acquired player."""

    name: str
    age: int
    minutes: float  # GP × MPG — the weight for team-level aggregation
    epm: float
    position: str | None
    fg3a: float
    fg3_pct: float
    fg3_data_present: bool
    base_player_value: float
    salary: int
    total_contract_surplus: float
    years_remaining: int
    timeline_modifier: float
    positional_modifier: float
    spacing_modifier: float

    @property
    def per_year_surplus(self) -> float:
        return self.total_contract_surplus / max(self.years_remaining, 1)


def _enrich_acquired(
    assets: TradeAssets,
    triples: list,
    epm_df: pd.DataFrame | None,
    stats_df: pd.DataFrame | None,
) -> list[_Acquired]:
    out: list[_Acquired] = []
    for entry, (base, ctx, multi) in zip(assets.players, triples, strict=True):
        player = entry.player
        minutes = float(player.stats.get("GP", 0.0)) * float(
            player.stats.get("MPG", 0.0)
        )

        epm_row = get_player_epm(epm_df, player.name) if epm_df is not None else None
        if epm_row is not None:
            epm = float(epm_row["epm"])
            position = str(epm_row.get("position") or "") or None
        else:
            # base.adjusted_net_rating carries whatever impact metric drove the
            # valuation (EPM / DPM / adjusted NR) — a fine stand-in for display.
            epm = float(base.adjusted_net_rating)
            position = None

        fg3a, fg3_pct, present, stats_pos = _fg3_profile(stats_df, player.name)
        if position is None:
            position = stats_pos

        out.append(
            _Acquired(
                name=player.name,
                age=player.age,
                minutes=minutes,
                epm=epm,
                position=position,
                fg3a=fg3a,
                fg3_pct=fg3_pct,
                fg3_data_present=present,
                base_player_value=base.player_value,
                salary=base.salary,
                total_contract_surplus=multi.total_contract_surplus,
                # total surplus only sums the projected horizon, so divide by
                # that same horizon to get the per-year figure.
                years_remaining=multi.years_remaining,
                timeline_modifier=ctx.timeline_modifier,
                positional_modifier=ctx.positional_modifier,
                spacing_modifier=ctx.spacing_modifier,
            )
        )
    return out


def _fg3_profile(
    stats_df: pd.DataFrame | None, name: str
) -> tuple[float, float, bool, str | None]:
    """Return ``(fg3a, fg3_pct, data_present, position)`` for a player.

    ``data_present`` is False when the 3PT columns are missing/NaN — callers
    must not infer "shooter" from the league-average fallback in that case.
    """
    row = _stats_row(stats_df, name)
    if row is None:
        return 0.0, LEAGUE_AVG_3PT_PCT, False, None

    raw_pct = row.get("FG3_PCT")
    pct_present = raw_pct is not None and not pd.isna(raw_pct)
    pct = float(raw_pct) if pct_present else LEAGUE_AVG_3PT_PCT

    raw_fg3a = row.get("FG3A")
    fg3a = float(raw_fg3a) if raw_fg3a is not None and not pd.isna(raw_fg3a) else 0.0

    pos = row.get("position") if "position" in row.index else None
    if pos is None or (isinstance(pos, float) and math.isnan(pos)):
        position = None
    else:
        position = str(pos)
    return fg3a, pct, pct_present, position


def _weighted(players: list[_Acquired], value: Callable[[_Acquired], float]) -> float:
    if not players:
        return 0.0
    total_weight = sum(p.minutes for p in players)
    if total_weight <= 0:
        return sum(value(p) for p in players) / len(players)
    return sum(value(p) * p.minutes for p in players) / total_weight


def _headline(players: list[_Acquired]) -> _Acquired | None:
    return max(players, key=lambda p: p.minutes) if players else None


def _primary_bucket(position: str | None) -> str | None:
    buckets = _coarse_position(position)
    return buckets[0][0] if buckets else None


def _existing_at_bucket(
    roster: list[dict], bucket: str | None, exclude: str
) -> list[str]:
    if bucket is None:
        return []
    matches: list[tuple[float, str]] = []
    for entry in roster:
        name = str(entry.get("player_name", ""))
        if name == exclude:
            continue
        if any(b == bucket for b, _ in _coarse_position(entry.get("position"))):
            matches.append((float(entry.get("MPG", 0) or 0), name))
    matches.sort(reverse=True)
    return [name for _, name in matches]


# ---------------------------------------------------------------------------
# Metric builders
# ---------------------------------------------------------------------------


def _impact_metric(players: list[_Acquired], label: str) -> MetricBreakdown:
    if not players:
        return MetricBreakdown(
            category="Impact",
            raw_value=0.0,
            raw_label="+0.0 EPM",
            tier=_epm_tier(0.0),
            explanation=f"The {label} acquire no players in this deal.",
        )
    avg = _weighted(players, lambda p: p.epm)
    tier = _epm_tier(avg)
    if len(players) == 1:
        p = players[0]
        explanation = (
            f"{p.name}'s teams outscore opponents by {p.epm:+.1f} points per 100 "
            f"possessions when he's on the court."
        )
    else:
        explanation = (
            f"The players the {label} acquire average {avg:+.1f} EPM — "
            f"{tier.split(' (')[0].lower()} on aggregate."
        )
    return MetricBreakdown(
        category="Impact",
        raw_value=avg,
        raw_label=f"{avg:+.1f} EPM",
        tier=tier,
        explanation=explanation,
    )


def _contract_metric(players: list[_Acquired], label: str) -> MetricBreakdown:
    if not players:
        return MetricBreakdown(
            category="Contract",
            raw_value=0.0,
            raw_label="+$0.0M/yr surplus",
            tier=_contract_tier(0.0),
            explanation=f"The {label} take on no salary in this deal.",
        )
    total = sum(p.total_contract_surplus for p in players)
    per_year = sum(p.per_year_surplus for p in players)
    tier = _contract_tier(per_year)
    if len(players) == 1:
        p = players[0]
        pv = p.base_player_value / _MILLION
        sal = p.salary / _MILLION
        py = p.per_year_surplus / _MILLION
        if p.per_year_surplus >= 0:
            explanation = (
                f"{p.name} is playing like a ${pv:.0f}M player on a ${sal:.0f}M "
                f"deal — about ${py:.0f}M in extra value per season."
            )
        elif pv >= 0:
            explanation = (
                f"{p.name} is paid ${sal:.0f}M but producing like a ${pv:.0f}M "
                f"player — the {label} are overpaying by ${abs(py):.0f}M per season."
            )
        else:
            explanation = (
                f"{p.name} is paid ${sal:.0f}M but producing below replacement "
                f"level — the {label} are overpaying by ${abs(py):.0f}M per season."
            )
    else:
        salary_m = sum(p.salary for p in players) / _MILLION
        verb = "a surplus" if per_year >= 0 else "a deficit"
        explanation = (
            f"The {label} take on ${salary_m:.0f}M in salary for "
            f"{_surplus_label(per_year)} — {verb} across the package."
        )
    return MetricBreakdown(
        category="Contract",
        raw_value=total,
        raw_label=_surplus_label(per_year),
        tier=tier,
        explanation=explanation,
    )


def _win_curve_metric(team_wins: float, label: str) -> MetricBreakdown:
    multiplier = calculate_win_curve_multiplier(team_wins)
    tier = _win_curve_tier(multiplier)
    pct = abs(multiplier - 1.0) * 100
    direction = "more" if multiplier >= 1.0 else "less"
    explanation = (
        f"The {label} are a {team_wins:.0f}-win team. Each win they add is worth "
        f"{pct:.0f}% {direction} than it would be for a .500 team."
    )
    return MetricBreakdown(
        category="Win Curve",
        raw_value=multiplier,
        raw_label=f"{multiplier:.1f}x",
        tier=tier,
        explanation=explanation,
    )


def _timeline_metric(
    players: list[_Acquired], core_age: float | None, label: str
) -> MetricBreakdown:
    if not players or core_age is None:
        return MetricBreakdown(
            category="Timeline",
            raw_value=0.0,
            raw_label="+0%",
            tier=_timeline_tier(0.0),
            explanation=f"No timeline signal for the {label} in this deal.",
        )
    modifier = _weighted(players, lambda p: p.timeline_modifier)
    tier = _timeline_tier(modifier)
    p = _headline(players)
    assert p is not None
    if modifier >= 0.03:
        story = "He'll peak alongside their core."
    elif modifier >= -0.03:
        story = "Reasonable age fit with their current window."
    elif p.age <= core_age:
        story = f"At {p.age} he's ahead of an aging core — the windows don't line up."
    else:
        years_to_prime = max(1, round(27 - core_age))
        story = (
            f"He'll be {p.age + years_to_prime} when their younger core hits its "
            f"prime — the timelines don't align."
        )
    explanation = (
        f"{p.name} is {p.age}. The {_possessive(label)} core averages "
        f"{core_age:.0f}. {story}"
    )
    return MetricBreakdown(
        category="Timeline",
        raw_value=modifier,
        raw_label=_pct_label(modifier),
        tier=tier,
        explanation=explanation,
    )


def _positional_metric(
    players: list[_Acquired],
    minutes_by_pos: dict[str, float],
    roster: list[dict],
    label: str,
) -> MetricBreakdown:
    if not players:
        return MetricBreakdown(
            category="Positional Fit",
            raw_value=0.0,
            raw_label="+0%",
            tier=_positional_tier(0.0),
            explanation=f"No positional impact for the {label} in this deal.",
        )
    modifier = _weighted(players, lambda p: p.positional_modifier)
    tier = _positional_tier(modifier)
    p = _headline(players)
    assert p is not None
    pos = p.position or "his position"
    bucket = _primary_bucket(p.position)
    minutes_filled = minutes_by_pos.get(bucket, 0.0) if bucket else 0.0
    if modifier >= 0.05:
        fit = f"{p.name} slots in without forcing anyone to the bench."
    elif modifier < -0.02:
        existing = _existing_at_bucket(roster, bucket, exclude=p.name)
        who = _natural_join(existing[:2]) or "the existing rotation"
        fit = f"Adding another {pos} creates a minutes crunch with {who}."
    else:
        fit = "A roughly neutral positional fit."
    explanation = (
        f"The {label} already commit {minutes_filled:.0f} minutes per game at "
        f"{pos}. {fit}"
    )
    return MetricBreakdown(
        category="Positional Fit",
        raw_value=modifier,
        raw_label=_pct_label(modifier),
        tier=tier,
        explanation=explanation,
    )


def _spacing_metric(
    players: list[_Acquired],
    team_rank: int | None,
    team_spacing_label: str,
    label: str,
) -> MetricBreakdown:
    if not players:
        return MetricBreakdown(
            category="Spacing",
            raw_value=0.0,
            raw_label="+0%",
            tier=_spacing_tier(0.0),
            explanation=f"No spacing impact for the {label} in this deal.",
        )
    modifier = _weighted(players, lambda p: p.spacing_modifier)
    tier = _spacing_tier(modifier)
    p = _headline(players)
    assert p is not None
    if not p.fg3_data_present or p.fg3a < 1.0:
        explanation = (
            f"{p.name} isn't a three-point threat ({p.fg3a:.1f} attempts/game) — "
            f"spacing impact for the {label} is negligible."
        )
    else:
        head = (
            f"{p.name} shoots {p.fg3_pct * 100:.0f}% from three on {p.fg3a:.1f} "
            f"attempts per game."
        )
        if modifier >= 0.01:
            where = (
                f"ranked {_ordinal(team_rank)} in spacing"
                if team_rank is not None
                else f"with {team_spacing_label} spacing"
            )
            tail = f"That helps a {label} squad {where}."
        elif modifier <= -0.01:
            tail = f"That hurts a {label} team that already struggles from three."
        else:
            tail = (
                f"Minimal impact on a team with {team_spacing_label} existing spacing."
            )
        explanation = f"{head} {tail}"
    return MetricBreakdown(
        category="Spacing",
        raw_value=modifier,
        raw_label=_pct_label(modifier),
        tier=tier,
        explanation=explanation,
    )


def _draft_capital(
    picks: list[DraftPick], stats_df: pd.DataFrame | None, label: str
) -> DraftCapitalBreakdown:
    if not picks:
        return DraftCapitalBreakdown(
            picks_description=[],
            total_pick_value=0.0,
            explanation="No draft picks involved.",
        )
    descriptions: list[str] = []
    total = 0.0
    for pick in picks:
        wins = _pick_team_wins(pick, stats_df)
        value = evaluate_draft_pick(pick, wins)
        total += value
        number = _projected_pick_number(pick, wins)
        ordinal = "1st" if pick.round == 1 else "2nd"
        descriptions.append(
            f"{pick.year} {pick.team} {ordinal} (projected pick {number}) — "
            f"${value / _MILLION:.1f}M value"
        )

    if len(picks) == 1 and picks[0].round == 1:
        number = _projected_pick_number(picks[0], _pick_team_wins(picks[0], stats_df))
        if number <= 10:
            stage, cost = "early", "high"
        elif number <= 20:
            stage, cost = "mid", "moderate"
        else:
            stage, cost = "late", "low"
        explanation = (
            f"The {label} receive a projected {stage} first — {cost}-value draft "
            f"capital (${total / _MILLION:.1f}M)."
        )
    else:
        explanation = (
            f"The {label} receive {len(picks)} picks worth a combined "
            f"${total / _MILLION:.1f}M in expected value — meaningful future assets."
        )
    return DraftCapitalBreakdown(
        picks_description=descriptions,
        total_pick_value=total,
        explanation=explanation,
    )


# ---------------------------------------------------------------------------
# Prose
# ---------------------------------------------------------------------------


def _build_prose(
    label: str,
    acquired: list[_Acquired],
    impact: MetricBreakdown,
    contract: MetricBreakdown,
    win_curve: MetricBreakdown,
    fit_metrics: list[MetricBreakdown],
    draft: DraftCapitalBreakdown,
) -> str:
    """2-3 deterministic, template-filled sentences in basketball language."""
    sentences: list[str] = []
    headline = _headline(acquired)

    # 1) What they acquire + impact tier.
    if headline is not None:
        extra = ""
        if len(acquired) > 1:
            others = len(acquired) - 1
            extra = f" and {others} other piece" + ("s" if others > 1 else "")
        sentences.append(
            f"The {label} add {headline.name}{extra}, "
            f"{impact.tier.split(' (')[0].lower()} on impact at {impact.raw_label}."
        )
    elif draft.total_pick_value > 0:
        sentences.append(
            f"The {label} take back draft capital and salary filler in this deal."
        )
    else:
        sentences.append(f"The {label} mostly move salary in this deal.")

    # 2) Contract context.
    sentences.append(contract.explanation)

    # 3) Competitive window + the strongest fit signal (positive or negative).
    strongest = max(fit_metrics, key=lambda m: abs(m.raw_value), default=None)
    window = f"As a {win_curve.tier.lower()} ({win_curve.raw_label}), the {label}"
    if strongest is not None and strongest.raw_value >= 0.03:
        sentences.append(f"{window} bank a clear win on fit — {strongest.explanation}")
    elif strongest is not None and strongest.raw_value <= -0.03:
        sentences.append(
            f"{window} have to weigh a real drawback — {strongest.explanation}"
        )
    elif draft.total_pick_value > 0:
        sentences.append(
            f"{window} also land ${draft.total_pick_value / _MILLION:.0f}M in "
            f"draft capital."
        )
    else:
        sentences.append(f"{window} make a move that fits their current window.")

    return " ".join(sentences)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def _asset_totals(
    assets: TradeAssets,
    epm_df: pd.DataFrame | None,
    darko_df: pd.DataFrame | None,
    stats_df: pd.DataFrame | None,
) -> tuple[float, float]:
    """Return ``(surplus, gross)`` for an asset package, team-agnostic.

    ``surplus`` is multi-year contract surplus plus pick values (the trade-
    decisive metric). ``gross`` is single-season production value plus pick
    values — the magnitude used to normalize the score. Both are independent
    of which team receives the assets, so the resulting score is zero-sum.
    """
    surplus = 0.0
    gross = 0.0
    for entry in assets.players:
        multi = evaluate_player_multiyear(
            entry.player, entry.contract, epm_df=epm_df, darko_df=darko_df
        )
        base = evaluate_player(
            entry.player, entry.contract, epm_df=epm_df, darko_df=darko_df
        )
        surplus += multi.total_contract_surplus
        gross += abs(base.player_value)
    for pick in assets.picks:
        value = evaluate_draft_pick(pick, _pick_team_wins(pick, stats_df))
        surplus += value
        gross += value
    return surplus, gross


def _score(surplus_delta: float, total_value: float) -> int:
    ratio = surplus_delta / max(total_value, 1.0)
    raw = 50.0 + ratio * SCORE_SCALING_FACTOR
    return int(max(0, min(100, round(raw))))


# ---------------------------------------------------------------------------
# Per-team grade
# ---------------------------------------------------------------------------


def _grade_team_side(
    team: Team,
    incoming: TradeAssets,
    outgoing: TradeAssets,
    stats_df: pd.DataFrame | None,
    epm_df: pd.DataFrame | None,
    darko_df: pd.DataFrame | None,
    score: int,
) -> TeamGrade:
    label = _short_label(team.name)
    team_wins = _team_wins(stats_df, team.abbreviation)
    roster = _roster_dicts(stats_df, team.abbreviation)
    context = TeamContext(
        projected_wins=team_wins, roster=roster, player_stats_df=stats_df
    )

    triples = evaluate_trade_assets_detailed(
        incoming, context, epm_df=epm_df, darko_df=darko_df
    )
    acquired = _enrich_acquired(incoming, triples, epm_df, stats_df)

    minutes_by_pos = _minutes_by_position(roster)
    core_ages = _team_core_ages(roster)
    core_age = (sum(core_ages) / len(core_ages)) if core_ages else None
    rank, spacing_label = _team_spacing(stats_df, team.abbreviation)

    impact = _impact_metric(acquired, label)
    contract = _contract_metric(acquired, label)
    win_curve = _win_curve_metric(team_wins, label)
    timeline = _timeline_metric(acquired, core_age, label)
    positional = _positional_metric(acquired, minutes_by_pos, roster, label)
    spacing = _spacing_metric(acquired, rank, spacing_label, label)
    draft = _draft_capital(list(incoming.picks), stats_df, label)

    prose = _build_prose(
        label,
        acquired,
        impact,
        contract,
        win_curve,
        [timeline, positional, spacing],
        draft,
    )

    return TeamGrade(
        team_name=team.name,
        team_abbreviation=team.abbreviation,
        score=score,
        verdict=get_verdict(score),
        players_acquired=[entry.player.name for entry in incoming.players],
        players_sent=[entry.player.name for entry in outgoing.players],
        impact=impact,
        contract=contract,
        win_curve=win_curve,
        timeline=timeline,
        positional_fit=positional,
        spacing=spacing,
        draft_capital=draft,
        prose=prose,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def grade_trade(
    trade: Trade,
    player_stats_df: pd.DataFrame,
    epm_df: pd.DataFrame | None = None,
    darko_df: pd.DataFrame | None = None,
    salary_df: pd.DataFrame | None = None,
) -> TradeGrade:
    """Grade a proposed trade for both teams.

    Illegal trades short-circuit to ``is_legal=False`` with both grades
    ``None``. Otherwise each side gets a full :class:`TeamGrade`.

    ``epm_df`` / ``darko_df`` are fetched on demand (24h cached) when omitted.
    ``salary_df`` is accepted for signature compatibility with the data
    pipeline; contracts are read directly from the trade's ``RosterEntry``
    objects, so the grader works fine with manually-specified contracts.
    """
    del salary_df  # contracts already live on the trade's RosterEntry objects

    legality = check_trade_legality(trade)
    if not legality.legal:
        return TradeGrade(is_legal=False, illegal_reason=legality.error_reason)

    if epm_df is None:
        epm_df = fetch_epm_data()
    if darko_df is None:
        darko_df = fetch_darko_data()

    # Score components are team-agnostic (multi-year surplus + pick values),
    # so they net out to a zero-sum pair. Computing from team A's frame covers
    # every asset exactly once.
    incoming_surplus, incoming_gross = _asset_totals(
        trade.team_b_sends, epm_df, darko_df, player_stats_df
    )
    outgoing_surplus, outgoing_gross = _asset_totals(
        trade.team_a_sends, epm_df, darko_df, player_stats_df
    )
    total_value = incoming_gross + outgoing_gross
    delta_a = incoming_surplus - outgoing_surplus
    score_a = _score(delta_a, total_value)
    score_b = _score(-delta_a, total_value)

    team_a_grade = _grade_team_side(
        trade.team_a,
        trade.team_b_sends,
        trade.team_a_sends,
        player_stats_df,
        epm_df,
        darko_df,
        score_a,
    )
    team_b_grade = _grade_team_side(
        trade.team_b,
        trade.team_a_sends,
        trade.team_b_sends,
        player_stats_df,
        epm_df,
        darko_df,
        score_b,
    )

    return TradeGrade(
        is_legal=True,
        illegal_reason=None,
        team_a_grade=team_a_grade,
        team_b_grade=team_b_grade,
    )
