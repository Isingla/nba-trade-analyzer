"""Ingest orchestration (Phase 2A): sources -> guards -> upserts -> overrides -> verify.

Commit discipline: data writes commit as one batch; the override pass and
verification rows commit separately; the run record is always the LAST thing
written (standalone), so a crash mid-run still leaves a coherent DB and the
failure path can record what happened. There are zero DELETEs anywhere.
"""

from __future__ import annotations

import json
import logging
import re
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone

from nba_trade_analyzer.data.cap_holds import (
    classify_cap_hold_teams,
    load_cap_holds_rows,
)
from nba_trade_analyzer.data.crosswalk import load_crosswalk
from nba_trade_analyzer.data.dead_money import (
    DeadMoneyRow,
    load_dead_money,
)
from nba_trade_analyzer.data.epm import api_cache_file, fetch_epm_data
from nba_trade_analyzer.data.guarantees import NonGuaranteeResolver
from nba_trade_analyzer.data.nba_salaries_csv import (
    load_nba_salaries,
    nba_salaries_season_coverage,
)
from nba_trade_analyzer.data.options_csv import load_options
from nba_trade_analyzer.data.salaries import build_contract, fetch_all_salaries
from nba_trade_analyzer.engine.constants import CAP_THRESHOLDS_BY_SEASON
from nba_trade_analyzer.export import API_ACTUALS_SEASON, salary_season_keys
from nba_trade_analyzer.ingest.db import IngestDb, PlayerRec
from nba_trade_analyzer.ingest.names import NameResolver
from nba_trade_analyzer.ingest.plans import (
    ActiveOverride,
    ContractSeasonAmounts,
    DbOptionState,
    TableStats,
    apply_baseline_acceptance,
    empty_source_guards,
    epm_vintage,
    evaluate_guards,
    plan_option_transitions,
    plan_override_retirements,
    separate_dead_money,
    staleness_warnings,
)
from nba_trade_analyzer.ingest.site_data import csv_git_date, site_data_root
from nba_trade_analyzer.ingest.verify import (
    FIELD_DUPLICATE_TEAM_ROWS,
    verify_salaries,
)
from nba_trade_analyzer.teams import ALL_TEAMS

logger = logging.getLogger(__name__)

# Spotrac WAIVED-marker (same shape the verifier/dead-money loader use):
# waived rows carry dead-money schedules and hold no current-team opinion.
_SPOTRAC_WAIVED_RE = re.compile(r"\s+WAIVED\s*$", re.IGNORECASE)

SRC_SALARIES = "ingest:bbref-contracts"
SRC_OPTIONS = "ingest:nba_options.csv"
SRC_CAP_HOLDS = "ingest:nba_cap_holds.csv"
SRC_DEAD_MONEY = "ingest:nba_dead_money.csv"
SRC_THRESHOLDS = "ingest:analyzer-cap-constants"

DISPLAY_TO_BBREF = {t.abbreviation: t.salary_abbreviation for t in ALL_TEAMS}
BBREF_TO_DISPLAY = {t.salary_abbreviation: t.abbreviation for t in ALL_TEAMS}


@dataclass
class RunResult:
    status: str  # success | failed | guard_blocked | dry_run
    rows_written: int = 0
    summary: dict = field(default_factory=dict)
    guard_failures: list[dict] = field(default_factory=list)
    error: str | None = None


def _contract_rows(salary_records: list[dict], seasons: list[str]) -> list[ContractSeasonAmounts]:
    out: list[ContractSeasonAmounts] = []
    for rec in salary_records:
        contract = build_contract(rec)
        amounts: dict[str, int] = {}
        for i, season in enumerate(seasons):
            if i >= len(contract.yearly_salaries):
                break
            if contract.yearly_salaries[i] > 0:
                amounts[season] = int(contract.yearly_salaries[i])
        out.append(
            ContractSeasonAmounts(
                slug=str(rec.get("bbref_slug") or "").strip(),
                player_name=str(rec.get("player_name") or "").strip(),
                team=str(rec.get("team") or "").strip(),
                amounts=amounts,
                is_rookie_scale=bool(rec.get("is_rookie_scale", False)),
                has_player_option=bool(rec.get("has_player_option", False)),
                has_team_option=bool(rec.get("has_team_option", False)),
            )
        )
    return out


