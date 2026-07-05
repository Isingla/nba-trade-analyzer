"""Pure ingest decision logic (Phase 2A) — no network, no DB, fully unit-tested.

Four decisions live here, each encoding an adjudicated rule from the databallr
Phase 0 trace:

  * :func:`separate_dead_money` — the Lillard/Beal fix (Path 1d): a BBRef
    contract row duplicated across teams is dropped ONLY when a dead-money row
    explains it to the dollar; anything less certain is flagged, never guessed.
  * :func:`plan_option_transitions` — the no-guess option differ (Path 3c/3d):
    a P/T marker that clears upstream is a HUMAN adjudication
    (exercised/declined/renegotiated), so the DB row is left alone and a
    mismatch verification is emitted.
  * :func:`evaluate_guards` / :func:`staleness_warnings` — the loud-failure
    guards (fact #5): row/dollar collapse and empty sources hard-block.
  * :func:`plan_override_retirements` — the override lifecycle: an active
    override whose value the fresh ingest now agrees with has served its
    purpose and is retired (docs/DB-CONTRACT-DATA.md semantics).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from nba_trade_analyzer.data.dead_money import DeadMoneyRow

# Codes that represent an actual option (a decision someone will make), as
# opposed to informational markers (NG/D/UFA/RFA drift with roster churn).
OPTION_DECISION_CODES = frozenset({"P", "T"})

# Row/dollar collapse threshold: a fresh ingest producing less than this
# fraction of the previous state is refused (mirrors sync-cap-data's
# MIN_RETAIN_RATIO = 0.8).
MIN_RETAIN_RATIO = 0.8

STALENESS_MAX_AGE_DAYS = 7


# ---------------------------------------------------------------------------
# Dead-money separation
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ContractSeasonAmounts:
    """One BBRef contract row, reduced to what separation needs."""

    slug: str
    player_name: str
    team: str  # BBRef salary code (BRK/CHO/PHO style)
    amounts: dict[str, int]  # season -> dollars (>0 only)
    is_rookie_scale: bool = False
    has_player_option: bool = False
    has_team_option: bool = False


@dataclass(frozen=True)
class DuplicateFlag:
    """A cross-team duplicate that dead money could NOT explain — human review."""

    slug: str
    player_name: str
    kept_team: str
    other_teams: tuple[str, ...]


@dataclass
class DeadMoneySeparation:
    kept: list[ContractSeasonAmounts] = field(default_factory=list)
    dropped: list[tuple[str, str, str]] = field(default_factory=list)  # (slug, team, why)
    flags: list[DuplicateFlag] = field(default_factory=list)


def _dead_row_explains(contract: ContractSeasonAmounts, dead: DeadMoneyRow) -> bool:
    """True iff the dead-money schedule matches the contract row to the dollar.

    The contract row's schedule must EQUAL the dead-money schedule — same
    seasons, same dollars. A subset match is not enough (a one-season dead
    row must never erase a five-season mixed row). Rows that fail this
    whole-schedule test get a second chance at the SEASON level
    (:func:`_season_level_split`) before falling back to the flag bucket.
    Exactness at whichever grain is the point: these branches silently drop
    salary data, so they must never fire on a coincidence.
    """
    if not dead.amounts:
        return False
    return contract.amounts == dead.amounts


def _merged_dead_seasons(dead_rows: list[DeadMoneyRow]) -> dict[str, int] | None:
    """Merge one team's dead-money rows into season -> dollars; None on conflict."""
    merged: dict[str, int] = {}
    for dead in dead_rows:
        for season, amount in dead.amounts.items():
            if season in merged and merged[season] != amount:
                return None  # two dead rows disagree — ambiguous, don't guess.
            merged[season] = amount
    return merged


