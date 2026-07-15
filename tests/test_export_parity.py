"""Unit tests for the parity acceptance harness (Phase 2 Day 3).

Every assertion's PASS and FAIL path on fabricated stub payloads — no DB, no
network, no subprocess. The operational main() is deliberately untested here;
it is a thin loop over these pure checks.
"""

from __future__ import annotations

import pytest

from nba_trade_analyzer import parity
from nba_trade_analyzer.parity import (
    check_cap_hold_debris,
    check_g7_canary,
    check_option_flags,
    check_override_diff,
    check_projections_identical,
    check_salary_diff_players,
    check_y6_canary,
    diff_salary_slugs,
    parse_applied_overrides,
    parse_stamped_row_count,
    run_all,
)


def _row(
    slug: str,
    team: str = "POR",
    salary: int = 10_000_000,
    yearly: list[int] | None = None,
    po: bool = False,
    to: bool = False,
):
    return {
        "playerName": slug.title(),
        "bbrefSlug": slug,
        "team": team,
        "salary": salary,
        "yearsRemaining": len(yearly) if yearly else 1,
        "isRookieScale": False,
        "hasPlayerOption": po,
        "hasTeamOption": to,
        "yearlySalaries": yearly or [salary],
        "nonGuaranteedSeasons": {},
    }


def _payload(salaries, projections=None, cap_holds=None, source_note=None):
    return {
        "metadata": {"salaryRows": len(salaries)},
        "salaries": salaries,
        "projections": projections or {},
        "capHolds": {"totals": cap_holds or {}},
        "capThresholds": {"seasons": {}},
        "sourceNote": source_note,
    }


TRIO_SCRAPE = [  # blended dual-team shape on the scrape side
    _row("lillada01", team="POR", salary=35_915_403),
    _row("bealbr01", team="LAC", salary=24_737_010),
    _row("prospol01", team="DAL", salary=3_500_172),
]
TRIO_DB = [  # decomposed actives on the db side
    _row("lillada01", team="POR", salary=13_398_800),
    _row("bealbr01", team="LAC", salary=5_621_700),
    _row("prospol01", team="MEM", salary=2_497_812),
]
STABLE = [_row("stable01", team="GSW", salary=50_000_000)]


# ---------------------------------------------------------------------------
# P1 — projections
# ---------------------------------------------------------------------------

def test_p1_identical_projections_pass_despite_key_order():
    a = {"a01": {"seasons": {"2026-27": {"waa": 1.5, "impact": 2.0}}}}
    b = {"a01": {"seasons": {"2026-27": {"impact": 2.0, "waa": 1.5}}}}  # reordered
    r = check_projections_identical(_payload([], projections=a), _payload([], projections=b))
    assert r.passed


def test_p1_float_drift_fails_and_names_the_slug():
    a = {"a01": {"seasons": {"2026-27": {"waa": 1.5}}}}
    b = {"a01": {"seasons": {"2026-27": {"waa": 1.5000001}}}}
    r = check_projections_identical(_payload([], projections=a), _payload([], projections=b))
    assert not r.passed
    assert "a01" in r.detail


# ---------------------------------------------------------------------------
# P2 — salary diff set
# ---------------------------------------------------------------------------

def test_p2_exact_trio_diff_passes():
    scrape = _payload(STABLE + TRIO_SCRAPE)
    db = _payload(STABLE + TRIO_DB)
    assert diff_salary_slugs(scrape, db) == set(parity.EXPECTED_SALARY_DIFF_SLUGS)
    assert check_salary_diff_players(scrape, db).passed


def test_p2_fourth_player_fails_with_offender_named():
    scrape = _payload(STABLE + TRIO_SCRAPE)
    db = _payload([_row("stable01", team="GSW", salary=51_000_000)] + TRIO_DB)
    r = check_salary_diff_players(scrape, db)
    assert not r.passed
    assert "stable01" in r.detail


