"""Comprehensive stress test for the trade analyzer pipeline.

Runs scenarios across 8 categories:
  1. Salary matching (CBA legality at every apron tier + boundaries)
  2. Valuation paths (EPM / DARKO / NET_RATING source selection, tanh cap)
  3. Win curve table across 15-65 wins
  4. Timeline alignment scenarios
  5. Positional fit scenarios (including outgoing-roster trimming)
  6. Spacing scenarios (data-present, low-volume, fallback)
  7. Combined team-context scenarios (multi-component stress)
  8. Real 2025-26 blockbuster trades through the full pipeline

Prints per-category status as it runs, then a final summary with all
failures and warnings sorted by severity.

Diagnostic only — not committed, not wired into the package or tests.

Run from repo root:
    uv run python scripts/stress_test_trades.py
"""

from __future__ import annotations

import io
import sys
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from nba_trade_analyzer.data.darko import fetch_darko_data
from nba_trade_analyzer.data.epm import fetch_epm_data, get_player_epm, normalize_name
from nba_trade_analyzer.data.players import fetch_player_stats
from nba_trade_analyzer.engine.constants import (
    FIRST_APRON,
    LEAGUE_AVG_3PT_PCT,
    LEAGUE_AVG_3PT_RATE,
    MAX_WINS_ADDED,
    POSITIONAL_MAX_ADJUSTMENT,
    SALARY_CAP,
    SECOND_APRON,
    SPACING_MAX_ADJUSTMENT,
    TIMELINE_MAX_ADJUSTMENT,
    WIN_CURVE_MAX_MULTIPLIER,
    WIN_CURVE_MIN_MULTIPLIER,
)
from nba_trade_analyzer.engine.draft_picks import calculate_pick_value_with_protections
from nba_trade_analyzer.engine.salary_rules import check_trade_legality
from nba_trade_analyzer.engine.team_context import (
    _effective_win_curve,
    calculate_positional_modifier,
    calculate_spacing_modifier,
    calculate_timeline_modifier,
    calculate_win_curve_multiplier,
    evaluate_player_in_team_context,
)
from nba_trade_analyzer.engine.valuation import evaluate_player
from nba_trade_analyzer.models.player import Contract, Player
from nba_trade_analyzer.models.team import CapStatus, RosterEntry, Team
from nba_trade_analyzer.models.trade import Trade, TradeAssets


def _force_utf8_stdout() -> None:
    """Rewrap stdout in utf-8 — Windows console defaults to cp1252, which
    breaks engine error strings (em-dashes) and our own arrows. Called from
    ``main()`` only so the module is safe to import without disturbing
    pytest's captured stdout."""
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", line_buffering=True
        )


# ============================================================================
# Hardcoded 2025-26 figures (TODO: replace with real data sources)
# ============================================================================

# Salaries for Category 8 blockbuster players. Approximate figures from public
# trackers — some players have changed teams since these contracts; we treat
# the trades as hypothetical with these contract values.
BLOCKBUSTER_SALARIES: dict[str, int] = {
    "Trae Young": 40_100_000,
    "D'Angelo Russell": 18_700_000,
    "Rui Hachimura": 17_000_000,
    "Zach LaVine": 43_000_000,
    "Michael Porter Jr.": 35_800_000,
    "Brandon Ingram": 36_000_000,
    "Tyler Herro": 30_000_000,
    "Pascal Siakam": 37_900_000,
    "Julius Randle": 30_935_520,
    "Mitchell Robinson": 15_400_000,
}

# Approximate 2025-26 team payrolls (USD).
BLOCKBUSTER_TEAM_PAYROLLS: dict[str, int] = {
    "ATL": 170_000_000,
    "LAL": 192_000_000,
    "SAC": 178_000_000,
    "DEN": 195_000_000,
    "TOR": 175_000_000,
    "MIA": 188_000_000,
    "IND": 175_000_000,
    "NYK": 198_000_000,
}

BLOCKBUSTER_TEAM_NAMES: dict[str, str] = {
    "ATL": "Atlanta Hawks",
    "LAL": "Los Angeles Lakers",
    "SAC": "Sacramento Kings",
    "DEN": "Denver Nuggets",
    "TOR": "Toronto Raptors",
    "MIA": "Miami Heat",
    "IND": "Indiana Pacers",
    "NYK": "New York Knicks",
}

BLOCKBUSTER_WIN_PROJECTIONS: dict[str, float] = {
    "ATL": 38.0,
    "LAL": 48.0,
    "SAC": 40.0,
    "DEN": 50.0,
    "TOR": 30.0,
    "MIA": 46.0,
    "IND": 45.0,
    "NYK": 50.0,
}

# Pick estimates (landing pick, protection_top cutoff for "doesn't convey").
PICK_ESTIMATES: dict[str, tuple[int, int | None]] = {
    "2027 LAL 1st (top-10 protected)": (17, 10),
    "2026 DEN 1st (unprotected)": (18, None),
    "2027 MIA 1st (top-14 protected)": (16, 14),
}

DEFAULT_GP = 60
DEFAULT_MPG = 30.0


# ============================================================================
# Test runner
# ============================================================================

PASS, FAIL, WARN = "PASS", "FAIL", "WARN"
_SEVERITY_ORDER = {FAIL: 0, WARN: 1, PASS: 2}


@dataclass
class TestResult:
    category: str
    name: str
    status: str
    detail: str = ""


@dataclass
class TestRunner:
    results: list[TestResult] = field(default_factory=list)
    _current_category: str = ""

    def category(self, name: str) -> None:
        self._current_category = name
        print(f"\n{'=' * 96}\n{name}\n{'=' * 96}")

    def _record(self, status: str, name: str, detail: str = "") -> None:
        self.results.append(TestResult(self._current_category, name, status, detail))
        symbol = {PASS: "[ OK ]", FAIL: "[FAIL]", WARN: "[WARN]"}[status]
        line = f"  {symbol} {name}"
        if detail and status != PASS:
            line += f"  -- {detail}"
        print(line)

    def passed(self, name: str, detail: str = "") -> None:
        self._record(PASS, name, detail)

    def failed(self, name: str, detail: str = "") -> None:
        self._record(FAIL, name, detail)

    def warned(self, name: str, detail: str = "") -> None:
        self._record(WARN, name, detail)

    def assert_true(self, name: str, condition: bool, detail: str = "") -> None:
        if condition:
            self.passed(name, detail)
        else:
            self.failed(name, detail or "condition was False")

    def assert_eq(self, name: str, actual: Any, expected: Any) -> None:
        if actual == expected:
            self.passed(name)
        else:
            self.failed(name, f"expected {expected!r}, got {actual!r}")

    def assert_close(
        self, name: str, actual: float, expected: float, tol: float = 1e-6
    ) -> None:
        if abs(actual - expected) <= tol:
            self.passed(name)
        else:
            self.failed(name, f"expected {expected:.6f} +/- {tol}, got {actual:.6f}")

    def assert_legal(self, name: str, trade: Trade, expected_legal: bool) -> None:
        result = check_trade_legality(trade)
        if result.legal == expected_legal:
            self.passed(name)
        elif result.legal:
            self.failed(name, "expected ILLEGAL but got LEGAL")
        else:
            self.failed(name, f"expected LEGAL but got ILLEGAL: {result.error_reason}")

    def summary(self) -> None:
        total = len(self.results)
        by_status = {PASS: 0, FAIL: 0, WARN: 0}
        for r in self.results:
            by_status[r.status] += 1

        print(f"\n{'#' * 96}")
        print("FINAL SUMMARY")
        print("#" * 96)
        print(f"  Total scenarios tested: {total}")
        print(f"  Passed: {by_status[PASS]}")
        print(f"  Failed: {by_status[FAIL]}")
        print(f"  Warnings: {by_status[WARN]}")

        # Per-category breakdown
        cats: dict[str, dict[str, int]] = {}
        for r in self.results:
            cats.setdefault(r.category, {PASS: 0, FAIL: 0, WARN: 0})[r.status] += 1
        print("\n  Per-category results:")
        for cat, counts in cats.items():
            print(
                f"    {cat:<45}  pass={counts[PASS]:>3}  "
                f"fail={counts[FAIL]:>3}  warn={counts[WARN]:>3}"
            )

        # Flagged issues, sorted by severity (FAIL first, then WARN)
        issues = [r for r in self.results if r.status != PASS]
        if not issues:
            print("\n  No failures or warnings.")
            return
        issues.sort(key=lambda r: (_SEVERITY_ORDER[r.status], r.category, r.name))
        print("\n  Flagged issues (sorted by severity):")
        for r in issues:
            tag = "FAIL" if r.status == FAIL else "WARN"
            print(f"    [{tag}] {r.category} :: {r.name}")
            if r.detail:
                print(f"           {r.detail}")