def _season_level_split(
    survivors: list[ContractSeasonAmounts],
    dead_rows: list[DeadMoneyRow],
    display_to_bbref: dict[str, str],
) -> tuple[ContractSeasonAmounts, ContractSeasonAmounts] | None:
    """The mixed-schedule rule for the primary target case (Phase 0 Path 1d).

    Lillard's duplicated rows carry a MIXED schedule — 36620603, 35915403,
    36620603, 22516603, 22516603 — where only the trailing seasons equal his
    MIL stretch charge (22516603, to the dollar). The whole-schedule matcher
    can't fire on that, so classify PER SEASON: a season whose amount exactly
    equals that season's dead-money amount on team X belongs to dead money;
    the remaining seasons form the active contract, attributed to the other
    (non-dead-money) team.

    Applies ONLY in the unambiguous shape — anything else returns None and
    the caller keeps the conservative first-team + duplicate_team_rows flag:
      - exactly 2 duplicate rows on 2 distinct teams;
      - dead-money rows exist for exactly ONE of those teams;
      - the two rows carry IDENTICAL schedules (BBRef's actual duplicate
        pattern; differing schedules are a different, unknown situation);
      - classification is non-degenerate: at least one season lands in each
        bucket (all-dead is the whole-schedule matcher's job; none-dead means
        there is nothing here for dead money to explain).

    Returns (kept_active_row, dropped_dead_team_row) or None.
    """
    if len(survivors) != 2:
        return None
    a, b = survivors
    if a.team == b.team or a.amounts != b.amounts:
        return None

    def _has_dead(team: str) -> bool:
        return any(display_to_bbref.get(d.team, d.team) == team for d in dead_rows)

    dead_teams = [t for t in (a.team, b.team) if _has_dead(t)]
    if len(dead_teams) != 1:
        return None  # zero or both sides hold dead money — ambiguous.
    dead_team = dead_teams[0]
    dead_row_for_team = [
        d for d in dead_rows if display_to_bbref.get(d.team, d.team) == dead_team
    ]
    dead_seasons = _merged_dead_seasons(dead_row_for_team)
    if dead_seasons is None:
        return None

    schedule = a.amounts  # identical on both rows (checked above)
    dead_bucket = {s: amt for s, amt in schedule.items() if dead_seasons.get(s) == amt}
    active_bucket = {s: amt for s, amt in schedule.items() if s not in dead_bucket}
    if not dead_bucket or not active_bucket:
        return None

    dropped = a if a.team == dead_team else b
    active_src = b if dropped is a else a
    kept = ContractSeasonAmounts(
        slug=active_src.slug,
        player_name=active_src.player_name,
        team=active_src.team,
        amounts=active_bucket,
        is_rookie_scale=active_src.is_rookie_scale,
        has_player_option=active_src.has_player_option,
        has_team_option=active_src.has_team_option,
    )
    return kept, dropped


def separate_dead_money(
    contracts: list[ContractSeasonAmounts],
    dead_by_slug: dict[str, list[DeadMoneyRow]],
    display_to_bbref: dict[str, str],
) -> DeadMoneySeparation:
    """Split phantom dead-money duplicates out of the BBRef contract rows.

    ``dead_by_slug`` maps resolved bbref slugs to their dead-money rows (the
    caller resolves names via ``ingest.names``). ``display_to_bbref`` converts
    the dead-money CSV's display team codes (PHX) to BBRef codes (PHO) so the
    team comparison is apples-to-apples.

    Rules (Phase 2A spec + the season-level extension):
      - a slug with one row passes through untouched;
      - for a duplicated slug, a row is dropped iff a dead-money row on that
        SAME team matches it to the dollar (see :func:`_dead_row_explains`);
      - surviving 2-team identical-schedule pairs get the season-level split
        (see :func:`_season_level_split`) — the mixed-schedule Lillard/Beal
        pattern resolves to (active contract on the other team, dead-money
        team row dropped) with no flag;
      - if after dropping explained phantoms more than one row remains, the
        FIRST row (BBRef order) is kept and the rest are flagged for human
        review — never silently merged.
    """
    by_slug: dict[str, list[ContractSeasonAmounts]] = {}
    order: list[str] = []
    result = DeadMoneySeparation()

    for c in contracts:
        if not c.slug:
            result.kept.append(c)  # un-slugged rows can't be grouped; pass through.
            continue
        if c.slug not in by_slug:
            order.append(c.slug)
        by_slug.setdefault(c.slug, []).append(c)

    for slug in order:
        rows = by_slug[slug]
        if len(rows) == 1:
            result.kept.append(rows[0])
            continue

        dead_rows = dead_by_slug.get(slug, [])
        survivors: list[ContractSeasonAmounts] = []
        for row in rows:
            explained = False
            for dead in dead_rows:
                dead_team_bbref = display_to_bbref.get(dead.team, dead.team)
                if dead_team_bbref == row.team and _dead_row_explains(row, dead):
                    result.dropped.append(
                        (slug, row.team, f"dead-money match ({dead.player_raw})")
                    )
                    explained = True
                    break
            if not explained:
                survivors.append(row)

        if not survivors:
            # Every row matched dead money — nothing contractual left. Keep
            # nothing but flag it: a player with ONLY dead money should not
            # have appeared on the contracts page at all.
            result.flags.append(
                DuplicateFlag(
                    slug=slug,
                    player_name=rows[0].player_name,
                    kept_team="",
                    other_teams=tuple(r.team for r in rows),
                )
            )
            continue

        if len(survivors) > 1:
            split = _season_level_split(survivors, dead_rows, display_to_bbref)
            if split is not None:
                kept, dropped_row = split
                result.kept.append(kept)
                result.dropped.append(
                    (
                        slug,
                        dropped_row.team,
                        "season-level dead-money split "
                        f"(dead: {', '.join(sorted(set(dropped_row.amounts) - set(kept.amounts)))})",
                    )
                )
                continue

        result.kept.append(survivors[0])
        if len(survivors) > 1:
            result.flags.append(
                DuplicateFlag(
                    slug=slug,
                    player_name=survivors[0].player_name,
                    kept_team=survivors[0].team,
                    other_teams=tuple(r.team for r in survivors[1:]),
                )
            )
    return result


