"""Clean-engine acceptance gauntlet (docs/SPEC-clean-engine.md §4).

Runs every acceptance gate against live data and prints PASS/FAIL per gate;
exits nonzero on any FAIL. Read-only everywhere: the DB connection (gate 5)
issues SELECTs only, and nothing on disk is modified.

Usage:
    uv run python scripts/clean_engine_gauntlet.py

Gate 5 needs CONTRACT_INGEST_DATABASE_URL in the environment (read-only
crosswalk queries against v3_players); unset → that gate prints SKIPPED.
"""

from __future__ import annotations

import json
import os
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nba_trade_analyzer.engine.clean_engine import (  # noqa: E402
    CLEAN_CONSTANTS,
    calculate_surplus,
    calculate_value,
    calculate_war,
    derive_dollars_per_win,
    load_epm_api_cache,
    pace_for_season,
    scale_dollars_per_win,
)

PACE = pace_for_season("2025-26")
RATE_25_26 = CLEAN_CONSTANTS.dollars_per_win_2025_26
RATE_26_27 = scale_dollars_per_win(RATE_25_26, 154_647_000, 164_961_000)
SALARY_CACHE = Path.home() / ".nba_trade_analyzer/cache/salaries_2025-26.json"

RESULTS: list[tuple[str, bool | None]] = []  # None = skipped


def report(gate: str, ok: bool | None, detail: str) -> None:
    status = "SKIPPED" if ok is None else ("PASS" if ok else "FAIL")
    print(f"[{status}] {gate}: {detail}")
    RESULTS.append((gate, ok))


def approx(a: float, b: float, tol: float) -> bool:
    return abs(a - b) <= tol


# ----- gates 1-3: the spec pins --------------------------------------------


def gate_pins() -> None:
    kuzma_war = calculate_war(-1.28, 1806, PACE)
    kuzma_value = calculate_value(kuzma_war, RATE_25_26)
    kuzma_surplus = calculate_surplus(kuzma_value, 20_194_392)
    ok = (
        approx(kuzma_war, 0.84, 0.1)
        and approx(kuzma_value, 6.6e6, 0.5e6)
        and approx(kuzma_surplus, -13.6e6, 0.5e6)
    )
    report(
        "GATE 1 Kuzma pin",
        ok,
        f"WAR {kuzma_war:.2f} (exp 0.84±0.1), value ${kuzma_value/1e6:.2f}M, "
        f"surplus ${kuzma_surplus/1e6:.2f}M",
    )

    wemby_war = calculate_war(8.74, 1866, PACE)
    wemby_value = calculate_value(wemby_war, RATE_26_27)
    wemby_surplus = calculate_surplus(wemby_value, 16_868_246)
    ok = (
        approx(wemby_war, 11.41, 0.1)
        and approx(wemby_value, 95.7e6, 0.5e6)
        and approx(wemby_surplus, 78.8e6, 0.5e6)
    )
    report(
        "GATE 2 Wemby pin",
        ok,
        f"WAR {wemby_war:.2f} (exp 11.41±0.1), value ${wemby_value/1e6:.2f}M "
        f"@ ${RATE_26_27/1e6:.2f}M/win, surplus ${wemby_surplus/1e6:.2f}M",
    )

    plus7 = calculate_war(7.0, 2800, PACE)
    report(
        "GATE 3 +7 EPM @ 2,800 min",
        approx(plus7, 14.38, 0.1),
        f"WAR {plus7:.2f} (exp 14.38±0.1)",
    )


# ----- gate 4: EPM source sanity -------------------------------------------


def gate_source_sanity(rows: list[dict]) -> None:
    wemby = next((r for r in rows if r.get("player_id") == 1641705), None)
    if wemby is None:
        report("GATE 4 EPM source sanity", False, "player_id 1641705 not in cache")
        return
    epm, mpg = float(wemby["epm"]), float(wemby["mpg"])
    ok = approx(epm, 8.74, 0.01) and approx(mpg, 29.1, 0.1)
    report(
        "GATE 4 EPM source sanity",
        ok,
        f"Wembanyama epm {epm:.4f} (exp 8.74±0.01), mpg {mpg:.2f} (exp "
        "29.1±0.1) — the scrape's Expected 7.80 @ 41.2 cannot pass",
    )


# ----- gate 5: dollars-per-win reproduction (DB crosswalk join) ------------


def _normalize(name: str) -> str:
    name = name.replace("ё", "e").replace("Ё", "E")
    nfkd = unicodedata.normalize("NFKD", name)
    return " ".join(
        "".join(c for c in nfkd if not unicodedata.combining(c)).casefold().split()
    )


def load_salaries() -> dict[str, dict]:
    payload = json.loads(SALARY_CACHE.read_text(encoding="utf-8"))
    rows = payload.get("value") if isinstance(payload, dict) else payload
    # Dedupe by slug keeping the LARGER amount — drops dead-money duplicate
    # stubs (Lillard/Beal/Prosper class).
    by_slug: dict[str, dict] = {}
    for row in rows:
        slug = row["bbref_slug"]
        if slug not in by_slug or row["salary"] > by_slug[slug]["salary"]:
            by_slug[slug] = row
    return by_slug


