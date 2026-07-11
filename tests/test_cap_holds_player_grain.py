"""Player-grain cap-holds loader + sentinel classification (Phase 2A)."""

from __future__ import annotations

import pytest

from nba_trade_analyzer.data.cap_holds import (
    CapHoldRow,
    classify_cap_hold_teams,
    load_cap_holds_rows,
)

_HEADER = "Team,Player,2026-27,2027-28,2028-29,2029-30,2030-31,Pos,Age,2031-32\n"


def _write(tmp_path, body: str):
    p = tmp_path / "nba_cap_holds.csv"
    p.write_text(_HEADER + body, encoding="utf-8")
    return p


def test_keeps_player_grain(tmp_path):
    body = (
        "WAS,Trae Young,48713805.0,0.0,0.0,0.0,0.0,,,\n"
        "WAS,Other Guy,2450000.0,2450000.0,0.0,0.0,0.0,,,\n"
    )
    rows = load_cap_holds_rows(_write(tmp_path, body), current_league_year="2025-26")
    assert rows[0] == CapHoldRow(team="WAS", season="2026-27", player_name="Trae Young", amount=48713805)
    # Player identity survives (Phase 0 Path 2b discarded it upstream).
    assert {r.player_name for r in rows} == {"Trae Young", "Other Guy"}
    assert len(rows) == 3  # Other Guy has two seasons


def test_elapsed_season_gate_matches_legacy_loader(tmp_path):
    # Same deliberate semantics change as the legacy loader's gate test
    # (fix/cap-holds-current-year-gate, 2026-07-11): the CURRENT league year
    # survives; only strictly-elapsed seasons drop. Both loaders share the
    # comparison, so this pin keeps them in lockstep.
    body = "GSW,Player A,2500000.0,2800000.0,0,0,0,,,\n"
    rows = load_cap_holds_rows(_write(tmp_path, body), current_league_year="2026-27")
    assert [r.season for r in rows] == ["2026-27", "2027-28"]
    rolled = load_cap_holds_rows(_write(tmp_path, body), current_league_year="2027-28")
    assert [r.season for r in rolled] == ["2027-28"]  # 2026-27 strictly elapsed


def test_missing_file_raises_for_ingest(tmp_path):
    # Contrast: legacy load_cap_holds returns {} (export-safe); ingest must fail loud.
    with pytest.raises(FileNotFoundError):
        load_cap_holds_rows(tmp_path / "nope.csv")


def test_sentinel_classification():
    rows = [
        CapHoldRow("WAS", "2026-27", "Trae Young", 48713805),
        CapHoldRow("WAS", "2026-27", "Old Guy", 4),  # real team can carry junk rows
        CapHoldRow("BOS", "2026-27", "Blake Griffin", 4),
        CapHoldRow("BOS", "2027-28", "Torrey Craig", 8),
        CapHoldRow("GSW", "2026-27", "Someone", 88),
    ]
    quality = classify_cap_hold_teams(rows)
    # WAS has a plausible hold -> real (all its rows get quality=real).
    # BOS/GSW carry ONLY single/double-digit sentinel cells -> sentinel.
    assert quality == {"WAS": "real", "BOS": "sentinel", "GSW": "sentinel"}
