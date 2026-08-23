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
    check_season_window_canary,
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


QUARTET_SCRAPE = [  # blended shapes on the scrape side (by design)
    _row("lillada01", team="POR", salary=35_915_403),
    _row("bealbr01", team="LAC", salary=24_737_010),
    _row("prospol01", team="DAL", salary=3_500_172),
    # Same-team blend (2026-07-17): BBRef prints Isaac's ORL cell as the
    # undecomposed waive-and-re-sign total 8,000,000 dead + 2,449,421 vet-min.
    _row("isaacjo01", team="ORL", salary=10_449_421),
    # KCP (2026-07-31): waived-via-buyout MEM 2026-07-25 — BBRef prints the
    # blended 17,744,971 dead + 3,876,529 active = 21,621,500 in his MEM cell.
    _row("caldwke01", team="MEM", salary=21_621_500),
]
QUARTET_DB = [  # decomposed actives on the db side
    _row("lillada01", team="POR", salary=13_398_800),
    _row("bealbr01", team="LAC", salary=5_621_700),
    _row("prospol01", team="MEM", salary=2_497_812),
    _row("isaacjo01", team="ORL", salary=2_449_421),
    _row("caldwke01", team="MEM", salary=3_876_529),
]
# looneke01 HEALED 2026-07-17 (BBRef corrected its two-stint SUM poison; the
# allowlist entry is removed). These fixtures now model the RELAPSE shape —
# the old scrape-side divergence reappearing — which must FAIL P2 as a new
# unexpected slug, since nothing allowlists him anymore.
LOONEY_RELAPSE_SCRAPE = [
    _row("looneke01", team="NOP", salary=10_449_421),
    _row("looneke01", team="LAL", salary=10_449_421),
]
LOONEY_RELAPSE_DB = [_row("looneke01", team="LAL", salary=10_449_421)]
# The healed steady state: one row, correct vet-min amount, both sides agree.
LOONEY_HEALED = [_row("looneke01", team="LAL", salary=2_449_421)]
STABLE = [_row("stable01", team="GSW", salary=50_000_000)]

# Pure-dead single rows (2026-07-18): the scrape still prints each waived
# player's charge-shaped row; the DB drops it (ingest classifier), so the
# diff is PRESENCE — scrape-only rows, no DB counterpart. DERIVED from the
# family constant (the MIN-trio fixture lesson: fixtures never hand-copy a
# membership list they exist to verify).
PURE_DEAD_SCRAPE = [
    _row(slug, team="MEM", salary=1_000_000)
    for slug in sorted(parity.PURE_DEAD_SINGLE_ROW_SLUGS)
]


# ---------------------------------------------------------------------------
# P1 — projections
# ---------------------------------------------------------------------------


def test_p1_identical_projections_pass_despite_key_order():
    a = {"a01": {"seasons": {"2026-27": {"waa": 1.5, "impact": 2.0}}}}
    b = {"a01": {"seasons": {"2026-27": {"impact": 2.0, "waa": 1.5}}}}  # reordered
    r = check_projections_identical(
        _payload([], projections=a), _payload([], projections=b)
    )
    assert r.passed


def test_p1_float_drift_fails_and_names_the_slug():
    a = {"a01": {"seasons": {"2026-27": {"waa": 1.5}}}}
    b = {"a01": {"seasons": {"2026-27": {"waa": 1.5000001}}}}
    r = check_projections_identical(
        _payload([], projections=a), _payload([], projections=b)
    )
    assert not r.passed
    assert "a01" in r.detail