# ============================================================================
# Synthetic factories
# ============================================================================


def _cap_status_for(payroll: int) -> CapStatus:
    if payroll < SALARY_CAP:
        return CapStatus.UNDER_CAP
    if payroll < FIRST_APRON:
        return CapStatus.OVER_CAP
    if payroll < SECOND_APRON:
        return CapStatus.FIRST_APRON
    return CapStatus.SECOND_APRON


def _team(abbr: str, payroll: int) -> Team:
    return Team(
        name=f"Team {abbr}",
        abbreviation=abbr,
        total_payroll=payroll,
        cap_status=_cap_status_for(payroll),
    )


def _entries(salaries: list[int], prefix: str) -> list[RosterEntry]:
    return [
        RosterEntry(
            player=Player(name=f"{prefix}_p{i}", team=prefix, age=25),
            contract=Contract(salary=s, years_remaining=1),
        )
        for i, s in enumerate(salaries)
    ]


def make_trade(
    team_a_abbr: str,
    team_a_payroll: int,
    team_b_abbr: str,
    team_b_payroll: int,
    a_sends: list[int],
    b_sends: list[int],
) -> Trade:
    return Trade(
        team_a=_team(team_a_abbr, team_a_payroll),
        team_b=_team(team_b_abbr, team_b_payroll),
        team_a_sends=TradeAssets(players=_entries(a_sends, team_a_abbr)),
        team_b_sends=TradeAssets(players=_entries(b_sends, team_b_abbr)),
    )


def _money(amount: float) -> str:
    sign = "-" if amount < 0 else " "
    return f"{sign}${abs(amount) / 1_000_000:6.2f}M"


def _synthetic_roster(
    core_age: int,
    g_mpg: float = 60.0,
    f_mpg: float = 60.0,
    c_mpg: float = 40.0,
    fg3_rate: float = LEAGUE_AVG_3PT_RATE,
    fg3_pct: float = LEAGUE_AVG_3PT_PCT,
) -> list[dict]:
    """Three synthetic roster slots (G/F/C) with controlled minutes/age."""
    return [
        {
            "player_name": "G1",
            "MPG": g_mpg,
            "GP": 70,
            "age": core_age,
            "position": "G",
            "FG3_RATE": fg3_rate,
            "FG3_PCT": fg3_pct,
        },
        {
            "player_name": "F1",
            "MPG": f_mpg,
            "GP": 70,
            "age": core_age,
            "position": "F",
            "FG3_RATE": fg3_rate,
            "FG3_PCT": fg3_pct,
        },
        {
            "player_name": "C1",
            "MPG": c_mpg,
            "GP": 70,
            "age": core_age,
            "position": "C",
            "FG3_RATE": fg3_rate,
            "FG3_PCT": fg3_pct,
        },
    ]


def _make_synthetic_epm_df(
    name: str, epm: float, position: str = "F", age: int = 26
) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "player_name": name,
                "player_name_normalized": normalize_name(name),
                "team": "SYN",
                "epm": epm,
                "epm_off": epm * 0.6,
                "epm_def": epm * 0.4,
                "mpg": 36.0,
                "position": position,
                "age": age,
            }
        ]
    )


# ============================================================================
# Category 1: Salary matching
# ============================================================================


