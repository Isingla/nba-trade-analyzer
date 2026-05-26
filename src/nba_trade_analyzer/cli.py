"""Typer CLI for the NBA Trade Analyzer (Phase 9).

Two commands:

- ``grade`` — evaluate a proposed trade between two teams and print the full
  fan-readable report (legality, per-team score + verdict, metric breakdowns).
- ``lookup`` — print a compact single-player valuation summary.

The CLI is a thin shell over the existing engine: it parses arguments, loads
the data sources (with progress feedback), builds the :class:`Trade` model, and
delegates to :func:`grade_trade`. Rendering is shared with the demo script via
:mod:`nba_trade_analyzer.report`.

Run via the installed entry point::

    uv run nba-trade-analyzer grade --team-a LAL --team-b DAL \
        --sends-a "Jarred Vanderbilt" --sends-a "2026 LAL 1st unprotected" \
        --sends-b "Daniel Gafford"
    uv run nba-trade-analyzer lookup "Daniel Gafford"
"""

from __future__ import annotations

import difflib
import re
import time
from collections.abc import Callable
from typing import NoReturn, TypeVar

import pandas as pd
import typer
from pydantic import ValidationError

from nba_trade_analyzer.data.darko import fetch_darko_data
from nba_trade_analyzer.data.epm import fetch_epm_data, get_player_epm, normalize_name
from nba_trade_analyzer.data.players import fetch_player_stats
from nba_trade_analyzer.data.salaries import (
    build_contract,
    fetch_all_salaries,
    get_player_salary,
)
from nba_trade_analyzer.engine.constants import FIRST_APRON, SALARY_CAP, SECOND_APRON
from nba_trade_analyzer.engine.grader import _epm_tier, grade_trade
from nba_trade_analyzer.engine.valuation import evaluate_player
from nba_trade_analyzer.models.draft_pick import DraftPick
from nba_trade_analyzer.models.player import Player
from nba_trade_analyzer.models.team import CapStatus, RosterEntry, Team
from nba_trade_analyzer.models.trade import Trade, TradeAssets
from nba_trade_analyzer.report import force_utf8_stdout, print_report
from nba_trade_analyzer.teams import TeamInfo, format_valid_teams, resolve_team

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Evaluate and grade NBA trades from the terminal.",
)

_MILLION = 1_000_000.0

# Fallback minutes load when a player has no usable GP/MPG row (injured, traded,
# very early season) — a reasonable "regular starter" so wins_added isn't pinned
# to zero. Mirrors the diagnostic scripts.
_DEFAULT_GP = 60
_DEFAULT_MPG = 30.0

