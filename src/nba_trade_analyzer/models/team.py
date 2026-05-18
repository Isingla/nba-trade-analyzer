"""Team, Roster, and salary-cap status models."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from nba_trade_analyzer.models.player import Contract, Player


class CapStatus(str, Enum):
    UNDER_CAP = "under_cap"
    OVER_CAP = "over_cap"
    FIRST_APRON = "first_apron"
    SECOND_APRON = "second_apron"


class Team(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1)
    abbreviation: str = Field(min_length=2, max_length=4)
    total_payroll: int = Field(ge=0, description="Total payroll in U.S. dollars.")
    cap_status: CapStatus


class RosterEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    player: Player
    contract: Contract


class Roster(BaseModel):
    model_config = ConfigDict(frozen=True)

    team: Team
    players: list[RosterEntry] = Field(default_factory=list)