def run_ingest(
    db: IngestDb,
    *,
    dry_run: bool = False,
    accept_baseline: str | None = None,
    now: datetime | None = None,
) -> RunResult:
    started_at = now or datetime.now(timezone.utc)
    try:
        return _run(
            db, started_at=started_at, dry_run=dry_run, accept_baseline=accept_baseline
        )
    except Exception as exc:  # noqa: BLE001 — every failure becomes a recorded run.
        logger.error("ingest failed: %s", exc)
        error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=8)}"
        if not dry_run:
            try:
                db.conn.rollback()
                db.insert_run(
                    started_at=started_at,
                    status="failed",
                    rows_written=0,
                    rows_changed=0,
                    guard_failures=None,
                    error=error,
                    summary=None,
                )
                db.conn.commit()
            except Exception:  # noqa: BLE001
                logger.exception("could not record failed run")
        return RunResult(status="failed", error=error)


def _run(
    db: IngestDb,
    *,
    started_at: datetime,
    dry_run: bool,
    accept_baseline: str | None = None,
) -> RunResult:
    # site_data_root() now fails loud on a missing checkout (consolidated
    # default); keep the ingest's contract that a missing source is a RECORDED
    # guard_blocked run, not a generic failure.
    try:
        root = site_data_root()
    except FileNotFoundError as exc:
        return _guard_blocked(
            db,
            started_at,
            dry_run,
            [{"guard": "empty_source", "subject": "SITE_DATA_ROOT", "detail": {"error": str(exc)}}],
        )
    # SALARY window (projection window + BBRef's y6 = 2031-32): salary rows,
    # dead-money season mapping, and verifier coverage all key off this;
    # projections/thresholds stay on export.season_keys().
    seasons = salary_season_keys()

    if accept_baseline is not None:
        # Prominent by design: this run can overwrite a larger baseline.
        banner = (
            "!! --accept-baseline ACTIVE: collapse guards are REPORT-ONLY this run "
            f"(reason: {accept_baseline}). Source-quality guards still block. !!"
        )
        print(banner)
        logger.warning(banner)

    # ---- load sources (strict: any failure here = failed run) -------------
    # BBRef scrape, STRICT — no committed-CSV fallback on the ingest path
    # (Phase 0: the silent fallback shipped months-stale data).
    salary_df = fetch_all_salaries(strict=True)
    salary_records = salary_df.to_dict(orient="records")

    dead_rows = load_dead_money(root / "nba_dead_money.csv")
    option_rows = load_options(root / "nba_options.csv")
    hold_rows = load_cap_holds_rows(root / "nba_cap_holds.csv")
    spotrac_rows = load_nba_salaries(root / "nba_salaries.csv")

    guard_failures = [
        f.__dict__
        for f in empty_source_guards(
            {
                "bbref-contracts": len(salary_records),
                "nba_dead_money.csv": len(dead_rows),
                "nba_options.csv": len(option_rows),
                "nba_cap_holds.csv": len(hold_rows),
                "nba_salaries.csv": len(spotrac_rows),
            }
        )
    ]
    if guard_failures:
        return _guard_blocked(db, started_at, dry_run, guard_failures)

    source_dates = {
        name: csv_git_date(root, name)
        for name in (
            "nba_dead_money.csv",
            "nba_options.csv",
            "nba_cap_holds.csv",
            "nba_salaries.csv",
        )
    }
    warnings = staleness_warnings(source_dates, started_at)
    for w in warnings:
        logger.warning("staleness: %s", w)

    # ---- EPM API cache refresh (ruled 2026-08-25) ---------------------------
    # The nightly ingest OWNS fetching the season actuals; the export is a
    # pure cache reader that fails loud past 48h. fetch_epm_data's 24h TTL
    # makes this a no-op while the cache is fresh and a refetch once it
    # expires. A fetch failure warns loudly but never fails the DB ingest —
    # the export's own staleness gate is the enforcement point.
    try:
        fetch_epm_data(season=API_ACTUALS_SEASON)
        logger.info(
            "EPM API cache refreshed/verified (season=%s)", API_ACTUALS_SEASON
        )
    except Exception:  # noqa: BLE001 — loud, non-fatal; export gates at 48h
        logger.warning(
            "EPM API cache refresh FAILED (season=%s) — the export will "
            "refuse the cache once it ages past 48h",
            API_ACTUALS_SEASON,
            exc_info=True,
        )

    # ---- EPM vintage stamp (approved 2026-07-07; retargeted 2026-08-25) -----
    # Stamp the vintage of the file the export actually reads — the
    # CURRENT-SEASON API ACTUALS cache, not the demoted scrape cache. The
    # stat is guarded — a missing/unreadable cache reads as "unknown" and
    # must never fail the ingest.
    try:
        epm_mtime: datetime | None = datetime.fromtimestamp(
            api_cache_file(API_ACTUALS_SEASON).stat().st_mtime, tz=timezone.utc
        )
    except Exception:  # noqa: BLE001 — any stat failure = unknown vintage
        epm_mtime = None
    epm_vintage_label, epm_stale = epm_vintage(epm_mtime, started_at)
    logger.info("EPM vintage: %s", epm_vintage_label)
    if epm_stale:
        logger.warning(
            "EPM data is stale or of unknown vintage (%s) — projections behind "
            "valuation are aging; refresh with a manual EPM pull",
            epm_vintage_label,
        )

    # ---- name resolution ---------------------------------------------------
    crosswalk = load_crosswalk()
    resolver = NameResolver(crosswalk)

    dead_by_slug: dict[str, list[DeadMoneyRow]] = {}
    unresolved_dead: list[DeadMoneyRow] = []
    for d in dead_rows:
        slug = resolver.resolve(d.player_name)
        if slug is None:
            unresolved_dead.append(d)
        else:
            dead_by_slug.setdefault(slug, []).append(d)

    # ---- dead-money separation (Lillard/Beal fix) ---------------------------
    # Spotrac current-team opinions (first NON-WAIVED row per slug, display
    # code -> BBRef code) feed the tier-3 duplicate tie-break ONLY: they choose
    # between the existing BBRef rows and never supply teams or dollars of
    # their own — BBRef stays source of record (see separate_dead_money).
    spotrac_team_bbref: dict[str, str] = {}
    for r in spotrac_rows:
        if _SPOTRAC_WAIVED_RE.search(r.player_raw):
            continue
        s = resolver.resolve(r.player_raw)
        if s is None:
            continue
        spotrac_team_bbref.setdefault(s, DISPLAY_TO_BBREF.get(r.team, r.team))

    contracts = _contract_rows(salary_records, seasons)
    separation = separate_dead_money(
        contracts, dead_by_slug, DISPLAY_TO_BBREF, spotrac_teams=spotrac_team_bbref
    )

    # ---- salaries plan -------------------------------------------------------
    ng = NonGuaranteeResolver.load()
    slug_names = {c.slug: c.player_name for c in separation.kept if c.slug}
    planned_salaries: list[tuple[ContractSeasonAmounts, str, int, bool]] = []
    for c in separation.kept:
        if not c.slug:
            logger.warning("salaries: skipping un-slugged row %r (%s)", c.player_name, c.team)
            continue
        nba_id = crosswalk.nba_id_for_slug(c.slug)
        for season, amount in c.amounts.items():
            is_ng = ng.is_non_guaranteed(
                season, nba_id=nba_id, player=c.player_name, team=c.team
            )
            planned_salaries.append((c, season, amount, is_ng))

    # ---- options plan --------------------------------------------------------
    option_status_as_of = (
        source_dates["nba_options.csv"].date() if source_dates["nba_options.csv"] else None
    )
    csv_codes: dict[str, dict[str, str]] = {}
    unresolved_options: list[str] = []
    for r in option_rows:
        # WAIVED rows carry D markers for dead money; resolve like any other.
        slug = resolver.resolve(
            r.player_raw.replace(" WAIVED", "").replace(" waived", "")
        )
        if slug is None:
            unresolved_options.append(f"{r.player_raw} ({r.team})")
            continue
        merged = csv_codes.setdefault(slug, {})
        for season, code in r.codes.items():
            merged.setdefault(season, code)
    csv_seasons = {s for codes in csv_codes.values() for s in codes}

    db_players = db.fetch_players()
    db_option_state = {
        key: DbOptionState(code=code, status=status)
        for key, (code, status) in db.fetch_option_state().items()
    }
    if option_status_as_of is None:
        # Rule: status_as_of is the CSV's git commit date, never the run date.
        logger.warning("options: no git commit date for nba_options.csv; status_as_of will be null")
    option_plan = plan_option_transitions(
        csv_codes,
        db_option_state,
        csv_seasons,
        option_status_as_of,
    )

    # ---- cap holds plan --------------------------------------------------------
    team_quality = classify_cap_hold_teams(hold_rows)

    # ---- guards against previous state -----------------------------------------
    previous_raw = db.fetch_table_stats()
    previous = {t: TableStats(rows=n, dollars=d) for t, (n, d) in previous_raw.items()}
    planned_stats = {
        "v3_contract_salaries": TableStats(
            rows=len(planned_salaries),
            dollars=sum(a for _, _, a, _ in planned_salaries),
        ),
        "v3_cap_holds": TableStats(
            rows=len(hold_rows), dollars=sum(h.amount for h in hold_rows)
        ),
        "v3_dead_money": TableStats(
            rows=sum(len(d.amounts) for d in dead_rows)
        ),
        "v3_cap_thresholds": TableStats(rows=len(CAP_THRESHOLDS_BY_SEASON)),
    }
    collapse = evaluate_guards(planned_stats, previous)
    # --accept-baseline downgrades ONLY previous-state comparisons to
    # report-only (the Phase 1 seed's placeholder cap holds make the first
    # real ingest look like a collapse). empty_source / missing-root guards
    # above are untouched by the flag.
    blocking, baseline_overrides = apply_baseline_acceptance(collapse, accept_baseline)
    if blocking:
        return _guard_blocked(db, started_at, dry_run, [f.__dict__ for f in blocking])
    for f in baseline_overrides:
        logger.warning(
            "baseline override (NOT blocking): %s on %s: %s", f.guard, f.subject, f.detail
        )

    # ---- report plan ------------------------------------------------------------
    logger.info(
        "plan: %d salary rows, %d option upserts (%d transition flags), "
        "%d hold rows, %d dead-money rows, %d thresholds; %d dup drops, %d dup flags",
        len(planned_salaries),
        len(option_plan.upserts),
        len(option_plan.flags),
        len(hold_rows),
        sum(len(d.amounts) for d in dead_rows),
        len(CAP_THRESHOLDS_BY_SEASON),
        len(separation.dropped),
        len(separation.flags),
    )
    for slug, team, why in separation.dropped:
        logger.info("dead-money separation: dropped %s@%s (%s)", slug, team, why)

    if dry_run:
        return RunResult(
            status="dry_run",
            summary={
                "planned": {t: s.rows for t, s in planned_stats.items()},
                "option_transition_flags": len(option_plan.flags),
                "duplicate_flags": len(separation.flags),
                "unresolved_dead_money": [d.player_raw for d in unresolved_dead],
                "unresolved_options": unresolved_options,
                "warnings": warnings,
                "baseline_overrides": [f.__dict__ for f in baseline_overrides],
                "baseline_override_reason": accept_baseline,
            },
        )

    # ---- write: players + salaries + options + holds + dead money + thresholds --
    rows_written = 0
    scraped_at = started_at
    slug_to_id: dict[str, str] = {slug: rec.id for slug, rec in db_players.items()}

    def player_id_for(slug: str, name: str, team_bbref: str | None) -> str:
        if slug in slug_to_id:
            return slug_to_id[slug]
        pid = db.upsert_player(
            bbref_slug=slug,
            name=name,
            nba_id=crosswalk.nba_id_for_slug(slug),
            team_bbref=team_bbref,
            team_display=BBREF_TO_DISPLAY.get(team_bbref) if team_bbref else None,
        )
        slug_to_id[slug] = pid
        return pid

    # Players from kept contract rows (authoritative team assignment).
    for c in separation.kept:
        if not c.slug:
            continue
        pid = db.upsert_player(
            bbref_slug=c.slug,
            name=c.player_name,
            nba_id=crosswalk.nba_id_for_slug(c.slug),
            team_bbref=c.team or None,
            team_display=BBREF_TO_DISPLAY.get(c.team) if c.team else None,
        )
        slug_to_id[c.slug] = pid
        rows_written += 1

    ingested_salaries: dict[str, dict[str, int]] = {}
    for c, season, amount, is_ng in planned_salaries:
        db.upsert_salary(
            player_id=slug_to_id[c.slug],
            season=season,
            amount=amount,
            guaranteed_amount=0 if is_ng else None,
            is_fully_ng=is_ng,
            is_rookie_scale=c.is_rookie_scale,
            has_player_option=c.has_player_option,
            has_team_option=c.has_team_option,
            source=SRC_SALARIES,
            scraped_at=scraped_at,
        )
        ingested_salaries.setdefault(c.slug, {})[season] = amount
        rows_written += 1

    for u in option_plan.upserts:
        name = slug_names.get(u.slug) or db_players.get(u.slug, PlayerRec("", u.slug, None, None)).name or u.slug
        db.upsert_option(
            player_id=player_id_for(u.slug, name, None),
            season=u.season,
            code=u.code,
            status=u.status,
            status_as_of=u.status_as_of,
            source=SRC_OPTIONS,
        )
        rows_written += 1

    for h in hold_rows:
        db.upsert_cap_hold(
            team=h.team,
            season=h.season,
            player_name=h.player_name or None,
            amount=h.amount,
            quality=team_quality.get(h.team, "sentinel"),
            source=SRC_CAP_HOLDS,
            scraped_at=scraped_at,
        )
        rows_written += 1

    dead_slug_amounts: dict[str, dict[str, int]] = {}
    for d in dead_rows:
        slug = next(
            (s for s, rows_ in dead_by_slug.items() if d in rows_),
            None,
        )
        pid = None
        if slug is not None:
            merged = dead_slug_amounts.setdefault(slug, {})
            for season, amount in d.amounts.items():
                merged.setdefault(season, amount)
            name = slug_names.get(slug) or d.player_name
            pid = player_id_for(slug, name, None)
        for season, amount in d.amounts.items():
            db.upsert_dead_money(
                team=d.team,
                season=season,
                player_name=d.player_raw,
                player_id=pid,
                amount=amount,
                source=SRC_DEAD_MONEY,
                scraped_at=scraped_at,
            )
            rows_written += 1

    for season, levels in CAP_THRESHOLDS_BY_SEASON.items():
        db.upsert_cap_threshold(
            season=season,
            salary_cap=int(levels["salary_cap"]),
            minimum_team_salary=int(levels["minimum_team_salary"]),
            luxury_tax=int(levels["luxury_tax"]),
            first_apron=int(levels["first_apron"]),
            second_apron=int(levels["second_apron"]),
            certified=bool(levels["certified"]),
            source=str(levels["source"]),
        )
        rows_written += 1

    db.conn.commit()

    # ---- override pass (after ingest, before verification) ----------------------
    ingested_values: dict[tuple[str, str, str], str] = {}
    for c, season, _amount, is_ng in planned_salaries:
        key = f"{c.slug}|{season}"
        ingested_values[("v3_contract_salaries", key, "is_fully_ng")] = (
            "true" if is_ng else "false"
        )
    for u in option_plan.upserts:
        key = f"{u.slug}|{u.season}"
        ingested_values[("v3_contract_options", key, "code")] = u.code
        ingested_values[("v3_contract_options", key, "status")] = u.status

    active = [
        ActiveOverride(o.id, o.table_name, o.row_key, o.field, o.value)
        for o in db.fetch_active_overrides()
    ]
    retire, _keep = plan_override_retirements(active, ingested_values)
    for o in retire:
        db.retire_override(o.id)
        logger.info(
            "override retired: %s %s.%s = %r (source caught up)",
            o.table_name,
            o.row_key,
            o.field,
            o.value,
        )
    db.conn.commit()

    # ---- layer-1 verification -----------------------------------------------------
    previous_mismatches = db.fetch_previous_mismatch_keys()

    bbref_amounts = {c.slug: c.amounts for c in separation.kept if c.slug}
    hold_slugs = {
        s
        for s in (resolver.resolve(h.player_name) for h in hold_rows if h.player_name)
        if s is not None
    }
    verify_rows, summary = verify_salaries(
        ingested=ingested_salaries,
        bbref=bbref_amounts,
        spotrac_rows=spotrac_rows,
        resolver=resolver,
        player_names={**{s: n for s, n in slug_names.items()}, **{s: (r.name or s) for s, r in db_players.items()}},
        cap_hold_slugs=hold_slugs,
        dead_amounts=dead_slug_amounts,
        # Mutual-coverage restriction: Spotrac's file has no 2025-26 column
        # and our ingest window stops at salary_season_keys() (incl. 2031-32
        # since 2026-07-16 — kills the ours_missing=["2031-32"] skip) —
        # whole-column gaps are skipped and recorded in the summary, never
        # per-row mismatches.
        spotrac_coverage=nba_salaries_season_coverage(root / "nba_salaries.csv"),
        our_coverage=set(seasons),
        # Team attribution cross-check (report-only): our side is the kept
        # post-separation rows — exactly what was just upserted into
        # v3_players.team_bbref. BBRef stays the team source of record.
        our_teams={c.slug: c.team for c in separation.kept if c.slug and c.team},
    )

    # The run row must exist BEFORE verification rows (v3_verifications.run_id
    # references it); the final summary lands on it afterwards via UPDATE.
    run_id = db.insert_run(
        started_at=started_at,
        status="success",
        rows_written=rows_written,
        rows_changed=rows_written,
        guard_failures=None,
        error=None,
        summary=None,
    )

    for f in separation.flags:
        db.insert_verification(
            player_id=slug_to_id.get(f.slug),
            field=FIELD_DUPLICATE_TEAM_ROWS,
            our_value=(
                f"kept {f.kept_team or 'none'}"
                + (" (spotrac tie-break)" if f.resolved_by_spotrac else "")
            ),
            bbref_value=f"also {', '.join(f.other_teams)}",
            spotrac_value=spotrac_team_bbref.get(f.slug),
            verdict="mismatch",
            run_id=run_id,
        )
        summary.mismatch += 1
        summary.mismatch_keys.add((f.slug, FIELD_DUPLICATE_TEAM_ROWS))

    for fl in option_plan.flags:
        # Season rides on the field (option_transition:<season>) so the DB row
        # is self-describing for the Hermes adjudicator AND matches the
        # mismatch_keys form used for the night-over-night delta.
        db.insert_verification(
            player_id=slug_to_id.get(fl.slug),
            field=f"option_transition:{fl.season}",
            our_value=f"{fl.db_code}:{fl.db_status}",
            bbref_value=None,
            spotrac_value=fl.csv_state,
            verdict="mismatch",
            run_id=run_id,
        )
        summary.mismatch += 1
        summary.mismatch_keys.add((fl.slug, f"option_transition:{fl.season}"))

    for row in verify_rows:
        db.insert_verification(
            player_id=slug_to_id.get(row.slug) if row.slug else None,
            field=row.field,
            our_value=row.our_value,
            bbref_value=row.bbref_value,
            spotrac_value=row.spotrac_value,
            verdict=row.verdict,
            run_id=run_id,
        )

    new_mismatches = sorted(
        f"{key}:{fld}" for key, fld in summary.mismatch_keys - previous_mismatches
    )
    final_summary = {
        "verdicts": summary.as_dict(),
        "new_mismatches": new_mismatches,
        "new_mismatch_count": len(new_mismatches),
        "override_retirements": [f"{o.table_name} {o.row_key}.{o.field}" for o in retire],
        "duplicate_flags": len(separation.flags),
        "option_transition_flags": len(option_plan.flags),
        "unresolved_dead_money": [d.player_raw for d in unresolved_dead],
        "unresolved_options": unresolved_options,
        "staleness_warnings": warnings,
        "epm_vintage": epm_vintage_label,
        "skipped_seasons": summary.skipped_seasons,
    }
    if accept_baseline is not None:
        final_summary["baseline_overrides"] = [f.__dict__ for f in baseline_overrides]
        final_summary["baseline_override_reason"] = accept_baseline
    _attach_summary(db, run_id, final_summary)
    db.conn.commit()

    _print_summary(final_summary, rows_written)
    return RunResult(
        status="success",
        rows_written=rows_written,
        summary=final_summary,
    )