def test_p1_quartet_only_drift_passes_and_names_the_carve_out():
    # The quartet's decomposed salaries feed their projections — forgiven,
    # but NEVER silently: the absorbed slugs must appear in the PASS message.
    # isaacjo01 (the same-team member) is carved exactly like the trio.
    a = {
        "lillada01": {"seasons": {"2026-27": {"waa": 2.0}}},
        "isaacjo01": {"seasons": {"2026-27": {"waa": 0.5}}},
        "stable01": {"seasons": {"2026-27": {"waa": 1.0}}},
    }
    b = {
        "lillada01": {"seasons": {"2026-27": {"waa": 2.4}}},  # drifted (carved)
        "isaacjo01": {"seasons": {"2026-27": {"waa": 0.9}}},  # drifted (carved)
        "stable01": {"seasons": {"2026-27": {"waa": 1.0}}},
    }
    r = check_projections_identical(
        _payload([], projections=a), _payload([], projections=b)
    )
    assert r.passed
    assert "lillada01" in r.detail  # carve-out visible in every run
    assert "isaacjo01" in r.detail


def test_p1_pure_dead_presence_drift_passes_and_is_named():
    # A dropped salary row builds no DB-side projection: scrape has the
    # player, db does not — presence drift, absorbed by the pure-dead
    # carve-out and NAMED in the pass line (never silent).
    a = {
        "rubiori01": {"seasons": {"2026-27": {"waa": 0.0}}},
        "stable01": {"seasons": {"2026-27": {"waa": 1.0}}},
    }
    b = {"stable01": {"seasons": {"2026-27": {"waa": 1.0}}}}
    r = check_projections_identical(
        _payload([], projections=a), _payload([], projections=b)
    )
    assert r.passed
    assert "pure-dead family absorbed" in r.detail
    assert "rubiori01" in r.detail


def test_p1_quartet_plus_extra_fails_naming_only_the_extra():
    a = {
        "lillada01": {"seasons": {"2026-27": {"waa": 2.0}}},
        "extra01": {"seasons": {"2026-27": {"waa": 1.0}}},
    }
    b = {
        "lillada01": {"seasons": {"2026-27": {"waa": 2.4}}},
        "extra01": {"seasons": {"2026-27": {"waa": 1.1}}},
    }
    r = check_projections_identical(
        _payload([], projections=a), _payload([], projections=b)
    )
    assert not r.passed
    unexpected_clause, _, carved_clause = r.detail.partition("quartet drift absorbed")
    assert "extra01" in unexpected_clause
    assert "lillada01" not in unexpected_clause  # quartet only in carved clause
    assert "lillada01" in carved_clause


# ---------------------------------------------------------------------------
# P2 — salary diff set
# ---------------------------------------------------------------------------


def test_p2_quartet_plus_pure_dead_diff_passes():
    # Green path: healed Looney identical on both sides contributes no diff;
    # the expected set is the quartet (dual-team trio + same-team isaacjo01)
    # PLUS the pure-dead family (scrape-only rows the DB drops).
    scrape = _payload(STABLE + QUARTET_SCRAPE + LOONEY_HEALED + PURE_DEAD_SCRAPE)
    db = _payload(STABLE + QUARTET_DB + LOONEY_HEALED)
    assert parity.SCRAPE_SIDE_DIFF_SLUGS == frozenset()
    assert diff_salary_slugs(scrape, db) == set(
        parity.EXPECTED_SALARY_DIFF_SLUGS | parity.PURE_DEAD_SINGLE_ROW_SLUGS
    )
    r = check_salary_diff_players(scrape, db)
    assert r.passed
    assert "pure-dead" in r.detail  # family visible in every PASS line


def test_p2_pure_dead_family_membership_is_the_nine_ruled_slugs():
    # Family membership pinned: 8 Spotrac-corroborated drops + thompkl01
    # (DAL buyout 08-21, joined 08-23).
    # rupertra01 aged out BEFORE the family existed and must never join it.
    assert parity.PURE_DEAD_SINGLE_ROW_SLUGS == frozenset(
        {
            "anthoco01",
            "littlna01",
            "mcgeeja01",
            "liddeej01",
            "micicva01",
            "diakima01",
            "rubiori01",
            "louzama01",
            "thompkl01",
        }
    )
    assert "rupertra01" not in parity.PURE_DEAD_SINGLE_ROW_SLUGS
    # Disjoint from both sibling families — different mechanisms by design.
    assert not (parity.PURE_DEAD_SINGLE_ROW_SLUGS & parity.EXPECTED_SALARY_DIFF_SLUGS)
    assert not (parity.PURE_DEAD_SINGLE_ROW_SLUGS & parity.SCRAPE_SIDE_DIFF_SLUGS)


