"""Tests for draft-pick ownership verification (pick_ownership.py)."""

from __future__ import annotations

import pytest

from nba_trade_analyzer.engine.salary_rules import check_trade_legality
from nba_trade_analyzer.models.draft_pick import DraftPick
from nba_trade_analyzer.models.team import CapStatus, Team
from nba_trade_analyzer.models.trade import Trade, TradeAssets
from nba_trade_analyzer.pick_ownership import (
    CANONICAL_TEAMS,
    Indeterminate,
    NoRecord,
    NotOwner,
    PickRegistry,
    PickRegistryError,
    PickRow,
    Verified,
    get_default_registry,
    verify_trade_pick_ownership,
)


# --- fixtures ---------------------------------------------------------------
@pytest.fixture(scope="module")
def registry() -> PickRegistry:
    """The real, committed mirror — also exercises the load-time validators."""
    return get_default_registry()


def _own(team: str, year: int = 2026, rnd: int = 1) -> PickRow:
    return PickRow(team, team, year, rnd, None, False, None)


def _full_base() -> list[PickRow]:
    """One own-pick row per canonical team — satisfies the coverage census."""
    return [_own(t) for t in CANONICAL_TEAMS]


def _team(abbr: str) -> Team:
    return Team(name=f"{abbr} team", abbreviation=abbr, total_payroll=0, cap_status=CapStatus.UNDER_CAP)


def _trade(a: str, a_picks: list[DraftPick], b: str, b_picks: list[DraftPick]) -> Trade:
    return Trade(
        team_a=_team(a),
        team_b=_team(b),
        team_a_sends=TradeAssets(picks=a_picks),
        team_b_sends=TradeAssets(picks=b_picks),
    )


# --- parity spot-checks against known rows ----------------------------------
def test_team_owns_its_own_pick(registry: PickRegistry):
    assert registry.verify("WAS", 2026, 1) == Verified("WAS")


def test_pick_traded_away_returns_not_owner(registry: PickRegistry):
    # LAC's 2026 first conveys to OKC (LAC,OKC,2026,1).
    assert registry.verify("LAC", 2026, 1) == NotOwner("OKC")


def test_dal_2026_first_is_self_owned_but_second_goes_to_okc(registry: PickRegistry):
    # The brief's "DAL 2026 1st -> OKC" was off by a round: DAL KEEPS its 2026 first.
    assert registry.verify("DAL", 2026, 1) == Verified("DAL")
    assert registry.verify("DAL", 2026, 2) == NotOwner("OKC")


def test_gapped_pick_is_indeterminate_with_verbatim_clause(registry: PickRegistry):
    verdict = registry.verify("SAS", 2027, 1)
    assert isinstance(verdict, Indeterminate)
    assert "1-16 to SAC" in verdict.clause  # verbatim RealGM clause from KNOWN_GAPS


def test_realgm_input_code_is_canonicalized(registry: PickRegistry):
    # SAN (RealGM) and SAS (nba_api) resolve to the same pick.
    assert registry.verify("SAN", 2027, 1) == registry.verify("SAS", 2027, 1)


def test_absent_key_is_norecord(registry: PickRegistry):
    # 2025 firsts have already conveyed; no row, no gap -> NoRecord (a rejection).
    assert registry.verify("WAS", 2025, 1) == NoRecord("WAS", 2025, 1)


def test_swap_right_resolves_to_the_holder(registry: PickRegistry):
    # DAL's 2028 first carries an OKC swap right (DAL,OKC,2028,1 swap=true).
    assert registry.verify("DAL", 2028, 1, swap=True) == NotOwner("OKC")
    # ...and there is no *outright* transfer of DAL's 2028 first (DAL keeps it).
    assert registry.verify("DAL", 2028, 1, swap=False) == Verified("DAL")


# --- load-time validator suite ----------------------------------------------
def test_clean_full_base_loads(registry: PickRegistry):
    PickRegistry(_full_base(), {})  # no raise


def test_contradiction_raises():
    rows = [*_full_base(), PickRow("DEN", "CHI", 2031, 2, None, False, None),
            PickRow("DEN", "LAC", 2031, 2, None, False, None)]
    with pytest.raises(PickRegistryError, match="contradiction"):
        PickRegistry(rows, {})