# ---------------------------------------------------------------------------
# Option transitions (the non-guesser)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DbOptionState:
    code: str
    status: str


@dataclass(frozen=True)
class OptionUpsert:
    slug: str
    season: str
    code: str
    status: str
    # The source CSV's git commit date — NEVER the run date (Phase 0 Path 3a:
    # regen date != data date). None when the checkout has no git history.
    status_as_of: date | None


@dataclass(frozen=True)
class OptionTransitionFlag:
    """A P/T marker changed/cleared upstream — adjudicate by hand, never guess."""

    slug: str
    season: str
    db_code: str
    db_status: str
    csv_state: str  # the new code, or "cleared" / "row_absent"


@dataclass
class OptionTransitionPlan:
    upserts: list[OptionUpsert] = field(default_factory=list)
    flags: list[OptionTransitionFlag] = field(default_factory=list)


def plan_option_transitions(
    csv_codes: dict[str, dict[str, str]],  # slug -> season -> code
    db_state: dict[tuple[str, str], DbOptionState],  # (slug, season) -> state
    csv_seasons: set[str],  # seasons the CSV meaningfully covers
    status_as_of: date | None,
) -> OptionTransitionPlan:
    """Diff CSV option codes against the DB without guessing outcomes.

    - same code both sides -> keep the DB status (pending if blank), refresh
      ``status_as_of`` (the marker is still present as of the CSV date);
    - P/T in DB but a DIFFERENT code / cleared cell / absent row in the CSV
      -> DB row untouched, transition flag emitted (exercised vs declined vs
      renegotiated is human judgment via v3_overrides — Phase 2A spec);
    - non-decision markers (NG/D/UFA/RFA) just track the CSV;
    - brand-new codes: P/T start ``pending``, markers start ``unknown``.

    ``csv_seasons`` bounds the cleared/absent sweep: the CSV cannot speak to
    seasons it has no real column for (its trailing 2025-26 column is junk),
    so DB rows for uncovered seasons are left entirely alone.
    """
    plan = OptionTransitionPlan()

    for slug, seasons in csv_codes.items():
        for season, code in seasons.items():
            db = db_state.get((slug, season))
            if db is None:
                status = "pending" if code in OPTION_DECISION_CODES else "unknown"
                plan.upserts.append(
                    OptionUpsert(slug, season, code, status, status_as_of)
                )
            elif db.code == code:
                status = db.status or "pending"
                plan.upserts.append(
                    OptionUpsert(slug, season, code, status, status_as_of)
                )
            elif db.code in OPTION_DECISION_CODES:
                # P/T changed to something else upstream: do not guess.
                plan.flags.append(
                    OptionTransitionFlag(slug, season, db.code, db.status, code)
                )
            else:
                # Marker drift (e.g. NG -> RFA): not an option decision; track it.
                status = "pending" if code in OPTION_DECISION_CODES else "unknown"
                plan.upserts.append(
                    OptionUpsert(slug, season, code, status, status_as_of)
                )

    for (slug, season), db in db_state.items():
        if db.code not in OPTION_DECISION_CODES:
            continue
        if season not in csv_seasons:
            continue  # the CSV can't speak to this season — leave it alone.
        csv_code = csv_codes.get(slug, {}).get(season)
        if csv_code is not None:
            continue  # handled (same-code refresh or change-flag) above.
        state = "cleared" if slug in csv_codes else "row_absent"
        plan.flags.append(
            OptionTransitionFlag(slug, season, db.code, db.status, state)
        )
    return plan


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GuardFailure:
    guard: str  # row_collapse | dollar_collapse | empty_source
    subject: str  # table or source name
    detail: dict


