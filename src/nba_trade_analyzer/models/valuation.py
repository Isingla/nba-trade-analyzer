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
    metric_source: str = Field(
        default="net_rating",
        description=(
            "Which impact metric drove this valuation. One of: "
            "'epm' (Estimated Plus-Minus from dunksandthrees.com), "
            "'darko' (DARKO DPM projection from kmedved sheet), "
            "'net_rating' (team-adjusted NET_RATING fallback when neither "
            "EPM nor DARKO is available)."
        ),
    )


class YearProjection(BaseModel):
    model_config = ConfigDict(frozen=True)

    year: int = Field(ge=1, description="1-indexed contract year (1 = current season).")
    projected_age: int = Field(ge=0)
    projected_epm: float
    projected_wins_added: float
    projected_value: float = Field(description="Dollar value of projected production.")
    salary: float = Field(ge=0)
    discount_factor: float = Field(gt=0.0, le=1.0)
    discounted_surplus: float
    projection_source: str = Field(
        description=(
            "Source of the impact projection for this year. One of: "
            "'epm' (current-season EPM, year 1), "
            "'darko' (DARKO forward projection, year 2 when available), "
            "'aging_curve' (aging factor applied to the year-2 anchor)."
        ),
    )


class MultiYearValuation(BaseModel):
    model_config = ConfigDict(frozen=True)

    player_name: str = Field(min_length=1)
    current_age: int = Field(ge=0)
    years_remaining: int = Field(ge=0)
    current_season_surplus: float = Field(
        description="Year-1-only surplus, matches single-season evaluate_player output."
    )
    total_contract_surplus: float = Field(
        description="Sum of discounted surplus across all projected contract years."
    )
    year_by_year: list[YearProjection]
    primary_metric_source: str = Field(
        description="Impact source that drove year 1: 'epm', 'darko', or 'net_rating'."
    )