def test_orphan_swap_raises():
    # swap right with no outright pick to reference
    rows = [*_full_base(), PickRow("BKN", "HOU", 2027, 1, None, True, None)]
    with pytest.raises(PickRegistryError, match="orphan-swap"):
        PickRegistry(rows, {})


def test_multiple_swaps_raises():
    rows = [
        *_full_base(),
        PickRow("PHI", "PHI", 2029, 1, None, False, None),
        PickRow("PHI", "LAC", 2029, 1, None, True, None),
        PickRow("PHI", "MEM", 2029, 1, None, True, None),
    ]
    with pytest.raises(PickRegistryError, match="multiple-swaps"):
        PickRegistry(rows, {})


def test_own_plus_swap_pair_is_valid():
    # An outright own row PLUS a swap right over the same pick is legitimate.
    rows = [*_full_base(), PickRow("BKN", "BKN", 2027, 1, None, False, None),
            PickRow("BKN", "HOU", 2027, 1, None, True, None)]
    PickRegistry(rows, {})  # no raise


def test_missing_team_fails_coverage_census():
    rows = [r for r in _full_base() if r.team != "CHA"]  # drop Charlotte
    with pytest.raises(PickRegistryError, match="coverage"):
        PickRegistry(rows, {})


def test_coverage_census_is_complete_on_the_real_mirror(registry: PickRegistry):
    assert len(CANONICAL_TEAMS) == 30
    assert registry.teams_missing_own_picks() == []


# --- trade integration ------------------------------------------------------
def test_trade_rejects_pick_team_does_not_own(registry: PickRegistry):
    # DAL tries to send LAC's 2026 first, which OKC controls.
    trade = _trade("DAL", [DraftPick(team="LAC", year=2026, round=1)], "OKC", [])
    report = verify_trade_pick_ownership(trade, registry)
    assert not report.ok
    assert any("OKC" in r and "DAL" in r for r in report.rejections)


def test_trade_rejects_norecord_pick(registry: PickRegistry):
    trade = _trade("WAS", [DraftPick(team="WAS", year=2025, round=1)], "BOS", [])
    report = verify_trade_pick_ownership(trade, registry)
    assert not report.ok
    assert any("no such pick" in r for r in report.rejections)


def test_trade_warns_on_indeterminate_pick_but_does_not_reject(registry: PickRegistry):
    # SAS sends its own (gapped) 2027 first.
    trade = _trade("SAS", [DraftPick(team="SAS", year=2027, round=1)], "BOS", [])
    report = verify_trade_pick_ownership(trade, registry)
    assert report.ok  # not rejected
    assert any("indeterminate" in w.lower() for w in report.warnings)


def test_swap_right_must_be_sent_by_the_holder(registry: PickRegistry):
    # OKC holds the swap on DAL's 2028 first -> OKC may send it; DAL may not.
    ok = _trade("OKC", [DraftPick(team="DAL", year=2028, round=1, swap=True)], "BOS", [])
    assert verify_trade_pick_ownership(ok, registry).ok
    bad = _trade("DAL", [DraftPick(team="DAL", year=2028, round=1, swap=True)], "BOS", [])
    assert not verify_trade_pick_ownership(bad, registry).ok


def test_check_trade_legality_rejects_unowned_pick_with_registry(registry: PickRegistry):
    trade = _trade("DAL", [DraftPick(team="LAC", year=2026, round=1)], "OKC", [])
    result = check_trade_legality(trade, registry=registry)
    assert result.legal is False
    assert "OKC" in (result.error_reason or "")


def test_check_trade_legality_surfaces_indeterminate_warning(registry: PickRegistry):
    trade = _trade("SAS", [DraftPick(team="SAS", year=2027, round=1)], "BOS", [])
    result = check_trade_legality(trade, registry=registry)
    assert result.legal is True
    assert result.pick_ownership_warnings


def test_check_trade_legality_without_registry_skips_ownership(registry: PickRegistry):
    # No registry -> back-compat: an unowned pick is NOT verified (stays legal).
    trade = _trade("DAL", [DraftPick(team="LAC", year=2026, round=1)], "OKC", [])
    assert check_trade_legality(trade).legal is True
