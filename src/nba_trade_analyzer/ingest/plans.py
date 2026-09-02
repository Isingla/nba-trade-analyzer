"""Pure ingest decision logic (Phase 2A) — no network, no DB, fully unit-tested.

Four decisions live here, each encoding an adjudicated rule from the databallr
Phase 0 trace:

  * :func:`separate_dead_money` — the Lillard/Beal fix (Path 1d): a BBRef
    contract row duplicated across teams is dropped ONLY when a dead-money row
    explains it to the dollar; anything less certain is flagged, never guessed.
    Extended to the SAME-TEAM blend (the Isaac waive-and-re-sign shape): a
    single row whose cell is the exact sum of the team's own dead charge and
    a plausible active remainder is decomposed in place.
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

import logging
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone

from nba_trade_analyzer.data.dead_money import DeadMoneyRow

logger = logging.getLogger(__name__)

# Codes that represent an actual option (a decision someone will make), as
# opposed to informational markers (NG/D/UFA/RFA drift with roster churn).
OPTION_DECISION_CODES = frozenset({"P", "T"})

# Row/dollar collapse threshold: a fresh ingest producing less than this
# fraction of the previous state is refused (mirrors sync-cap-data's
# MIN_RETAIN_RATIO = 0.8).
MIN_RETAIN_RATIO = 0.8

STALENESS_MAX_AGE_DAYS = 7

# Floor for a decomposed ACTIVE season salary. The 2026-27 league minimum is
# ~$1.27M; a blend-subtraction residual below this is more likely a wrong
# pairing of charge and cell than a real contract year — refuse it loudly
# (keep the original value + warning) instead of writing implausible money.
MIN_PLAUSIBLE_ACTIVE_SALARY = 1_000_000

# Per-season dollar slack for the PURE-DEAD single-row classifier ONLY (every
# other equality check stays exact). Absorbs CSV rounding of an evenly
# stretched charge: $804,095 over 3 seasons is 268,032 + 268,031 + 268,031 in
# the dead-money CSV while BBRef prints 268,032 each season (Louzada/POR).
PURE_DEAD_ROUNDING_TOLERANCE = 1


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
    # Per-team RAW amounts from BBRef's /contracts/{TEAM}.html page, attached
    # by the ingest for multi-stint slugs only (invariant-verified: the
    # per-team sum equals the blended league cell). None = single-stint or
    # invariant failure — the pre-raw machinery applies unchanged.
    raw_amounts: dict[str, int] | None = None
    # Team-page option flags, attached WITH raw_amounts under the same sum-
    # invariant gate (None = no team-page opinion; league flags stand). The
    # league table stamps the blended cell's option class on every stint row
    # (KCP's dead-MEM player option surfaced on his kept PHI row), so when
    # raw dollars are authoritative the flags are too.
    raw_player_option: bool | None = None
    raw_team_option: bool | None = None
    is_rookie_scale: bool = False
    has_player_option: bool = False
    has_team_option: bool = False


@dataclass(frozen=True)
class DuplicateFlag:
    """A cross-team duplicate that dead money could NOT explain — human review.

    Emitted even when the Spotrac tie-break resolved the kept side
    (``resolved_by_spotrac=True``): resolution changes WHICH row is kept,
    never whether the duplicate is surfaced for verification.
    """

    slug: str
    player_name: str
    kept_team: str
    other_teams: tuple[str, ...]
    resolved_by_spotrac: bool = False


@dataclass
class DeadMoneySeparation:
    kept: list[ContractSeasonAmounts] = field(default_factory=list)
    dropped: list[tuple[str, str, str]] = field(
        default_factory=list
    )  # (slug, team, why)
    flags: list[DuplicateFlag] = field(default_factory=list)


def _dead_row_explains(
    contract: ContractSeasonAmounts, dead: DeadMoneyRow, tolerance: int = 0
) -> bool:
    """True iff the dead-money schedule matches the contract row to the dollar.

    The contract row's schedule must EQUAL the dead-money schedule — same
    seasons, same dollars. A subset match is not enough (a one-season dead
    row must never erase a five-season mixed row). Rows that fail this
    whole-schedule test get a second chance at the SEASON level
    (:func:`_season_level_split`) before falling back to the flag bucket.
    Exactness at whichever grain is the point: these branches silently drop
    salary data, so they must never fire on a coincidence.

    ``tolerance`` (default 0 = exact, the standard everywhere else) allows a
    per-season dollar slack. Only the pure-dead single-row classifier passes
    a non-zero value, to absorb CSV rounding of an evenly-stretched charge
    (e.g. $804,095 / 3 = 268,031.67: the dead-money CSV prints
    268,032 + 268,031 + 268,031 while BBRef prints 268,032 every season).
    Season KEYS must still match exactly regardless of tolerance.
    """
    if not dead.amounts:
        return False
    if tolerance <= 0:
        return contract.amounts == dead.amounts
    if contract.amounts.keys() != dead.amounts.keys():
        return False
    return all(
        abs(contract.amounts[season] - dead.amounts[season]) <= tolerance
        for season in contract.amounts
    )


def _merged_dead_seasons(dead_rows: list[DeadMoneyRow]) -> dict[str, int] | None:
    """Merge one team's dead-money rows into season -> dollars; None on conflict."""
    merged: dict[str, int] = {}
    for dead in dead_rows:
        for season, amount in dead.amounts.items():
            if season in merged and merged[season] != amount:
                return None  # two dead rows disagree — ambiguous, don't guess.
            merged[season] = amount
    return merged


