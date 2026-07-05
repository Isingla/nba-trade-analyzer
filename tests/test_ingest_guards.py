"""Ingest guards: collapse, empty-source, staleness, override retirement (Phase 2A)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from nba_trade_analyzer.ingest.plans import (
    ActiveOverride,
    GuardFailure,
    TableStats,
    apply_baseline_acceptance,
    empty_source_guards,
    evaluate_guards,
    plan_override_retirements,
    staleness_warnings,
)

NOW = datetime(2026, 7, 5, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Row / dollar collapse
# ---------------------------------------------------------------------------

def test_row_collapse_blocks_below_80_percent():
    failures = evaluate_guards(
        planned={"v3_contract_salaries": TableStats(rows=799, dollars=None)},
        previous={"v3_contract_salaries": TableStats(rows=1000, dollars=None)},
    )
    assert [f.guard for f in failures] == ["row_collapse"]
    assert failures[0].detail["previous_rows"] == 1000
    assert failures[0].detail["planned_rows"] == 799


def test_row_count_at_exactly_80_percent_passes():
    failures = evaluate_guards(
        planned={"t": TableStats(rows=800)},
        previous={"t": TableStats(rows=1000)},
    )
    assert failures == []


def test_dollar_collapse_blocks_even_when_row_count_holds():
    failures = evaluate_guards(
        planned={"v3_cap_holds": TableStats(rows=1000, dollars=700)},
        previous={"v3_cap_holds": TableStats(rows=1000, dollars=1000)},
    )
    assert [f.guard for f in failures] == ["dollar_collapse"]


def test_first_run_with_empty_previous_state_passes():
    failures = evaluate_guards(
        planned={"t": TableStats(rows=5, dollars=100)},
        previous={"t": TableStats(rows=0, dollars=0)},
    )
    assert failures == []


def test_both_guards_can_fire_for_one_table():
    failures = evaluate_guards(
        planned={"t": TableStats(rows=10, dollars=10)},
        previous={"t": TableStats(rows=100, dollars=100)},
    )
    assert sorted(f.guard for f in failures) == ["dollar_collapse", "row_collapse"]


# ---------------------------------------------------------------------------
# Empty sources
# ---------------------------------------------------------------------------

def test_empty_source_guard_fires_per_empty_source():
    failures = empty_source_guards(
        {"nba_options.csv": 0, "nba_cap_holds.csv": 328, "bbref-contracts": 0}
    )
    assert sorted(f.subject for f in failures) == ["bbref-contracts", "nba_options.csv"]
    assert all(f.guard == "empty_source" for f in failures)


def test_no_empty_sources_no_failures():
    assert empty_source_guards({"a": 1, "b": 2}) == []


# ---------------------------------------------------------------------------
# Staleness (warn, never block)
# ---------------------------------------------------------------------------

def test_stale_csv_warns_after_seven_days():
    warnings = staleness_warnings(
        {"nba_options.csv": NOW - timedelta(days=8)}, NOW
    )
    assert len(warnings) == 1
    assert "8 days old" in warnings[0]


def test_fresh_csv_does_not_warn():
    assert staleness_warnings({"x.csv": NOW - timedelta(days=3)}, NOW) == []


def test_missing_git_date_warns():
    warnings = staleness_warnings({"x.csv": None}, NOW)
    assert warnings == ["x.csv: no git commit date available"]


# ---------------------------------------------------------------------------
# --accept-baseline: collapse guards become report-only, nothing else does.
# ---------------------------------------------------------------------------

_ROW = GuardFailure(guard="row_collapse", subject="v3_cap_holds", detail={"previous_rows": 66})
_DOLLAR = GuardFailure(guard="dollar_collapse", subject="v3_cap_holds", detail={"previous_dollars": 1})
_EMPTY = GuardFailure(guard="empty_source", subject="nba_options.csv", detail={"rows": 0})


def test_without_flag_everything_blocks_unchanged():
    blocking, report_only = apply_baseline_acceptance([_ROW, _DOLLAR, _EMPTY], None)
    assert blocking == [_ROW, _DOLLAR, _EMPTY]
    assert report_only == []


def test_flag_downgrades_collapse_guards_to_report_only():
    blocking, report_only = apply_baseline_acceptance(
        [_ROW, _DOLLAR], "first real ingest over seeded placeholder cap holds"
    )
    assert blocking == []
    assert report_only == [_ROW, _DOLLAR]  # recorded, never silently dropped


def test_flag_never_bypasses_source_quality_guards():
    blocking, report_only = apply_baseline_acceptance(
        [_ROW, _EMPTY], "baseline reset"
    )
    assert blocking == [_EMPTY]  # empty_source still blocks
    assert report_only == [_ROW]


# ---------------------------------------------------------------------------
# Override retirement
# ---------------------------------------------------------------------------

def _override(row_key, value, field="is_fully_ng"):
    return ActiveOverride(
        id=f"id-{row_key}",
        table_name="v3_contract_salaries",
        row_key=row_key,
        field=field,
        value=value,
    )


def test_override_retires_when_ingested_value_catches_up():
    o = _override("dunnkr01|2026-27", "true")
    retire, keep = plan_override_retirements(
        [o],
        {("v3_contract_salaries", "dunnkr01|2026-27", "is_fully_ng"): "true"},
    )
    assert retire == [o]
    assert keep == []


def test_override_kept_while_source_still_disagrees():
    o = _override("dunnkr01|2026-27", "true")
    retire, keep = plan_override_retirements(
        [o],
        {("v3_contract_salaries", "dunnkr01|2026-27", "is_fully_ng"): "false"},
    )
    assert retire == []
    assert keep == [o]


def test_override_kept_when_ingest_produced_no_value_for_its_target():
    # We learned nothing about this key this run — do not touch the override.
    o = _override("name_team:Pete Nance|MIL|2026-27", "true")
    retire, keep = plan_override_retirements([o], {})
    assert retire == []
    assert keep == [o]