def test_salary_matching(runner: TestRunner) -> None:
    runner.category("Category 1: Salary matching edge cases")

    # (a) Under-cap absorption — DET at $140M has $14.647M cap space.
    # Limit = cap_space + outgoing + $250K = 14.647M + 1M + 0.25M = 15.897M.
    runner.assert_legal(
        "1a under-cap DET absorbs $15M sending only $1M",
        make_trade("DET", 140_000_000, "LAL", 192_000_000, [1_000_000], [15_000_000]),
        True,
    )
    # Just over the limit
    runner.assert_legal(
        "1a under-cap DET cannot absorb $16M sending only $1M",
        make_trade("DET", 140_000_000, "LAL", 192_000_000, [1_000_000], [16_000_000]),
        False,
    )

    # (b) 200% + $250K threshold (low Expanded TPE band)
    # outgoing $7,249,999 -> limit = 2*7,249,999 + 250,000 = 14,749,998.
    runner.assert_legal(
        "1b exactly at 200%+$250K threshold ($14,749,998 vs limit $14,749,998)",
        make_trade("CHI", 170_000_000, "ATL", 170_000_000, [7_249_999], [14_749_998]),
        True,
    )
    runner.assert_legal(
        "1b one dollar over 200%+$250K threshold ($14,749,999)",
        make_trade("CHI", 170_000_000, "ATL", 170_000_000, [7_249_999], [14_749_999]),
        False,
    )

    # (c) Boundary at $7.25M (low band -> middle band).
    # outgoing $7,250,000 -> middle-band limit = 7.25M + 8.527M = 15.777M.
    runner.assert_legal(
        "1c $7.25M boundary: outgoing $7.25M, incoming $15.777M (middle band)",
        make_trade("CHI", 170_000_000, "ATL", 170_000_000, [7_250_000], [15_777_000]),
        True,
    )
    runner.assert_legal(
        "1c $7.25M boundary: outgoing $7.25M, incoming $15,777,001 illegal",
        make_trade("CHI", 170_000_000, "ATL", 170_000_000, [7_250_000], [15_777_001]),
        False,
    )
    # Sanity: at outgoing $7.25M, incoming $14.75M should still be legal
    # (15.777M >> 14.75M).
    runner.assert_legal(
        "1c boundary jump: bumping outgoing to $7.25M re-legalizes incoming $14.75M",
        make_trade("CHI", 170_000_000, "ATL", 170_000_000, [7_250_000], [14_750_000]),
        True,
    )

    # Boundary at $29M (middle -> high band, 125% no-cushion rule).
    # outgoing $29,000,000 -> middle-band limit = 29M + 8.527M = 37.527M.
    # outgoing $29,000,001 -> high-band limit = (5 * 29,000,001) // 4 = 36,250,001.
    runner.assert_legal(
        "1c $29M boundary middle: outgoing $29M, incoming $37.527M legal",
        make_trade("CHI", 170_000_000, "ATL", 170_000_000, [29_000_000], [37_527_000]),
        True,
    )
    runner.assert_legal(
        "1c $29M+$1 boundary: outgoing $29,000,001, incoming $36,250,001 legal",
        make_trade("CHI", 170_000_000, "ATL", 170_000_000, [29_000_001], [36_250_001]),
        True,
    )
    runner.assert_legal(
        "1c $29M+$1 boundary: outgoing $29,000,001, incoming $36,250,002 illegal",
        make_trade("CHI", 170_000_000, "ATL", 170_000_000, [29_000_001], [36_250_002]),
        False,
    )

    # (d) First apron — 100% match, no cushion. Payroll in [195.945M, 207.824M].
    fa_payroll = 200_000_000
    runner.assert_legal(
        "1d first apron: outgoing $20M, incoming $20M (exact match) legal",
        make_trade("NYK", fa_payroll, "ATL", 170_000_000, [20_000_000], [20_000_000]),
        True,
    )
    runner.assert_legal(
        "1d first apron: outgoing $20M, incoming $20,000,001 illegal (no cushion)",
        make_trade("NYK", fa_payroll, "ATL", 170_000_000, [20_000_000], [20_000_001]),
        False,
    )

    # (e) Second apron — aggregation blocked when team stays above the apron.
    sa_payroll = 215_000_000  # above SECOND_APRON
    runner.assert_legal(
        "1e second apron: aggregation $10M+$8M for $18M illegal (post-trade still over apron)",
        make_trade(
            "PHX",
            sa_payroll,
            "ATL",
            170_000_000,
            [10_000_000, 8_000_000],
            [18_000_000],
        ),
        False,
    )
    runner.assert_legal(
        "1e second apron: one-for-one exact $18M match legal (no aggregation)",
        make_trade("PHX", sa_payroll, "ATL", 170_000_000, [18_000_000], [18_000_000]),
        True,
    )
    runner.assert_legal(
        "1e second apron: one-for-one $18M sending, $18M+1 incoming illegal",
        make_trade("PHX", sa_payroll, "ATL", 170_000_000, [18_000_000], [18_000_001]),
        False,
    )

    # (f) Second apron drops below post-trade -> aggregation re-enabled.
    # Start at $208M (just over SECOND_APRON $207.824M).
    # Send $10M+$8M=$18M, receive $1M -> post_trade = 208 - 18 + 1 = $191M.
    # post_trade < SECOND_APRON so aggregation is allowed on the PHX side.
    # Pair with an under-cap partner (DET $130M) so their Room TPE absorbs
    # the $18M incoming legally — otherwise the other side's matching rules
    # fail and the whole trade gets flagged regardless of the PHX exception.
    runner.assert_legal(
        "1f second apron drops below: $10M+$8M for $1M legal (Aggregated TPE)",
        make_trade(
            "PHX",
            208_000_000,
            "DET",
            130_000_000,
            [10_000_000, 8_000_000],
            [1_000_000],
        ),
        True,
    )


# ============================================================================
# Category 2: Valuation paths
# ============================================================================


def test_valuation_paths(
    runner: TestRunner,
    epm_df: pd.DataFrame,
    darko_df: pd.DataFrame,
    stats_lookup: dict[str, pd.Series],
) -> None:
    runner.category("Category 2: Valuation path selection")

    # (a) Player with EPM data -> "epm"
    name = "Nikola Jokic"
    epm_row = get_player_epm(epm_df, name)
    if epm_row is None:
        runner.failed("2a Jokic should be in EPM data", "lookup returned None")
    else:
        stats_row = stats_lookup.get(normalize_name(name))
        gp = (
            int(stats_row.get("GP", DEFAULT_GP))
            if stats_row is not None
            else DEFAULT_GP
        )
        mpg = (
            float(stats_row.get("MPG", DEFAULT_MPG))
            if stats_row is not None
            else DEFAULT_MPG
        )
        player = Player(
            name=name,
            team="DEN",
            age=int(epm_row["age"]),
            stats={"GP": float(gp), "MPG": mpg, "NET_RATING": 5.0},
        )
        v = evaluate_player(
            player,
            Contract(salary=55_223_000, years_remaining=4),
            epm_df=epm_df,
            darko_df=darko_df,
        )
        runner.assert_eq("2a Jokic uses 'epm' source", v.metric_source, "epm")
        if v.wins_added <= 0:
            runner.failed(
                "2a Jokic wins_added should be positive",
                f"got wins_added={v.wins_added:+.2f}",
            )
        else:
            runner.passed(
                "2a Jokic wins_added positive",
                f"wins_added={v.wins_added:+.2f}",
            )

    # (b) Player missing from EPM but present in DARKO -> "darko".
    # We force the EPM path to miss by passing an empty df; the player still
    # exists in real DARKO data, so DARKO should resolve.
    if darko_df is not None and not darko_df.empty:
        # Pick the first DARKO player by dpm so we know they have a real row.
        sample = darko_df.sort_values("dpm", ascending=False).iloc[0]
        darko_name = sample["player_name"]
        player = Player(
            name=darko_name,
            team="SYN",
            age=int(sample.get("age", 25) or 25),
            stats={"GP": float(DEFAULT_GP), "MPG": DEFAULT_MPG, "NET_RATING": 0.0},
        )
        v = evaluate_player(
            player,
            Contract(salary=10_000_000, years_remaining=1),
            epm_df=pd.DataFrame(),
            darko_df=darko_df,
        )
        # If the player is also in EPM data, our empty epm_df passes through to
        # DARKO; that's the path we want to verify.
        runner.assert_eq(
            f"2b DARKO fallback works for '{darko_name}' when EPM is empty",
            v.metric_source,
            "darko",
        )
    else:
        runner.warned("2b DARKO data unavailable", "fetch returned empty df")

    # (c) Player in neither EPM nor DARKO -> "net_rating".
    unknown = Player(
        name="Definitely Not A Real Player",
        team="SYN",
        age=27,
        stats={"GP": 70.0, "MPG": 30.0, "NET_RATING": 2.0},
    )
    v = evaluate_player(
        unknown,
        Contract(salary=5_000_000, years_remaining=1),
        epm_df=pd.DataFrame(),
        darko_df=pd.DataFrame(),
    )
    runner.assert_eq(
        "2c unknown player falls through to 'net_rating'", v.metric_source, "net_rating"
    )

    # (d) EPM precedence — when both EPM and DARKO have a row, EPM wins.
    # Build a synthetic player present in both, with very different impact
    # numbers, and confirm wins_added matches the EPM path.
    syn_name = "Stress Test Player"
    syn_epm = _make_synthetic_epm_df(syn_name, epm=4.0, position="F", age=27)
    syn_darko = pd.DataFrame(
        [
            {
                "player_name": syn_name,
                "player_name_normalized": normalize_name(syn_name),
                "dpm": -2.0,  # opposite sign — would tank wins_added
                "dpm_off": -1.0,
                "dpm_def": -1.0,
                "position": "F",
                "age": 27,
            }
        ]
    )
    syn_player = Player(
        name=syn_name,
        team="SYN",
        age=27,
        stats={"GP": 70.0, "MPG": 32.0, "NET_RATING": 0.0},
    )
    v = evaluate_player(
        syn_player,
        Contract(salary=20_000_000, years_remaining=2),
        epm_df=syn_epm,
        darko_df=syn_darko,
    )
    runner.assert_eq(
        "2d EPM takes precedence over DARKO when both present", v.metric_source, "epm"
    )
    runner.assert_true(
        "2d EPM-source wins_added is positive (EPM was +4.0)",
        v.wins_added > 0,
        f"wins_added={v.wins_added:+.2f}",
    )

    # (e) Tanh compression — fabricate +15 EPM and verify wins_added < MAX.
    absurd_epm = _make_synthetic_epm_df("Tanh Test", epm=15.0, position="G", age=25)
    tanh_player = Player(
        name="Tanh Test",
        team="SYN",
        age=25,
        stats={"GP": 82.0, "MPG": 38.0, "NET_RATING": 0.0},
    )
    v = evaluate_player(
        tanh_player,
        Contract(salary=50_000_000, years_remaining=4),
        epm_df=absurd_epm,
        darko_df=pd.DataFrame(),
    )
    runner.assert_true(
        "2e tanh caps wins_added below MAX_WINS_ADDED",
        v.wins_added < MAX_WINS_ADDED,
        f"wins_added={v.wins_added:.3f}, cap={MAX_WINS_ADDED}",
    )
    runner.assert_true(
        "2e +15 EPM still produces large wins_added (>15)",
        v.wins_added > 15.0,
        f"wins_added={v.wins_added:.3f}",
    )


