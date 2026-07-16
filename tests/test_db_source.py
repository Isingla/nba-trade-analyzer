"""Tests for the DB-sourced export salary frame (export --source db, Day 1).

All builders are exercised on stub snapshot rows — no live DB, mirroring
build_export's injectable pattern. They lock the seam-map derivations: the
EXPECTED_COLUMNS shape, the G2 0-fill/truncation rule, the interim G4 derived
option flags (open statuses only), the override overlay (default-on, listed,
disableable), the fail-loud freshness guard, the G7 grain canary, and the
fresh+real-only cap-hold totals.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest

from nba_trade_analyzer.data.db_source import (
    DbCapHoldRow,
    DbDeadMoneyRow,
    DbNonGuaranteeResolver,
    DbOptionRow,
    DbOverrideRow,
    DbSalaryRow,
    DbSnapshot,
    StaleRunError,
    build_cap_hold_totals,
    build_salary_records,
    check_run_freshness,
    select_dead_money_rows,
)
from nba_trade_analyzer.data.salaries import EXPECTED_COLUMNS, build_contract

SEASONS = ["2026-27", "2027-28", "2028-29", "2029-30", "2030-31"]
# The salary window since 2026-07-16: projection window + BBRef's y6.
SALARY_SEASONS = [*SEASONS, "2031-32"]
RUN_AT = datetime(2026, 7, 11, 5, 2, 27, tzinfo=timezone.utc)


def _salary(
    slug: str,
    season: str,
    amount: int,
    *,
    name: str | None = None,
    team: str = "POR",
    nba_id: int | None = None,
    ng: bool = False,
    rookie: bool = False,
    stored_po: bool | None = None,
    stored_to: bool | None = None,
) -> DbSalaryRow:
    return DbSalaryRow(
        slug=slug,
        player_name=name or slug.title(),
        team=team,
        nba_id=nba_id,
        season=season,
        amount=amount,
        is_fully_ng=ng,
        is_rookie_scale=rookie,
        has_player_option=stored_po,
        has_team_option=stored_to,
    )


def _snapshot(
    salaries: list[DbSalaryRow],
    options: list[DbOptionRow] | None = None,
    overrides: list[DbOverrideRow] | None = None,
    cap_holds: list[DbCapHoldRow] | None = None,
) -> DbSnapshot:
    return DbSnapshot(
        run_id="run-1",
        run_started_at=RUN_AT,
        salaries=salaries,
        options=options or [],
        overrides=overrides or [],
        cap_holds=cap_holds or [],
    )


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------

def test_records_match_expected_columns_and_build_contract():
    snap = _snapshot(
        [
            _salary("doe01", "2026-27", 10_000_000, rookie=True),
            _salary("doe01", "2027-28", 11_000_000),
        ]
    )
    build = build_salary_records(snap, SEASONS)

    assert len(build.records) == 1
    record = build.records[0]
    assert tuple(record.keys()) == EXPECTED_COLUMNS
    assert record["salary"] == 10_000_000
    assert record["years_remaining"] == 2
    assert record["is_rookie_scale"] is True
    assert record["yearly_salaries"] == "10000000|11000000"

    # The record round-trips through the scrape path's own contract adapter.
    contract = build_contract(record)
    assert contract.yearly_salaries == (10_000_000, 11_000_000)
    assert contract.years_remaining == 2


# ---------------------------------------------------------------------------
# G2 — 0-fill + truncation, years_remaining = row count
# ---------------------------------------------------------------------------

def test_zero_fill_interior_gap_and_truncate_trailing():
    snap = _snapshot(
        [
            _salary("gap01", "2026-27", 5_000_000),
            # 2027-28 missing (post-separation interior gap)
            _salary("gap01", "2028-29", 7_000_000),
        ]
    )
    build = build_salary_records(snap, SEASONS)
    record = build.records[0]

    # Interior gap 0-filled, list truncated after the last present season.
    assert record["yearly_salaries"] == "5000000|0|7000000"
    # years_remaining counts ROWS, not list length.
    assert record["years_remaining"] == 2


def test_sixth_season_row_survives_the_salary_window():
    # The Wemby shape (2026-07-15): real money in BBRef's y6 = 2031-32.
    # Under the old 5-season window this row raised KeyError at the G2
    # positional mapping; under salary_season_keys() it must ride through
    # untruncated as a 6-entry yearly array.
    snap = _snapshot(
        [
            _salary("wemba01", "2026-27", 50_000_000),
            _salary("wemba01", "2027-28", 51_000_000),
            _salary("wemba01", "2028-29", 52_000_000),
            _salary("wemba01", "2029-30", 53_000_000),
            _salary("wemba01", "2030-31", 55_000_000),
            _salary("wemba01", "2031-32", 57_420_000),
        ]
    )
    build = build_salary_records(snap, SALARY_SEASONS)
    record = build.records[0]
    assert record["years_remaining"] == 6
    assert record["yearly_salaries"] == (
        "50000000|51000000|52000000|53000000|55000000|57420000"
    )


def test_row_outside_salary_window_fails_crisply_not_keyerror():
    from nba_trade_analyzer.data.db_source import DbSourceError

    snap = _snapshot(
        [
            _salary("next01", "2026-27", 1_000_000),
            _salary("next01", "2032-33", 2_000_000),  # the NEXT surprise column
        ]
    )
    with pytest.raises(DbSourceError) as exc:
        build_salary_records(snap, SALARY_SEASONS)
    assert "2032-33" in str(exc.value)
    assert "next01" in str(exc.value)


def test_player_without_current_season_row_is_skipped_loudly(caplog):
    snap = _snapshot([_salary("futur01", "2027-28", 9_000_000)])
    with caplog.at_level(logging.WARNING):
        build = build_salary_records(snap, SEASONS)
    assert build.records == []
    assert build.skipped_no_current == ["futur01"]
    assert "futur01" in caplog.text


# ---------------------------------------------------------------------------
# G4 interim — derived option flags
# ---------------------------------------------------------------------------

def test_option_flags_derived_open_statuses_only():
    snap = _snapshot(
        [
            _salary("open01", "2026-27", 1_000_000),
            _salary("open01", "2027-28", 1_100_000),
            _salary("done01", "2026-27", 2_000_000),
            _salary("gone01", "2026-27", 3_000_000),
        ],
        options=[
            # pending P on a fresh season -> flag on
            DbOptionRow("open01", "2027-28", "P", "pending"),
            # unknown T on a fresh season -> flag on (unknown is still open)
            DbOptionRow("open01", "2026-27", "T", "unknown"),
            # exercised option is settled -> excluded
            DbOptionRow("done01", "2026-27", "T", "exercised"),
            # option on a season with NO fresh salary row -> ignored
            DbOptionRow("gone01", "2028-29", "P", "pending"),
        ],
    )
    by_slug = {r["bbref_slug"]: r for r in build_salary_records(snap, SEASONS).records}

    assert by_slug["open01"]["has_player_option"] is True
    assert by_slug["open01"]["has_team_option"] is True
    assert by_slug["done01"]["has_team_option"] is False
    assert by_slug["done01"]["has_player_option"] is False
    assert by_slug["gone01"]["has_player_option"] is False


# ---------------------------------------------------------------------------
# Override overlay — default-on, listed, disableable
# ---------------------------------------------------------------------------

def test_status_override_flips_derived_flag_and_is_listed():
    stub_override = DbOverrideRow(
        "v3_contract_options", "ayton01|2026-27", "status", "exercised"
    )
    snap = _snapshot(
        [
            _salary("ayton01", "2026-27", 35_000_000),
        ],
        options=[DbOptionRow("ayton01", "2026-27", "T", "pending")],
        overrides=[stub_override],
    )

    with_overlay = build_salary_records(snap, SEASONS)
    without_overlay = build_salary_records(snap, SEASONS, apply_overrides=False)

    # Overlay: the pending T is adjudicated exercised -> no open option.
    assert with_overlay.records[0]["has_team_option"] is False
    assert with_overlay.applied_overrides == [stub_override]

    # --no-overrides: base row wins, nothing applied. The on/off difference
    # is exactly the stub override list.
    assert without_overlay.records[0]["has_team_option"] is True
    assert without_overlay.applied_overrides == []


def test_is_fully_ng_override_feeds_ng_marks_both_directions():
    mark_true = DbOverrideRow(
        "v3_contract_salaries", "unmark01|2027-28", "is_fully_ng", "true"
    )
    mark_false = DbOverrideRow(
        "v3_contract_salaries", "marked01|2027-28", "is_fully_ng", "false"
    )
    snap = _snapshot(
        [
            _salary("unmark01", "2026-27", 2_000_000, nba_id=77),
            _salary("unmark01", "2027-28", 2_100_000, nba_id=77, ng=False),
            _salary("marked01", "2026-27", 3_000_000),
            _salary("marked01", "2027-28", 3_100_000, ng=True),
        ],
        overrides=[mark_true, mark_false],
    )

    build = build_salary_records(snap, SEASONS)
    resolver = DbNonGuaranteeResolver(build.ng_name_keys, build.ng_id_keys)

    # Override true over stored false -> marked (name/team key and id key).
    assert resolver.is_non_guaranteed(
        "2027-28", player="Unmark01", team="POR"
    )
    assert resolver.is_non_guaranteed("2027-28", nba_id=77)
    # Override false over stored true -> unmarked.
    assert not resolver.is_non_guaranteed(
        "2027-28", player="Marked01", team="POR"
    )
    assert sorted(build.applied_overrides, key=lambda o: o.row_key) == [
        mark_false,
        mark_true,
    ]


def test_stored_ng_mark_without_override():
    snap = _snapshot(
        [
            _salary("ngguy01", "2026-27", 2_000_000),
            _salary("ngguy01", "2027-28", 2_200_000, ng=True),
        ]
    )
    build = build_salary_records(snap, SEASONS)
    resolver = DbNonGuaranteeResolver(build.ng_name_keys, build.ng_id_keys)
    assert resolver.is_non_guaranteed("2027-28", player="Ngguy01", team="POR")
    assert not resolver.is_non_guaranteed("2026-27", player="Ngguy01", team="POR")


# ---------------------------------------------------------------------------
# G4(b) — stored CSS-truth flags (Day 2)
# ---------------------------------------------------------------------------

def test_stored_flags_beat_derivation():
    # Stored says PO=True/TO=False; the options table would derive the
    # OPPOSITE (no P row, an open T row). Stored wins on both.
    snap = _snapshot(
        [_salary("store01", "2026-27", 9_000_000, stored_po=True, stored_to=False)],
        options=[DbOptionRow("store01", "2026-27", "T", "pending")],
    )
    record = build_salary_records(snap, SEASONS).records[0]
    assert record["has_player_option"] is True
    assert record["has_team_option"] is False


def test_null_stored_flags_fall_back_to_derivation():
    snap = _snapshot(
        [_salary("null01", "2026-27", 9_000_000)],  # stored flags None
        options=[DbOptionRow("null01", "2026-27", "T", "pending")],
    )
    record = build_salary_records(snap, SEASONS).records[0]
    assert record["has_team_option"] is True
    assert record["has_player_option"] is False


def test_override_outranks_stored_flag():
    # Stored CSS still paints a T year, but the option was adjudicated
    # exercised — the override-touched derivation outranks stored.
    snap = _snapshot(
        [_salary("adjud01", "2026-27", 9_000_000, stored_po=False, stored_to=True)],
        options=[DbOptionRow("adjud01", "2026-27", "T", "pending")],
        overrides=[
            DbOverrideRow(
                "v3_contract_options", "adjud01|2026-27", "status", "exercised"
            )
        ],
    )
    with_overlay = build_salary_records(snap, SEASONS).records[0]
    without_overlay = build_salary_records(
        snap, SEASONS, apply_overrides=False
    ).records[0]
    assert with_overlay["has_team_option"] is False
    # --no-overrides: stored flag is the base truth again.
    assert without_overlay["has_team_option"] is True


# ---------------------------------------------------------------------------
# Dead money — any-fresh-else-all freshness rule
# ---------------------------------------------------------------------------

def _dead(team: str, season: str, name: str, amount: int, fresh: bool | None):
    return DbDeadMoneyRow(
        team=team,
        season=season,
        player_name=name,
        bbref_slug=None,
        amount=amount,
        fresh=fresh,
    )


def test_dead_money_fresh_rows_exclude_stale_and_unstamped():
    rows = [
        _dead("MIL", "2026-27", "Damian Lillard", 22_516_603, True),
        _dead("PHX", "2026-27", "Old Zombie", 1_000_000, False),
        _dead("DAL", "2026-27", "Unstamped Pre-Migration", 2_000_000, None),
    ]
    kept = select_dead_money_rows(rows)
    assert [r.player_name for r in kept] == ["Damian Lillard"]


def test_dead_money_all_unstamped_keeps_everything_loudly(caplog):
    rows = [
        _dead("MIL", "2026-27", "A", 1, None),
        _dead("PHX", "2027-28", "B", 2, None),
    ]
    with caplog.at_level(logging.WARNING):
        kept = select_dead_money_rows(rows)
    assert len(kept) == 2
    assert "no freshness-stamped rows yet" in caplog.text


# ---------------------------------------------------------------------------
# Freshness guard
# ---------------------------------------------------------------------------

def test_freshness_guard_refuses_stale_run():
    now = RUN_AT + timedelta(days=3, hours=6)
    with pytest.raises(StaleRunError) as exc:
        check_run_freshness(RUN_AT, now=now)
    assert "3.2 days old" in str(exc.value)
    assert "REFUSING" in str(exc.value)


def test_freshness_guard_passes_recent_run():
    check_run_freshness(RUN_AT, now=RUN_AT + timedelta(days=1))


# ---------------------------------------------------------------------------
# G7 canary
# ---------------------------------------------------------------------------

def test_g7_canary_warns_on_duplicate_player_season(caplog):
    snap = _snapshot(
        [
            _salary("dupe01", "2026-27", 1_000_000),
            _salary("dupe01", "2026-27", 1_500_000),  # grain violation
        ]
    )
    with caplog.at_level(logging.WARNING):
        build = build_salary_records(snap, SEASONS)
    assert build.canary_ok is False
    assert "G7 canary" in caplog.text
    # Still one payload row per player (last-row-wins), never two.
    assert len(build.records) == 1


def test_g7_canary_ok_on_clean_grain():
    snap = _snapshot(
        [
            _salary("a01", "2026-27", 1_000_000),
            _salary("b01", "2026-27", 2_000_000),
        ]
    )
    build = build_salary_records(snap, SEASONS)
    assert build.canary_ok is True
    assert len(build.records) == 2


# ---------------------------------------------------------------------------
# Cap holds — fresh AND real only
# ---------------------------------------------------------------------------

def test_cap_hold_totals_exclude_sentinel_and_stale():
    holds = [
        DbCapHoldRow("GSW", "2026-27", 20_000_000, "real", True),
        DbCapHoldRow("GSW", "2026-27", 7_100_000, "real", True),  # sums
        DbCapHoldRow("GSW", "2027-28", 5_000_000, "real", True),
        DbCapHoldRow("WAS", "2026-27", 48_713_805, "real", False),  # stale zombie
        DbCapHoldRow("BOS", "2026-27", 4, "sentinel", True),  # placeholder debris
        DbCapHoldRow("BOS", "2026-27", 0, "real", True),  # non-positive
    ]
    totals = build_cap_hold_totals(holds)
    assert totals == {
        "GSW": {"2026-27": 27_100_000, "2027-28": 5_000_000},
    }
