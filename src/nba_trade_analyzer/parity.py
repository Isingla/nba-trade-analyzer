"""Parity acceptance harness for the export-reads-DB cutover (Phase 2 Day 3).

Runs the export three times — ``--source scrape``, ``--source db
--no-overrides``, ``--source db`` — into TEMP paths (never the real
snapshot), normalizes the JSON payloads, and asserts the cutover contract:

  P1  projections byte-identical (normalized) between scrape and db modes
  P2  salary-side diff == EXACTLY the known-fixed trio (+ documented
      cap-holds debris allowlist, currently EMPTY — see ALLOWED_CAP_HOLD_DEBRIS)
  P3  option-flag deltas outside the trio == zero (G4(b) stored flags)
  P4  overrides-on vs overrides-off diff == the payload's applied-override
      metadata stamp, set-equal in BOTH directions
  P5  y6 canary: no 2031-32 season anywhere in the DB-sourced payload (G1)
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

# The three players whose salary rows are EXPECTED to differ between the
# scrape path and the DB path: the commit-2315e89 dual-team blend defect
# (Lillard, Beal, Prosper), fixed DB-side by dead-money separation. Any other
# player in the salary diff is a cutover blocker.
EXPECTED_SALARY_DIFF_SLUGS = frozenset({"lillada01", "bealbr01", "prospol01"})

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

# G1: the export window is 5 seasons (2026-27..2030-31). The 6th BBRef year
# column (2031-32) must not leak into a DB-sourced payload — the ingest
# window is deliberately NOT widened; presence here means it was.
Y6_SEASON = "2031-32"
MAX_WINDOW_YEARS = 5


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


def parse_applied_overrides(source_note: str | None) -> list[tuple[str, str, str, str]] | None:
    """Applied-override tuples from the provenance stamp; [] when the stamp
    says zero/disabled; None when the stamp is missing or unparseable (the
    caller must FAIL on None — an unreadable stamp is not a pass)."""
    if not source_note:
        return None
    if "overrides overlay DISABLED" in source_note or "0 overrides applied" in source_note:
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
    name = "P1 projections byte-identical"
    a = canonical_json(scrape.get("projections", {}))
    b = canonical_json(db.get("projections", {}))
    if a == b:
        return CheckResult(name, True, f"{len(db.get('projections', {}))} players, no delta")
    drifted = [
        slug
        for slug in set(scrape.get("projections", {})) | set(db.get("projections", {}))
        if canonical_json(scrape.get("projections", {}).get(slug))
        != canonical_json(db.get("projections", {}).get(slug))
    ]
    return CheckResult(
        name, False, f"projections differ for {len(drifted)} slug(s): {sorted(drifted)[:10]}"
    )


def check_salary_diff_players(scrape: dict, db: dict) -> CheckResult:
    name = "P2 salary diff == known-fixed trio"
    diff = diff_salary_slugs(scrape, db)
    unexpected = diff - EXPECTED_SALARY_DIFF_SLUGS
    missing = EXPECTED_SALARY_DIFF_SLUGS - diff
    if not unexpected and not missing:
        return CheckResult(name, True, f"diff set exactly {sorted(diff)}")
    detail = []
    if unexpected:
        detail.append(f"UNEXPECTED diff players: {sorted(unexpected)}")
    if missing:
        detail.append(
            f"expected trio member(s) NOT in diff (fix regressed?): {sorted(missing)}"
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
            name, True, f"{allowed} delta(s), all within allowlist ({len(ALLOWED_CAP_HOLD_DEBRIS)} entries)"
        )
    sample = sorted(violations.items())[:10]
    return CheckResult(name, False, f"non-allowlisted team-season deltas: {sample}")


def check_option_flags(scrape: dict, db: dict) -> CheckResult:
    name = "P3 option-flag deltas outside trio == 0"
    mismatches: list[tuple[str, str, str]] = []
    scrape_rows = {
        (r.get("bbrefSlug"), r.get("team")): r for r in scrape.get("salaries", [])
    }
    for row in db.get("salaries", []):
        slug = row.get("bbrefSlug")
        if slug in EXPECTED_SALARY_DIFF_SLUGS:
            continue  # trio rows legitimately differ wholesale
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
        detail.append(f"rows changed WITHOUT a stamped override: {sorted(only_in_diff)}")
    if only_in_stamp:
        detail.append(
            "stamped override with NO row effect (moot override? retire it, or "
            f"a masked overlay bug): {sorted(only_in_stamp)}"
        )
    return CheckResult(name, False, "; ".join(detail))


def _find_y6(value, path: str, hits: list[str]) -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            if k == Y6_SEASON:
                hits.append(f"{path}.{k}")
            _find_y6(v, f"{path}.{k}", hits)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _find_y6(v, f"{path}[{i}]", hits)


def check_y6_canary(db: dict) -> CheckResult:
    name = "P5 y6 canary: no 2031-32 in DB payload"
    hits: list[str] = []
    _find_y6(db, "$", hits)
    for row in db.get("salaries", []):
        if len(row.get("yearlySalaries", []) or []) > MAX_WINDOW_YEARS:
            hits.append(
                f"$.salaries[{row.get('bbrefSlug')}].yearlySalaries has "
                f"{len(row['yearlySalaries'])} entries (index {MAX_WINDOW_YEARS} = {Y6_SEASON})"
            )
    if not hits:
        return CheckResult(name, True, "no 2031-32 season anywhere")
    return CheckResult(name, False, f"2031-32 present at: {hits[:10]}")


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
        return CheckResult(name, True, f"{len(rows)} rows == {len(set(slugs))} players ({stamp_note})")
    return CheckResult(name, False, "; ".join(problems))


def run_all(scrape: dict, db_without: dict, db_with: dict) -> list[CheckResult]:
    return [
        check_projections_identical(scrape, db_without),
        check_salary_diff_players(scrape, db_without),
        check_cap_hold_debris(scrape, db_without),
        check_option_flags(scrape, db_without),
        check_override_diff(db_with, db_without),
        check_y6_canary(db_without),
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
