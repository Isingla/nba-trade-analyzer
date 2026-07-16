"""databallr export sourced from the v3_* contract DB (``export --source db``).

Phase 2 of export-reads-DB (spec: the Phase 1 seam map). Produces the same
salary-frame shape the Basketball Reference scrape yields (``EXPECTED_COLUMNS``,
``data/salaries.py``) from the adjudicated contract tables instead, so
``build_export`` runs unchanged through its existing injectable parameters.
Projections/EPM/DARKO stay on the cache path — this module only replaces the
salary frame, the NG marks, and the cap-hold totals.

Ground rules (all ratified):

* **Freshness predicate.** Only rows stamped by the last SUCCESSFUL ingest run
  (``scraped_at >= run.started_at``) exist as far as this reader is concerned.
  Upsert-never-delete means the tables accumulate zombie rows; the predicate is
  what keeps them out of the payload.
* **Fail-loud staleness.** A last-success run older than ``MAX_RUN_AGE_DAYS``
  REFUSES to export (:class:`StaleRunError`) — no silent stale payloads,
  mirroring the ingest's guard posture.
* **Overrides on read, default-on.** Active ``v3_overrides`` (``retired_at IS
  NULL``) overlay the base rows: option ``status``/``code`` overrides feed the
  derived option flags, ``is_fully_ng`` overrides feed the NG marks. Callers
  can disable the overlay (``--no-overrides``) for parity debugging; applied
  overrides are reported so a payload is self-describing.
* **Derived option flags are interim** (seam map G4): ``has_player_option`` /
  ``has_team_option`` are derived from ``v3_contract_options`` codes on seasons
  that carry a fresh salary row, counting only OPEN statuses
  (pending/unknown) — an exercised or declined option is plain salary or gone.
  The Day 2 schema columns replace this derivation.
* **Read-only.** Connects as the restricted ``contract_ingest`` role via
  ``$CONTRACT_INGEST_DATABASE_URL`` and issues SELECTs only.

Every builder below is pure over plain dataclasses so tests stub rows without
a live DB, mirroring ``build_export``'s injectable-frame pattern.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pandas as pd

from nba_trade_analyzer.data.salaries import EXPECTED_COLUMNS, _YEARLY_SEP

logger = logging.getLogger(__name__)

# Refuse to export DB state older than this. The nightly ingest runs at 1:15am;
# three missed nights means the watch/runbook has already escalated and a
# payload built from that state would silently ship pre-incident data.
MAX_RUN_AGE_DAYS = 3

# Option statuses that still represent an OPEN decision. Exercised/declined
# options are settled — the year is plain salary (or off the books) and BBRef's
# CSS would no longer paint it as an option year.
OPEN_OPTION_STATUSES = frozenset({"pending", "unknown"})

_PLAYER_OPTION_CODE = "P"
_TEAM_OPTION_CODE = "T"

_SALARIES_TABLE = "v3_contract_salaries"
_OPTIONS_TABLE = "v3_contract_options"


class DbSourceError(RuntimeError):
    """Any condition that makes a DB-sourced export unsafe to produce."""


class StaleRunError(DbSourceError):
    """The last successful ingest run is too old to export from."""


# ---------------------------------------------------------------------------
# Snapshot dataclasses — the fetch layer's output, the builders' input.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DbSalaryRow:
    """One FRESH ``v3_contract_salaries`` row joined to its player."""

    slug: str
    player_name: str
    team: str  # v3_players.team_bbref (the scrape's BRK/CHO/PHO code system)
    nba_id: int | None
    season: str
    amount: int
    is_fully_ng: bool
    is_rookie_scale: bool
    # Stored BBRef CSS-truth flags (G4(b), migration 20260714210000).
    # None = pre-migration row -> the Day-1 derivation is the fallback.
    has_player_option: bool | None = None
    has_team_option: bool | None = None


@dataclass(frozen=True)
class DbOptionRow:
    slug: str
    season: str
    code: str
    status: str


@dataclass(frozen=True)
class DbOverrideRow:
    table_name: str
    row_key: str  # "<slug>|<season>"
    field: str
    value: str


@dataclass(frozen=True)
class DbCapHoldRow:
    team: str
    season: str
    amount: int
    quality: str  # 'real' | 'sentinel'
    fresh: bool  # scraped_at >= run.started_at


@dataclass(frozen=True)
class DbDeadMoneyRow:
    team: str  # display code (PHX/MIL style)
    season: str
    player_name: str  # raw source name (may carry a WAIVED marker)
    bbref_slug: str | None
    amount: int
    # True/False vs run.started_at; None = unstamped pre-migration row
    # (v3_dead_money gained scraped_at in migration 20260714210500).
    fresh: bool | None


@dataclass(frozen=True)
class DbSnapshot:
    """Everything one DB round-trip yields; builders never touch the DB."""

    run_id: str
    run_started_at: datetime
    salaries: list[DbSalaryRow]  # fresh rows only
    options: list[DbOptionRow]  # all option rows (builders gate to fresh seasons)
    overrides: list[DbOverrideRow]  # ACTIVE overrides only
    cap_holds: list[DbCapHoldRow]  # all rows, freshness precomputed
    dead_money: list[DbDeadMoneyRow] = field(default_factory=list)


@dataclass
class SalaryBuild:
    """Output of :func:`build_salary_records` + provenance for the payload."""

    records: list[dict]
    # (player_name, team, season) and (nba_id, season) keys the NG resolver
    # answers True for — see DbNonGuaranteeResolver.
    ng_name_keys: set[tuple[str, str, str]] = field(default_factory=set)
    ng_id_keys: set[tuple[int, str]] = field(default_factory=set)
    applied_overrides: list[DbOverrideRow] = field(default_factory=list)
    skipped_no_current: list[str] = field(default_factory=list)  # slugs
    canary_ok: bool = True


# ---------------------------------------------------------------------------
# Freshness guard
# ---------------------------------------------------------------------------

def check_run_freshness(
    started_at: datetime,
    *,
    now: datetime | None = None,
    max_age_days: int = MAX_RUN_AGE_DAYS,
) -> None:
    """Raise :class:`StaleRunError` when the last success run is too old."""
    now = now or datetime.now(timezone.utc)
    age = now - started_at
    age_days = age.total_seconds() / 86400.0
    if age_days > max_age_days:
        raise StaleRunError(
            f"last successful ingest run started {started_at.isoformat()} is "
            f"{age_days:.1f} days old (max {max_age_days}) — REFUSING to export "
            "stale DB state. Run `nba-trade-analyzer ingest` first, or use "
            "`export --source scrape`."
        )


# ---------------------------------------------------------------------------
# Pure builders
# ---------------------------------------------------------------------------

def _override_values(
    overrides: list[DbOverrideRow], table: str, fld: str
) -> dict[str, DbOverrideRow]:
    """Active overrides for one (table, field), keyed by row_key."""
    return {
        o.row_key: o
        for o in overrides
        if o.table_name == table and o.field == fld
    }


def build_salary_records(
    snapshot: DbSnapshot,
    seasons: list[str],
    *,
    apply_overrides: bool = True,
) -> SalaryBuild:
    """Reduce a snapshot to scrape-shaped salary records (EXPECTED_COLUMNS).

    ``seasons`` is the SALARY window (``export.salary_season_keys()`` —
    projection window + BBRef's y6, 2031-32), current season first. Derivations per the seam map: ``yearly_salaries`` maps rows onto the
    window positionally with 0-fill for interior gaps and truncation after the
    last present season (G2); ``years_remaining`` is the fresh-row count;
    option flags are derived (G4 interim; see module docstring);
    ``is_rookie_scale`` and NG marks read the stored columns.
    """
    build = SalaryBuild(records=[])
    if not seasons:
        raise DbSourceError("empty season window — cannot shape salary records")
    current_season = seasons[0]
    season_index = {s: i for i, s in enumerate(seasons)}

    # G7 canary, input side: the DB grain guarantees one row per
    # (player, season); duplicates here mean the fetch or the grain broke.
    seen_pairs: set[tuple[str, str]] = set()
    duplicate_pairs: set[tuple[str, str]] = set()
    for row in snapshot.salaries:
        pair = (row.slug, row.season)
        if pair in seen_pairs:
            duplicate_pairs.add(pair)
        seen_pairs.add(pair)
    if duplicate_pairs:
        build.canary_ok = False
        logger.warning(
            "G7 canary: %d duplicate player-season salary row(s) in the DB "
            "read (%s) — the player-season grain assumption is violated; "
            "payload amounts for these players are last-row-wins",
            len(duplicate_pairs),
            sorted(duplicate_pairs),
        )

    by_slug: dict[str, dict[str, DbSalaryRow]] = {}
    order: list[str] = []  # first-seen order, deterministic output
    for row in snapshot.salaries:
        if row.slug not in by_slug:
            order.append(row.slug)
        by_slug.setdefault(row.slug, {})[row.season] = row

    status_overrides = _override_values(snapshot.overrides, _OPTIONS_TABLE, "status")
    code_overrides = _override_values(snapshot.overrides, _OPTIONS_TABLE, "code")
    ng_overrides = _override_values(snapshot.overrides, _SALARIES_TABLE, "is_fully_ng")
    applied: dict[tuple[str, str, str], DbOverrideRow] = {}

    # (slug, code) pairs an override actually spoke to. Where an override has
    # spoken, the adjudicated DERIVATION outranks the stored CSS flag (house
    # semantics: an active override beats ingested data — including the
    # scraped flags). Everywhere else, stored flags are the parity truth.
    override_touched: set[tuple[str, str]] = set()

    def _effective_option(opt: DbOptionRow) -> tuple[str, str]:
        """(code, status) after the overlay; records what it applied."""
        code, status = opt.code, opt.status
        if not apply_overrides:
            return code, status
        key = f"{opt.slug}|{opt.season}"
        if key in code_overrides:
            o = code_overrides[key]
            code = o.value
            applied[(o.table_name, o.row_key, o.field)] = o
            override_touched.add((opt.slug, opt.code))
            override_touched.add((opt.slug, code))
        if key in status_overrides:
            o = status_overrides[key]
            status = o.value
            applied[(o.table_name, o.row_key, o.field)] = o
            override_touched.add((opt.slug, code))
        return code, status

    # Option rows only count on seasons that carry a FRESH salary row for the
    # same player (an option marker on an off-the-books season is history).
    open_codes: dict[str, set[str]] = {}
    for opt in snapshot.options:
        if opt.season not in by_slug.get(opt.slug, {}):
            continue
        code, status = _effective_option(opt)
        if status in OPEN_OPTION_STATUSES:
            open_codes.setdefault(opt.slug, set()).add(code)

    for slug in order:
        rows = by_slug[slug]
        current = rows.get(current_season)
        if current is None:
            # Mirror the scrape ("no current-season figure -> no usable
            # contract"), but say so: in DB mode this is a data oddity worth
            # eyes, not routine.
            build.skipped_no_current.append(slug)
            logger.warning(
                "db_source: skipping %s — %d fresh row(s) but none for the "
                "current season %s",
                slug,
                len(rows),
                current_season,
            )
            continue

        # A fresh row OUTSIDE the salary window means BBRef grew another
        # year column (the next y6-class surprise) — fail crisply instead of
        # the bare KeyError the positional mapping would otherwise raise;
        # silent dropping is never an option (that was the Wemby 2031-32 bug).
        outside = sorted(set(rows) - set(season_index))
        if outside:
            raise DbSourceError(
                f"{slug} has fresh salary row(s) outside the salary window "
                f"(last window season {seasons[-1]}): {outside} — widen "
                "export.salary_season_keys() deliberately; never drop money."
            )

        # G2: positional 0-fill onto the window, truncated after the last
        # season that actually has a row.
        last_present = max(season_index[s] for s in rows)
        yearly = [
            rows[seasons[i]].amount if seasons[i] in rows else 0
            for i in range(last_present + 1)
        ]

        # NG marks: stored is_fully_ng, then the override overlay.
        for season, row in rows.items():
            marked = row.is_fully_ng
            if apply_overrides:
                o = ng_overrides.get(f"{slug}|{season}")
                if o is not None:
                    marked = o.value.strip().lower() == "true"
                    applied[(o.table_name, o.row_key, o.field)] = o
            if marked:
                build.ng_name_keys.add((row.player_name, row.team, season))
                if row.nba_id is not None:
                    build.ng_id_keys.add((int(row.nba_id), season))

        # Flag precedence per code: an override that touched this player's
        # code wins (adjudicated derivation) > stored CSS flag (parity truth,
        # non-NULL) > Day-1 derivation (pre-migration NULL rows).
        codes = open_codes.get(slug, set())

        def _flag(code: str, stored: bool | None) -> bool:
            if stored is None or (slug, code) in override_touched:
                return code in codes
            return stored

        build.records.append(
            {
                "player_name": current.player_name,
                "bbref_slug": slug,
                "team": current.team,
                "salary": int(current.amount),
                "years_remaining": len(rows),
                "is_rookie_scale": bool(current.is_rookie_scale),
                "has_player_option": _flag(
                    _PLAYER_OPTION_CODE, current.has_player_option
                ),
                "has_team_option": _flag(_TEAM_OPTION_CODE, current.has_team_option),
                "yearly_salaries": _YEARLY_SEP.join(str(v) for v in yearly),
            }
        )

    build.applied_overrides = sorted(
        applied.values(), key=lambda o: (o.table_name, o.row_key, o.field)
    )

    # G7 canary, output side: one payload row per distinct fresh player.
    distinct_players = len(by_slug) - len(build.skipped_no_current)
    if len(build.records) != distinct_players:
        build.canary_ok = False
        logger.warning(
            "G7 canary: payload rows (%d) != distinct fresh players (%d)",
            len(build.records),
            distinct_players,
        )
    return build


def build_cap_hold_totals(
    holds: list[DbCapHoldRow],
) -> dict[str, dict[str, int]]:
    """Team-season hold totals from FRESH rows of REAL quality only.

    Sentinel rows are placeholder debris (Phase 0 Path 2d), stale rows are
    upsert zombies absent from the current source — both excluded (seam map
    G5). Matches the shape ``load_cap_holds`` returns and ``build_export``'s
    ``cap_holds`` parameter expects.
    """
    totals: dict[str, dict[str, int]] = {}
    for row in holds:
        if not row.fresh or row.quality != "real" or row.amount <= 0:
            continue
        team_map = totals.setdefault(row.team, {})
        team_map[row.season] = team_map.get(row.season, 0) + row.amount
    return totals


def select_dead_money_rows(rows: list[DbDeadMoneyRow]) -> list[DbDeadMoneyRow]:
    """Freshness rule for dead money: any-fresh-else-all (self-healing).

    Once ANY row carries a fresh stamp (the first post-migration nightly has
    run), only fresh rows are kept — NULL-stamped rows are pre-migration
    zombies. Until then every row is unstamped (scraped_at was added by
    migration 20260714210500), so all rows pass with a loud warning rather
    than silently exporting an empty block on day one.
    """
    if any(r.fresh is True for r in rows):
        return [r for r in rows if r.fresh is True]
    if rows:
        logger.warning(
            "dead money: no freshness-stamped rows yet (pre-first-nightly "
            "after migration 20260714210500) — exporting all %d rows; "
            "stamps arrive with the next ingest",
            len(rows),
        )
    return list(rows)


class DbNonGuaranteeResolver:
    """Duck-type of ``NonGuaranteeResolver.is_non_guaranteed`` backed by stored
    ``is_fully_ng`` marks (plus any override overlay already applied by
    :func:`build_salary_records`).

    No season gating here: the ingest applied the resolver's future-season gate
    when it wrote the marks, and the stored column is the adjudicated truth.
    Name/team keys are matched verbatim — both sides of the lookup originate
    from the same DB rows, so no normalization layer is needed.
    """

    def __init__(
        self,
        name_keys: set[tuple[str, str, str]],
        id_keys: set[tuple[int, str]],
    ) -> None:
        self._name_keys = set(name_keys)
        self._id_keys = set(id_keys)

    def is_non_guaranteed(
        self,
        season: str,
        *,
        nba_id: int | None = None,
        player: str | None = None,
        team: str | None = None,
    ) -> bool:
        if nba_id is not None and (int(nba_id), season) in self._id_keys:
            return True
        return (player or "", team or "", season) in self._name_keys


# ---------------------------------------------------------------------------
# Fetch layer (the only part that talks to the DB — SELECTs as contract_ingest)
# ---------------------------------------------------------------------------

def fetch_db_snapshot(conn) -> DbSnapshot:
    """One read-only round-trip: run, fresh salaries, options, overrides, holds."""
    with conn.cursor() as cur:
        cur.execute(
            "select id::text, started_at from public.v3_ingest_runs "
            "where status = 'success' order by started_at desc limit 1"
        )
        run = cur.fetchone()
        if run is None:
            raise DbSourceError(
                "no successful ingest run recorded in v3_ingest_runs — "
                "the DB has never been ingested; use --source scrape"
            )
        run_id, started_at = run[0], run[1]

        cur.execute(
            """
            select p.bbref_slug, coalesce(p.name, ''), coalesce(p.team_bbref, ''),
                   p.nba_id, s.season, s.amount, s.is_fully_ng, s.is_rookie_scale,
                   s.has_player_option, s.has_team_option
            from public.v3_contract_salaries s
            join public.v3_players p on p.id = s.player_id
            where s.scraped_at >= %s
            order by p.bbref_slug, s.season
            """,
            (started_at,),
        )
        salaries = [
            DbSalaryRow(
                slug=r[0],
                player_name=r[1],
                team=r[2],
                nba_id=int(r[3]) if r[3] is not None else None,
                season=r[4],
                amount=int(r[5]),
                is_fully_ng=bool(r[6]),
                is_rookie_scale=bool(r[7]),
                has_player_option=None if r[8] is None else bool(r[8]),
                has_team_option=None if r[9] is None else bool(r[9]),
            )
            for r in cur.fetchall()
        ]

        cur.execute(
            """
            select p.bbref_slug, o.season, o.code, o.status
            from public.v3_contract_options o
            join public.v3_players p on p.id = o.player_id
            """
        )
        options = [
            DbOptionRow(slug=r[0], season=r[1], code=r[2], status=r[3])
            for r in cur.fetchall()
        ]

        cur.execute(
            "select table_name, row_key, field, value from public.v3_overrides "
            "where retired_at is null"
        )
        overrides = [
            DbOverrideRow(table_name=r[0], row_key=r[1], field=r[2], value=r[3])
            for r in cur.fetchall()
        ]

        cur.execute(
            """
            select team, season, amount, quality, (scraped_at >= %s)
            from public.v3_cap_holds
            """,
            (started_at,),
        )
        cap_holds = [
            DbCapHoldRow(
                team=r[0],
                season=r[1],
                amount=int(r[2]),
                quality=r[3],
                fresh=bool(r[4]),
            )
            for r in cur.fetchall()
        ]

        cur.execute(
            """
            select d.team, d.season, d.player_name, p.bbref_slug, d.amount,
                   (d.scraped_at >= %s)
            from public.v3_dead_money d
            left join public.v3_players p on p.id = d.player_id
            """,
            (started_at,),
        )
        dead_money = [
            DbDeadMoneyRow(
                team=r[0],
                season=r[1],
                player_name=r[2],
                bbref_slug=r[3],
                amount=int(r[4]),
                fresh=None if r[5] is None else bool(r[5]),
            )
            for r in cur.fetchall()
        ]

    return DbSnapshot(
        run_id=run_id,
        run_started_at=started_at,
        salaries=salaries,
        options=options,
        overrides=overrides,
        cap_holds=cap_holds,
        dead_money=dead_money,
    )


# ---------------------------------------------------------------------------
# Orchestrator — what the CLI's db mode calls
# ---------------------------------------------------------------------------

@dataclass
class DbSalarySource:
    """Everything ``export --source db`` feeds into ``build_export`` + the
    provenance stamped into the payload's ``source_note``."""

    frame: pd.DataFrame
    cap_holds: dict[str, dict[str, int]]
    resolver: DbNonGuaranteeResolver
    dead_money: list  # list[export.DeadMoneyCharge] (lazy import boundary)
    run_id: str
    run_started_at: datetime
    applied_overrides: list[DbOverrideRow]
    overrides_applied: bool
    skipped_no_current: list[str]

    @property
    def source_note(self) -> str:
        overlay = (
            f"{len(self.applied_overrides)} override(s) applied: "
            + ", ".join(
                f"{o.table_name}:{o.row_key}.{o.field}={o.value}"
                for o in self.applied_overrides
            )
            if self.overrides_applied and self.applied_overrides
            else (
                "0 overrides applied"
                if self.overrides_applied
                else "overrides overlay DISABLED (--no-overrides)"
            )
        )
        skipped = (
            f"; skipped (no current-season row): {', '.join(self.skipped_no_current)}"
            if self.skipped_no_current
            else ""
        )
        return (
            f"DB-SOURCED (v3_* contract tables): ingest run {self.run_id} "
            f"started {self.run_started_at.isoformat()}; "
            f"{len(self.frame)} salary rows; "
            f"{len(self.dead_money)} dead-money charge(s); {overlay}{skipped}"
        )


def load_db_salary_source(
    *,
    apply_overrides: bool = True,
    now: datetime | None = None,
    conn=None,
) -> DbSalarySource:
    """Fetch, guard, and shape the DB-sourced salary inputs for build_export.

    Owns the connection lifecycle unless one is injected (tests). Raises
    :class:`DbSourceError` / :class:`StaleRunError` — callers decide exit style.
    """
    owns_conn = conn is None
    if owns_conn:
        # Local import mirrors cli.export's lazy-psycopg convention.
        from nba_trade_analyzer.ingest.db import IngestDbError, connect_from_env

        try:
            conn = connect_from_env()
        except IngestDbError as exc:
            raise DbSourceError(str(exc)) from exc
    try:
        snapshot = fetch_db_snapshot(conn)
    except Exception as exc:
        # psycopg.errors.UndefinedColumn (raw import avoided here) — the one
        # anticipated shape: Day-2 code against a pre-migration DB.
        if type(exc).__name__ == "UndefinedColumn":
            raise DbSourceError(
                f"{exc} — the Day-2 schema migrations have not been applied. "
                "Run supabase/migrations/20260714210000_add_v3_salary_option_flags.sql "
                "and 20260714210500_add_v3_dead_money_scraped_at.sql (by hand, "
                "as service_role) before exporting with --source db."
            ) from exc
        raise
    finally:
        if owns_conn:
            conn.close()

    check_run_freshness(snapshot.run_started_at, now=now)

    # Local import: export.py is heavyweight (engine, pydantic) and importing
    # it at module top would also be a cycle risk if export ever imports us.
    from nba_trade_analyzer.export import DeadMoneyCharge, salary_season_keys

    build = build_salary_records(
        snapshot, salary_season_keys(), apply_overrides=apply_overrides
    )
    frame = pd.DataFrame(build.records, columns=list(EXPECTED_COLUMNS))
    dead_money = [
        DeadMoneyCharge(
            team=r.team,
            season=r.season,
            player_name=r.player_name,
            bbref_slug=r.bbref_slug,
            amount=r.amount,
        )
        for r in select_dead_money_rows(snapshot.dead_money)
    ]
    return DbSalarySource(
        frame=frame,
        cap_holds=build_cap_hold_totals(snapshot.cap_holds),
        resolver=DbNonGuaranteeResolver(build.ng_name_keys, build.ng_id_keys),
        dead_money=dead_money,
        run_id=snapshot.run_id,
        run_started_at=snapshot.run_started_at,
        applied_overrides=build.applied_overrides,
        overrides_applied=apply_overrides,
        skipped_no_current=build.skipped_no_current,
    )
