from __future__ import annotations

import pytest
from pydantic import ValidationError

from nba_trade_analyzer.models.player import Contract, Player
from nba_trade_analyzer.models.team import CapStatus, Roster, RosterEntry, Team
from nba_trade_analyzer.models.trade import Trade, TradeAssets, TradeResult


def _lebron() -> Player:
    return Player(
        name="LeBron James",
        team="LAL",
        age=40,
        stats={"PIE": 0.18, "USG_PCT": 0.30},
    )


def _max_contract() -> Contract:
    return Contract(salary=52_627_153, years_remaining=2, has_player_option=True)


def _lakers() -> Team:
    return Team(
        name="Los Angeles Lakers",
        abbreviation="LAL",
        total_payroll=190_000_000,
        cap_status=CapStatus.SECOND_APRON,
    )


def _nuggets() -> Team:
    return Team(
        name="Denver Nuggets",
        abbreviation="DEN",
        total_payroll=178_000_000,
        cap_status=CapStatus.FIRST_APRON,
    )


def test_player_basic_construction():
    p = _lebron()
    assert p.name == "LeBron James"
    assert p.team == "LAL"
    assert p.age == 40
    assert p.stats["PIE"] == pytest.approx(0.18)


def test_contract_basic_construction():
    c = _max_contract()
    assert c.salary == 52_627_153
    assert c.years_remaining == 2
    assert c.has_player_option is True
    assert c.has_team_option is False
    assert c.is_rookie_scale is False


def test_contract_rejects_zero_salary():
    with pytest.raises(ValidationError):
        Contract(salary=0, years_remaining=1)


def test_contract_rejects_negative_salary():
    with pytest.raises(ValidationError):
        Contract(salary=-1, years_remaining=1)


def test_player_rejects_negative_age():
    with pytest.raises(ValidationError):
        Player(name="X", team="LAL", age=-1)


def test_cap_status_enum_values():
    assert CapStatus.UNDER_CAP.value == "under_cap"
    assert CapStatus.OVER_CAP.value == "over_cap"
    assert CapStatus.FIRST_APRON.value == "first_apron"
    assert CapStatus.SECOND_APRON.value == "second_apron"
    assert {s.value for s in CapStatus} == {
        "under_cap",
        "over_cap",
        "first_apron",
        "second_apron",
    }


def test_team_basic_construction():
    t = _lakers()
    assert t.cap_status is CapStatus.SECOND_APRON
    assert t.total_payroll == 190_000_000


def test_team_rejects_bad_abbreviation():
    with pytest.raises(ValidationError):
        Team(
            name="Los Angeles Lakers",
            abbreviation="L",
            total_payroll=190_000_000,
            cap_status=CapStatus.SECOND_APRON,
        )


def test_roster_holds_players_with_contracts():
    roster = Roster(
        team=_lakers(),
        players=[RosterEntry(player=_lebron(), contract=_max_contract())],
    )
    assert len(roster.players) == 1
    assert roster.players[0].player.name == "LeBron James"
    assert roster.players[0].contract.salary == 52_627_153


def test_trade_construction_with_assets_each_side():
    trade = Trade(
        team_a=_lakers(),
        team_b=_nuggets(),
        team_a_sends=TradeAssets(players=[_lebron()], picks=["2027 LAL 1st"]),
        team_b_sends=TradeAssets(picks=["2028 DEN 1st (top-4 prot)"]),
    )
    assert trade.team_a.abbreviation == "LAL"
    assert trade.team_b.abbreviation == "DEN"
    assert trade.team_a_sends.players[0].name == "LeBron James"
    assert trade.team_b_sends.players == []
    assert trade.team_b_sends.picks == ["2028 DEN 1st (top-4 prot)"]


def test_trade_result_legal_and_illegal():
    legal = TradeResult(
        legal=True,
        grade_a="A-",
        grade_b="B+",
        surplus_delta_a=12.5,
        surplus_delta_b=-3.0,
    )
    assert legal.legal is True
    assert legal.error_reason is None

    illegal = TradeResult(
        legal=False,
        error_reason="Salary mismatch: team_a sends $52M, team_b sends $30M.",
        grade_a="F",
        grade_b="F",
        surplus_delta_a=0.0,
        surplus_delta_b=0.0,
    )
    assert illegal.legal is False
    assert "Salary mismatch" in (illegal.error_reason or "")