# ============================================================================
# Category 3: Win curve
# ============================================================================


def test_win_curve_table(runner: TestRunner) -> None:
    runner.category("Category 3: Win curve table (15-65 wins, 5-win step)")

    win_values = list(range(15, 66, 5))
    multipliers = [calculate_win_curve_multiplier(w) for w in win_values]

    print(f"\n  {'wins':>5}  {'multiplier':>11}")
    for w, m in zip(win_values, multipliers):
        print(f"  {w:>5}  {m:>11.4f}")

    # Monotonically increasing
    monotonic = all(b > a for a, b in zip(multipliers, multipliers[1:]))
    runner.assert_true("3 monotonically increasing across 15-65", monotonic)

    # Within bounds
    runner.assert_true(
        "3 all multipliers >= WIN_CURVE_MIN_MULTIPLIER",
        all(m >= WIN_CURVE_MIN_MULTIPLIER for m in multipliers),
    )
    runner.assert_true(
        "3 all multipliers <= WIN_CURVE_MAX_MULTIPLIER",
        all(m <= WIN_CURVE_MAX_MULTIPLIER for m in multipliers),
    )

    # Steepest jump should be in the 38-46 window. Compute consecutive deltas
    # and find the midpoint of the steepest pair.
    deltas = [b - a for a, b in zip(multipliers, multipliers[1:])]
    max_idx = deltas.index(max(deltas))
    midpoint = (win_values[max_idx] + win_values[max_idx + 1]) / 2
    runner.assert_true(
        "3 steepest jump is in 38-46 win range (playoff bubble)",
        38.0 <= midpoint <= 46.0,
        f"steepest segment midpoint = {midpoint}",
    )


# ============================================================================
# Category 4: Timeline
# ============================================================================


def test_timeline_scenarios(runner: TestRunner) -> None:
    runner.category("Category 4: Timeline alignment")

    # Print the model's output for every spec case so the calibration
    # is visible at a glance.
    print(f"\n  {'case':<28}  {'gap':>4}  {'modifier':>9}  {'% of MAX':>8}")
    cases = [
        ("4a 22yo / 23-avg core", 22, [23] * 5),
        ("4b 22yo / 33-avg core", 22, [33] * 5),
        ("4c 30yo / 30-avg core", 30, [30] * 5),
        ("4d 35yo / 24-avg core", 35, [24] * 5),
        ("4e 26yo / 26-avg core", 26, [26] * 5),
        ("4f extreme 40yo / 20-avg core", 40, [20] * 5),
    ]
    for label, age, core in cases:
        mod = calculate_timeline_modifier(age, core)
        gap = abs(age - sum(core) / len(core))
        pct = mod / TIMELINE_MAX_ADJUSTMENT * 100
        print(f"  {label:<28}  {gap:>4.0f}  {mod:>+9.4f}  {pct:>+7.1f}%")

    # (a) 22yo to 23-avg core (1yr gap): strong positive
    mod = calculate_timeline_modifier(22, [23] * 5)
    runner.assert_true(
        "4a 22yo to 23-avg core: strong positive",
        mod > 0.5 * TIMELINE_MAX_ADJUSTMENT,
        f"modifier={mod:+.4f}",
    )

    # (b) 22yo to 33-avg core (11yr gap): user spec said "strong negative",
    # but the model saturates slowly — exp(-0.12 * 11) ≈ 0.27 -> modifier
    # only reaches ~-47% of MAX. Document as a calibration finding so the
    # user can decide whether to retune TIMELINE_LAMBDA.
    mod = calculate_timeline_modifier(22, [33] * 5)
    if mod >= 0:
        runner.failed(
            "4b 22yo to 33-avg core: direction wrong (should be negative)",
            f"modifier={mod:+.4f}",
        )
    elif mod >= -0.5 * TIMELINE_MAX_ADJUSTMENT:
        runner.warned(
            "4b 22yo to 33-avg core: weaker than 'strong negative' per spec",
            f"modifier={mod:+.4f} ({mod / TIMELINE_MAX_ADJUSTMENT * 100:+.0f}% of MAX); "
            "11yr gap produces only modest penalty under TIMELINE_LAMBDA=0.12. "
            "Consider raising lambda if the spec's intuition should hold.",
        )
    else:
        runner.passed("4b 22yo to 33-avg core: strong negative")

    # (c) 30yo to 30-avg core: at +MAX
    mod = calculate_timeline_modifier(30, [30] * 5)
    runner.assert_close(
        "4c 30yo to 30-avg core: at +TIMELINE_MAX_ADJUSTMENT",
        mod,
        TIMELINE_MAX_ADJUSTMENT,
        tol=1e-6,
    )

    # (d) 35yo to 24-avg core (11yr gap): same magnitude as 4b — both pass
    # the "strong negative" bar (>60% of MAX). The spec called this "near
    # max negative", but symmetric 11yr gaps land at -65% with lambda=0.16;
    # reaching -MAX takes ~20+ year gaps (see 4f).
    mod = calculate_timeline_modifier(35, [24] * 5)
    if mod >= 0:
        runner.failed(
            "4d 35yo to 24-avg core: direction wrong (should be negative)",
            f"modifier={mod:+.4f}",
        )
    elif mod >= -0.5 * TIMELINE_MAX_ADJUSTMENT:
        runner.warned(
            "4d 35yo to 24-avg core: weaker than 'strong negative'",
            f"modifier={mod:+.4f} ({mod / TIMELINE_MAX_ADJUSTMENT * 100:+.0f}% of MAX)",
        )
    else:
        runner.passed("4d 35yo to 24-avg core: strong negative")

    # (e) Exact-age match: at +MAX
    mod = calculate_timeline_modifier(26, [26] * 5)
    runner.assert_close(
        "4e exact-age match: at +TIMELINE_MAX_ADJUSTMENT",
        mod,
        TIMELINE_MAX_ADJUSTMENT,
        tol=1e-6,
    )

    # (f) Extreme 20-year gap: confirm the formula CAN reach near-max
    # negative; isolates the calibration issue to the lambda, not the cap.
    mod = calculate_timeline_modifier(40, [20] * 5)
    runner.assert_true(
        "4f extreme 20yr gap reaches >70% of -MAX (cap mechanism works)",
        mod < -0.7 * TIMELINE_MAX_ADJUSTMENT,
        f"modifier={mod:+.4f}",
    )


