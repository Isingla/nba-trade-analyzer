"""Layer-1 verifier (Phase 2A): three-way per player-season salary compare.

DB value (what ingest just wrote) vs BBRef-scraped value vs the
Spotrac-derived ``nba_salaries.csv`` value. Runs at the end of every ingest.

Adjudicated rules, implemented exactly (Phase 2A spec):
  - ``nba_salaries.csv`` parsed by header name, artifact cells (<$10k
    non-zero) dropped at the loader, "0" = no contract;
  - name matching via the crosswalk resolver; UNMATCHED names get a
    ``verdict=unverifiable`` row — never silently skipped;
  - a player present in cap holds and absent from salaries on BOTH sides is
    a clean FA — no rows at all;
  - a player in v3_dead_money duplicated in raw BBRef is the known
    waive-and-stretch pattern, not a mismatch: contract comparison uses the
    separated/kept row, and WAIVED-marked Spotrac rows are compared against
    the DEAD-MONEY schedule (field ``dead_money:<season>``), not contracts;
  - EXACT dollar equality only; any difference is a mismatch with both
    values recorded;
  - every compared player-season gets a verdict row, matches included, tied
    to the run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from nba_trade_analyzer.data.nba_salaries_csv import NbaSalaryCsvRow
from nba_trade_analyzer.ingest.names import NameResolver
from nba_trade_analyzer.teams import resolve_team

FIELD_SALARY_PREFIX = "salary:"
FIELD_DEAD_MONEY_PREFIX = "dead_money:"
FIELD_NAME_MATCH = "spotrac_name_match"
FIELD_DUPLICATE_TEAM_ROWS = "duplicate_team_rows"
# Current-state team attribution — deliberately NO season suffix, so its
# (slug, 'team') delta key can never collide with 'salary:<season>' keys.
FIELD_TEAM = "team"
# Field suffix tagging a match recognized via the dead-money carve-out
# (v3_verifications has no detail column; the tag rides on the field, e.g.
# "salary:2026-27:dead_money_pattern" — season bucketing via split(':')[1]
# still works).
DETAIL_DEAD_MONEY_PATTERN = "dead_money_pattern"

_WAIVED_RE = re.compile(r"\s+WAIVED\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class VerificationRow:
    slug: str | None  # None when the player never resolved to a slug
    player_name: str
    field: str
    our_value: str
    bbref_value: str | None
    spotrac_value: str | None
    verdict: str  # match | mismatch | unverifiable


@dataclass
class VerifySummary:
    match: int = 0
    mismatch: int = 0
    unverifiable: int = 0
    dead_money_pattern: int = 0  # matches recognized via the dead-money carve-out
    team_match: int = 0  # team-attribution agreements (subset of match)
    team_mismatch: int = 0  # team-attribution disagreements (subset of mismatch)
    mismatch_keys: set[tuple[str, str]] = field(default_factory=set)  # (slug/name, field)
    # Whole-column coverage gaps, recorded ONCE per run instead of as hundreds
    # of per-row verdicts: spotrac_missing = seasons our data has but the
    # Spotrac file has no column for (e.g. 2025-26); ours_missing = seasons
    # Spotrac carries but our ingest window doesn't (e.g. 2030-31).
    skipped_seasons: dict[str, list[str]] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "match": self.match,
            "mismatch": self.mismatch,
            "unverifiable": self.unverifiable,
            "dead_money_pattern": self.dead_money_pattern,
            "team_match": self.team_match,
            "team_mismatch": self.team_mismatch,
        }


def _fmt(value: int | None) -> str:
    return "absent" if value is None else str(value)


def _fmt_opt(value: int | None) -> str | None:
    return None if value is None else str(value)


def verify_salaries(
    *,
    ingested: dict[str, dict[str, int]],  # slug -> season -> dollars (post-separation)
    bbref: dict[str, dict[str, int]],  # slug -> season -> dollars (kept scrape rows)
    spotrac_rows: list[NbaSalaryCsvRow],
    resolver: NameResolver,
    player_names: dict[str, str],  # slug -> display name
    cap_hold_slugs: set[str],  # slugs resolved from cap-hold rows
    dead_amounts: dict[str, dict[str, int]],  # slug -> season -> dead-money dollars
    spotrac_coverage: set[str] | None = None,  # seasons the CSV has COLUMNS for
    our_coverage: set[str] | None = None,  # seasons the ingest window covers
    our_teams: dict[str, str] | None = None,  # slug -> our team code (BBRef style)
) -> tuple[list[VerificationRow], VerifySummary]:
    """Three-way compare. Comparisons run only within MUTUAL season coverage:
    a source with no column for a season has no opinion (missing-from-source),
    which is neither 0 nor a mismatch — those seasons are skipped wholesale and
    recorded once in ``summary.skipped_seasons``. ``None`` coverage = no
    restriction (test convenience; the runner always passes real sets)."""
    rows: list[VerificationRow] = []
    summary = VerifySummary()

    def emit(row: VerificationRow) -> None:
        rows.append(row)
        if row.verdict == "match":
            summary.match += 1
        elif row.verdict == "mismatch":
            summary.mismatch += 1
            summary.mismatch_keys.add((row.slug or row.player_name, row.field))
        else:
            summary.unverifiable += 1

    # ---- resolve the Spotrac side ------------------------------------------
    spotrac_by_slug: dict[str, dict[str, int]] = {}
    spotrac_team_by_slug: dict[str, str] = {}  # first non-WAIVED row's team
    waived_slugs: set[str] = set()  # slugs Spotrac lists via a WAIVED row
    for r in spotrac_rows:
        # Strip the WAIVED marker before resolving — "Lillard Damian WAIVED"
        # must resolve like "Lillard Damian" (the resolver handles word order).
        slug = resolver.resolve(_WAIVED_RE.sub("", r.player_raw))
        if slug is None:
            # Rule: unmatched names are unverifiable, never silently skipped.
            emit(
                VerificationRow(
                    slug=None,
                    player_name=r.player_raw,
                    field=FIELD_NAME_MATCH,
                    our_value=r.player_raw,
                    bbref_value=None,
                    spotrac_value=f"{r.team}:{len(r.amounts)} season(s)",
                    verdict="unverifiable",
                )
            )
            continue

        if _WAIVED_RE.search(r.player_raw):
            # WAIVED rows are dead-money schedules, not contracts (the known
            # waive-and-stretch pattern) — compare against v3_dead_money.
            # Spotrac coverage applies here too (a season the file has no
            # column for is silence); dead money is NOT bounded by the ingest
            # window, so our_coverage deliberately does not.
            waived_slugs.add(slug)
            dead = dead_amounts.get(slug, {})
            dead_seasons = set(r.amounts) | set(dead)
            if spotrac_coverage is not None:
                dead_seasons &= spotrac_coverage
            for season in sorted(dead_seasons):
                ours = dead.get(season)
                sp = r.amounts.get(season)
                emit(
                    VerificationRow(
                        slug=slug,
                        player_name=player_names.get(slug, r.player_raw),
                        field=f"{FIELD_DEAD_MONEY_PREFIX}{season}",
                        our_value=_fmt(ours),
                        bbref_value=None,
                        spotrac_value=_fmt_opt(sp),
                        verdict="match" if ours == sp else "mismatch",
                    )
                )
            continue

        merged = spotrac_by_slug.setdefault(slug, {})
        for season, amount in r.amounts.items():
            # Two Spotrac rows for one player (team churn) — keep the first;
            # a real disagreement still surfaces as a mismatch below.
            merged.setdefault(season, amount)
        # Current-team opinion: first NON-WAIVED row wins (same keep-first
        # convention). WAIVED rows never set it — a player whose only Spotrac
        # presence is a dead-money entry has no "current team" on that side.
        spotrac_team_by_slug.setdefault(slug, r.team)

    # ---- coverage intersection (the 786-mismatch fix) -----------------------
    # A source with no COLUMN for a season is missing-from-source: neither 0
    # nor a mismatch. Whole-column absences are skipped and recorded once —
    # never as hundreds of per-row verdicts. our_coverage bounds Spotrac's
    # far-future columns (2030-31+) that the ingest window truncates; the
    # window itself is a deferred rollover decision (see export.season_keys).
    all_our_seasons = {s for amounts in ingested.values() for s in amounts}
    all_sp_seasons = {s for amounts in spotrac_by_slug.values() for s in amounts}
    comparable: set[str] = all_our_seasons | all_sp_seasons
    if spotrac_coverage is not None:
        comparable &= spotrac_coverage
    if our_coverage is not None:
        comparable &= our_coverage
    summary.skipped_seasons = {
        "spotrac_missing": sorted(
            all_our_seasons - spotrac_coverage if spotrac_coverage is not None else []
        ),
        "ours_missing": sorted(
            all_sp_seasons - our_coverage if our_coverage is not None else []
        ),
    }

    # ---- per player-season three-way compare -------------------------------
    all_slugs = set(ingested) | set(spotrac_by_slug)
    for slug in sorted(all_slugs):
        ours = ingested.get(slug, {})
        bb = bbref.get(slug, {})
        sp = spotrac_by_slug.get(slug, {})

        # Clean-FA rule: in cap holds, no salary on either side -> no rows.
        if not ours and not sp and slug in cap_hold_slugs:
            continue

        name = player_names.get(slug, slug)
        for season in sorted((set(ours) | set(sp)) & comparable):
            our_amount = ours.get(season)
            bb_amount = bb.get(season)
            sp_amount = sp.get(season)

            # Known-pattern carve-out #2 (dead money listed as an unmarked
            # salary row): our side is absent because separation moved this
            # money to v3_dead_money, and Spotrac's figure equals the dead
            # charge TO THE DOLLAR. Tagged match — distinguishable from a
            # plain agreement. Any delta falls through to mismatch.
            if (
                our_amount is None
                and sp_amount is not None
                and dead_amounts.get(slug, {}).get(season) == sp_amount
            ):
                summary.dead_money_pattern += 1
                emit(
                    VerificationRow(
                        slug=slug,
                        player_name=name,
                        field=f"{FIELD_SALARY_PREFIX}{season}:{DETAIL_DEAD_MONEY_PATTERN}",
                        our_value=_fmt(our_amount),
                        bbref_value=_fmt_opt(bb_amount),
                        spotrac_value=_fmt_opt(sp_amount),
                        verdict="match",
                    )
                )
                continue

            # Exact-dollar equality only. Absent-vs-present between our DB and
            # Spotrac is a difference ("0"/absent = no contract). The BBRef
            # column is the same source ingest wrote from, so it only adds a
            # mismatch when it is present AND disagrees with the DB value
            # (e.g. an override changed the DB after ingest).
            #
            # Known-pattern carve-out #1: when Spotrac lists this player ONLY
            # via a WAIVED (dead-money) row, it has no opinion on his real
            # contract — a Spotrac-absent season is silence, not disagreement.
            spotrac_silent = sp_amount is None and slug in waived_slugs and not sp
            is_mismatch = our_amount != sp_amount and not spotrac_silent
            if (
                our_amount is not None
                and bb_amount is not None
                and bb_amount != our_amount
            ):
                is_mismatch = True

            emit(
                VerificationRow(
                    slug=slug,
                    player_name=name,
                    field=f"{FIELD_SALARY_PREFIX}{season}",
                    our_value=_fmt(our_amount),
                    bbref_value=_fmt_opt(bb_amount),
                    spotrac_value=_fmt_opt(sp_amount),
                    verdict="mismatch" if is_mismatch else "match",
                )
            )

    # ---- team attribution (current employment, REPORT-ONLY) -----------------
    # BBRef remains the team source of record — v3_players.team_bbref is
    # ingested from the BBRef scrape and this check never changes what ingest
    # writes anywhere. Spotrac's Team column is a cross-check OPINION: a
    # mismatch is exactly the salary-staleness class of drift (Ayton POR->WAS,
    # Smart WAS->? in the July-6 wave) and routes to Hermes/human adjudication
    # like everything else. Notes:
    #  - both sides normalize to the DISPLAY abbreviation system via
    #    resolve_team (accepts BBRef BRK/CHO/PHO and Spotrac BKN/CHA/PHX);
    #  - crosswalk-unmatched Spotrac names already produced ONE unverifiable
    #    row (FIELD_NAME_MATCH) above — not re-reported per field;
    #  - WAIVED-only players have no Spotrac current team -> skipped (their
    #    dead-money identity is handled by the carve-outs above);
    #  - a player with dead money on the OLD team and team_bbref = NEW team is
    #    CORRECT by schema design — nothing special here, we compare current
    #    employment only.
    if our_teams is not None:
        for slug in sorted(set(our_teams) & set(spotrac_team_by_slug)):
            ours_raw = our_teams[slug]
            sp_raw = spotrac_team_by_slug[slug]
            ours_norm = resolve_team(ours_raw)
            sp_norm = resolve_team(sp_raw)
            # Display-system codes for the verdict row; unknown codes stay raw
            # (and surface as a mismatch rather than being silently dropped).
            our_disp = ours_norm.abbreviation if ours_norm else ours_raw
            sp_disp = sp_norm.abbreviation if sp_norm else sp_raw
            is_match = our_disp == sp_disp
            if is_match:
                summary.team_match += 1
            else:
                summary.team_mismatch += 1
            emit(
                VerificationRow(
                    slug=slug,
                    player_name=player_names.get(slug, slug),
                    field=FIELD_TEAM,
                    our_value=our_disp,
                    bbref_value=None,
                    spotrac_value=sp_disp,
                    verdict="match" if is_match else "mismatch",
                )
            )
    return rows, summary
