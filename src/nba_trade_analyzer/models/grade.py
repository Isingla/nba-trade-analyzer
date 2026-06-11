"""Trade-grade domain models (Phase 7).

The valuation/team-context engines speak in dollars and modifiers; these
models translate that into a fan-readable verdict. A :class:`TradeGrade`
short-circuits to ``is_legal=False`` (with both team grades ``None``) when
the CBA salary-matching engine rejects the trade — illegal trades never get
a surplus analysis. Otherwise each side gets a :class:`TeamGrade`: a 0-100
score, a verdict label, seven :class:`MetricBreakdown`\\s, a draft-capital
summary, and a short basketball-prose write-up.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class MetricBreakdown(BaseModel):
    """A single graded metric (impact, contract, win curve, ...) for one team."""

    model_config = ConfigDict(frozen=True)

    category: str = Field(description='e.g. "Impact", "Contract", "Win Curve".')
    raw_value: float = Field(
        description="The underlying number — EPM, surplus dollars, a multiplier."
    )
    raw_label: str = Field(
        description='Formatted number string: "+3.4 EPM", "$18.6M/yr", "1.6x".'
    )
    tier: str = Field(
        description='Qualitative bucket: "Elite", "Above Average", "Poor", ...'
    )
    explanation: str = Field(description="Plain-English sentence for a casual fan.")


class DraftCapitalBreakdown(BaseModel):
    """Draft-pick summary for the picks one team acquires in the trade."""

    model_config = ConfigDict(frozen=True)

    picks_description: list[str] = Field(
        default_factory=list,
        description='One line per acquired pick, e.g. "2027 NYK 1st '
        '(projected pick 24) — $15.3M value".',
    )
    total_pick_value: float = Field(
        default=0.0, description="Sum of all acquired picks' dollar values."
    )
    explanation: str = Field(description="Plain-English summary of the pick haul.")


class TeamGrade(BaseModel):
    """Grade for one team's side of a trade."""

    model_config = ConfigDict(frozen=True)

    team_name: str
    team_abbreviation: str
    score: int = Field(ge=0, le=100, description="0-100, where 50 is a fair deal.")
    verdict: str = Field(description='e.g. "Clear Win", "Overpay".')
    players_acquired: list[str] = Field(default_factory=list)
    players_sent: list[str] = Field(default_factory=list)
    impact: MetricBreakdown
    contract: MetricBreakdown
    win_curve: MetricBreakdown
    timeline: MetricBreakdown
    positional_fit: MetricBreakdown
    spacing: MetricBreakdown
    draft_capital: DraftCapitalBreakdown
    prose: str = Field(description="2-3 sentence verdict in basketball language.")


class TradeGrade(BaseModel):
    """Complete trade grade for both sides (or an illegality verdict)."""

    model_config = ConfigDict(frozen=True)

    is_legal: bool
    illegal_reason: str | None = Field(
        default=None, description="Populated only when is_legal is False."
    )
    team_a_grade: TeamGrade | None = Field(
        default=None, description="None when the trade is illegal."
    )
    team_b_grade: TeamGrade | None = Field(
        default=None, description="None when the trade is illegal."
    )
    pick_ownership_warnings: list[str] = Field(
        default_factory=list,
        description="Non-fatal pick-ownership notes (e.g. a gapped pick whose "
        "ownership is indeterminate), surfaced from the legality check.",
    )
