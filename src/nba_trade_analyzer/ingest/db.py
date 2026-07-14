"""DB access for the ingest command (Phase 2A) — restricted role, upsert-only.

Connects EXCLUSIVELY via ``$CONTRACT_INGEST_DATABASE_URL`` (the limited
``contract_ingest`` role: INSERT/UPDATE grants + RLS policies, no DELETE at
either layer — databallr docs/DB-CONTRACT-DATA.md). Never falls back to
service_role or any other credential; a missing env var refuses to run.

Every write is ``INSERT ... ON CONFLICT ... DO UPDATE`` or a targeted
``UPDATE``. There are ZERO DELETE statements in this module, matching the
role's capabilities.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date, datetime

import psycopg
from psycopg.rows import tuple_row

ENV_VAR = "CONTRACT_INGEST_DATABASE_URL"

RUN_SOURCE = "nba-trade-analyzer ingest"


class IngestDbError(RuntimeError):
    pass


@dataclass(frozen=True)
class PlayerRec:
    id: str
    bbref_slug: str
    name: str | None
    nba_id: int | None


@dataclass(frozen=True)
class OverrideRec:
    id: str
    table_name: str
    row_key: str
    field: str
    value: str


def connect_from_env() -> psycopg.Connection:
    url = os.environ.get(ENV_VAR)
    if not url:
        raise IngestDbError(
            f"{ENV_VAR} is not set. Ingest only runs as the restricted "
            "contract_ingest role — it never uses service_role or DATABASE_URL."
        )
    return psycopg.connect(url, row_factory=tuple_row)


class IngestDb:
    """Thin, explicit SQL wrapper. One instance per run; caller owns commit points."""

    def __init__(self, conn: psycopg.Connection) -> None:
        self.conn = conn

    # ---- reads ---------------------------------------------------------

    def fetch_players(self) -> dict[str, PlayerRec]:
        with self.conn.cursor() as cur:
            cur.execute(
                "select id::text, bbref_slug, name, nba_id from public.v3_players"
            )
            return {
                slug: PlayerRec(id=pid, bbref_slug=slug, name=name, nba_id=nba_id)
                for pid, slug, name, nba_id in cur.fetchall()
            }

    def fetch_option_state(self) -> dict[tuple[str, str], tuple[str, str]]:
        """(bbref_slug, season) -> (code, status)."""
        with self.conn.cursor() as cur:
            cur.execute(
                """
                select p.bbref_slug, o.season, o.code, o.status
                from public.v3_contract_options o
                join public.v3_players p on p.id = o.player_id
                """
            )
            return {(slug, season): (code, status) for slug, season, code, status in cur.fetchall()}

    def fetch_active_overrides(self) -> list[OverrideRec]:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                select id::text, table_name, row_key, field, value
                from public.v3_overrides
                where retired_at is null
                """
            )
            return [OverrideRec(*row) for row in cur.fetchall()]

    def fetch_table_stats(self) -> dict[str, tuple[int, int | None]]:
        """Current (row count, dollar sum where meaningful) per guarded table."""
        stats: dict[str, tuple[int, int | None]] = {}
        with self.conn.cursor() as cur:
            cur.execute("select count(*), coalesce(sum(amount), 0) from public.v3_contract_salaries")
            n, dollars = cur.fetchone()
            stats["v3_contract_salaries"] = (int(n), int(dollars))
            cur.execute("select count(*), coalesce(sum(amount), 0) from public.v3_cap_holds")
            n, dollars = cur.fetchone()
            stats["v3_cap_holds"] = (int(n), int(dollars))
            for table in ("v3_players", "v3_contract_options", "v3_dead_money", "v3_cap_thresholds"):
                cur.execute(f"select count(*) from public.{table}")  # noqa: S608 — fixed table list
                stats[table] = (int(cur.fetchone()[0]), None)
        return stats

    def fetch_previous_mismatch_keys(self) -> set[tuple[str, str]]:
        """Mismatch (player key, field) pairs from the most recent prior run."""
        with self.conn.cursor() as cur:
            cur.execute(
                """
                select id::text from public.v3_ingest_runs
                where source = %s and status = 'success'
                order by started_at desc limit 1
                """,
                (RUN_SOURCE,),
            )
            row = cur.fetchone()
            if row is None:
                return set()
            cur.execute(
                """
                select coalesce(p.bbref_slug, v.player_id::text, ''), v.field
                from public.v3_verifications v
                left join public.v3_players p on p.id = v.player_id
                where v.run_id = %s and v.verdict = 'mismatch'
                """,
                (row[0],),
            )
            return {(key, fld) for key, fld in cur.fetchall()}

    # ---- writes (upsert-only) -------------------------------------------

    def upsert_player(
        self,
        *,
        bbref_slug: str,
        name: str,
        nba_id: int | None,
        team_bbref: str | None,
        team_display: str | None,
    ) -> str:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                insert into public.v3_players (bbref_slug, name, nba_id, team_bbref, team_display)
                values (%s, %s, %s, %s, %s)
                on conflict (bbref_slug) do update set
                  name = excluded.name,
                  nba_id = excluded.nba_id,
                  team_bbref = excluded.team_bbref,
                  team_display = excluded.team_display
                returning id::text
                """,
                (bbref_slug, name, nba_id, team_bbref, team_display),
            )
            return cur.fetchone()[0]

    def upsert_salary(
        self,
        *,
        player_id: str,
        season: str,
        amount: int,
        guaranteed_amount: int | None,
        is_fully_ng: bool,
        is_rookie_scale: bool,
        has_player_option: bool,
        has_team_option: bool,
        source: str,
        scraped_at: datetime,
    ) -> None:
        # has_player_option / has_team_option are the BBRef CSS-truth flags
        # (contract-level, repeated per season row — G4(b), migration
        # 20260714210000). Requires that migration; fails loud without it.
        with self.conn.cursor() as cur:
            cur.execute(
                """
                insert into public.v3_contract_salaries
                  (player_id, season, amount, guaranteed_amount, is_fully_ng,
                   is_rookie_scale, has_player_option, has_team_option,
                   source, scraped_at)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (player_id, season) do update set
                  amount = excluded.amount,
                  guaranteed_amount = excluded.guaranteed_amount,
                  is_fully_ng = excluded.is_fully_ng,
                  is_rookie_scale = excluded.is_rookie_scale,
                  has_player_option = excluded.has_player_option,
                  has_team_option = excluded.has_team_option,
                  source = excluded.source,
                  scraped_at = excluded.scraped_at
                """,
                (
                    player_id,
                    season,
                    amount,
                    guaranteed_amount,
                    is_fully_ng,
                    is_rookie_scale,
                    has_player_option,
                    has_team_option,
                    source,
                    scraped_at,
                ),
            )

    def upsert_option(
        self,
        *,
        player_id: str,
        season: str,
        code: str,
        status: str,
        status_as_of: date | None,
        source: str,
    ) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                insert into public.v3_contract_options
                  (player_id, season, code, status, status_as_of, source)
                values (%s, %s, %s, %s, %s, %s)
                on conflict (player_id, season) do update set
                  code = excluded.code,
                  status = excluded.status,
                  status_as_of = excluded.status_as_of,
                  source = excluded.source
                """,
                (player_id, season, code, status, status_as_of, source),
            )

    def upsert_cap_hold(
        self,
        *,
        team: str,
        season: str,
        player_name: str | None,
        amount: int,
        quality: str,
        source: str,
        scraped_at: datetime,
    ) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                insert into public.v3_cap_holds
                  (team, season, player_name, amount, quality, source, scraped_at)
                values (%s, %s, %s, %s, %s, %s, %s)
                on conflict (team, season, coalesce(player_name, '')) do update set
                  amount = excluded.amount,
                  quality = excluded.quality,
                  source = excluded.source,
                  scraped_at = excluded.scraped_at
                """,
                (team, season, player_name, amount, quality, source, scraped_at),
            )

    def upsert_dead_money(
        self,
        *,
        team: str,
        season: str,
        player_name: str,
        player_id: str | None,
        amount: int,
        source: str,
        scraped_at: datetime,
    ) -> None:
        # scraped_at is the freshness stamp (migration 20260714210500) the
        # db-mode export filters on; refreshed on every upsert like the other
        # ingested tables. Requires that migration; fails loud without it.
        with self.conn.cursor() as cur:
            cur.execute(
                """
                insert into public.v3_dead_money
                  (team, season, player_name, player_id, amount, source, scraped_at)
                values (%s, %s, %s, %s, %s, %s, %s)
                on conflict (team, season, player_name) do update set
                  player_id = excluded.player_id,
                  amount = excluded.amount,
                  source = excluded.source,
                  scraped_at = excluded.scraped_at
                """,
                (team, season, player_name, player_id, amount, source, scraped_at),
            )

    def upsert_cap_threshold(
        self,
        *,
        season: str,
        salary_cap: int,
        minimum_team_salary: int,
        luxury_tax: int,
        first_apron: int,
        second_apron: int,
        certified: bool,
        source: str,
    ) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                insert into public.v3_cap_thresholds
                  (season, salary_cap, minimum_team_salary, luxury_tax,
                   first_apron, second_apron, certified, source, updated_at)
                values (%s, %s, %s, %s, %s, %s, %s, %s, now())
                on conflict (season) do update set
                  salary_cap = excluded.salary_cap,
                  minimum_team_salary = excluded.minimum_team_salary,
                  luxury_tax = excluded.luxury_tax,
                  first_apron = excluded.first_apron,
                  second_apron = excluded.second_apron,
                  certified = excluded.certified,
                  source = excluded.source,
                  updated_at = now()
                """,
                (
                    season,
                    salary_cap,
                    minimum_team_salary,
                    luxury_tax,
                    first_apron,
                    second_apron,
                    certified,
                    source,
                ),
            )

    def retire_override(self, override_id: str) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "update public.v3_overrides set retired_at = now() where id = %s and retired_at is null",
                (override_id,),
            )

    def insert_verification(
        self,
        *,
        player_id: str | None,
        field: str,
        our_value: str,
        bbref_value: str | None,
        spotrac_value: str | None,
        verdict: str,
        run_id: str,
    ) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                insert into public.v3_verifications
                  (player_id, field, our_value, bbref_value, spotrac_value, verdict, run_id)
                values (%s, %s, %s, %s, %s, %s, %s)
                """,
                (player_id, field, our_value, bbref_value, spotrac_value, verdict, run_id),
            )

    def insert_run(
        self,
        *,
        started_at: datetime,
        status: str,
        rows_written: int,
        rows_changed: int,
        guard_failures: list[dict] | None,
        error: str | None,
        summary: dict | None,
    ) -> str:
        """Insert the run record. ``summary`` needs the follow-up migration
        (v3_ingest_runs.summary jsonb); the column is probed first so a
        not-yet-applied migration degrades to a summary-less record instead
        of an error (and never triggers a transaction rollback mid-run)."""
        guard_json = json.dumps(guard_failures) if guard_failures else None
        include_summary = summary is not None and self._has_summary_column()
        cols = "source, started_at, finished_at, status, rows_written, rows_changed, guard_failures, error"
        placeholders = "%s, %s, now(), %s, %s, %s, %s, %s"
        params: list = [
            RUN_SOURCE,
            started_at,
            status,
            rows_written,
            rows_changed,
            guard_json,
            error,
        ]
        if include_summary:
            cols += ", summary"
            placeholders += ", %s"
            params.append(json.dumps(summary))
        with self.conn.cursor() as cur:
            cur.execute(
                f"insert into public.v3_ingest_runs ({cols}) values ({placeholders}) returning id::text",
                params,
            )
            return cur.fetchone()[0]

    def _has_summary_column(self) -> bool:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                select 1 from information_schema.columns
                where table_schema = 'public' and table_name = 'v3_ingest_runs'
                  and column_name = 'summary'
                """
            )
            return cur.fetchone() is not None
