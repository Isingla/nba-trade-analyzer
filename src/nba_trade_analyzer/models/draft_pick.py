"""Draft pick domain model.

Replaces the old free-text ``list[str]`` pick identifiers (e.g.
``"2027 LAL 1st (top-4 prot)"``) with a structured, validated model. This lets
picks be valued in a team-aware way — an OKC first is worth far less than a
Wizards first — and represents protections, swaps, and previously-traded
("via") picks as real fields instead of substrings to be parsed.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, field_validator

EARLIEST_PICK_YEAR = 2025
LATEST_PICK_YEAR = 2035


class DraftPick(BaseModel):
    model_config = ConfigDict(frozen=True)

    team: str
    """3-letter abbreviation of the originating team — whose record determines
    where the pick lands, and therefore its value."""

    year: int
    round: int
    protections: str | None = None
    """Human-readable protection, e.g. ``"top-4 protected"`` or
    ``"lottery protected"``. ``None`` means unprotected."""

    swap: bool = False
    """True when this is a pick-swap right rather than an outright pick."""

    via_team: str | None = None
    """Set when the pick was previously traded: ``team`` is the current holder's
    obligation, ``via_team`` the team it originally routed through."""

    @field_validator("team", "via_team")
    @classmethod
    def _validate_abbreviation(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if len(value) != 3 or not value.isalpha() or not value.isupper():
            raise ValueError(
                f"team must be a 3-letter uppercase abbreviation, got {value!r}"
            )
        return value

    @field_validator("round")
    @classmethod
    def _validate_round(cls, value: int) -> int:
        if value not in (1, 2):
            raise ValueError(f"round must be 1 or 2, got {value}")
        return value

    @field_validator("year")
    @classmethod
    def _validate_year(cls, value: int) -> int:
        if not EARLIEST_PICK_YEAR <= value <= LATEST_PICK_YEAR:
            raise ValueError(
                f"year must be between {EARLIEST_PICK_YEAR} and {LATEST_PICK_YEAR}, "
                f"got {value}"
            )
        return value

    @property
    def label(self) -> str:
        """Reconstruct the old human-readable string form, for display and
        backward compatibility with code that logged pick identifiers.

        Examples: ``"2027 LAL 1st (top-4 protected)"``,
        ``"2026 WAS 2nd (unprotected)"``, ``"2028 OKC 1st swap"``.
        """
        ordinal = "1st" if self.round == 1 else "2nd"
        text = f"{self.year} {self.team} {ordinal}"
        if self.swap:
            text = f"{text} swap"
        else:
            text = f"{text} ({self.protections or 'unprotected'})"
        if self.via_team is not None:
            text = f"{text} via {self.via_team}"
        return text

    def __str__(self) -> str:
        return self.label