def _attach_summary(db: IngestDb, run_id: str, summary: dict) -> None:
    if not db._has_summary_column():  # noqa: SLF001 — same module family
        logger.info("v3_ingest_runs.summary column absent (migration pending); summary printed only")
        return
    with db.conn.cursor() as cur:
        cur.execute(
            "update public.v3_ingest_runs set summary = %s where id = %s",
            (json.dumps(summary), run_id),
        )


def _guard_blocked(
    db: IngestDb, started_at: datetime, dry_run: bool, failures: list[dict]
) -> RunResult:
    logger.error("guard_blocked: %s", failures)
    if not dry_run:
        db.conn.rollback()
        db.insert_run(
            started_at=started_at,
            status="guard_blocked",
            rows_written=0,
            rows_changed=0,
            guard_failures=failures,
            error=None,
            summary=None,
        )
        db.conn.commit()
    return RunResult(status="guard_blocked", guard_failures=failures)


def _print_summary(summary: dict, rows_written: int) -> None:
    v = summary["verdicts"]
    print(
        f"ingest ok: {rows_written} rows upserted | verdicts: "
        f"{v['match']} match / {v['mismatch']} mismatch / {v['unverifiable']} unverifiable | "
        f"{summary['new_mismatch_count']} NEW mismatch(es) vs previous run | "
        f"{len(summary['override_retirements'])} override(s) retired"
    )
    for m in summary["new_mismatches"][:25]:
        print(f"  new mismatch: {m}")
    if summary["new_mismatch_count"] > 25:
        print(f"  ... and {summary['new_mismatch_count'] - 25} more")
    for r in summary["override_retirements"]:
        print(f"  retired override: {r}")
    for w in summary["staleness_warnings"]:
        print(f"  ⚠ {w}")
    if summary.get("epm_vintage"):
        print(f"  EPM vintage: {summary['epm_vintage']}")
    for b in summary.get("baseline_overrides", []):
        print(
            f"  !! baseline override accepted: {b['guard']} on {b['subject']} {b['detail']} "
            f"(reason: {summary.get('baseline_override_reason')})"
        )