def _dead_charge_totals(
    dead_rows: list[DeadMoneyRow],
    exclude_team: str,
    display_to_bbref: dict[str, str],
) -> dict[str, int]:
    """Sum this player's dead charges per season across teams OTHER than
    ``exclude_team`` (BBRef code). Multiple streams for one season add up —
    a blend containing two former teams' charges needs the sum subtracted."""
    totals: dict[str, int] = {}
    for dead in dead_rows:
        team = display_to_bbref.get(dead.team, dead.team)
        if team == exclude_team:
            continue
        for season, amount in dead.amounts.items():
            if amount > 0:
                totals[season] = totals.get(season, 0) + amount
    return totals


def _subtract_dead_blends(
    kept: ContractSeasonAmounts,
    dead_rows: list[DeadMoneyRow],
    display_to_bbref: dict[str, str],
) -> ContractSeasonAmounts:
    """Decompose blended cells left on a separated survivor.

    contracts/players.html publishes the SUM of active salary and the other
    team's dead charge in overlap seasons (lillada01 2026-27 = 35,915,403 =
    13,398,800 POR active + 22,516,603 MIL dead), so even after row/season
    separation the kept "active" row can still carry blends. Where the
    dead-money source has a charge for the same player+season on a DIFFERENT
    team and the kept amount STRICTLY exceeds it, the difference is the
    active salary — write that, and log the decomposition at INFO so nightly
    logs show it working.

    Guardrails — never subtract blindly:
      - callers gate this to the kept survivor of a duplicate group whose
        other row dead money explained (whole-row or season-level). A
        single-row player is never touched by CROSS-team subtraction: a
        normal active row coexisting with old stretch charges is NOT a
        blend, and ``amount > charge`` alone cannot tell the two apart —
        only the duplicate fingerprint can. (Single rows blended with the
        SAME team's charge are a different shape with its own fingerprint —
        see :func:`_subtract_same_team_blend`);
      - the shared arithmetic guardrails of :func:`_subtract_charge_totals`.
    """
    if not dead_rows:
        return kept
    totals = _dead_charge_totals(dead_rows, kept.team, display_to_bbref)
    return _subtract_charge_totals(kept, totals)


