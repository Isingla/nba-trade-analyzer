"""Fail-loud resolution tests for moved (trade-input) players.

Every player named in a trade must resolve to BOTH a salary entry and a current
roster entry, joined through the crosswalk. These tests prove that each side's
failure raises a specific, player-named exception — never a silent drop,
default, or empty return — and that roster membership comes from the roster id
set (nba_api), not the salary feed's stale team column.
"""

from __future__ import annotations

import pandas as pd
import pytest

from nba_trade_analyzer.data.crosswalk import Crosswalk, CrosswalkEntry
from nba_trade_analyzer.engine.resolution import (
    ResolvedTradePlayer,
    TradePlayerResolutionError,
    resolve_trade_player,
)


# Doncic was traded LAL <- DAL; we use that to show the salary team can be stale
# while roster membership (the source of truth) is current.
_CROSSWALK = Crosswalk(
    [
        CrosswalkEntry(1629029, "Luka Dončić", "doncilu01", "Luka Dončić"),
        CrosswalkEntry(203999, "Nikola Jokić", "jokicni01", "Nikola Jokić"),
    ]
)


def _salary_df(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


def _doncic_salary(team: str = "DAL") -> pd.DataFrame:
    # Salary feed still lists Doncic under his pre-trade team (stale on purpose).
    return _salary_df(
        [{"player_name": "Luka Dončić", "bbref_slug": "doncilu01", "team": team}]
    )


def test_resolves_when_salary_crosswalk_and_roster_all_present():
    resolved = resolve_trade_player(
        "Luka Doncic",  # no diacritics — get_player_salary normalizes
        "LAL",
        salary_df=_doncic_salary(),
        crosswalk=_CROSSWALK,
        roster_ids={1629029},
    )
    assert isinstance(resolved, ResolvedTradePlayer)
    assert resolved.nba_player_id == 1629029
    assert resolved.bbref_slug == "doncilu01"


def test_raises_when_absent_from_salary():
    with pytest.raises(TradePlayerResolutionError) as exc:
        resolve_trade_player(
            "Nobody McFake",
            "LAL",
            salary_df=_doncic_salary(),
            crosswalk=_CROSSWALK,
            roster_ids={1629029},
        )
    assert exc.value.side == "salary"
    assert "Nobody McFake" in str(exc.value)


def test_raises_when_absent_from_roster():
    # Present in salary + crosswalk, but not on the sending team's roster.
    with pytest.raises(TradePlayerResolutionError) as exc:
        resolve_trade_player(
            "Luka Doncic",
            "LAL",
            salary_df=_doncic_salary(),
            crosswalk=_CROSSWALK,
            roster_ids=set(),  # roster fetched, player not on it
        )
    assert exc.value.side == "roster"
    assert "Luka Doncic" in str(exc.value)


def test_raises_when_slug_absent_from_crosswalk():
    # Salary row carries a slug the crosswalk has never seen.
    salary = _salary_df(
        [{"player_name": "Mystery Man", "bbref_slug": "myster01", "team": "LAL"}]
    )
    with pytest.raises(TradePlayerResolutionError) as exc:
        resolve_trade_player(
            "Mystery Man",
            "LAL",
            salary_df=salary,
            crosswalk=_CROSSWALK,
            roster_ids={999999},
        )
    assert exc.value.side == "crosswalk"
    assert "Mystery Man" in str(exc.value)


def test_raises_when_salary_row_has_empty_slug():
    salary = _salary_df([{"player_name": "No Slug", "bbref_slug": "", "team": "LAL"}])
    with pytest.raises(TradePlayerResolutionError) as exc:
        resolve_trade_player(
            "No Slug",
            "LAL",
            salary_df=salary,
            crosswalk=_CROSSWALK,
            roster_ids=set(),
        )
    assert exc.value.side == "crosswalk"


def test_membership_comes_from_roster_not_stale_salary_team():
    # The salary feed lists Doncic under DAL (stale), but he is on LAL now.
    # Resolving as an LAL send succeeds because his id is on LAL's roster set;
    # resolving as a DAL send fails — proving membership is roster-sourced, not
    # read from the salary team column.
    salary = _doncic_salary(team="DAL")

    resolved = resolve_trade_player(
        "Luka Doncic",
        "LAL",
        salary_df=salary,
        crosswalk=_CROSSWALK,
        roster_ids={1629029},  # LAL's current roster
    )
    assert resolved.nba_player_id == 1629029

    with pytest.raises(TradePlayerResolutionError) as exc:
        resolve_trade_player(
            "Luka Doncic",
            "DAL",
            salary_df=salary,
            crosswalk=_CROSSWALK,
            roster_ids=set(),  # DAL's roster no longer has him
        )
    assert exc.value.side == "roster"


def test_never_returns_none_for_a_failure():
    # Belt-and-suspenders: every failure path raises; there is no silent
    # default/empty-return for a moved player.
    for roster_ids in (set(), {1629029}):
        try:
            out = resolve_trade_player(
                "Ghost Player",
                "LAL",
                salary_df=_doncic_salary(),
                crosswalk=_CROSSWALK,
                roster_ids=roster_ids,
            )
        except TradePlayerResolutionError:
            continue
        pytest.fail(f"expected a raise, got {out!r}")