def test_p2_aged_out_pure_dead_member_fails_demanding_removal():
    # A member vanishing from the scrape (charge aged off BBRef / row healed)
    # must FAIL with the removal demand — carve-outs never outlive their
    # cause (the Looney lifecycle, now direction-enforced for this family).
    scrape_missing_one = [
        row for row in PURE_DEAD_SCRAPE if row["bbrefSlug"] != "rubiori01"
    ]
    scrape = _payload(STABLE + QUARTET_SCRAPE + LOONEY_HEALED + scrape_missing_one)
    db = _payload(STABLE + QUARTET_DB + LOONEY_HEALED)
    r = check_salary_diff_players(scrape, db)
    assert not r.passed
    assert "rubiori01" in r.detail
    assert "REMOVE from PURE_DEAD_SINGLE_ROW_SLUGS" in r.detail
    # The demand names ONLY the aged-out member, not the still-live seven.
    assert "micicva01" not in r.detail.split("REMOVE")[1]


def test_p2_looney_relapse_fails_as_new_unexpected_slug():
    # The old two-stint poison reappearing is no longer forgiven: with the
    # allowlist empty, a looneke01 diff is a NEW unexpected slug.
    scrape = _payload(
        STABLE + QUARTET_SCRAPE + LOONEY_RELAPSE_SCRAPE + PURE_DEAD_SCRAPE
    )
    db = _payload(STABLE + QUARTET_DB + LOONEY_RELAPSE_DB)
    r = check_salary_diff_players(scrape, db)
    assert not r.passed
    assert "UNEXPECTED" in r.detail
    assert "looneke01" in r.detail


def test_p2_fifth_player_fails_with_offender_named():
    scrape = _payload(STABLE + QUARTET_SCRAPE + LOONEY_HEALED + PURE_DEAD_SCRAPE)
    db = _payload(
        [_row("stable01", team="GSW", salary=51_000_000)] + QUARTET_DB + LOONEY_HEALED
    )
    r = check_salary_diff_players(scrape, db)
    assert not r.passed
    assert "stable01" in r.detail


def test_p2_missing_quartet_member_fails_as_regression():
    # QUARTET BEHAVIOR UNCHANGED by the pure-dead family: Lillard identical
    # on both sides -> the fix regressed (blend came back to the db side or
    # the scrape got fixed silently) -> surface it with the same message.
    scrape = _payload(STABLE + QUARTET_SCRAPE + LOONEY_HEALED + PURE_DEAD_SCRAPE)
    db = _payload(STABLE + [QUARTET_SCRAPE[0]] + QUARTET_DB[1:] + LOONEY_HEALED)
    r = check_salary_diff_players(scrape, db)
    assert not r.passed
    assert "lillada01" in r.detail
    assert "regressed" in r.detail


# ---------------------------------------------------------------------------
# P2b — cap-holds debris allowlist
# ---------------------------------------------------------------------------


def test_p2b_equal_totals_pass():
    holds = {"GSW": {"2026-27": 27_100_000}}
    r = check_cap_hold_debris(
        _payload([], cap_holds=holds), _payload([], cap_holds=holds)
    )
    assert r.passed


def test_p2b_non_allowlisted_delta_fails():
    a = _payload([], cap_holds={"BOS": {"2026-27": 24}})  # sentinel debris
    b = _payload([], cap_holds={})
    r = check_cap_hold_debris(a, b)
    assert not r.passed
    assert "BOS" in r.detail


def test_p2b_allowlisted_delta_passes(monkeypatch):
    monkeypatch.setattr(parity, "ALLOWED_CAP_HOLD_DEBRIS", {("BOS", "2026-27"): 24})
    a = _payload([], cap_holds={"BOS": {"2026-27": 24}})
    b = _payload([], cap_holds={})
    assert check_cap_hold_debris(a, b).passed


