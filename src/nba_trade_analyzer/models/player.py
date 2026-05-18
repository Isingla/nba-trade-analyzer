"""Player and Contract domain models."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class Contract(BaseModel):
    model_config = ConfigDict(frozen=True)

    salary: int = Field(gt=0, description="Annual salary in U.S. dollars.")
    years_remaining: int = Field(ge=0)
    is_rookie_scale: bool = False
    has_player_option: bool = False
    has_team_option: bool = False


class Player(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1)
    team: str = Field(min_length=1, description="Team abbreviation, e.g. LAL.")
    age: int = Field(ge=0)
    stats: dict[str, float] = Field(default_factory=dict)