@dataclass(frozen=True)
class TableStats:
    rows: int
    dollars: int | None = None  # only meaningful for salaries / cap holds


def evaluate_guards(
    planned: dict[str, TableStats],
    previous: dict[str, TableStats],
    min_ratio: float = MIN_RETAIN_RATIO,
) -> list[GuardFailure]:
    """Row-count and total-dollar collapse guards against the previous state.

    ``previous`` comes from the live DB (which reflects the last successful
    run). A table with no previous rows can't collapse — first-run friendly.
    """
    failures: list[GuardFailure] = []
    for table, prev in previous.items():
        plan = planned.get(table)
        if plan is None or prev.rows <= 0:
            continue
        if plan.rows < prev.rows * min_ratio:
            failures.append(
                GuardFailure(
                    guard="row_collapse",
                    subject=table,
                    detail={
                        "previous_rows": prev.rows,
                        "planned_rows": plan.rows,
                        "min_ratio": min_ratio,
                    },
                )
            )
        if (
            prev.dollars is not None
            and prev.dollars > 0
            and plan.dollars is not None
            and plan.dollars < prev.dollars * min_ratio
        ):
            failures.append(
                GuardFailure(
                    guard="dollar_collapse",
                    subject=table,
                    detail={
                        "previous_dollars": prev.dollars,
                        "planned_dollars": plan.dollars,
                        "min_ratio": min_ratio,
                    },
                )
            )
    return failures


# Guard types an operator may downgrade to report-only with --accept-baseline.
# ONLY previous-state comparisons are bypassable: the seed's known-placeholder
# baseline (docs/DB-CONTRACT-DATA.md) makes a first real ingest look like a
# collapse. Source-quality guards (empty_source) are NEVER bypassable — a
# broken source is broken regardless of what the DB currently holds.
BASELINE_BYPASSABLE_GUARDS = frozenset({"row_collapse", "dollar_collapse"})


def apply_baseline_acceptance(
    failures: list[GuardFailure],
    accept_reason: str | None,
) -> tuple[list[GuardFailure], list[GuardFailure]]:
    """Split guard failures into (blocking, report_only) under --accept-baseline.

    Without a reason (flag absent), everything blocks — current behavior.
    With a reason, collapse guards become report-only (recorded in the run
    summary as baseline_overrides, never silently dropped); any other guard
    type still blocks.
    """
    if accept_reason is None:
        return failures, []
    blocking = [f for f in failures if f.guard not in BASELINE_BYPASSABLE_GUARDS]
    report_only = [f for f in failures if f.guard in BASELINE_BYPASSABLE_GUARDS]
    return blocking, report_only


def empty_source_guards(source_rows: dict[str, int]) -> list[GuardFailure]:
    """An empty source is a broken source — guard_blocked, never an empty write."""
    return [
        GuardFailure(guard="empty_source", subject=name, detail={"rows": rows})
        for name, rows in source_rows.items()
        if rows <= 0
    ]


def staleness_warnings(
    source_dates: dict[str, datetime | None],
    now: datetime,
    max_age_days: int = STALENESS_MAX_AGE_DAYS,
) -> list[str]:
    """Warn (not block) when a site_Data CSV's git commit date is too old."""
    warnings: list[str] = []
    for name, dt in source_dates.items():
        if dt is None:
            warnings.append(f"{name}: no git commit date available")
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age = now - dt
        if age > timedelta(days=max_age_days):
            warnings.append(
                f"{name}: last commit {dt.date().isoformat()} is {age.days} days old (>{max_age_days})"
            )
    return warnings


# ---------------------------------------------------------------------------
# Override retirement
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ActiveOverride:
    id: str
    table_name: str
    row_key: str
    field: str
    value: str


def plan_override_retirements(
    overrides: list[ActiveOverride],
    ingested_values: dict[tuple[str, str, str], str],
) -> tuple[list[ActiveOverride], list[ActiveOverride]]:
    """Split active overrides into (retire, keep).

    An override is retired when the freshly ingested value for its
    (table, row_key, field) EQUALS the override's value — the upstream source
    caught up, the override has served its purpose (DB-CONTRACT-DATA.md
    semantics). Overrides whose target the ingest didn't produce a value for
    are kept untouched (we know nothing new about them).
    """
    retire: list[ActiveOverride] = []
    keep: list[ActiveOverride] = []
    for o in overrides:
        current = ingested_values.get((o.table_name, o.row_key, o.field))
        if current is not None and current == o.value:
            retire.append(o)
        else:
            keep.append(o)
    return retire, keep
