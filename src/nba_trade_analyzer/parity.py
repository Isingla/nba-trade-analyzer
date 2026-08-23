"""Parity acceptance harness for the export-reads-DB cutover (Phase 2 Day 3).

Runs the export three times — ``--source scrape``, ``--source db
--no-overrides``, ``--source db`` — into TEMP paths (never the real
snapshot), normalizes the JSON payloads, and asserts the cutover contract:

  P1  projections byte-identical (normalized) between scrape and db modes,
      OUTSIDE the carve-out set (their salary decomposition feeds
      projections; absorbed drift is always named in the output)
  P2  salary-side diff == EXACTLY the known-fixed carve-out set + the scrape-side
      allowlist (SCRAPE_SIDE_DIFF_SLUGS, per-entry receipts; healed entries
      FAIL with a remove-me message) + documented cap-holds debris allowlist
      (currently EMPTY — see ALLOWED_CAP_HOLD_DEBRIS)
  P3  option-flag deltas outside the carve-out set == zero (G4(b) stored flags)
  P4  overrides-on vs overrides-off diff == the payload's applied-override
      metadata stamp, set-equal in BOTH directions
  P5  season-window canary: no season OUTSIDE the salary window
      (export.salary_season_keys(), 2026-27..2031-32) anywhere in the
      DB-sourced payload — guards the next surprise BBRef column (G1)
  P6  G7 canary: one salary row per player, and the row count matches the
      reader's own count in the provenance stamp

Every check prints a named PASS/FAIL line; any FAIL exits nonzero and the
summary ends with a single ``CUTOVER: GO`` / ``CUTOVER: NO-GO`` verdict.

READ-ONLY BY CONSTRUCTION: the harness invokes ``nba-trade-analyzer export``
(itself read-only) and writes nothing except its temp output files, which are
deleted on exit. It never touches the committed snapshots, the DB, or any
repo file.

All comparison logic is pure over parsed payload dicts so the unit tests
(tests/test_export_parity.py) exercise every pass/fail path with stub
fixtures — no DB, no network, no subprocess.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from nba_trade_analyzer.export import salary_season_keys

# The players whose salary rows are EXPECTED to differ between the scrape
# path and the DB path — the DESIGN-DIFFERENCE family: BBRef publishes a
# real-but-blended total, the scrape side carries it by design, and the DB
# side decomposes it. Any other player in the salary diff is a cutover
# blocker. SHARED by P2's expected-diff set and P1's projection carve-out
# (payload diff 2026-07-16: the salary decomposition feeds projections, so
# exactly these slugs drift P1 too) — one constant, never two lists.
#
#   - lillada01, bealbr01, prospol01 — the commit-2315e89 DUAL-team blend
#     defect, fixed DB-side by dead-money separation.
#   - isaacjo01 — added 2026-07-17, the SAME-TEAM blend variant: ORL waived
#     him 6/27 ($8,000,000 gtd, not stretched) and he re-signed ORL on a
#     vet minimum during the moratorium (moratorium finalization proves the
#     minimum; cap hit $2,449,421). BBRef prints the undecomposed total
#     $10,449,421 in his ORL cell; the DB side decomposes 10,449,421 -
#     8,000,000 = 2,449,421 active, the scrape side carries BBRef's blended
#     total by design. DESIGN-DIFFERENCE family (this constant, with the
#     trio) — NOT the scrape-side-lie family (SCRAPE_SIDE_DIFF_SLUGS,
#     currently empty): there the SOURCE is wrong and the entry must be
#     removed when it heals; here both sides are faithful to their design
#     and the entry stays until BBRef changes how it publishes. Different
#     constants for a reason.
EXPECTED_SALARY_DIFF_SLUGS = frozenset(
    # caldwke01: KCP waived-via-buyout MEM 2026-07-25; scrape carries
    # blended 21,621,500, DB separates 17,744,971 dead (single-source:
    # Spotrac-derived CSV; Spotrac breakdown paywalled 2026-07-31)
    # + 3,876,529 active. Verified Ishaan 2026-07-31 (BBRef+Spotrac
    # transactions). MEM stretch decision open until 8/31 — revisit
    # if a stretch reshapes the dead schedule.
    {"lillada01", "bealbr01", "prospol01", "isaacjo01", "caldwke01"}
)

# Scrape-side-broken players: the DB is not the diverging side, so these are
# a SEPARATE allowlist from the quartet (different disease — do not merge).
# Absence from a live diff means the entry HEALED and must be removed (P2
# fails with a remove-the-entry message rather than passing silently).
# Currently EMPTY — retired entries keep their full lifecycle documented
# below so the next entry has a template and the history has receipts.
#
# looneke01 — allowlisted 2026-07-16, HEALED AND REMOVED 2026-07-17:
#   BBRef contracts page printed the two-stint SUM (declined NOP $8M TO +
#   LAL cap hit $2,449,421 = $10,449,421) on BOTH stint rows; scrape payload
#   carried him twice at the wrong amount, DB once (dedup) at the wrong
#   amount. Truth: 1yr LAL vet-min, cap hit $2,449,421 (Spotrac). BBRef
#   corrected the page: the 2026-07-17 ingest ran with 0 dup flags and the
#   fresh scrape carries him once at $2,449,421; the harness fired its
#   designed "source healed; REMOVE the allowlist entry" failure, and the
#   related status override was retired the same day (P4's no-row-effect
#   tell, the Reaves pattern). Any looneke01 diff from here on is a NEW
#   regression and must fail P2 as an unexpected slug.
SCRAPE_SIDE_DIFF_SLUGS: frozenset[str] = frozenset()

# PURE-DEAD SINGLE-ROW family (added 2026-07-18): players waived and signed
# nowhere, whose ONE BBRef row equals their own team's dead-money charge for
# every printed season. The DB ingest DROPS these rows (the single-row
# pure-dead classifier in ingest/plans.py, Spotrac-corroborated — see the
# ingest log's "pure-dead single row" entries); the scrape side still prints
# them. A THIRD family, deliberately separate from both above:
#   - not the quartet: nothing is decomposed — the whole row is charge, so
#     the DB has NO row at all (the diff is presence, not amounts);
#   - not the scrape-side-lie family: BBRef is not wrong, it just publishes
#     dead charges as roster-shaped rows by design.
# LIFECYCLE (the Looney pattern, direction-enforced in P2): each member is
# valid only while BBRef still prints the row. Charges expire and pages heal
# — rupertra01 aged out 2026-07-18 BEFORE this family existed, which is why
# he is not in it. When a member stops appearing in the live diff, P2 FAILS
# demanding its removal here; a carve-out must never outlive its cause.
# These slugs also vanish from DB-side projections (projections are built
# per salary row), so P1 carves the same set.
PURE_DEAD_SINGLE_ROW_SLUGS = frozenset(
    {
        "anthoco01",  # Cole Anthony @ MEM
        "littlna01",  # Nassir Little @ PHO (charge runs past BBRef's printed seasons)
        "mcgeeja01",  # JaVale McGee @ DAL
        "liddeej01",  # E.J. Liddell @ PHO
        "micicva01",  # Vasilije Micic @ MIL
        "diakima01",  # Mamadi Diakite @ MEM
        "rubiori01",  # Ricky Rubio @ CLE
        "louzama01",  # Didi Louzada @ POR (±$1 stretch-split rounding member)
        "thompkl01",  # Klay Thompson @ DAL (buyout 2026-08-21; full $17.46M charge pending posted give-back terms)
    }
)

# Cap-holds sentinel-debris allowance: {(team, season): max_abs_dollar_delta}.
# EXPLICIT allowlist, deliberately EMPTY as of 2026-07-14 — the live probe
# that day found all 111 sentinel rows STALE (absent from the current CSV),
# so scrape totals carry no debris and db totals (fresh AND quality='real')
# must match to the dollar. If a future CSV regen reintroduces sub-$10k
# placeholder cells, enumerate the exact (team, season) deltas HERE with a
# dated comment — never widen this into a fuzzy tolerance.
ALLOWED_CAP_HOLD_DEBRIS: dict[tuple[str, str], int] = {}

# Contract-level option flags on a salary row (BBRef CSS truth via G4(b)).
# Flags carry no per-season grain on the wire, so mismatches are reported as
# (player, team, field).
FLAG_FIELDS = ("hasPlayerOption", "hasTeamOption")

# G1 (retuned 2026-07-16): the SALARY window now carries BBRef's y6
# (2026-27..2031-32, export.salary_season_keys()). The canary no longer bans
# 2031-32 — it bans any season OUTSIDE the salary window (e.g. a surprise
# 2032-33 column), so the next BBRef header growth fails here instead of
# silently truncating money. Derived from the export module — never a
# hardcoded season list that could drift from the ingest window.
SALARY_WINDOW: tuple[str, ...] = tuple(salary_season_keys())
_SEASON_KEY_RE = re.compile(r"^\d{4}-\d{2}$")


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    detail: str

    @property
    def line(self) -> str:
        return f"{'PASS' if self.passed else 'FAIL'}  {self.name}: {self.detail}"


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def normalize(value):
    """Canonical form: dict keys sorted (via canonical_json), -0.0 -> 0.0.

    Ints and floats keep their types — both payloads come from the same
    serializer, so 1 vs 1.0 drift cannot occur unless the code paths truly
    diverge, which is exactly what should fail.
    """
    if isinstance(value, dict):
        return {k: normalize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [normalize(v) for v in value]
    if isinstance(value, float) and value == 0.0:
        return 0.0  # collapses -0.0
    return value


def canonical_json(value) -> str:
    return json.dumps(normalize(value), sort_keys=True, separators=(",", ":"))


# ---------------------------------------------------------------------------
# Payload accessors
# ---------------------------------------------------------------------------


def salary_rows_by_slug(payload: dict) -> dict[str, list[str]]:
    """slug -> sorted canonical row strings (a player may have >1 row on the
    scrape path — the dual-team shape)."""
    out: dict[str, list[str]] = {}
    for row in payload.get("salaries", []):
        out.setdefault(row.get("bbrefSlug", ""), []).append(canonical_json(row))
    for rows in out.values():
        rows.sort()
    return out


def diff_salary_slugs(a: dict, b: dict) -> set[str]:
    """Players whose salary row set differs (changed, added, or removed)."""
    rows_a, rows_b = salary_rows_by_slug(a), salary_rows_by_slug(b)
    return {
        slug
        for slug in set(rows_a) | set(rows_b)
        if rows_a.get(slug) != rows_b.get(slug)
    }


def cap_hold_totals(payload: dict) -> dict[tuple[str, str], int]:
    totals = payload.get("capHolds", {}).get("totals", {}) or {}
    return {
        (team, season): int(amount)
        for team, seasons in totals.items()
        for season, amount in seasons.items()
    }


_OVERRIDE_ITEM_RE = re.compile(
    r"(?P<table>[A-Za-z0-9_]+):(?P<row_key>[^.\s]+)\.(?P<field>[A-Za-z0-9_]+)=(?P<value>[^,]+)"
)
_APPLIED_RE = re.compile(r"(\d+) override\(s\) applied: (?P<items>.+?)(?:; skipped|$)")
_ROW_COUNT_RE = re.compile(r"(\d+) salary rows")


def parse_applied_overrides(
    source_note: str | None,
) -> list[tuple[str, str, str, str]] | None:
    """Applied-override tuples from the provenance stamp; [] when the stamp
    says zero/disabled; None when the stamp is missing or unparseable (the
    caller must FAIL on None — an unreadable stamp is not a pass)."""
    if not source_note:
        return None
    if (
        "overrides overlay DISABLED" in source_note
        or "0 overrides applied" in source_note
    ):
        return []
    m = _APPLIED_RE.search(source_note)
    if not m:
        return None
    items = [
        (g["table"], g["row_key"], g["field"], g["value"].strip())
        for g in (i.groupdict() for i in _OVERRIDE_ITEM_RE.finditer(m.group("items")))
    ]
    return items or None


def parse_stamped_row_count(source_note: str | None) -> int | None:
    if not source_note:
        return None
    m = _ROW_COUNT_RE.search(source_note)
    return int(m.group(1)) if m else None


def override_slugs(overrides: list[tuple[str, str, str, str]]) -> set[str]:
    """row_key convention is '<slug>|<season>' (DB-CONTRACT-DATA.md)."""
    return {row_key.split("|", 1)[0] for _, row_key, _, _ in overrides}


# ---------------------------------------------------------------------------
# Checks (pure — unit-tested with stub payloads)
# ---------------------------------------------------------------------------


def check_projections_identical(scrape: dict, db: dict) -> CheckResult:
    """Projections byte-identical OUTSIDE the carve-out families.

    The carve-out set's salary decomposition feeds their projections (2026-07-16
    payload diff), and the pure-dead family's DROPPED rows build no DB-side
    projection at all (presence drift) — so P1 excludes the SAME constants
    P2 forgives, and always NAMES what each carve-out absorbed, so the
    exceptions are visible in every run's output, never silent.
    """
    name = "P1 projections byte-identical outside carve-outs"
    scrape_proj = scrape.get("projections", {})
    db_proj = db.get("projections", {})
    drifted = {
        slug
        for slug in set(scrape_proj) | set(db_proj)
        if canonical_json(scrape_proj.get(slug)) != canonical_json(db_proj.get(slug))
    }
    carved = sorted(drifted & EXPECTED_SALARY_DIFF_SLUGS)
    carved_pure_dead = sorted(drifted & PURE_DEAD_SINGLE_ROW_SLUGS)
    unexpected = sorted(
        drifted - EXPECTED_SALARY_DIFF_SLUGS - PURE_DEAD_SINGLE_ROW_SLUGS
    )
    notes = []
    if carved:
        notes.append(f"quartet drift absorbed by carve-out: {carved}")
    if carved_pure_dead:
        # Dropped salary rows build no projection — presence drift, absorbed.
        notes.append(f"pure-dead family absorbed: {carved_pure_dead}")
    carved_note = "; ".join(notes) if notes else "carve-out unused"
    if not unexpected:
        return CheckResult(
            name,
            True,
            f"{len(db_proj)} players, no delta outside carve-out; {carved_note}",
        )
    return CheckResult(
        name,
        False,
        f"UNEXPECTED projection drift for {len(unexpected)} slug(s): "
        f"{unexpected[:10]}; {carved_note}",
    )


def check_salary_diff_players(scrape: dict, db: dict) -> CheckResult:
    """Salary diff == carve-out set + pure-dead family + scrape-side allowlist, exactly.

    diff_salary_slugs compares each slug's full ROW SET, so a structural
    count difference (looneke01: two scrape rows vs one DB row) is the same
    kind of diff as an amount difference — the allowlist forgives the slug,
    whatever shape its divergence takes. Missing members fail in both
    directions, with direction-specific messages: a missing carve-out member
    means the DB-side fix regressed; a missing scrape-side member means the
    source HEALED and the allowlist entry must be removed.
    """
    name = "P2 salary diff == quartet + pure-dead family + scrape-side allowlist"
    expected = (
        EXPECTED_SALARY_DIFF_SLUGS
        | PURE_DEAD_SINGLE_ROW_SLUGS
        | SCRAPE_SIDE_DIFF_SLUGS
    )
    diff = diff_salary_slugs(scrape, db)
    unexpected = diff - expected
    missing = expected - diff
    if not unexpected and not missing:
        return CheckResult(
            name,
            True,
            f"diff set exactly {sorted(diff)} "
            f"(quartet {sorted(EXPECTED_SALARY_DIFF_SLUGS)} + "
            f"pure-dead {sorted(PURE_DEAD_SINGLE_ROW_SLUGS)} + "
            f"scrape-side {sorted(SCRAPE_SIDE_DIFF_SLUGS)})",
        )
    detail = []
    if unexpected:
        detail.append(f"UNEXPECTED diff players: {sorted(unexpected)}")
    regressed = missing & EXPECTED_SALARY_DIFF_SLUGS
    if regressed:
        detail.append(
            f"expected quartet member(s) NOT in diff (fix regressed?): {sorted(regressed)}"
        )
    aged_out = missing & PURE_DEAD_SINGLE_ROW_SLUGS
    if aged_out:
        detail.append(
            "pure-dead family member(s) NOT in diff — charge aged off BBRef "
            "or the row healed; REMOVE from PURE_DEAD_SINGLE_ROW_SLUGS: "
            f"{sorted(aged_out)}"
        )
    healed = missing & SCRAPE_SIDE_DIFF_SLUGS
    if healed:
        detail.append(
            "scrape-side allowlist member(s) NOT in diff — source healed; "
            f"REMOVE the allowlist entry: {sorted(healed)}"
        )
    return CheckResult(name, False, "; ".join(detail))


def check_cap_hold_debris(scrape: dict, db: dict) -> CheckResult:
    name = "P2b cap-holds delta within documented allowlist"
    a, b = cap_hold_totals(scrape), cap_hold_totals(db)
    deltas = {
        key: a.get(key, 0) - b.get(key, 0)
        for key in set(a) | set(b)
        if a.get(key, 0) != b.get(key, 0)
    }
    violations = {
        key: delta
        for key, delta in deltas.items()
        if abs(delta) > ALLOWED_CAP_HOLD_DEBRIS.get(key, 0)
    }
    if not violations:
        allowed = len(deltas)
        return CheckResult(
            name,
            True,
            f"{allowed} delta(s), all within allowlist ({len(ALLOWED_CAP_HOLD_DEBRIS)} entries)",
        )
    sample = sorted(violations.items())[:10]
    return CheckResult(name, False, f"non-allowlisted team-season deltas: {sample}")


def check_option_flags(scrape: dict, db: dict) -> CheckResult:
    name = "P3 option-flag deltas outside quartet == 0"
    mismatches: list[tuple[str, str, str]] = []
    scrape_rows = {
        (r.get("bbrefSlug"), r.get("team")): r for r in scrape.get("salaries", [])
    }
    for row in db.get("salaries", []):
        slug = row.get("bbrefSlug")
        if slug in EXPECTED_SALARY_DIFF_SLUGS:
            continue  # quartet rows legitimately differ wholesale
        other = scrape_rows.get((slug, row.get("team")))
        if other is None:
            continue  # presence differences are P2's finding, not a flag delta
        for field in FLAG_FIELDS:
            if bool(row.get(field)) != bool(other.get(field)):
                mismatches.append((slug, row.get("team"), field))
    if not mismatches:
        return CheckResult(name, True, "0 flag deltas")
    return CheckResult(
        name,
        False,
        "flag mismatches (player, team, field — flags are contract-level, no "
        f"season grain on the wire): {sorted(mismatches)[:20]}",
    )


def check_override_diff(db_with: dict, db_without: dict) -> CheckResult:
    name = "P4 override diff == metadata stamp (both directions)"
    applied = parse_applied_overrides(db_with.get("sourceNote"))
    if applied is None:
        return CheckResult(
            name, False, "could not parse applied-override stamp from sourceNote"
        )
    stamped = override_slugs(applied)
    diff = diff_salary_slugs(db_with, db_without)
    only_in_diff = diff - stamped
    only_in_stamp = stamped - diff
    if not only_in_diff and not only_in_stamp:
        return CheckResult(
            name, True, f"{len(stamped)} override slug(s), diff matches exactly"
        )
    detail = []
    if only_in_diff:
        detail.append(
            f"rows changed WITHOUT a stamped override: {sorted(only_in_diff)}"
        )
    if only_in_stamp:
        detail.append(
            "stamped override with NO row effect (moot override? retire it, or "
            f"a masked overlay bug): {sorted(only_in_stamp)}"
        )
    return CheckResult(name, False, "; ".join(detail))


def _find_out_of_window(value, path: str, hits: list[str]) -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            if (
                isinstance(k, str)
                and _SEASON_KEY_RE.match(k)
                and k not in SALARY_WINDOW
            ):
                hits.append(f"{path}.{k}")
            _find_out_of_window(v, f"{path}.{k}", hits)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _find_out_of_window(v, f"{path}[{i}]", hits)


def check_season_window_canary(db: dict) -> CheckResult:
    name = "P5 season-window canary: no season outside salary window"
    hits: list[str] = []
    _find_out_of_window(db, "$", hits)
    for row in db.get("salaries", []):
        if len(row.get("yearlySalaries", []) or []) > len(SALARY_WINDOW):
            hits.append(
                f"$.salaries[{row.get('bbrefSlug')}].yearlySalaries has "
                f"{len(row['yearlySalaries'])} entries (window is "
                f"{len(SALARY_WINDOW)} seasons, last {SALARY_WINDOW[-1]})"
            )
    if not hits:
        return CheckResult(
            name, True, f"all seasons within {SALARY_WINDOW[0]}..{SALARY_WINDOW[-1]}"
        )
    return CheckResult(name, False, f"out-of-window seasons at: {hits[:10]}")


def check_g7_canary(db: dict) -> CheckResult:
    name = "P6 G7 canary: one row per player, count matches reader stamp"
    rows = db.get("salaries", [])
    slugs = [r.get("bbrefSlug") for r in rows]
    dupes = sorted({s for s in slugs if slugs.count(s) > 1})
    problems = []
    if dupes:
        problems.append(f"duplicate player rows (grain collapsed): {dupes}")
    stamped = parse_stamped_row_count(db.get("sourceNote"))
    if stamped is not None and stamped != len(rows):
        problems.append(f"payload has {len(rows)} rows but reader stamped {stamped}")
    if not problems:
        stamp_note = "unstamped" if stamped is None else f"stamp={stamped}"
        return CheckResult(
            name, True, f"{len(rows)} rows == {len(set(slugs))} players ({stamp_note})"
        )
    return CheckResult(name, False, "; ".join(problems))


def run_all(scrape: dict, db_without: dict, db_with: dict) -> list[CheckResult]:
    return [
        check_projections_identical(scrape, db_without),
        check_salary_diff_players(scrape, db_without),
        check_cap_hold_debris(scrape, db_without),
        check_option_flags(scrape, db_without),
        check_override_diff(db_with, db_without),
        check_season_window_canary(db_without),
        check_g7_canary(db_without),
    ]


# ---------------------------------------------------------------------------
# Operational entry — three CLI invocations into temp files, then the checks.
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_export(args: list[str], out_path: Path) -> None:
    cmd = ["uv", "run", "nba-trade-analyzer", "export", *args, "--out", str(out_path)]
    result = subprocess.run(cmd, cwd=_REPO_ROOT, text=True, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"export {' '.join(args)} failed (exit {result.returncode}):\n"
            + result.stderr.strip()[-2000:]
        )


def main(argv: list[str] | None = None) -> int:
    if "CONTRACT_INGEST_DATABASE_URL" not in os.environ:
        print(
            "CONTRACT_INGEST_DATABASE_URL is not set — the two db-mode runs "
            "need the restricted contract_ingest role.\n"
            "Run:  source ~/.config/contract-ingest.env  and retry.",
            file=sys.stderr,
        )
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="export-parity-"))
    try:
        paths = {
            "scrape": tmp / "scrape.json",
            "db_without": tmp / "db-no-overrides.json",
            "db_with": tmp / "db-with-overrides.json",
        }
        print(f"[parity] temp dir {tmp} (removed on exit; snapshots untouched)")
        print("[parity] run 1/3: --source scrape")
        _run_export(["--source", "scrape"], paths["scrape"])
        print("[parity] run 2/3: --source db --no-overrides")
        _run_export(["--source", "db", "--no-overrides"], paths["db_without"])
        print("[parity] run 3/3: --source db")
        _run_export(["--source", "db"], paths["db_with"])

        payloads = {k: json.loads(p.read_text()) for k, p in paths.items()}
        results = run_all(
            payloads["scrape"], payloads["db_without"], payloads["db_with"]
        )

        print("\n=== parity assertions ===")
        for r in results:
            print(r.line)
        go = all(r.passed for r in results)
        print("\nCUTOVER: " + ("GO" if go else "NO-GO"))
        return 0 if go else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
