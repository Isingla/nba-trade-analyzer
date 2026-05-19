"""Player valuation domain model."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class PlayerValuation(BaseModel):
    model_config = ConfigDict(frozen=True)

    player_name: str = Field(min_length=1)
    team: str = Field(min_length=1)
    adjusted_net_rating: float
    wins_added: float
    player_value: float = Field(description="Dollar value of on-court production.")
    surplus_value: float = Field(description="player_value minus salary.")
    salary: int = Field(ge=0)
    confidence: float = Field(ge=0.0, le=1.0)
    metric_source: str = "net_rating_adjusted"