def test_p2_missing_trio_member_fails_as_regression():
    # Lillard identical on both sides -> the fix regressed (blend came back
    # to the db side or the scrape got fixed silently) -> surface it.
    scrape = _payload(STABLE + TRIO_SCRAPE)
    db = _payload(STABLE + [TRIO_SCRAPE[0]] + TRIO_DB[1:])
    r = check_salary_diff_players(scrape, db)
    assert not r.passed
    assert "lillada01" in r.detail


# ---------------------------------------------------------------------------
# P2b — cap-holds debris allowlist
# ---------------------------------------------------------------------------

def test_p2b_equal_totals_pass():
    holds = {"GSW": {"2026-27": 27_100_000}}
    r = check_cap_hold_debris(_payload([], cap_holds=holds), _payload([], cap_holds=holds))
    assert r.passed


def test_p2b_non_allowlisted_delta_fails():
    a = _payload([], cap_holds={"BOS": {"2026-27": 24}})  # sentinel debris
    b = _payload([], cap_holds={})
    r = check_cap_hold_debris(a, b)
    assert not r.passed
    assert "BOS" in r.detail


def test_p2b_allowlisted_delta_passes(monkeypatch):
    monkeypatch.setattr(
        parity, "ALLOWED_CAP_HOLD_DEBRIS", {("BOS", "2026-27"): 24}
    )
    a = _payload([], cap_holds={"BOS": {"2026-27": 24}})
    b = _payload([], cap_holds={})
    assert check_cap_hold_debris(a, b).passed


# ---------------------------------------------------------------------------
# P3 — option-flag deltas
# ---------------------------------------------------------------------------

def test_p3_no_flag_deltas_pass_and_trio_is_exempt():
    scrape = _payload(TRIO_SCRAPE + [_row("flags01", po=True)])
    db = _payload(TRIO_DB + [_row("flags01", po=True)])
    assert check_option_flags(scrape, db).passed


def test_p3_stray_flag_delta_fails_with_player_team_field():
    scrape = _payload([_row("flags01", team="GSW", to=False)])
    db = _payload([_row("flags01", team="GSW", to=True)])
    r = check_option_flags(scrape, db)
    assert not r.passed
    assert "flags01" in r.detail
    assert "GSW" in r.detail
    assert "hasTeamOption" in r.detail


# ---------------------------------------------------------------------------
# P4 — override diff vs metadata stamp
# ---------------------------------------------------------------------------

STAMP = (
    "DB-SOURCED (v3_* contract tables): ingest run r1 started "
    "2026-07-14T16:21:40+00:00; {n} salary rows; 3 dead-money charge(s); {overlay}"
)


def _stamp(n: int, overlay: str) -> str:
    return STAMP.format(n=n, overlay=overlay)


def test_p4_exact_match_passes():
    without = _payload([_row("ayton01", to=True), _row("stable01")])
    with_ov = _payload(
        [_row("ayton01", to=False), _row("stable01")],
        source_note=_stamp(
            2,
            "1 override(s) applied: v3_contract_options:ayton01|2026-27.status=exercised",
        ),
    )
    assert check_override_diff(with_ov, without).passed


def test_p4_diff_without_stamp_fails_overshoot():
    without = _payload([_row("ayton01", to=True), _row("rogue01", po=True)])
    with_ov = _payload(
        [_row("ayton01", to=False), _row("rogue01", po=False)],  # rogue01 unexplained
        source_note=_stamp(
            2,
            "1 override(s) applied: v3_contract_options:ayton01|2026-27.status=exercised",
        ),
    )
    r = check_override_diff(with_ov, without)
    assert not r.passed
    assert "rogue01" in r.detail


def test_p4_stamp_without_diff_fails_undershoot():
    rows = [_row("embiijo01", po=True), _row("stable01")]
    without = _payload(list(rows))
    with_ov = _payload(
        list(rows),  # identical — the stamped override changed nothing
        source_note=_stamp(
            2,
            "1 override(s) applied: v3_contract_options:embiijo01|2026-27.status=declined",
        ),
    )
    r = check_override_diff(with_ov, without)
    assert not r.passed
    assert "embiijo01" in r.detail
    assert "moot" in r.detail


def test_p4_unparseable_stamp_fails():
    without = _payload([_row("stable01")])
    with_ov = _payload([_row("stable01")], source_note="gibberish with no stamp")
    r = check_override_diff(with_ov, without)
    assert not r.passed
    assert "parse" in r.detail


