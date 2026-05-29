"""Fail-loud resolution of trade-input (moved) players.

Rule: every player named in a trade must resolve to BOTH a salary entry
(Basketball Reference, the contract source) AND a roster entry (nba_api
``CommonTeamRoster``, the membership source), joined through the crosswalk. A
player that fails either side is never dropped, skipped, defaulted, or guessed
— resolution raises :class:`TradePlayerResolutionError` naming the player and
which side failed. This is the runtime enforcement of the project's hard rule;
the rest-of-roster (non-moved players, used only for positional context) is
allowed to fall back, but moved players must fully resolve.

The join itself is a deterministic id lookup: salary -> ``bbref_slug`` ->
(crosswalk) -> ``nba_id`` -> membership check against the sending team's roster
id set. No fuzzy name matching happens here.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from nba_trade_analyzer.data.crosswalk import Crosswalk
from nba_trade_analyzer.data.salaries import get_player_salary


class TradePlayerResolutionError(Exception):
    """A moved player failed to resolve to a salary and/or roster entry.

    ``side`` is one of ``"salary"``, ``"crosswalk"``, or ``"roster"``.
    """

    def __init__(self, player: str, side: str, detail: str) -> None:
        self.player = player
        self.side = side
        super().__init__(detail)


@dataclass(frozen=True)
class ResolvedTradePlayer:
    name: str
    bbref_slug: str
    nba_player_id: int
    salary_data: dict


def resolve_trade_player(
    name: str,
    sending_team_abbr: str,
    *,
    salary_df: pd.DataFrame,
    crosswalk: Crosswalk,
    roster_ids: set[int],
) -> ResolvedTradePlayer:
    """Resolve one moved player, or raise :class:`TradePlayerResolutionError`.

    - salary side: ``name`` must match a Basketball Reference contract that
      carries a ``bbref_slug``.
    - crosswalk: that slug must map to an nba_api player id.
    - roster side: that id must be on ``sending_team_abbr``'s current roster.
    """
    salary_data = get_player_salary(salary_df, name)
    if salary_data is None:
        raise TradePlayerResolutionError(
            name,
            "salary",
            f"'{name}' did not resolve to a Basketball Reference salary entry "
            "— check spelling or use the full name.",
        )

    slug = str(salary_data.get("bbref_slug") or "")
    nba_id = crosswalk.nba_id_for_slug(slug) if slug else None
    if nba_id is None:
        raise TradePlayerResolutionError(
            name,
            "crosswalk",
            f"'{name}' (BBRef slug {slug or '<none>'!r}) has no entry in the "
            "player crosswalk — rebuild it with scripts/build_crosswalk.py.",
        )

    if nba_id not in roster_ids:
        raise TradePlayerResolutionError(
            name,
            "roster",
            f"'{name}' (nba_id {nba_id}) is not on {sending_team_abbr}'s current "
            "nba_api roster — they cannot be sent by that team.",
        )

    return ResolvedTradePlayer(
        name=name,
        bbref_slug=slug,
        nba_player_id=int(nba_id),
        salary_data=salary_data,
    )