def _subtract_same_team_blend(
    row: ContractSeasonAmounts,
    dead_rows: list[DeadMoneyRow],
    display_to_bbref: dict[str, str],
) -> ContractSeasonAmounts:
    """Decompose a single row blended with its OWN team's dead charge.

    The same disease as the dual-team blends, SAME-TEAM variant (isaacjo01:
    ORL waived him 6/27 with $8,000,000 guaranteed, he re-signed ORL on a
    vet minimum during the moratorium; BBRef's ORL cell prints the
    undecomposed total 10,449,421 = 8,000,000 dead + 2,449,421 active).
    Because both the charge and the new contract sit on ONE team, BBRef
    emits ONE row and the duplicate-shaped machinery never sees it — the
    fingerprint here is the dead-money row on the row's own team plus exact
    arithmetic: cell - charge = a plausible active remainder.

    Only the row's OWN team's charges are eligible. A charge on a DIFFERENT
    team next to a single row is the old-stretch coexistence shape (active
    row on the new team, stale charge on the former team, nothing blended)
    and must never be subtracted — when BBRef blends across two teams it
    prints two rows, which is the duplicate machinery's territory.
    Arithmetic that doesn't produce a plausible remainder is refused by the
    shared guardrails in :func:`_subtract_charge_totals` (original value
    kept, WARNING naming the player).
    """
    if not dead_rows:
        return row
    totals: dict[str, int] = {}
    for dead in dead_rows:
        if display_to_bbref.get(dead.team, dead.team) != row.team:
            continue
        for season, amount in dead.amounts.items():
            if amount > 0:
                totals[season] = totals.get(season, 0) + amount
    return _subtract_charge_totals(row, totals)


def _own_team_charge_overlap(
    row: ContractSeasonAmounts,
    dead_rows: list[DeadMoneyRow],
    display_to_bbref: dict[str, str],
) -> dict[str, int] | None:
    """Seasons where the row's OWN team holds a positive dead charge that the
    row also prints. None when there is no overlap OR when two same-team dead
    rows disagree on a season's amount (ambiguous — never guess, matching
    :func:`_merged_dead_seasons`)."""
    own = [
        dead
        for dead in dead_rows
        if display_to_bbref.get(dead.team, dead.team) == row.team
    ]
    merged = _merged_dead_seasons(own) if own else None
    if merged is None:
        return None
    overlap = {
        season: amount
        for season, amount in merged.items()
        if amount > 0 and season in row.amounts
    }
    return overlap or None


def _log_fossil_drop(
    row: ContractSeasonAmounts,
    overlap: dict[str, int],
    result: DeadMoneySeparation,
) -> None:
    """INFO-log and record one fossil drop (the dead-money separation's own
    log register): player, team, season, amount per overlapping season."""
    for season in sorted(overlap):
        logger.info(
            "dead-money separation: FOSSIL salary row dropped — %s (%s) "
            "%s %s: dropped amount %d (league row prints %s; the dead-money "
            "row is this team's truth and Spotrac places the player "
            "elsewhere)",
            row.player_name,
            row.slug,
            row.team,
            season,
            overlap[season],
            row.amounts.get(season, "—"),
        )
    result.dropped.append(
        (
            row.slug,
            row.team,
            "fossil pre-waive row ("
            + ", ".join(f"{s}: {overlap[s]}" for s in sorted(overlap))
            + f"; dead charge on {row.team})",
        )
    )


def _resolve_kept_amounts(
    kept: ContractSeasonAmounts,
    arithmetic: ContractSeasonAmounts,
) -> ContractSeasonAmounts:
    """PRECEDENCE, stated: the team page's RAW figure is authoritative for a
    multi-stint kept row; the split/blend arithmetic result is a CROSS-CHECK
    that logs a disagreement when it differs — it never overrides.

    Why: subtracting a Spotrac dead-money figure from a BBRef blended cell
    is structurally unsound — right only when the blend is exactly
    dead + active AND the two sources agree to the dollar (Klay's give-back
    buyout broke the first assumption for $15.4M; Lillard's $1,205,550
    cross-source disagreement broke the second). The team page is BBRef
    answering the per-team question directly.
    """
    if kept.raw_amounts is None:
        return arithmetic
    # Flags travel WITH the dollars: the team page's own cells decide the
    # kept row's option flags whenever its raw is authoritative (table cells
    # only — Player Notes prose is never parsed). None = no opinion.
    flag_updates: dict[str, bool] = {}
    if kept.raw_player_option is not None:
        flag_updates["has_player_option"] = kept.raw_player_option
    if kept.raw_team_option is not None:
        flag_updates["has_team_option"] = kept.raw_team_option
    if arithmetic.amounts != kept.raw_amounts:
        # INFO, not WARNING (2026-09-01): when raw is present it always wins,
        # so a divergence here is a RESOLVED question — the persistent
        # Klay/Lillard shapes fired this nightly at WARNING for a settled
        # outcome. WARNING is reserved for unresolved states (the sum
        # invariant upstream, which withholds raw entirely).
        logger.info(
            "raw-vs-blend DISAGREEMENT for %s (%s) on %s: blend arithmetic "
            "%s vs team-page raw %s — raw is authoritative; arithmetic kept "
            "as cross-check only",
            kept.player_name,
            kept.slug,
            kept.team,
            arithmetic.amounts,
            kept.raw_amounts,
        )
    return replace(arithmetic, amounts=dict(kept.raw_amounts), **flag_updates)