# ---------------------------------------------------------------------------
# P3 — option-flag deltas
# ---------------------------------------------------------------------------


def test_p3_no_flag_deltas_pass_and_quartet_is_exempt():
    scrape = _payload(QUARTET_SCRAPE + [_row("flags01", po=True)])
    db = _payload(QUARTET_DB + [_row("flags01", po=True)])
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
# P5 — season-window canary (salary window = 2026-27..2031-32 since 2026-07-16)
# ---------------------------------------------------------------------------


def test_p5_clean_payload_passes():
    assert check_season_window_canary(
        _payload([_row("a01", yearly=[1, 2, 3, 4, 5])])
    ).passed


def test_p5_2031_32_now_inside_the_window_passes():
    # Wemby shape: 6-entry yearly (index 5 = 2031-32) + an in-window NG key.
    row = _row("wembavi01", yearly=[1, 2, 3, 4, 5, 57_420_000])
    row["nonGuaranteedSeasons"] = {"2031-32": 57_420_000}
    assert check_season_window_canary(_payload([row])).passed


def test_p5_seven_year_salary_list_fails():
    r = check_season_window_canary(
        _payload([_row("seven01", yearly=[1, 2, 3, 4, 5, 6, 7])])
    )
    assert not r.passed
    assert "seven01" in r.detail


def test_p5_2032_33_season_key_fails():
    payload = _payload([])
    payload["capHolds"]["totals"]["GSW"] = {"2032-33": 1_000_000}
    r = check_season_window_canary(payload)
    assert not r.passed
    assert "2032-33" in r.detail


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
    # Projections drift on quartet members (absorbed by the P1 carve-out) and
    # agree everywhere else; salaries diff on exactly the quartet (post-heal:
    # the scrape-side allowlist is empty, healed Looney contributes no diff).
    scrape_projections = {
        "stable01": {"seasons": {"2026-27": {"waa": 1.0}}},
        "lillada01": {"seasons": {"2026-27": {"waa": 2.0}}},
        "isaacjo01": {"seasons": {"2026-27": {"waa": 0.5}}},
    }
    db_projections = {
        "stable01": {"seasons": {"2026-27": {"waa": 1.0}}},
        "lillada01": {"seasons": {"2026-27": {"waa": 2.4}}},
        "isaacjo01": {"seasons": {"2026-27": {"waa": 0.9}}},
    }
    # Pure-dead members: scrape carries the row AND a projection; the DB
    # dropped the row, so it has neither (presence drift, carved by P1).
    scrape_projections.update(
        {
            slug: {"seasons": {"2026-27": {"waa": 0.0}}}
            for slug in parity.PURE_DEAD_SINGLE_ROW_SLUGS
        }
    )
    scrape = _payload(
        STABLE + QUARTET_SCRAPE + LOONEY_HEALED + PURE_DEAD_SCRAPE,
        projections=scrape_projections,
    )
    db_without = _payload(
        STABLE + QUARTET_DB + LOONEY_HEALED,
        projections=db_projections,
        source_note=_stamp(7, "overrides overlay DISABLED (--no-overrides)"),
    )
    db_with = _payload(
        STABLE + QUARTET_DB + LOONEY_HEALED,
        projections=db_projections,
        source_note=_stamp(7, "0 overrides applied"),
    )
    results = run_all(scrape, db_without, db_with)
    assert [r.name for r in results] == [
        "P1 projections byte-identical outside carve-outs",
        "P2 salary diff == quartet + pure-dead family + scrape-side allowlist",
        "P2b cap-holds delta within documented allowlist",
        "P3 option-flag deltas outside quartet == 0",
        "P4 override diff == metadata stamp (both directions)",
        "P5 season-window canary: no season outside salary window",
        "P6 G7 canary: one row per player, count matches reader stamp",
    ]
    assert all(r.passed for r in results)
