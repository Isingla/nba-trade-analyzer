"""Trade proposal and trade-result models."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from nba_trade_analyzer.models.draft_pick import DraftPick
from nba_trade_analyzer.models.team import RosterEntry, Team


class TradeAssets(BaseModel):
    model_config = ConfigDict(frozen=True)

    players: list[RosterEntry] = Field(
        default_factory=list,
        description="Players being sent, each paired with their contract.",
    )
    picks: list[DraftPick] = Field(
        default_factory=list,
        description="Draft picks being sent, e.g. DraftPick(team='LAL', year=2027, "
        "round=1, protections='top-4 protected').",
    )

    @property
    def total_salary(self) -> int:
        return sum(entry.contract.salary for entry in self.players)


class Trade(BaseModel):
    model_config = ConfigDict(frozen=True)

    team_a: Team
    team_b: Team
    team_a_sends: TradeAssets = Field(default_factory=TradeAssets)
    team_b_sends: TradeAssets = Field(default_factory=TradeAssets)


class TradeResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    legal: bool
    error_reason: str | None = None
    grade_a: str
    grade_b: str
    surplus_delta_a: float
    surplus_delta_b: float
