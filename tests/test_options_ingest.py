"""Options CSV loader + the option-transition non-guesser (Phase 2A)."""

from __future__ import annotations

from datetime import date

import pytest

from nba_trade_analyzer.data.options_csv import load_options
from nba_trade_analyzer.ingest.plans import (
    DbOptionState,
    plan_option_transitions,
)

# Real header shape incl. the trailing junk columns (Phase 0, Path 3c).
_HEADER = "Player,Team,2026-27,2027-28,2028-29,2029-30,2030-31,2031-32,Pos,Age,2025-26\n"

AS_OF = date(2026, 7, 1)


def _write(tmp_path, body: str):
    p = tmp_path / "nba_options.csv"
    p.write_text(_HEADER + body, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def test_loads_codes_and_ignores_zero_cells(tmp_path):
    body = (
        "Jonathan Kuminga,ATL,T,UFA,0,0,0,0,0,0,\n"
        "Buddy Hield,ATL,NG,P,UFA,0,0,0,0,0,\n"
    )
    rows = load_options(_write(tmp_path, body))
    assert rows[0].codes == {"2026-27": "T", "2027-28": "UFA"}
    assert rows[1].codes == {"2026-27": "NG", "2027-28": "P", "2028-29": "UFA"}


def test_unknown_codes_are_skipped_and_logged(tmp_path, caplog):
    body = "Some Player,BOS,XX,T,0,0,0,0,0,0,\n"
    with caplog.at_level("WARNING"):
        rows = load_options(_write(tmp_path, body))
    assert rows[0].codes == {"2027-28": "T"}
    assert any("unknown code" in r.message for r in caplog.records)


def test_ee_cells_are_ignored_without_the_unknown_code_warning(tmp_path, caplog):
    # EE (extension eligible) is a calendar fact, not an option state — the
    # 2026-07-08 rebuild carried 94 of them once Spotrac added EXTENSION
    # ELIGIBLE deadline rows. They must vanish quietly: no code imported, and
    # no "unknown code" warning spam (that stays reserved for real surprises).
    body = "Dalton Knecht,LAL,T,EE,RFA,0,0,0,0,0,\n"
    with caplog.at_level("WARNING"):
        rows = load_options(_write(tmp_path, body))
    assert rows[0].codes == {"2026-27": "T", "2028-29": "RFA"}
    assert not any("unknown code" in r.message for r in caplog.records)


def test_junk_trailing_season_column_yields_no_codes(tmp_path):
    # The trailing 2025-26 column is scraper junk (0/blank) — header-name
    # parsing keeps it harmless.
    body = "Some Player,BOS,T,0,0,0,0,0,0,0,0\n"
    rows = load_options(_write(tmp_path, body))
    assert "2025-26" not in rows[0].codes


def test_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_options(tmp_path / "nope.csv")


# ---------------------------------------------------------------------------
# Transition planning: never guess an outcome.
# ---------------------------------------------------------------------------

def test_same_code_keeps_db_status_and_refreshes_as_of():
    plan = plan_option_transitions(
        {"quetane01": {"2026-27": "T"}},
        {("quetane01", "2026-27"): DbOptionState(code="T", status="pending")},
        {"2026-27"},
        AS_OF,
    )
    assert len(plan.upserts) == 1
    up = plan.upserts[0]
    assert (up.code, up.status, up.status_as_of) == ("T", "pending", AS_OF)
    assert plan.flags == []


def test_exercised_status_survives_a_still_present_marker():
    plan = plan_option_transitions(
        {"x01": {"2026-27": "T"}},
        {("x01", "2026-27"): DbOptionState(code="T", status="exercised")},
        {"2026-27"},
        AS_OF,
    )
    assert plan.upserts[0].status == "exercised"


def test_cleared_team_option_is_flagged_not_guessed():
    # Risacher pattern: DB says T pending; the fresh CSV cleared 2026-27.
    plan = plan_option_transitions(
        {"risacza01": {"2027-28": "T"}},  # player still present, cell cleared
        {("risacza01", "2026-27"): DbOptionState(code="T", status="pending")},
        {"2026-27", "2027-28"},
        AS_OF,
    )
    assert [f for f in plan.flags if f.season == "2026-27"] != []
    flag = next(f for f in plan.flags if f.season == "2026-27")
    assert (flag.db_code, flag.csv_state) == ("T", "cleared")
    # The DB row was NOT rewritten: no upsert touches 2026-27.
    assert all(u.season != "2026-27" for u in plan.upserts)


def test_absent_player_row_is_flagged_as_row_absent():
    # Trae Young pattern: PO in DB, player vanished from the CSV entirely.
    plan = plan_option_transitions(
        {},
        {("youngtr01", "2026-27"): DbOptionState(code="P", status="pending")},
        {"2026-27"},
        AS_OF,
    )
    assert plan.flags == [
        # (slug, season, db_code, db_status, csv_state)
        plan.flags[0]
    ]
    f = plan.flags[0]
    assert (f.slug, f.csv_state) == ("youngtr01", "row_absent")
    assert plan.upserts == []


def test_pt_code_change_is_flagged_not_overwritten():
    # T in DB, RFA in CSV: an option resolution — human adjudication only.
    plan = plan_option_transitions(
        {"millebr02": {"2027-28": "RFA"}},
        {("millebr02", "2027-28"): DbOptionState(code="T", status="pending")},
        {"2027-28"},
        AS_OF,
    )
    assert plan.upserts == []
    assert plan.flags[0].csv_state == "RFA"


def test_marker_drift_is_tracked_without_flag():
    # NG -> RFA is roster churn, not an option decision: just track it.
    plan = plan_option_transitions(
        {"x01": {"2026-27": "RFA"}},
        {("x01", "2026-27"): DbOptionState(code="NG", status="unknown")},
        {"2026-27"},
        AS_OF,
    )
    assert plan.upserts[0].code == "RFA"
    assert plan.flags == []


def test_ng_never_overwrites_a_different_marker():
    # DB says UFA; the fresh CSV degraded to NG (guarantee-phase marker).
    # NG carries strictly weaker information — the DB row stays untouched
    # and no flag is raised (this is CSV degradation, not a decision).
    plan = plan_option_transitions(
        {"x01": {"2026-27": "NG"}},
        {("x01", "2026-27"): DbOptionState(code="UFA", status="unknown")},
        {"2026-27"},
        AS_OF,
    )
    assert plan.upserts == []
    assert plan.flags == []


def test_ng_vs_exercised_option_still_flags_never_overwrites():
    # davisjd01 pattern (2026-07-08): exercised club option's deadline row was
    # pruned from Spotrac's forward-looking table, leaving only the
    # guarantee-date row -> CSV shows NG. The T row must survive and the
    # change must surface for human adjudication.
    plan = plan_option_transitions(
        {"davisjd01": {"2026-27": "NG"}},
        {("davisjd01", "2026-27"): DbOptionState(code="T", status="exercised")},
        {"2026-27"},
        AS_OF,
    )
    assert plan.upserts == []
    assert plan.flags[0].csv_state == "NG"
    assert (plan.flags[0].db_code, plan.flags[0].db_status) == ("T", "exercised")


def test_ng_still_fills_a_season_the_db_has_no_row_for():
    # Filling an empty season is not an overwrite — genuine new information.
    plan = plan_option_transitions(
        {"x01": {"2027-28": "NG"}},
        {},
        {"2027-28"},
        AS_OF,
    )
    assert [(u.code, u.status) for u in plan.upserts] == [("NG", "unknown")]


def test_ng_same_code_still_refreshes():
    # NG == NG is the same-code branch: refresh as-of, keep status.
    plan = plan_option_transitions(
        {"x01": {"2026-27": "NG"}},
        {("x01", "2026-27"): DbOptionState(code="NG", status="unknown")},
        {"2026-27"},
        AS_OF,
    )
    assert plan.upserts[0].code == "NG"
    assert plan.flags == []


def test_new_codes_start_pending_for_options_unknown_for_markers():
    plan = plan_option_transitions(
        {"a01": {"2027-28": "P"}, "b01": {"2027-28": "UFA"}},
        {},
        {"2027-28"},
        AS_OF,
    )
    statuses = {u.slug: u.status for u in plan.upserts}
    assert statuses == {"a01": "pending", "b01": "unknown"}


def test_seasons_the_csv_cannot_speak_to_are_left_alone():
    # DB has a 2025-26 team option; the CSV's 2025-26 column is junk (not in
    # csv_seasons) -> no flag, no touch.
    plan = plan_option_transitions(
        {"x01": {"2026-27": "T"}},
        {("x01", "2025-26"): DbOptionState(code="T", status="pending")},
        {"2026-27"},
        AS_OF,
    )
    assert all(f.season != "2025-26" for f in plan.flags)
