"""Team-context valuation model.

Wraps the four Phase 5 adjustments — win curve, timeline, positional fit,
spacing — alongside the base (team-agnostic) player value, so callers can
inspect how a player's worth shifts for a specific acquiring team.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class TeamContextValuation(BaseModel):
    model_config = ConfigDict(frozen=True)

    base_value: float = Field(
        description="Team-agnostic player value from Phase 4/1.5 (wins_added * DOLLARS_PER_WIN)."
    )
    win_curve_multiplier: float = Field(
        description="Multiplicative rescale on $/win driven by the acquiring team's win projection."
    )
    timeline_modifier: float = Field(
        description="Additive fraction of context_value from age-alignment with the team core."
    )
    positional_modifier: float = Field(
        description="Additive fraction of context_value from positional need vs. logjam."
    )
    spacing_modifier: float = Field(
        description="Additive fraction of context_value from spacing fit."
    )
    team_adjusted_value: float = Field(
        description="Final dollar value of production for this acquiring team."
    )
    team_surplus: float = Field(
        description="team_adjusted_value minus the player's salary."
    )