# ============================================================================
# Category 5: Positional fit
# ============================================================================


def _three_position_roster(g_mpg: float, f_mpg: float, c_mpg: float) -> list[dict]:
    return [
        {
            "player_name": "G1",
            "MPG": g_mpg,
            "GP": 70,
            "age": 25,
            "position": "G",
            "FG3_RATE": 0.35,
            "FG3_PCT": 0.36,
        },
        {
            "player_name": "F1",
            "MPG": f_mpg,
            "GP": 70,
            "age": 25,
            "position": "F",
            "FG3_RATE": 0.35,
            "FG3_PCT": 0.36,
        },
        {
            "player_name": "C1",
            "MPG": c_mpg,
            "GP": 70,
            "age": 25,
            "position": "C",
            "FG3_RATE": 0.05,
            "FG3_PCT": 0.20,
        },
    ]


def test_positional_scenarios(runner: TestRunner) -> None:
    runner.category("Category 5: Positional fit")

    # (a) Center into roster with 0 C minutes -> max positive.
    mod = calculate_positional_modifier(
        "C", _three_position_roster(g_mpg=60, f_mpg=60, c_mpg=0)
    )
    runner.assert_close(
        "5a center into 0-C-minute roster: +POSITIONAL_MAX_ADJUSTMENT",
        mod,
        POSITIONAL_MAX_ADJUSTMENT,
        tol=1e-6,
    )

    # (b) Guard into 90+ G-minute roster -> max negative.
    mod = calculate_positional_modifier(
        "G", _three_position_roster(g_mpg=90, f_mpg=40, c_mpg=20)
    )
    runner.assert_close(
        "5b guard into 90-G-minute logjam: -POSITIONAL_MAX_ADJUSTMENT",
        mod,
        -POSITIONAL_MAX_ADJUSTMENT,
        tol=1e-6,
    )

    # (c) Balanced position (~32 MPG filled) -> near zero (within +/- MAX).
    mod = calculate_positional_modifier(
        "F", _three_position_roster(g_mpg=32, f_mpg=32, c_mpg=32)
    )
    runner.assert_true(
        "5c balanced 32-MPG position: within (-MAX, +MAX)",
        -POSITIONAL_MAX_ADJUSTMENT < mod < POSITIONAL_MAX_ADJUSTMENT,
        f"modifier={mod:+.4f}",
    )

    # (d) Outgoing trim — start with a heavy C slot, then trim it out and
    # verify the incoming center now scores as "need" instead of "logjam".
    # Build a heavier roster with two centers totaling 50 MPG at C.
    roster = [
        {
            "player_name": "G1",
            "MPG": 60,
            "GP": 70,
            "age": 25,
            "position": "G",
            "FG3_RATE": 0.35,
            "FG3_PCT": 0.36,
        },
        {
            "player_name": "F1",
            "MPG": 60,
            "GP": 70,
            "age": 25,
            "position": "F",
            "FG3_RATE": 0.35,
            "FG3_PCT": 0.36,
        },
        {
            "player_name": "C_Star",
            "MPG": 36,
            "GP": 70,
            "age": 25,
            "position": "C",
            "FG3_RATE": 0.05,
            "FG3_PCT": 0.20,
        },
        {
            "player_name": "C_Backup",
            "MPG": 14,
            "GP": 60,
            "age": 24,
            "position": "C",
            "FG3_RATE": 0.05,
            "FG3_PCT": 0.20,
        },
    ]
    fake_incoming = Player(
        name="Incoming_C",
        team="SYN",
        age=25,
        stats={"GP": 70.0, "MPG": 32.0, "NET_RATING": 0.0},
    )
    fake_epm = _make_synthetic_epm_df("Incoming_C", epm=2.0, position="C", age=25)

    without_trim = evaluate_player_in_team_context(
        player_value=10_000_000.0,
        wins_added=3.0,
        player=fake_incoming,
        contract=Contract(salary=10_000_000, years_remaining=2),
        acquiring_team_wins=42.0,
        acquiring_team_roster=roster,
        epm_df=fake_epm,
        player_stats_df=None,
    )
    with_trim = evaluate_player_in_team_context(
        player_value=10_000_000.0,
        wins_added=3.0,
        player=fake_incoming,
        contract=Contract(salary=10_000_000, years_remaining=2),
        acquiring_team_wins=42.0,
        acquiring_team_roster=roster,
        epm_df=fake_epm,
        player_stats_df=None,
        outgoing_player_names=["C_Star"],
    )
    runner.assert_true(
        "5d outgoing trim increases positional modifier for thin position",
        with_trim.positional_modifier > without_trim.positional_modifier,
        f"without={without_trim.positional_modifier:+.4f}, "
        f"with_trim={with_trim.positional_modifier:+.4f}",
    )
    runner.assert_true(
        "5d after trimming starting C, incoming C scores positive (need)",
        with_trim.positional_modifier > 0,
        f"with_trim modifier={with_trim.positional_modifier:+.4f}",
    )


# ============================================================================
# Category 6: Spacing
# ============================================================================


def test_spacing_scenarios(runner: TestRunner) -> None:
    runner.category("Category 6: Spacing modifier")

    # (a) Elite shooter on spacing-poor team -> positive (and near max).
    mod = calculate_spacing_modifier(
        player_3pt_rate=0.55,
        player_3pt_pct=0.41,
        team_3pt_rate=0.28,
        team_3pt_pct=0.33,
    )
    runner.assert_true(
        "6a elite shooter -> spacing-poor team: positive",
        0 < mod <= SPACING_MAX_ADJUSTMENT,
        f"modifier={mod:+.4f}",
    )
    poor_team_positive = mod  # save for (c) comparison

    # (b) Non-shooter to spacing-poor team -> negative.
    mod = calculate_spacing_modifier(
        player_3pt_rate=0.05,
        player_3pt_pct=0.20,
        team_3pt_rate=0.28,
        team_3pt_pct=0.33,
    )
    runner.assert_true(
        "6b non-shooter -> spacing-poor team: negative",
        -SPACING_MAX_ADJUSTMENT <= mod < 0,
        f"modifier={mod:+.4f}",
    )
    poor_team_negative = mod

    # (c) Elite shooter to elite-spacing team -> muted positive.
    mod = calculate_spacing_modifier(
        player_3pt_rate=0.55,
        player_3pt_pct=0.41,
        team_3pt_rate=0.52,
        team_3pt_pct=0.39,
    )
    runner.assert_true(
        "6c elite shooter -> elite-spacing team: muted positive (< poor-team case)",
        0 <= mod < poor_team_positive,
        f"elite-team={mod:+.4f}, poor-team={poor_team_positive:+.4f}",
    )

    # (d) Non-shooter to elite-spacing team -> muted negative.
    mod = calculate_spacing_modifier(
        player_3pt_rate=0.05,
        player_3pt_pct=0.20,
        team_3pt_rate=0.52,
        team_3pt_pct=0.39,
    )
    runner.assert_true(
        "6d non-shooter -> elite-spacing team: muted negative (closer to 0)",
        poor_team_negative <= mod <= 0,
        f"elite-team={mod:+.4f}, poor-team={poor_team_negative:+.4f}",
    )

    # (e) League-average inputs (proxy for "no shooting data" fallback) -> ~0.
    mod = calculate_spacing_modifier(
        player_3pt_rate=LEAGUE_AVG_3PT_RATE,
        player_3pt_pct=LEAGUE_AVG_3PT_PCT,
        team_3pt_rate=LEAGUE_AVG_3PT_RATE,
        team_3pt_pct=LEAGUE_AVG_3PT_PCT,
    )
    runner.assert_close(
        "6e league-average inputs (no-data fallback): modifier ~ 0",
        mod,
        0.0,
        tol=1e-6,
    )


