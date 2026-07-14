"""Write-shape tests for IngestDb upserts (Phase 2 Day 2, Part A).

A recording cursor stands in for psycopg — these lock that the salary upsert
persists the G4(b) CSS-truth option flags and the dead-money upsert stamps
scraped_at, both in the INSERT and the ON CONFLICT update set.
"""

from __future__ import annotations

from datetime import datetime, timezone

from nba_trade_analyzer.ingest.db import IngestDb

SCRAPED_AT = datetime(2026, 7, 14, 5, 15, 0, tzinfo=timezone.utc)


class _RecordingCursor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []

    def execute(self, sql: str, params: tuple | None = None) -> None:
        self.calls.append((sql, params))

    def __enter__(self) -> "_RecordingCursor":
        return self

    def __exit__(self, *args) -> None:
        return None


class _RecordingConn:
    def __init__(self) -> None:
        self.cursors: list[_RecordingCursor] = []

    def cursor(self) -> _RecordingCursor:
        cur = _RecordingCursor()
        self.cursors.append(cur)
        return cur


def _single_call(conn: _RecordingConn) -> tuple[str, tuple]:
    calls = [call for cur in conn.cursors for call in cur.calls]
    assert len(calls) == 1
    return calls[0]


def test_upsert_salary_writes_option_flags():
    conn = _RecordingConn()
    IngestDb(conn).upsert_salary(
        player_id="pid-1",
        season="2026-27",
        amount=13_398_800,
        guaranteed_amount=None,
        is_fully_ng=False,
        is_rookie_scale=False,
        has_player_option=True,
        has_team_option=False,
        source="ingest:bbref-contracts",
        scraped_at=SCRAPED_AT,
    )
    sql, params = _single_call(conn)
    assert "has_player_option" in sql and "has_team_option" in sql
    assert "has_player_option = excluded.has_player_option" in sql
    assert "has_team_option = excluded.has_team_option" in sql
    # Positional params carry the flags in column order.
    assert params == (
        "pid-1",
        "2026-27",
        13_398_800,
        None,
        False,
        False,
        True,
        False,
        "ingest:bbref-contracts",
        SCRAPED_AT,
    )


def test_upsert_dead_money_stamps_scraped_at():
    conn = _RecordingConn()
    IngestDb(conn).upsert_dead_money(
        team="MIL",
        season="2026-27",
        player_name="Damian Lillard",
        player_id=None,
        amount=22_516_603,
        source="ingest:nba_dead_money.csv",
        scraped_at=SCRAPED_AT,
    )
    sql, params = _single_call(conn)
    assert "scraped_at" in sql
    assert "scraped_at = excluded.scraped_at" in sql
    assert params[-1] == SCRAPED_AT