def test_p4_zero_overrides_and_zero_diff_passes():
    rows = [_row("stable01")]
    without = _payload(list(rows))
    with_ov = _payload(list(rows), source_note=_stamp(1, "0 overrides applied"))
    assert check_override_diff(with_ov, without).passed


# ---------------------------------------------------------------------------
# P5 — y6 canary
# ---------------------------------------------------------------------------

def test_p5_clean_payload_passes():
    assert check_y6_canary(_payload([_row("a01", yearly=[1, 2, 3, 4, 5])])).passed


def test_p5_six_year_salary_list_fails():
    r = check_y6_canary(_payload([_row("six01", yearly=[1, 2, 3, 4, 5, 6])]))
    assert not r.passed
    assert "six01" in r.detail


def test_p5_2031_32_season_key_fails():
    payload = _payload([])
    payload["capThresholds"]["seasons"]["2031-32"] = {"salaryCap": 1}
    r = check_y6_canary(payload)
    assert not r.passed
    assert "2031-32" in r.detail


# ---------------------------------------------------------------------------
# P6 — G7 canary
# ---------------------------------------------------------------------------

def test_p6_clean_grain_and_matching_stamp_passes():
    p = _payload(
        [_row("a01"), _row("b01")],
        source_note=_stamp(2, "0 overrides applied"),
    )
    assert check_g7_canary(p).passed


def test_p6_duplicate_player_fails_naming_them():
    p = _payload([_row("dupe01", team="IND"), _row("dupe01", team="DAL")])
    r = check_g7_canary(p)
    assert not r.passed
    assert "dupe01" in r.detail


def test_p6_stamp_count_mismatch_fails():
    p = _payload([_row("a01")], source_note=_stamp(5, "0 overrides applied"))
    r = check_g7_canary(p)
    assert not r.passed
    assert "stamped 5" in r.detail


# ---------------------------------------------------------------------------
# Stamp parsers + full-run wiring
# ---------------------------------------------------------------------------

def test_parse_applied_overrides_real_format():
    note = _stamp(
        412,
        "2 override(s) applied: v3_contract_options:aytonde01|2026-27.status=exercised, "
        "v3_contract_salaries:mykhasv01|2025-26.is_fully_ng=true",
    )
    parsed = parse_applied_overrides(note)
    assert parsed == [
        ("v3_contract_options", "aytonde01|2026-27", "status", "exercised"),
        ("v3_contract_salaries", "mykhasv01|2025-26", "is_fully_ng", "true"),
    ]
    assert parse_stamped_row_count(note) == 412


def test_parse_disabled_overlay_is_empty_not_none():
    note = _stamp(412, "overrides overlay DISABLED (--no-overrides)")
    assert parse_applied_overrides(note) == []


@pytest.mark.parametrize("bad", [None, "", "no stamp here"])
def test_parse_missing_stamp_is_none(bad):
    assert parse_applied_overrides(bad) is None


def test_run_all_green_path_reports_all_pass():
    projections = {"stable01": {"seasons": {"2026-27": {"waa": 1.0}}}}
    scrape = _payload(STABLE + TRIO_SCRAPE, projections=projections)
    db_without = _payload(
        STABLE + TRIO_DB,
        projections=projections,
        source_note=_stamp(4, "overrides overlay DISABLED (--no-overrides)"),
    )
    db_with = _payload(
        STABLE + TRIO_DB,
        projections=projections,
        source_note=_stamp(4, "0 overrides applied"),
    )
    results = run_all(scrape, db_without, db_with)
    assert [r.name for r in results] == [
        "P1 projections byte-identical",
        "P2 salary diff == known-fixed trio",
        "P2b cap-holds delta within documented allowlist",
        "P3 option-flag deltas outside trio == 0",
        "P4 override diff == metadata stamp (both directions)",
        "P5 y6 canary: no 2031-32 in DB payload",
        "P6 G7 canary: one row per player, count matches reader stamp",
    ]
    assert all(r.passed for r in results)