def _subtract_charge_totals(
    kept: ContractSeasonAmounts,
    totals: dict[str, int],
) -> ContractSeasonAmounts:
    """Shared subtraction core: write ``cell - charge`` as the active salary.

    Arithmetic guardrails (both blend shapes):
      - ``amount == charge`` is the pure-dead season and belongs to the
        exact-match classifiers, never here;
      - ``amount < charge`` cannot be a blend containing the charge — left
        alone (this is also what a source-side fix looks like: BBRef starts
        publishing the true active value, and subtraction self-disarms);
      - a residual below ``MIN_PLAUSIBLE_ACTIVE_SALARY`` is refused loudly:
        original value kept, WARNING naming the player.
    """
    if not totals:
        return kept

    amounts = dict(kept.amounts)
    changed = False
    for season, amount in sorted(kept.amounts.items()):
        charge = totals.get(season)
        if charge is None or amount <= charge:
            continue
        active = amount - charge
        if active < MIN_PLAUSIBLE_ACTIVE_SALARY:
            logger.warning(
                "dead-money blend: REFUSING %s (%s) %s: %d - %d = %d is below "
                "the plausible active minimum — keeping the original value",
                kept.player_name,
                kept.slug,
                season,
                amount,
                charge,
                active,
            )
            continue
        logger.info(
            "dead-money blend: %s (%s) %s: %d (blended) - %d (dead) = %d (active)",
            kept.player_name,
            kept.slug,
            season,
            amount,
            charge,
            active,
        )
        amounts[season] = active
        changed = True

    if not changed:
        return kept
    return replace(kept, amounts=amounts)


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
    the remaining seasons are attributed to the other (non-dead-money) team.

    Those remaining cells are NOT the active salary yet: BBRef publishes the
    SUM of both teams' figures in overlap seasons (35,915,403 = 13,398,800
    POR active + 22,516,603 MIL dead — verified against BBRef's per-team
    pages 2026-07-09). The caller decomposes the kept row's blends via
    :func:`_subtract_dead_blends`; this function only decides which seasons
    and which team survive.

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
    spotrac_teams: dict[str, str] | None = None,
) -> DeadMoneySeparation:
    """Split phantom dead-money duplicates out of the BBRef contract rows.

    ``dead_by_slug`` maps resolved bbref slugs to their dead-money rows (the
    caller resolves names via ``ingest.names``). ``display_to_bbref`` converts
    the dead-money CSV's display team codes (PHX) to BBRef codes (PHO) so the
    team comparison is apples-to-apples.

    Rules (Phase 2A spec + the season-level extension):
      - a slug with one row is FIRST checked for the pure-dead shape (every
        season the row prints equals the own-team dead charge within
        ``PURE_DEAD_ROUNDING_TOLERANCE``; the charge may extend BEYOND the
        row's printed seasons but never cover fewer — the
        waived-and-signed-nowhere class: Micic/Rubio/McGee/Louzada/Little,
        gap fix 2026-07-18): with Spotrac corroboration that the player is
        gone it is DROPPED (charge lives only in dead money — team totals
        are unchanged because consumers add the dead-money bucket back);
        equality without corroboration keeps the row and flags it; then
      - a single row is checked for the SAME-TEAM blend (a fresh
        dead-money row on the row's own team whose subtraction leaves a
        plausible active remainder — the isaacjo01 waive-and-re-sign shape,
        see :func:`_subtract_same_team_blend`) and otherwise passes through
        untouched;
      - for a duplicated slug, a row is dropped iff a dead-money row on that
        SAME team matches it to the dollar (see :func:`_dead_row_explains`);
      - surviving 2-team identical-schedule pairs get the season-level split
        (see :func:`_season_level_split`) — the mixed-schedule Lillard/Beal
        pattern resolves to (active contract on the other team, dead-money
        team row dropped) with no flag;
      - the kept survivor of a dead-money-explained group (whole-row drop or
        season-level split) then has its BLENDED overlap cells decomposed:
        where BBRef's cell strictly exceeds the other team's dead charge for
        that season, the charge is subtracted to recover the active salary
        (see :func:`_subtract_dead_blends` for the guardrails). Tier-3
        survivors are NOT decomposed — there dead money explained nothing,
        so the blend fingerprint is absent;
      - if after dropping explained phantoms more than one row remains
        (tier 3, the davisjd01 class: BBRef two-stint duplicates with NO
        dead-money signal on either side), ``spotrac_teams`` — the caller's
        slug -> BBRef-style team map from nba_salaries.csv — is used as a
        TIE-BREAK between the existing BBRef rows: if Spotrac's team matches
        EXACTLY ONE surviving row, that row is kept (file order was the old,
        arbitrary rule and landed the stale stint on the player's main row).
        Spotrac only chooses among BBRef rows — it never supplies a team that
        isn't on one of them and never supplies any dollar value; BBRef stays
        source of record. No Spotrac row / zero matches / multiple matches ->
        fall back to keeping the FIRST row (BBRef order). Never guess.
        Either way the duplicate is flagged for human review — resolution
        changes which row is kept, not its visibility.
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
        dead_rows = dead_by_slug.get(slug, [])
        if len(rows) == 1:
            row = rows[0]
            # PURE-DEAD single row (gap fix 2026-07-18): a player waived and
            # signed NOWHERE gets ONE BBRef row — his stretch schedule on the
            # waiving team — so the duplicate machinery never sees it, and the
            # same-team subtractor's `amount <= charge` gate deliberately
            # skips equality. Classify it here, BEFORE any subtraction
            # attempt: every season the ROW prints must equal the row's OWN
            # team's dead charge (the _dead_row_explains standard, reused via
            # a schedule restricted to the row's seasons — not forked).
            #
            # Two data realities force the "restricted" part and the
            # tolerance, both verified against live CSVs 2026-07-18:
            #   * SUPERSET charges (Little/PHO): a 5-season stretch charge
            #     where BBRef truncates its table at 3 seasons. Every visible
            #     dollar is still fully explained, so the row drops. The
            #     dangerous direction — charge covering FEWER seasons than
            #     the row — still fails (a missing season breaks coverage).
            #   * ±$1 CSV rounding (Louzada/POR): see
            #     PURE_DEAD_ROUNDING_TOLERANCE.
            #
            # CORROBORATION GATE (never guess): equality alone cannot
            # distinguish "waived, gone" from a re-signed minimum that
            # coincidentally equals the charge. Drop ONLY when Spotrac
            # corroborates the player is gone from this team — i.e. the
            # caller's slug -> current-team map (built from NON-WAIVED
            # Spotrac rows only) does not place him on this team. No
            # Spotrac data at all (spotrac_teams is None) counts as no
            # corroboration: keep the row and flag it for human review.
            own_dead = [
                dead
                for dead in dead_rows
                if display_to_bbref.get(dead.team, dead.team) == row.team
            ]
            merged_own = _merged_dead_seasons(own_dead) if own_dead else None
            matching_dead = None
            if (
                merged_own
                and row.amounts
                and all(season in merged_own for season in row.amounts)
            ):
                restricted = replace(
                    own_dead[0],
                    amounts={season: merged_own[season] for season in row.amounts},
                )
                if _dead_row_explains(
                    row, restricted, tolerance=PURE_DEAD_ROUNDING_TOLERANCE
                ):
                    matching_dead = restricted
            if matching_dead is not None:
                spotrac_says_active_here = (
                    spotrac_teams is not None and spotrac_teams.get(slug) == row.team
                )
                corroborated = (
                    spotrac_teams is not None and not spotrac_says_active_here
                )
                if corroborated:
                    result.dropped.append(
                        (
                            slug,
                            row.team,
                            f"pure-dead single row ({matching_dead.player_raw})",
                        )
                    )
                    continue
                result.flags.append(
                    DuplicateFlag(
                        slug=slug,
                        player_name=row.player_name,
                        kept_team=row.team,
                        other_teams=(),
                    )
                )
                result.kept.append(row)
                continue
            # STRETCHED PURE-DEAD single row (Fix B, ruled 2026-08-31): a
            # stretch spreads the charge across more seasons, so no season
            # EQUALS the row's cell and the exact classifier above misses
            # (Whitmore: CLE row 5,458,310 vs a multi-season stretched
            # schedule). The signal is categorical, not arithmetic: the
            # player appears in the dead-money frame for this team AND is
            # absent from Spotrac's ACTIVE salaries (the spotrac_teams map
            # is built from non-waived rows only — absence there while
            # present as dead money means no active contract anywhere).
            # Active = 0: drop the row, log it. Louzada/Micic (exact
            # matches) resolved above with their reasons intact; Isaac
            # (dead + Spotrac-ACTIVE on the same team) has a spotrac entry
            # and falls through to the same-team blend, untouched.
            # Arithmetic evidence required (review finding: an UNRELATED
            # own-team charge on non-overlapping seasons must not delete an
            # active contract, and conflicting same-team charges are
            # ambiguous): the charge must OVERLAP the row's printed seasons
            # — _own_team_charge_overlap also refuses on conflicts.
            stretch_overlap = _own_team_charge_overlap(row, dead_rows, display_to_bbref)
            if (
                stretch_overlap is not None
                and spotrac_teams is not None
                and slug not in spotrac_teams
            ):
                logger.info(
                    "dead-money separation: STRETCHED PURE-DEAD single row "
                    "dropped — %s (%s) %s: row %s beside a dead-money "
                    "schedule, player absent from Spotrac active salaries; "
                    "active = 0",
                    row.player_name,
                    row.slug,
                    row.team,
                    row.amounts,
                )
                result.dropped.append(
                    (
                        slug,
                        row.team,
                        "stretched pure-dead single row (absent from active "
                        "salaries; dead-money schedule is the team's truth)",
                    )
                )
                # Review visibility (finding): every sibling path that
                # removes a player's LAST row flags; this one must too.
                result.flags.append(
                    DuplicateFlag(
                        slug=slug,
                        player_name=row.player_name,
                        kept_team="",
                        other_teams=(row.team,),
                    )
                )
                continue

            # FOSSIL single row (2026-08-31, verified on BBRef by hand): a
            # NON-exact own-team charge overlap on a player Spotrac
            # AFFIRMATIVELY places at a different team is the 08-25 Klay
            # shape — BBRef still shows only the waiving team's table (the
            # FULL pre-waive salary) and the new team's table hasn't landed.
            # The old fallthrough fed this to the same-team blend, which
            # minted a phantom "active" (17,460,317 − 7,660,317 = a
            # 9,800,000 that never existed): that subtractor's fingerprint
            # is waive-and-RE-SIGN (Isaac — Spotrac ON the team), not
            # waived-and-left. Ordering is load-bearing: the pure-dead
            # exact classifier above keeps its reason string and no-flag
            # semantics; Spotrac ABSENCE (None/no row) stays with the
            # cautious machinery (the Louzada adjudication) — only
            # affirmative elsewhere-placement drops. Flagged for review:
            # a player whose every salary row vanished should be seen.
            spotrac_team = None if spotrac_teams is None else spotrac_teams.get(slug)
            overlap = _own_team_charge_overlap(row, dead_rows, display_to_bbref)
            if (
                overlap is not None
                and spotrac_team is not None
                and spotrac_team != row.team
            ):
                _log_fossil_drop(row, overlap, result)
                result.flags.append(
                    DuplicateFlag(
                        slug=slug,
                        player_name=row.player_name,
                        kept_team="",
                        other_teams=(row.team,),
                    )
                )
                continue
            result.kept.append(
                _subtract_same_team_blend(row, dead_rows, display_to_bbref)
            )
            continue

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
                # The split REBUILDS the kept row (raw_amounts lost) — rebind
                # from the original survivor so raw authority and its
                # cross-check reach the founding Lillard/Beal path too
                # (review finding: without this, raw never applied here).
                original = next((r for r in survivors if r.team == kept.team), None)
                if original is not None and original.raw_amounts is not None:
                    kept = replace(
                        kept,
                        raw_amounts=dict(original.raw_amounts),
                        raw_player_option=original.raw_player_option,
                        raw_team_option=original.raw_team_option,
                    )
                arithmetic = _subtract_dead_blends(kept, dead_rows, display_to_bbref)
                result.kept.append(_resolve_kept_amounts(kept, arithmetic))
                result.dropped.append(
                    (
                        slug,
                        dropped_row.team,
                        "season-level dead-money split "
                        f"(dead: {', '.join(sorted(set(dropped_row.amounts) - set(kept.amounts)))})",
                    )
                )
                continue

        # FOSSIL rows (2026-08-31, verified on BBRef by hand): when a waived
        # player signs elsewhere, BBRef keeps the old team's PRE-WAIVE table
        # beside the new team's (Klay: DAL 17,460,317 fossil beside MIA
        # 5,600,000; KCP: legacy MEM 20,194,392 beside the PHI minimum). The
        # dead-money frame is already the old team's truth, so the row drops.
        #
        # ORDERING IS LOAD-BEARING: this runs AFTER the exact matcher and the
        # season-level split, because the identical-schedule blended-duplicate
        # shape (Lillard/Beal) also has "a charge at one row's team" — but
        # there BOTH rows print the blended totals and the SPLIT is the truth;
        # a fossil drop there would strand pure-dead seasons on the survivor
        # as phantom salary. Divergent-schedule groups reach here.
        #
        # GATES (all must hold; anything less falls to tier-3's flag —
        # never guess):
        #   * the row's own team holds a positive charge overlapping the
        #     row's printed seasons, with no same-team charge conflicts;
        #   * Spotrac AFFIRMATIVELY places the player on the team of a
        #     SPECIFIC surviving sibling row (a third-team opinion or
        #     absence corroborates nothing — old behavior + flag);
        #   * that corroborated survivor's own team holds NO overlapping
        #     charge itself (both-teams-dead is ambiguous — old behavior).
        if len(survivors) > 1 and spotrac_teams is not None:
            spotrac_team = spotrac_teams.get(slug)
            corroborated = [r for r in survivors if r.team == spotrac_team]
            if len(corroborated) == 1 and (
                _own_team_charge_overlap(corroborated[0], dead_rows, display_to_bbref)
                is None
            ):
                fossils = [
                    (r, _own_team_charge_overlap(r, dead_rows, display_to_bbref))
                    for r in survivors
                    if r is not corroborated[0]
                ]
                if all(overlap is not None for _, overlap in fossils):
                    for row, overlap in fossils:
                        # A fossil with raw amounts drops ITS OWN figure —
                        # the log names the real pre-waive dollars, not the
                        # blended league cell.
                        _log_fossil_drop(
                            row,
                            dict(row.raw_amounts)
                            if row.raw_amounts
                            else (overlap or {}),
                            result,
                        )
                    arithmetic = _subtract_dead_blends(
                        corroborated[0], dead_rows, display_to_bbref
                    )
                    kept = _resolve_kept_amounts(corroborated[0], arithmetic)
                    result.kept.append(kept)
                    # The duplicate stays VISIBLE (the DuplicateFlag
                    # invariant: resolution changes which row is kept,
                    # never whether the duplicate is surfaced).
                    result.flags.append(
                        DuplicateFlag(
                            slug=slug,
                            player_name=kept.player_name,
                            kept_team=kept.team,
                            other_teams=tuple(r.team for r, _ in fossils),
                            resolved_by_spotrac=True,
                        )
                    )
                    continue

        # Tier 3: unexplained duplicate. Spotrac tie-break chooses WHICH BBRef
        # row is current; anything short of exactly-one match keeps file-first.
        kept_row = survivors[0]
        if len(survivors) == 1:
            kept_row = _resolve_kept_amounts(
                kept_row, _subtract_dead_blends(kept_row, dead_rows, display_to_bbref)
            )
        # (Exactly-one-survivor decomposition runs through
        # _resolve_kept_amounts above: the blend arithmetic computes as
        # before and raw, when attached, is authoritative over it. Tier-3
        # multi-survivor rows stay excluded from SUBTRACTION — dead money
        # explained nothing there, and subtracting other-team charges from
        # an ambiguous duplicate would be a guess.)
        resolved_by_spotrac = False
        if len(survivors) > 1 and spotrac_teams is not None:
            opinion = spotrac_teams.get(slug)
            if opinion is not None:
                matches = [r for r in survivors if r.team == opinion]
                if len(matches) == 1:
                    kept_row = matches[0]
                    resolved_by_spotrac = True
        # The flag compares by IDENTITY against the ORIGINAL survivor object
        # — captured BEFORE any raw replace() below creates a new object, or
        # the kept team lists itself among its own duplicates (review
        # finding, reproduced: other_teams=('MEM','PHI') with kept 'PHI').
        kept_source = kept_row
        if len(survivors) > 1 and kept_row.raw_amounts is not None:
            # Tier-3 with an invariant-VERIFIED per-team figure attached
            # (review finding: the clean midseason-trade class — no dead
            # money anywhere — was still shipping the blended league cell,
            # an 8x overcount for KCP-shaped minimums). Applied AFTER the
            # Spotrac tie-break so raw lands on the FINAL kept row. There
            # is no dead-money subtraction on this path, so there is no
            # arithmetic to cross-check — stated, not fabricated. The
            # "authoritative" claim is scoped: it settles the kept row's
            # DOLLARS (BBRef's own per-team figure), never WHICH row is
            # kept — that stays with the tie-break state logged here and
            # carried on the DuplicateFlag.
            logger.info(
                "raw applied for %s (%s) on %s with no arithmetic "
                "cross-check — tier-3 has no dead-money subtraction to "
                "compare against; team-page raw is authoritative for this "
                "row's dollars (which-row resolved by Spotrac: %s)",
                kept_row.player_name,
                kept_row.slug,
                kept_row.team,
                resolved_by_spotrac,
            )
            tier3_flags: dict[str, bool] = {}
            if kept_row.raw_player_option is not None:
                tier3_flags["has_player_option"] = kept_row.raw_player_option
            if kept_row.raw_team_option is not None:
                tier3_flags["has_team_option"] = kept_row.raw_team_option
            kept_row = replace(
                kept_row, amounts=dict(kept_row.raw_amounts), **tier3_flags
            )

        result.kept.append(kept_row)
        if len(survivors) > 1:
            result.flags.append(
                DuplicateFlag(
                    slug=slug,
                    player_name=kept_row.player_name,
                    kept_team=kept_row.team,
                    other_teams=tuple(
                        r.team for r in survivors if r is not kept_source
                    ),
                    resolved_by_spotrac=resolved_by_spotrac,
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
    - non-decision markers (D/UFA/RFA) just track the CSV;
    - NG never overwrites: it may fill a season the DB has no row for, but it
      never replaces a different existing code (see the drift branch);
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
                # EXCEPT incoming NG: a guarantee-phase marker is weaker
                # information than any code already present. Spotrac's
                # Deadlines table is forward-looking — once an exercised
                # option's row is pruned, only the guarantee-date row remains
                # and the CSV degrades to NG (davisjd01 2026-27, verified
                # 2026-07-08). Letting NG replace richer DB state would erase
                # option history; NG may only fill empty seasons (the
                # db-is-None branch above).
                if code == "NG":
                    continue
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
        plan.flags.append(OptionTransitionFlag(slug, season, db.code, db.status, state))
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


# EPM refreshes only on a manual pull; past this age the projections behind
# valuation/WAR are quietly rotting and the nightly report should say so.
EPM_MAX_AGE_DAYS = 30


def epm_vintage(
    mtime: datetime | None,
    now: datetime,
    max_age_days: int = EPM_MAX_AGE_DAYS,
) -> tuple[str, bool]:
    """Human-readable EPM cache vintage + staleness flag (pure; approved 07-07).

    Returns ``("YYYY-MM-DD (N days old)", stale)`` from the cache file's
    mtime, where ``stale`` fires strictly PAST ``max_age_days`` (exactly 30
    days old is still fresh). ``None`` — no cache file, unreadable stat —
    is ``("unknown", True)``: an unknown vintage must alert, never pass
    silently.
    """
    if mtime is None:
        return "unknown", True
    if mtime.tzinfo is None:
        mtime = mtime.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    age_days = max((now - mtime).days, 0)
    return f"{mtime.date().isoformat()} ({age_days} days old)", age_days > max_age_days


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
