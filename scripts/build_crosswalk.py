"""Build (or refresh) the NBA-id <-> Basketball-Reference-slug crosswalk.

This is the ONE place name-based matching happens. It runs offline against the
nba_api static player registry (bundled, no network) and the Basketball
Reference contracts (slugs + names), proposes a best id match for every
contract using the project's single name normalizer, and freezes the result
into ``data/player_crosswalk.json`` (committed, human-auditable). Runtime joins
never re-derive these matches — they are pure id lookups (see data/crosswalk).

ASSISTED mode (the default): prints the full proposed mapping in a reviewable
table, with all low-confidence / ambiguous / unmatched / collision rows flagged
at the top, plus a diff vs the committed crosswalk and a coverage report. Exits
non-zero if contract coverage is below ``--threshold`` (default 0.95) so a bad
build can't pass silently.

Usage::

    python scripts/build_crosswalk.py                 # build + write + report
    python scripts/build_crosswalk.py --dry-run       # report only, don't write
    python scripts/build_crosswalk.py --threshold 0.9
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from dataclasses import dataclass

from nba_api.stats.static import players as static_players

from nba_trade_analyzer.data.cache import JsonCache

from nba_trade_analyzer.data.crosswalk import (
    DEFAULT_CROSSWALK_PATH,
    CrosswalkEntry,
    crosswalk_from_dict,
    dump_crosswalk,
    load_crosswalk,
)
from nba_trade_analyzer.data.epm import NAME_ALIASES, normalize_name
from nba_trade_analyzer.data.salaries import _DEFAULT_SEASON, fetch_all_salaries

# Aliases are normalized on both sides so a colloquial/canonical split (e.g.
# "Nic Claxton" vs "Nicolas Claxton") collapses to one key on registry + query.
_NORMALIZED_ALIASES: dict[str, str] = {
    normalize_name(k): normalize_name(v) for k, v in NAME_ALIASES.items()
}


def _resolve(norm: str) -> str:
    return _NORMALIZED_ALIASES.get(norm, norm)


@dataclass
class _Proposal:
    bbref_slug: str
    bbref_name: str
    nba_id: int | None
    nba_name: str
    flag: str  # "ok" | "unmatched" | "ambiguous" | "no-slug" | "collision"


def _build_registry() -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
    """Return ``(active_by_key, all_by_key)`` maps of alias-resolved name -> players."""
    active: dict[str, list[dict]] = {}
    everyone: dict[str, list[dict]] = {}
    for p in static_players.get_players():
        key = _resolve(normalize_name(p["full_name"]))
        everyone.setdefault(key, []).append(p)
        if p.get("is_active"):
            active.setdefault(key, []).append(p)
    return active, everyone


def _match_one(
    slug: str,
    name: str,
    active: dict[str, list[dict]],
    everyone: dict[str, list[dict]],
) -> _Proposal:
    if not slug:
        return _Proposal(slug, name, None, "", "no-slug")
    key = _resolve(normalize_name(name))
    # Prefer a unique active player; fall back to a unique all-time match.
    for pool in (active.get(key, []), everyone.get(key, [])):
        if len(pool) == 1:
            p = pool[0]
            return _Proposal(slug, name, int(p["id"]), p["full_name"], "ok")
        if len(pool) > 1:
            return _Proposal(slug, name, None, "", "ambiguous")
    return _Proposal(slug, name, None, "", "unmatched")


def _propose_all(salary_df) -> list[_Proposal]:
    active, everyone = _build_registry()
    # A player can appear on multiple contract rows (dead money split across the
    # teams that waived/traded them). The slug is the same on each, so the
    # crosswalk needs one entry per *unique* slug — dedupe before matching.
    seen_slugs: set[str] = set()
    proposals: list[_Proposal] = []
    for _, row in salary_df.iterrows():
        slug = str(row["bbref_slug"])
        if slug and slug in seen_slugs:
            continue
        if slug:
            seen_slugs.add(slug)
        proposals.append(_match_one(slug, str(row["player_name"]), active, everyone))
    # Guard against two slugs resolving to the same nba_id — that would make an
    # invalid crosswalk (the loader raises). Flag every member of a collision so
    # a human resolves it; none are written.
    seen: dict[int, list[_Proposal]] = {}
    for p in proposals:
        if p.flag == "ok" and p.nba_id is not None:
            seen.setdefault(p.nba_id, []).append(p)
    for nba_id, group in seen.items():
        if len(group) > 1:
            for p in group:
                p.flag = "collision"
    return proposals


def _print_table(proposals: list[_Proposal]) -> None:
    flagged = [p for p in proposals if p.flag != "ok"]
    ok = [p for p in proposals if p.flag == "ok"]

    if flagged:
        print("\n=== FLAGGED ROWS (review these) ===")
        print(f"{'FLAG':<11} {'SLUG':<12} {'BBREF NAME':<26} {'NBA NAME':<26}")
        for p in sorted(flagged, key=lambda x: (x.flag, x.bbref_name)):
            print(
                f"{p.flag:<11} {p.bbref_slug:<12} {p.bbref_name:<26} {p.nba_name:<26}"
            )
    else:
        print("\nNo flagged rows — every contract matched cleanly.")

    print(f"\n=== MATCHED ({len(ok)}) ===")
    for p in sorted(ok, key=lambda x: x.bbref_slug):
        print(
            f"{'ok':<11} {p.bbref_slug:<12} {p.bbref_name:<26} "
            f"{p.nba_id:<10} {p.nba_name:<26}"
        )


def _print_diff(new_entries: list[CrosswalkEntry]) -> None:
    if not DEFAULT_CROSSWALK_PATH.exists():
        print("\n=== DIFF ===\n(no existing crosswalk — this is the first build)")
        return
    try:
        old = {e.bbref_slug: e for e in load_crosswalk().entries}
    except Exception as exc:  # noqa: BLE001
        print(f"\n=== DIFF ===\n(could not read existing crosswalk: {exc})")
        return
    new = {e.bbref_slug: e for e in new_entries}
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    changed = sorted(s for s in set(new) & set(old) if new[s].nba_id != old[s].nba_id)
    print("\n=== DIFF vs committed crosswalk ===")
    if not (added or removed or changed):
        print("(no changes)")
    for s in added:
        print(f"  + {s}  {new[s].bbref_name} -> {new[s].nba_id}")
    for s in removed:
        print(f"  - {s}  {old[s].bbref_name} (was {old[s].nba_id})")
    for s in changed:
        print(f"  ~ {s}  {old[s].nba_id} -> {new[s].nba_id}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--threshold", type=float, default=0.95)
    parser.add_argument("--dry-run", action="store_true", help="don't write the file")
    parser.add_argument("--season", default=_DEFAULT_SEASON)
    args = parser.parse_args()

    print(f"Fetching Basketball Reference contracts ({args.season})...")
    # Fresh temp cache so we always scrape live slugs (a stale pre-slug cache
    # entry would otherwise yield empty slugs and tank coverage).
    fresh_cache = JsonCache(tempfile.mkdtemp(prefix="crosswalk_build_"))
    salary_df = fetch_all_salaries(season=args.season, cache=fresh_cache)
    print(f"  {len(salary_df)} contract rows")

    proposals = _propose_all(salary_df)
    # Coverage is over unique players (slugs), not raw rows — a player with dead
    # money on several teams has one slug and needs one crosswalk entry.
    total = len(proposals)
    print(f"  {total} unique players")
    entries = [
        CrosswalkEntry(p.nba_id, p.nba_name, p.bbref_slug, p.bbref_name)
        for p in proposals
        if p.flag == "ok" and p.nba_id is not None
    ]

    _print_table(proposals)

    matched = len(entries)
    coverage = matched / total if total else 0.0
    print("\n=== COVERAGE ===")
    print(f"  matched {matched}/{total} contracts ({coverage:.1%})")
    unmatched = [p for p in proposals if p.flag != "ok"]
    if unmatched:
        print(f"  {len(unmatched)} unmatched/flagged contracts:")
        for p in unmatched:
            print(f"    [{p.flag}] {p.bbref_slug or '<no-slug>'}  {p.bbref_name}")

    # Validate the proposed payload round-trips through the loader's guards.
    payload = dump_crosswalk(entries, season=args.season)
    crosswalk_from_dict(payload)  # raises CrosswalkError on any dup/collision

    _print_diff(entries)

    if args.dry_run:
        print("\n--dry-run: not writing.")
    else:
        DEFAULT_CROSSWALK_PATH.parent.mkdir(parents=True, exist_ok=True)
        DEFAULT_CROSSWALK_PATH.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        print(f"\nWrote {DEFAULT_CROSSWALK_PATH} ({matched} entries).")

    if coverage < args.threshold:
        print(
            f"\nFAIL: coverage {coverage:.1%} below threshold {args.threshold:.1%}.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    print(f"\nOK: coverage {coverage:.1%} >= threshold {args.threshold:.1%}.")


if __name__ == "__main__":
    main()