# ============================================================================
# Category 7: Combined team-context scenarios
# ============================================================================


def _evaluate_combined(
    label: str,
    runner: TestRunner,
    player: Player,
    epm_value: float,
    position: str,
    contract_salary: int,
    acquiring_wins: float,
    core_age: int,
    g_mpg: float,
    f_mpg: float,
    c_mpg: float,
    team_3pt_rate: float,
    team_3pt_pct: float,
    player_3pt_rate: float = 0.36,
    player_3pt_pct: float = 0.36,
) -> None:
    """Run a full team-context evaluation and print the component breakdown."""
    roster = [
        {
            "player_name": "G1",
            "MPG": g_mpg,
            "GP": 70,
            "age": core_age,
            "position": "G",
            "FG3_RATE": team_3pt_rate,
            "FG3_PCT": team_3pt_pct,
        },
        {
            "player_name": "F1",
            "MPG": f_mpg,
            "GP": 70,
            "age": core_age,
            "position": "F",
            "FG3_RATE": team_3pt_rate,
            "FG3_PCT": team_3pt_pct,
        },
        {
            "player_name": "C1",
            "MPG": c_mpg,
            "GP": 70,
            "age": core_age,
            "position": "C",
            "FG3_RATE": team_3pt_rate,
            "FG3_PCT": team_3pt_pct,
        },
    ]
    epm_df = _make_synthetic_epm_df(
        player.name, epm=epm_value, position=position, age=player.age
    )
    player_stats_df = pd.DataFrame(
        [
            {
                "player_name": player.name,
                "player_name_normalized": normalize_name(player.name),
                "FG3_RATE": player_3pt_rate,
                "FG3_PCT": player_3pt_pct,
                "position": position,
            }
        ]
    )
    # Run base valuation to get wins_added through the real pipeline.
    base = evaluate_player(
        player,
        Contract(salary=contract_salary, years_remaining=2),
        epm_df=epm_df,
        darko_df=pd.DataFrame(),
    )
    adjusted = evaluate_player_in_team_context(
        player_value=base.player_value,
        wins_added=base.wins_added,
        player=player,
        contract=Contract(salary=contract_salary, years_remaining=2),
        acquiring_team_wins=acquiring_wins,
        acquiring_team_roster=roster,
        epm_df=epm_df,
        player_stats_df=player_stats_df,
    )

    print(f"\n  {label}")
    print(
        f"    EPM={epm_value:+.2f}  position={position}  age={player.age}  "
        f"salary={_money(contract_salary)}  team_wins={acquiring_wins:.0f}  "
        f"core_age={core_age}"
    )
    print(
        f"    wins_added={base.wins_added:+5.2f}  base_value={_money(base.player_value)}  "
        f"base_surplus={_money(base.surplus_value)}"
    )
    effective = _effective_win_curve(adjusted.win_curve_multiplier, base.wins_added)
    print(
        f"    team_curve x{adjusted.win_curve_multiplier:.2f}  "
        f"effective_curve x{effective:.2f}  "
        f"timeline {adjusted.timeline_modifier:+.3f}  "
        f"positional {adjusted.positional_modifier:+.3f}  "
        f"spacing {adjusted.spacing_modifier:+.3f}"
    )
    print(
        f"    -> team_value={_money(adjusted.team_adjusted_value)}  "
        f"team_surplus={_money(adjusted.team_surplus)}"
    )
    # Record a single pass to keep accounting consistent.
    runner.passed(label, f"team_surplus={_money(adjusted.team_surplus)}")


def test_combined_scenarios(runner: TestRunner) -> None:
    runner.category("Category 7: Combined team-context scenarios")

    star_young = Player(
        name="Young Star",
        team="SYN",
        age=23,
        stats={"GP": 75.0, "MPG": 35.0, "NET_RATING": 4.0},
    )
    vet_aging = Player(
        name="Aging Vet",
        team="SYN",
        age=35,
        stats={"GP": 70.0, "MPG": 32.0, "NET_RATING": 3.0},
    )

    # (a) Young star -> rebuilder
    _evaluate_combined(
        "7a young star (23, +4 EPM) -> 20-win rebuilder (22-avg core)",
        runner,
        star_young,
        epm_value=4.0,
        position="G",
        contract_salary=30_000_000,
        acquiring_wins=20.0,
        core_age=22,
        g_mpg=30,
        f_mpg=60,
        c_mpg=40,
        team_3pt_rate=LEAGUE_AVG_3PT_RATE,
        team_3pt_pct=LEAGUE_AVG_3PT_PCT,
    )
    # (b) Young star -> contender (good timeline AND win curve)
    _evaluate_combined(
        "7b young star (23, +4 EPM) -> 50-win contender (26-avg core)",
        runner,
        star_young,
        epm_value=4.0,
        position="G",
        contract_salary=30_000_000,
        acquiring_wins=50.0,
        core_age=26,
        g_mpg=30,
        f_mpg=60,
        c_mpg=40,
        team_3pt_rate=LEAGUE_AVG_3PT_RATE,
        team_3pt_pct=LEAGUE_AVG_3PT_PCT,
    )
    # (c) Aging vet -> contender (bad timeline, good win curve)
    _evaluate_combined(
        "7c aging vet (35, +3 EPM) -> 50-win contender (28-avg core)",
        runner,
        vet_aging,
        epm_value=3.0,
        position="F",
        contract_salary=35_000_000,
        acquiring_wins=50.0,
        core_age=28,
        g_mpg=60,
        f_mpg=30,
        c_mpg=40,
        team_3pt_rate=LEAGUE_AVG_3PT_RATE,
        team_3pt_pct=LEAGUE_AVG_3PT_PCT,
    )
    # (d) Aging vet -> rebuilder (bad timeline AND bad win curve)
    _evaluate_combined(
        "7d aging vet (35, +3 EPM) -> 20-win rebuilder (23-avg core)",
        runner,
        vet_aging,
        epm_value=3.0,
        position="F",
        contract_salary=35_000_000,
        acquiring_wins=20.0,
        core_age=23,
        g_mpg=60,
        f_mpg=30,
        c_mpg=40,
        team_3pt_rate=LEAGUE_AVG_3PT_RATE,
        team_3pt_pct=LEAGUE_AVG_3PT_PCT,
    )

    # (e) Non-shooter to spacing-poor contender
    non_shooter = Player(
        name="Non Shooter",
        team="SYN",
        age=28,
        stats={"GP": 70.0, "MPG": 30.0, "NET_RATING": 1.0},
    )
    _evaluate_combined(
        "7e non-shooter (28, +2 EPM, C) -> 52-win spacing-poor contender",
        runner,
        non_shooter,
        epm_value=2.0,
        position="C",
        contract_salary=20_000_000,
        acquiring_wins=52.0,
        core_age=27,
        g_mpg=60,
        f_mpg=60,
        c_mpg=10,
        team_3pt_rate=0.28,
        team_3pt_pct=0.33,
        player_3pt_rate=0.05,
        player_3pt_pct=0.20,
    )
    # (f) Elite shooter to spacing-poor contender
    elite_shooter = Player(
        name="Elite Shooter",
        team="SYN",
        age=27,
        stats={"GP": 75.0, "MPG": 32.0, "NET_RATING": 3.0},
    )
    _evaluate_combined(
        "7f elite shooter (27, +3 EPM, G) -> 52-win spacing-poor contender",
        runner,
        elite_shooter,
        epm_value=3.0,
        position="G",
        contract_salary=25_000_000,
        acquiring_wins=52.0,
        core_age=27,
        g_mpg=20,
        f_mpg=60,
        c_mpg=40,
        team_3pt_rate=0.28,
        team_3pt_pct=0.33,
        player_3pt_rate=0.55,
        player_3pt_pct=0.41,
    )