def gate_dollars_reproduction(rows: list[dict]) -> tuple[dict[int, str] | None]:
    dsn = os.environ.get("CONTRACT_INGEST_DATABASE_URL")
    if not dsn:
        report(
            "GATE 5 dollars/win reproduction",
            None,
            "CONTRACT_INGEST_DATABASE_URL unset — cannot run the crosswalk join",
        )
        return (None,)
    import psycopg

    # SKIPPED is permitted ONLY when the env var is unset (handled above).
    # With the env var PRESENT, any connection or query error is a FAIL —
    # printed with the error and driving a nonzero exit — never a skip and
    # never a bare traceback in place of the report.
    try:
        with psycopg.connect(dsn) as conn:  # read-only: one SELECT
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT nba_id, bbref_slug FROM v3_players "
                    "WHERE nba_id IS NOT NULL AND bbref_slug IS NOT NULL"
                )
                crosswalk = {int(nba_id): slug for nba_id, slug in cur.fetchall()}
    except Exception as exc:  # psycopg errors vary; env var present = FAIL
        report(
            "GATE 5 dollars/win reproduction",
            False,
            f"CONTRACT_INGEST_DATABASE_URL is set but the DB step failed: "
            f"{type(exc).__name__}: {exc}",
        )
        return (None,)

    salaries = load_salaries()
    payroll = 0.0
    league_war = 0.0
    matched = 0
    for row in rows:
        if float(row.get("mp") or 0) <= 0:
            continue
        slug = crosswalk.get(int(row["player_id"]))
        if slug is None or slug not in salaries:
            continue
        payroll += salaries[slug]["salary"]
        league_war += calculate_war(
            float(row["epm"]), float(row["mp"]), PACE,
            player_name=row.get("player_name"),
        )
        matched += 1

    derived = derive_dollars_per_win(payroll, league_war)
    ok = approx(derived, 7.86e6, 0.1e6)
    report(
        "GATE 5 dollars/win reproduction",
        ok,
        f"matched {matched} players (exp ~365), payroll ${payroll/1e9:.3f}B "
        f"(exp ≈$5.325B), WAR {league_war:.1f}, derived "
        f"${derived/1e6:.3f}M/win (exp 7.86±0.1); stored constant "
        f"${RATE_25_26/1e6:.2f}M",
    )
    return (crosswalk,)


# ----- gate 6: zero-centering ----------------------------------------------


def gate_zero_centering(rows: list[dict]) -> None:
    # WAA measured against league average 0 — NO replacement offset — over
    # the FULL API population with mp > 0 (not the salary-matched subset).
    total_waa = 0.0
    n = 0
    for row in rows:
        mp = float(row.get("mp") or 0)
        if mp <= 0:
            continue
        total_waa += (
            float(row["epm"]) * (mp * PACE / 48.0) / 100.0
            / CLEAN_CONSTANTS.points_per_win
        )
        n += 1
    report(
        "GATE 6 zero-centering",
        approx(total_waa, 0.0, 5.0),
        f"ΣWAA over {n} players with mp>0 = {total_waa:+.2f} wins (exp 0±5)",
    )


# ----- spec gate: pointsPerWin refit (dataset not in repo) -----------------


def gate_points_per_win_refit() -> None:
    # Spec §4 requires refitting pointsPerWin on the 90-team-season dataset
    # (expect 36.7 ±0.3). That dataset is not committed anywhere in this
    # repo, so the gate cannot run here — skipped, not silently dropped.
    report(
        "SPEC GATE pointsPerWin refit",
        None,
        "90-team-season standings dataset not present in the repo; the "
        "36.7 refit cannot be reproduced from committed data",
    )


# ----- join audit (informational; hard-fails only on the named four) -------

MUST_MATCH = {
    "victor wembanyama": "Wembanyama",
    "kyle kuzma": "Kuzma",
    "ronald holland ii": "Ron Holland",
    "ron holland": "Ron Holland",
    "egor demin": "Egor Dëmin",
}


def join_audit(rows: list[dict], crosswalk: dict[int, str] | None) -> None:
    if crosswalk is None:
        print("[INFO] join audit: skipped (no DB crosswalk available)")
        return
    salaries = load_salaries()
    unmatched: list[tuple[str, str]] = []
    for row in rows:
        if float(row.get("mp") or 0) <= 0:
            continue
        slug = crosswalk.get(int(row["player_id"]))
        if slug is None:
            unmatched.append((row["player_name"], "no-crosswalk-entry"))
        elif slug not in salaries:
            unmatched.append((row["player_name"], "no-salary-row"))
    print(f"[INFO] join audit: {len(unmatched)} EPM players with mp>0 unmatched")
    for name, reason in sorted(unmatched):
        print(f"       {name}: {reason}")
    critical = [
        name for name, _ in unmatched if _normalize(name) in MUST_MATCH
    ]
    report(
        "JOIN AUDIT critical players",
        not critical,
        "Wembanyama/Kuzma/Holland/Dëmin all matched"
        if not critical
        else f"CRITICAL unmatched: {critical}",
    )


def main() -> int:
    rows = load_epm_api_cache(season=2026)
    print(
        f"clean-engine gauntlet — {len(rows)} API EPM rows, pace {PACE}, "
        f"rates ${RATE_25_26/1e6:.2f}M (25-26) / ${RATE_26_27/1e6:.2f}M (26-27)\n"
    )
    gate_pins()
    gate_source_sanity(rows)
    (crosswalk,) = gate_dollars_reproduction(rows)
    gate_zero_centering(rows)
    gate_points_per_win_refit()
    join_audit(rows, crosswalk)

    failures = [g for g, ok in RESULTS if ok is False]
    skips = [g for g, ok in RESULTS if ok is None]
    print(
        f"\n{len(RESULTS) - len(failures) - len(skips)} passed, "
        f"{len(failures)} failed, {len(skips)} skipped"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