# A string is treated as a draft pick if it matches "{year} {TEAM} {1st|2nd}
# [protections]", e.g. "2026 LAL 1st unprotected", "2027 DAL 1st top-4 protected".
PICK_PATTERN = re.compile(r"^(\d{4})\s+([A-Z]{3})\s+(1st|2nd)\s*(.*)$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_pick(s: str) -> DraftPick | None:
    """Parse a string as a :class:`DraftPick`, or ``None`` if it isn't one.

    Returns ``None`` silently for anything that doesn't look like a pick (it's
    a player name). When a string *looks* like a pick but fails model
    validation (e.g. a year outside the supported range), warns and returns
    ``None`` so the caller falls back to treating it as a player name.
    """
    m = PICK_PATTERN.match(s.strip())
    if not m:
        return None
    year = int(m.group(1))
    team = m.group(2).upper()
    round_num = 1 if m.group(3).lower() == "1st" else 2
    protections = m.group(4).strip() or None
    if protections and protections.lower() == "unprotected":
        protections = None
    try:
        return DraftPick(team=team, year=year, round=round_num, protections=protections)
    except ValidationError:
        typer.secho(
            f"Warning: '{s}' looks like a pick but couldn't be parsed. "
            "Treating as player name.",
            fg=typer.colors.YELLOW,
            err=True,
        )
        return None


# ---------------------------------------------------------------------------
# Data loading (with progress feedback)
# ---------------------------------------------------------------------------

_T = TypeVar("_T")


def _timed(label: str, fn: Callable[[], _T]) -> _T:
    """Run ``fn``, printing ``Loading {label}... done (Ns)`` around it."""
    typer.secho(f"Loading {label}...", nl=False)
    start = time.perf_counter()
    result = fn()
    typer.secho(f" done ({time.perf_counter() - start:.1f}s)", fg=typer.colors.GREEN)
    return result


def _empty_darko() -> pd.DataFrame:
    """An empty DARKO frame — passed in ``--quick`` mode so the grader's
    on-demand fetch is skipped while the EPM → DARKO → NET_RATING chain still
    degrades cleanly (every ``get_player_darko`` lookup misses)."""
    return pd.DataFrame(
        columns=[
            "player_name",
            "player_name_normalized",
            "dpm",
            "dpm_off",
            "dpm_def",
            "position",
            "age",
        ]
    )


def _augment_with_epm_position(
    stats_df: pd.DataFrame, epm_df: pd.DataFrame
) -> pd.DataFrame:
    """Attach EPM ``position`` onto the player-stats frame via normalized name.

    nba_api exposes no positional label, but the team-context engine needs one
    for positional fit. EPM has it — merge on normalized name. Also adds the
    ``player_name_normalized`` column the per-player lookups key off.
    """
    stats_df = stats_df.copy()
    stats_df["player_name_normalized"] = stats_df["player_name"].map(normalize_name)
    pos_lookup = dict(
        zip(epm_df["player_name_normalized"], epm_df["position"], strict=True)
    )
    stats_df["position"] = stats_df["player_name_normalized"].map(pos_lookup)
    return stats_df


def _load_core_data() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Load + shape the three always-needed sources (stats, EPM, salaries)."""
    stats_df = _timed("player stats", fetch_player_stats)
    epm_df = _timed("EPM data", fetch_epm_data)
    salary_df = _timed("salary data", fetch_all_salaries)
    stats_df = _augment_with_epm_position(stats_df, epm_df)
    return epm_df, stats_df, salary_df


def _stats_lookup(stats_df: pd.DataFrame) -> dict[str, pd.Series]:
    return {row["player_name_normalized"]: row for _, row in stats_df.iterrows()}


# ---------------------------------------------------------------------------
# Player / team resolution
# ---------------------------------------------------------------------------


def _build_player(
    name: str,
    epm_row: pd.Series | None,
    stats_row: pd.Series | None,
) -> Player:
    """Construct a :class:`Player`: team/age from EPM, GP/MPG from nba_api."""
    if epm_row is not None:
        team = str(epm_row["team"])
        age = int(epm_row["age"])
        epm_mpg = float(epm_row["mpg"])
    else:
        team = "FA"
        age = 0
        epm_mpg = _DEFAULT_MPG

    if isinstance(stats_row, pd.Series):
        gp = int(stats_row.get("GP", 0) or 0)
        mpg = float(stats_row.get("MPG", 0.0) or 0.0)
        if "age" in stats_row.index and stats_row["age"] is not None:
            age = int(stats_row["age"] or age)
        net_rating = float(stats_row.get("NET_RATING", 0.0) or 0.0)
        if epm_row is None and stats_row.get("team"):
            team = str(stats_row["team"])
    else:
        gp = _DEFAULT_GP
        mpg = epm_mpg if epm_mpg > 0 else _DEFAULT_MPG
        net_rating = 0.0

    # If nba_api had no usable row (didn't play, traded, injured), fall back to
    # a reasonable starter load so wins_added isn't pinned to zero.
    if gp <= 0 or mpg <= 0:
        gp = _DEFAULT_GP
        mpg = epm_mpg if epm_mpg > 0 else _DEFAULT_MPG

    return Player(
        name=name,
        team=team,
        age=age,
        stats={"NET_RATING": net_rating, "GP": float(gp), "MPG": mpg},
    )


def _fuzzy_suggestions(name: str, salary_df: pd.DataFrame) -> list[str]:
    if salary_df is None or salary_df.empty:
        return []
    names = salary_df["player_name"].astype(str).tolist()
    return difflib.get_close_matches(name, names, n=3, cutoff=0.6)


def _player_not_found(name: str, salary_df: pd.DataFrame) -> NoReturn:
    typer.secho(
        f"Error: Could not find '{name}' in salary data. Check spelling or try "
        "the full name (e.g., 'Robert Williams III' not 'Rob Williams').",
        fg=typer.colors.RED,
        err=True,
    )
    suggestions = _fuzzy_suggestions(name, salary_df)
    if suggestions:
        typer.secho(
            f"Did you mean: {', '.join(suggestions)}?",
            fg=typer.colors.YELLOW,
            err=True,
        )
    raise typer.Exit(code=1)


def _resolve_player_entry(
    name: str,
    epm_df: pd.DataFrame,
    stats_lookup: dict[str, pd.Series],
    salary_df: pd.DataFrame,
) -> RosterEntry:
    """Resolve a player name to a :class:`RosterEntry` (contract + Player).

    Salary data is the source of truth for the contract; a miss is fatal (with
    a spelling hint and fuzzy suggestions) since there's no contract to value.
    """
    salary_data = get_player_salary(salary_df, name)
    if salary_data is None:
        _player_not_found(name, salary_df)
    contract = build_contract(salary_data)
    epm_row = get_player_epm(epm_df, name)
    stats_row = stats_lookup.get(normalize_name(name))
    player = _build_player(name, epm_row, stats_row)
    return RosterEntry(player=player, contract=contract)


def _parse_assets(
    sends: list[str],
    epm_df: pd.DataFrame,
    stats_lookup: dict[str, pd.Series],
    salary_df: pd.DataFrame,
) -> tuple[list[RosterEntry], list[DraftPick]]:
    """Split a side's ``--sends`` strings into player entries and draft picks."""
    players: list[RosterEntry] = []
    picks: list[DraftPick] = []
    for raw in sends:
        pick = parse_pick(raw)
        if pick is not None:
            picks.append(pick)
        else:
            players.append(_resolve_player_entry(raw, epm_df, stats_lookup, salary_df))
    return players, picks


def _resolve_team_or_exit(raw: str) -> TeamInfo:
    info = resolve_team(raw)
    if info is None:
        typer.secho(
            f"Error: '{raw}' is not a valid NBA team abbreviation.",
            fg=typer.colors.RED,
            err=True,
        )
        typer.secho("Valid abbreviations:", err=True)
        typer.secho(format_valid_teams(), err=True)
        raise typer.Exit(code=1)
    return info


def _cap_status_for(payroll: int) -> CapStatus:
    if payroll < SALARY_CAP:
        return CapStatus.UNDER_CAP
    if payroll < FIRST_APRON:
        return CapStatus.OVER_CAP
    if payroll < SECOND_APRON:
        return CapStatus.FIRST_APRON
    return CapStatus.SECOND_APRON


def _team_payroll(salary_df: pd.DataFrame, salary_abbr: str) -> int:
    """Sum every contract on a team to approximate its total payroll."""
    if salary_df is None or salary_df.empty or "team" not in salary_df.columns:
        return 0
    rows = salary_df[salary_df["team"] == salary_abbr]
    return int(rows["salary"].sum())


def _build_team(info: TeamInfo, salary_df: pd.DataFrame) -> Team:
    """Build a :class:`Team` — payroll summed from the salary frame, abbreviation
    kept in nba_api form so the grader's roster/context lookups resolve."""
    payroll = _team_payroll(salary_df, info.salary_abbreviation)
    return Team(
        name=info.name,
        abbreviation=info.abbreviation,
        total_payroll=payroll,
        cap_status=_cap_status_for(payroll),
    )


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@app.command()
def grade(
    team_a: str = typer.Option(
        ..., "--team-a", "-a", help="Team A's 3-letter abbreviation (e.g. LAL)."
    ),
    team_b: str = typer.Option(
        ..., "--team-b", "-b", help="Team B's 3-letter abbreviation (e.g. DAL)."
    ),
    sends_a: list[str] = typer.Option(
        ...,
        "--sends-a",
        "-sa",
        help="A player name or pick string team A sends. Repeat for each asset.",
    ),
    sends_b: list[str] = typer.Option(
        ...,
        "--sends-b",
        "-sb",
        help="A player name or pick string team B sends. Repeat for each asset.",
    ),
    quick: bool = typer.Option(
        False, "--quick", "-q", help="Skip the DARKO fetch for a faster evaluation."
    ),
) -> None:
    """Grade a proposed trade between two teams."""
    force_utf8_stdout()

    info_a = _resolve_team_or_exit(team_a)
    info_b = _resolve_team_or_exit(team_b)

    epm_df, stats_df, salary_df = _load_core_data()
    if quick:
        typer.secho("Skipping DARKO projections (--quick).", fg=typer.colors.YELLOW)
        darko_df = _empty_darko()
    else:
        darko_df = _timed("DARKO projections", fetch_darko_data)
    stats_lookup = _stats_lookup(stats_df)

    typer.echo()
    typer.echo("Evaluating trade...")
    typer.echo()

    players_a, picks_a = _parse_assets(sends_a, epm_df, stats_lookup, salary_df)
    players_b, picks_b = _parse_assets(sends_b, epm_df, stats_lookup, salary_df)

    trade = Trade(
        team_a=_build_team(info_a, salary_df),
        team_b=_build_team(info_b, salary_df),
        team_a_sends=TradeAssets(players=players_a, picks=picks_a),
        team_b_sends=TradeAssets(players=players_b, picks=picks_b),
    )
    result = grade_trade(
        trade,
        player_stats_df=stats_df,
        epm_df=epm_df,
        darko_df=darko_df,
        salary_df=salary_df,
    )
    print_report(trade, result)


def _epm_tier_display(epm: float) -> str:
    """'Average (top 50%)' -> 'Average — top 50%' for the one-line lookup."""
    return _epm_tier(epm).replace(" (", " — ").rstrip(")")


def _surplus_str(surplus: float) -> str:
    millions = abs(surplus) / _MILLION
    sign = "+" if surplus >= 0 else "-"
    return f"{sign}${millions:.1f}M/yr"


@app.command()
def lookup(
    player: str = typer.Argument(..., help='Player full name, e.g. "Daniel Gafford".'),
) -> None:
    """Print a compact valuation summary for a single player."""
    force_utf8_stdout()

    epm_df, stats_df, salary_df = _load_core_data()
    stats_lookup = _stats_lookup(stats_df)
    typer.echo()

    entry = _resolve_player_entry(player, epm_df, stats_lookup, salary_df)

    # EPM is the primary path; skip the DARKO fetch for a fast single lookup.
    valuation = evaluate_player(
        entry.player, entry.contract, epm_df=epm_df, darko_df=_empty_darko()
    )
    epm_row = get_player_epm(epm_df, player)

    position = "—"
    if epm_row is not None and epm_row.get("position"):
        position = str(epm_row["position"])

    p = entry.player
    c = entry.contract
    impact = valuation.adjusted_net_rating

    typer.echo(f"{p.name} ({p.team}) — {position}, Age {p.age}")
    typer.echo(f"  EPM: {impact:+.1f} ({_epm_tier_display(impact)})")
    typer.echo(f"  Salary: ${c.salary:,} / {c.years_remaining}yr remaining")
    typer.echo(f"  Surplus: {_surplus_str(valuation.surplus_value)}")


if __name__ == "__main__":
    app()