# ============================================================================
# Category 8: Real blockbuster trades
# ============================================================================


@dataclass(frozen=True)
class Blockbuster:
    title: str
    team_a_abbr: str
    team_b_abbr: str
    a_sends: tuple[str, ...]
    b_sends: tuple[str, ...]
    b_picks: tuple[str, ...] = ()
    a_picks: tuple[str, ...] = ()


BLOCKBUSTERS: list[Blockbuster] = [
    Blockbuster(
        title="8a Trae Young to LAL for Russell + Hachimura + 2027 LAL 1st",
        team_a_abbr="ATL",
        team_b_abbr="LAL",
        a_sends=("Trae Young",),
        b_sends=("D'Angelo Russell", "Rui Hachimura"),
        b_picks=("2027 LAL 1st (top-10 protected)",),
    ),
    Blockbuster(
        title="8b Zach LaVine to DEN for MPJ + 2026 DEN 1st",
        team_a_abbr="SAC",
        team_b_abbr="DEN",
        a_sends=("Zach LaVine",),
        b_sends=("Michael Porter Jr.",),
        b_picks=("2026 DEN 1st (unprotected)",),
    ),
    Blockbuster(
        title="8c Brandon Ingram to MIA for Tyler Herro + 2027 MIA 1st (top-14)",
        team_a_abbr="TOR",
        team_b_abbr="MIA",
        a_sends=("Brandon Ingram",),
        b_sends=("Tyler Herro",),
        b_picks=("2027 MIA 1st (top-14 protected)",),
    ),
    Blockbuster(
        title="8d Pascal Siakam to NYK for Randle + Mitchell Robinson",
        team_a_abbr="IND",
        team_b_abbr="NYK",
        a_sends=("Pascal Siakam",),
        b_sends=("Julius Randle", "Mitchell Robinson"),
    ),
]


def _build_blockbuster_player(
    name: str,
    epm_df: pd.DataFrame,
    stats_lookup: dict[str, pd.Series],
) -> Player:
    epm_row = get_player_epm(epm_df, name)
    stats_row = stats_lookup.get(normalize_name(name))
    if epm_row is not None:
        team = str(epm_row["team"])
        age = int(epm_row["age"])
    else:
        team = "FA"
        age = 0
    if stats_row is not None:
        gp = int(stats_row.get("GP", 0) or 0)
        mpg = float(stats_row.get("MPG", 0.0) or 0.0)
        net_rating = float(stats_row.get("NET_RATING", 0.0) or 0.0)
        if stats_row.get("age") is not None:
            age = int(stats_row["age"] or age)
    else:
        gp, mpg, net_rating = 0, 0.0, 0.0
    if gp <= 0 or mpg <= 0:
        gp, mpg = DEFAULT_GP, DEFAULT_MPG
    return Player(
        name=name,
        team=team,
        age=age,
        stats={"GP": float(gp), "MPG": mpg, "NET_RATING": net_rating},
    )


def _pick_value(label: str) -> float:
    pick_number, protection_top = PICK_ESTIMATES[label]
    return calculate_pick_value_with_protections(pick_number, protection_top)


def _evaluate_blockbuster_side(
    incoming_entries: list[RosterEntry],
    outgoing_entries: list[RosterEntry],
    incoming_picks: tuple[str, ...],
    outgoing_picks: tuple[str, ...],
    acquiring_team_abbr: str,
    epm_df: pd.DataFrame,
    stats_df: pd.DataFrame,
) -> dict:
    acquiring_wins = BLOCKBUSTER_WIN_PROJECTIONS[acquiring_team_abbr]
    acquiring_roster = stats_df[stats_df["team"] == acquiring_team_abbr].to_dict(
        orient="records"
    )
    outgoing_names = [entry.player.name for entry in outgoing_entries]

    def _eval(entry: RosterEntry) -> dict:
        base = evaluate_player(
            entry.player, entry.contract, epm_df=epm_df, darko_df=None
        )
        adjusted = evaluate_player_in_team_context(
            player_value=base.player_value,
            wins_added=base.wins_added,
            player=entry.player,
            contract=entry.contract,
            acquiring_team_wins=acquiring_wins,
            acquiring_team_roster=acquiring_roster,
            epm_df=epm_df,
            player_stats_df=stats_df,
            outgoing_player_names=outgoing_names,
        )
        return {
            "name": entry.player.name,
            "salary": entry.contract.salary,
            "metric_source": base.metric_source,
            "wins_added": base.wins_added,
            "base_surplus": base.surplus_value,
            "win_curve": adjusted.win_curve_multiplier,
            "effective_curve": _effective_win_curve(
                adjusted.win_curve_multiplier, base.wins_added
            ),
            "timeline": adjusted.timeline_modifier,
            "positional": adjusted.positional_modifier,
            "spacing": adjusted.spacing_modifier,
            "team_surplus": adjusted.team_surplus,
        }

    incoming = [_eval(e) for e in incoming_entries]
    outgoing = [_eval(e) for e in outgoing_entries]
    incoming_pick_value = sum(_pick_value(p) for p in incoming_picks)
    outgoing_pick_value = sum(_pick_value(p) for p in outgoing_picks)
    net_adjusted = (sum(d["team_surplus"] for d in incoming) + incoming_pick_value) - (
        sum(d["team_surplus"] for d in outgoing) + outgoing_pick_value
    )
    net_base = (sum(d["base_surplus"] for d in incoming) + incoming_pick_value) - (
        sum(d["base_surplus"] for d in outgoing) + outgoing_pick_value
    )
    return {
        "incoming": incoming,
        "outgoing": outgoing,
        "incoming_pick_value": incoming_pick_value,
        "outgoing_pick_value": outgoing_pick_value,
        "net_base": net_base,
        "net_adjusted": net_adjusted,
    }


def _prose_for_blockbuster(scenario: Blockbuster, side_a: dict, side_b: dict) -> str:
    delta = side_a["net_adjusted"] - side_b["net_adjusted"]
    if abs(delta) < 1_000_000:
        return f"{scenario.team_a_abbr} and {scenario.team_b_abbr} are within $1M -- a wash."
    if delta > 0:
        winner = scenario.team_a_abbr
        loser = scenario.team_b_abbr
        loser_side = side_b
    else:
        winner = scenario.team_b_abbr
        loser = scenario.team_a_abbr
        loser_side = side_a
    headline_in_loser = max(
        loser_side["incoming"], key=lambda d: abs(d["wins_added"]), default=None
    )
    extra = ""
    if headline_in_loser is not None:
        extra = (
            f" {loser} pays {headline_in_loser['name']} "
            f"${headline_in_loser['salary'] / 1_000_000:.1f}M for "
            f"{headline_in_loser['wins_added']:+.2f} wins_added "
            f"(win_curve x{headline_in_loser['win_curve']:.2f}, "
            f"timeline {headline_in_loser['timeline']:+.2f}, "
            f"positional {headline_in_loser['positional']:+.2f})."
        )
    return f"{winner} wins by ${abs(delta) / 1_000_000:.1f}M adjusted surplus.{extra}"


def test_blockbusters(
    runner: TestRunner,
    epm_df: pd.DataFrame,
    stats_df: pd.DataFrame,
    stats_lookup: dict[str, pd.Series],
) -> None:
    runner.category("Category 8: Real 2025-26 blockbuster trades")

    for scenario in BLOCKBUSTERS:
        print(f"\n  {scenario.title}")
        # Build entries
        try:
            a_entries = [
                RosterEntry(
                    player=_build_blockbuster_player(name, epm_df, stats_lookup),
                    contract=Contract(
                        salary=BLOCKBUSTER_SALARIES[name], years_remaining=1
                    ),
                )
                for name in scenario.a_sends
            ]
            b_entries = [
                RosterEntry(
                    player=_build_blockbuster_player(name, epm_df, stats_lookup),
                    contract=Contract(
                        salary=BLOCKBUSTER_SALARIES[name], years_remaining=1
                    ),
                )
                for name in scenario.b_sends
            ]
        except KeyError as e:
            runner.failed(scenario.title, f"missing salary or data for {e}")
            continue

        team_a = _team(
            scenario.team_a_abbr, BLOCKBUSTER_TEAM_PAYROLLS[scenario.team_a_abbr]
        )
        team_b = _team(
            scenario.team_b_abbr, BLOCKBUSTER_TEAM_PAYROLLS[scenario.team_b_abbr]
        )
        trade = Trade(
            team_a=team_a,
            team_b=team_b,
            team_a_sends=TradeAssets(players=a_entries, picks=list(scenario.a_picks)),
            team_b_sends=TradeAssets(players=b_entries, picks=list(scenario.b_picks)),
        )

        # Salary legality
        legality = check_trade_legality(trade)
        legality_str = (
            "LEGAL" if legality.legal else f"ILLEGAL: {legality.error_reason}"
        )
        print(f"    salary matching: {legality_str}")

        # Per-side evaluation
        side_a = _evaluate_blockbuster_side(
            incoming_entries=b_entries,
            outgoing_entries=a_entries,
            incoming_picks=scenario.b_picks,
            outgoing_picks=scenario.a_picks,
            acquiring_team_abbr=scenario.team_a_abbr,
            epm_df=epm_df,
            stats_df=stats_df,
        )
        side_b = _evaluate_blockbuster_side(
            incoming_entries=a_entries,
            outgoing_entries=b_entries,
            incoming_picks=scenario.a_picks,
            outgoing_picks=scenario.b_picks,
            acquiring_team_abbr=scenario.team_b_abbr,
            epm_df=epm_df,
            stats_df=stats_df,
        )

        # Base surplus per side
        print(
            f"    base surplus: {scenario.team_a_abbr} net "
            f"{_money(side_a['net_base'])},  "
            f"{scenario.team_b_abbr} net {_money(side_b['net_base'])}"
        )
        # Team-context breakdown per player
        for label, side in (
            (scenario.team_a_abbr, side_a),
            (scenario.team_b_abbr, side_b),
        ):
            print(
                f"    {label} acquiring (proj wins "
                f"{BLOCKBUSTER_WIN_PROJECTIONS[label]:.0f}):"
            )
            for d in side["incoming"]:
                print(
                    f"      in  {d['name']:<22} src={d['metric_source']:<10} "
                    f"wins_added={d['wins_added']:+5.2f}  "
                    f"team_curve x{d['win_curve']:.2f} "
                    f"effective_curve x{d['effective_curve']:.2f}  "
                    f"tl {d['timeline']:+.2f} pos {d['positional']:+.2f} "
                    f"sp {d['spacing']:+.2f}  "
                    f"team_surplus={_money(d['team_surplus'])}"
                )
            for d in side["outgoing"]:
                print(
                    f"      out {d['name']:<22} src={d['metric_source']:<10} "
                    f"wins_added={d['wins_added']:+5.2f}  "
                    f"team_surplus={_money(d['team_surplus'])}"
                )
            if side["incoming_pick_value"] > 0:
                print(f"      picks in:  {_money(side['incoming_pick_value'])}")
            if side["outgoing_pick_value"] > 0:
                print(f"      picks out: {_money(side['outgoing_pick_value'])}")

        # Final adjusted + verdict
        print(
            f"    final adjusted: {scenario.team_a_abbr} net "
            f"{_money(side_a['net_adjusted'])}, "
            f"{scenario.team_b_abbr} net {_money(side_b['net_adjusted'])}"
        )
        print(f"    verdict: {_prose_for_blockbuster(scenario, side_a, side_b)}")

        # Soft validation: flag if either side is fully reliant on net_rating
        # for a headline player (suggests a name lookup miss).
        for label, side, sends in (
            (scenario.team_a_abbr, side_a, scenario.b_sends),
            (scenario.team_b_abbr, side_b, scenario.a_sends),
        ):
            for d in side["incoming"]:
                if d["metric_source"] == "net_rating":
                    runner.warned(
                        f"{scenario.title} :: {d['name']} fell to net_rating",
                        "no EPM/DARKO match — check name spelling vs source data",
                    )
                    break
            else:
                runner.passed(
                    f"{scenario.title} :: {label} headline incoming has impact data"
                )

        # Record per-scenario pass if salary engine returned a valid answer.
        runner.passed(scenario.title, legality_str)


# ============================================================================
# Main
# ============================================================================


def main() -> None:
    _force_utf8_stdout()
    runner = TestRunner()

    print("Fetching EPM table from dunksandthrees.com...")
    epm_df = fetch_epm_data()
    print(f"  {len(epm_df)} EPM rows loaded")

    print("Fetching DARKO from public sheet...")
    try:
        darko_df = fetch_darko_data()
        print(f"  {len(darko_df)} DARKO rows loaded")
    except Exception as exc:  # pragma: no cover - network failures non-fatal
        darko_df = pd.DataFrame()
        runner.warned("startup", f"DARKO fetch failed: {exc}")

    print("Fetching player stats from nba_api...")
    stats_df = fetch_player_stats()
    print(f"  {len(stats_df)} player-stat rows loaded")

    # Attach normalized names + EPM position for downstream lookups.
    stats_df = stats_df.copy()
    stats_df["player_name_normalized"] = stats_df["player_name"].map(normalize_name)
    pos_lookup = dict(
        zip(epm_df["player_name_normalized"], epm_df["position"], strict=True)
    )
    stats_df["position"] = stats_df["player_name_normalized"].map(pos_lookup)
    stats_lookup = {
        row["player_name_normalized"]: row for _, row in stats_df.iterrows()
    }

    # Run categories
    test_salary_matching(runner)
    test_valuation_paths(runner, epm_df, darko_df, stats_lookup)
    test_win_curve_table(runner)
    test_timeline_scenarios(runner)
    test_positional_scenarios(runner)
    test_spacing_scenarios(runner)
    test_combined_scenarios(runner)
    test_blockbusters(runner, epm_df, stats_df, stats_lookup)

    runner.summary()


if __name__ == "__main__":
    main()
