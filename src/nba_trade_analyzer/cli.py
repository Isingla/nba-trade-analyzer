"""Typer CLI for the NBA Trade Analyzer (Phase 9).

Three commands:

- ``grade`` — evaluate a proposed trade between two teams and print the full
  fan-readable report (legality, per-team score + verdict, metric breakdowns).
- ``lookup`` — print a compact single-player valuation summary.
- ``roster`` — show a team's current roster, per-player contracts, and the
  team's standing against all four salary lines.

The CLI is a thin shell over the existing engine: it parses arguments, loads
the data sources (with progress feedback), builds the :class:`Trade` model, and
delegates to :func:`grade_trade`. Rendering is shared with the demo script via
:mod:`nba_trade_analyzer.report`.

Run via the installed entry point::

    uv run nba-trade-analyzer grade --team-a LAL --team-b DAL \
        --sends-a "Jarred Vanderbilt" --sends-a "2026 LAL 1st unprotected" \
        --sends-b "Daniel Gafford"
    uv run nba-trade-analyzer lookup "Daniel Gafford"
    uv run nba-trade-analyzer roster --team LAL
"""

from __future__ import annotations

import difflib
import json
import logging
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn, TypeVar

import pandas as pd
import typer
from pydantic import ValidationError

from nba_trade_analyzer.data.crosswalk import Crosswalk, CrosswalkError, load_crosswalk
from nba_trade_analyzer.data.darko import fetch_darko_data
from nba_trade_analyzer.data.epm import fetch_epm_data, get_player_epm, normalize_name
from nba_trade_analyzer.data.players import (
    fetch_player_stats,
    fetch_roster_player_ids,
    fetch_team_roster,
)
from nba_trade_analyzer.data.salaries import (
    build_contract,
    fetch_all_salaries,
    get_player_salary,
)
from nba_trade_analyzer.engine.constants import (
    FIRST_APRON,
    LUXURY_TAX,
    SALARY_CAP,
    SECOND_APRON,
)
from nba_trade_analyzer.engine.grader import _epm_tier, grade_trade
from nba_trade_analyzer.engine.resolution import (
    TradePlayerResolutionError,
    resolve_trade_player,
)
from nba_trade_analyzer.engine.valuation import evaluate_player
from nba_trade_analyzer.export import build_export
from nba_trade_analyzer.models.draft_pick import DraftPick
from nba_trade_analyzer.models.player import Player
from nba_trade_analyzer.pick_ownership import (
    Indeterminate,
    NoRecord,
    PickRegistryError,
    Verified,
    get_default_registry,
    iter_trade_pick_verdicts,
)
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

    Salary-only path used by ``lookup`` (no team context, so no roster check).
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


def _resolution_failed(
    exc: TradePlayerResolutionError, salary_df: pd.DataFrame
) -> NoReturn:
    """Print a specific, fail-loud error for a moved player and exit."""
    typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
    if exc.side == "salary":
        suggestions = _fuzzy_suggestions(exc.player, salary_df)
        if suggestions:
            typer.secho(
                f"Did you mean: {', '.join(suggestions)}?",
                fg=typer.colors.YELLOW,
                err=True,
            )
    raise typer.Exit(code=1)


def _resolve_trade_entry(
    name: str,
    epm_df: pd.DataFrame,
    stats_lookup: dict[str, pd.Series],
    salary_df: pd.DataFrame,
    sending_team: str,
    crosswalk: Crosswalk,
    roster_ids: set[int],
) -> RosterEntry:
    """Resolve a *moved* (trade-input) player, failing loud on any gap.

    A trade-input player must resolve to BOTH a salary entry and a current
    roster entry on ``sending_team`` (joined through the crosswalk); any failure
    raises through :func:`_resolution_failed` naming the player and side. Never
    drops, skips, or defaults a moved player.
    """
    try:
        resolved = resolve_trade_player(
            name,
            sending_team,
            salary_df=salary_df,
            crosswalk=crosswalk,
            roster_ids=roster_ids,
        )
    except TradePlayerResolutionError as exc:
        _resolution_failed(exc, salary_df)
    contract = build_contract(resolved.salary_data)
    epm_row = get_player_epm(epm_df, name)
    stats_row = stats_lookup.get(normalize_name(name))
    player = _build_player(name, epm_row, stats_row)
    return RosterEntry(player=player, contract=contract)


def _parse_assets(
    sends: list[str],
    epm_df: pd.DataFrame,
    stats_lookup: dict[str, pd.Series],
    salary_df: pd.DataFrame,
    sending_team: str,
    crosswalk: Crosswalk,
    roster_ids: set[int],
) -> tuple[list[RosterEntry], list[DraftPick]]:
    """Split a side's ``--sends`` strings into player entries and draft picks.

    Players are resolved fail-loud against the sending team's current roster.
    """
    players: list[RosterEntry] = []
    picks: list[DraftPick] = []
    for raw in sends:
        pick = parse_pick(raw)
        if pick is not None:
            picks.append(pick)
        else:
            players.append(
                _resolve_trade_entry(
                    raw,
                    epm_df,
                    stats_lookup,
                    salary_df,
                    sending_team,
                    crosswalk,
                    roster_ids,
                )
            )
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


def _load_crosswalk_or_exit() -> Crosswalk:
    """Load the committed NBA-id <-> BBRef-slug crosswalk, or exit loud."""
    try:
        return load_crosswalk()
    except CrosswalkError as exc:
        typer.secho(
            f"Error: could not load the player crosswalk ({exc}). "
            "Rebuild it with: python scripts/build_crosswalk.py",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1) from exc


def _fetch_roster_ids_or_exit(team_abbr: str) -> set[int]:
    """Fetch a team's current-roster player ids, or exit loud.

    Moved-player validation depends on this set, so a failed fetch is fatal
    rather than silently skipped (which would let an unresolved player through).
    """
    try:
        return fetch_roster_player_ids(team_abbr)
    except Exception as exc:  # noqa: BLE001 - surface any fetch failure to the user
        typer.secho(
            f"Error: could not fetch {team_abbr}'s current roster from nba_api "
            f"({exc}). Roster membership is required to validate the trade.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1) from exc


def _fetch_team_roster_or_exit(team_abbr: str) -> list[dict]:
    """Fetch a team's current roster records (id + name), or exit loud."""
    try:
        return fetch_team_roster(team_abbr)
    except Exception as exc:  # noqa: BLE001 - surface any fetch failure to the user
        typer.secho(
            f"Error: could not fetch {team_abbr}'s current roster from nba_api "
            f"({exc}).",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1) from exc


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
    no_ownership_check: bool = typer.Option(
        False,
        "--no-ownership-check",
        "--skip-ownership",
        help="Skip draft-pick ownership verification (escape hatch for a stale mirror).",
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

    # Roster membership (and the id <-> slug crosswalk that joins it to salaries)
    # is required to validate that every moved player resolves; load both before
    # parsing assets so failures surface immediately.
    crosswalk = _load_crosswalk_or_exit()
    roster_ids_a = _fetch_roster_ids_or_exit(info_a.abbreviation)
    roster_ids_b = _fetch_roster_ids_or_exit(info_b.abbreviation)

    typer.echo()
    typer.echo("Evaluating trade...")
    typer.echo()

    players_a, picks_a = _parse_assets(
        sends_a,
        epm_df,
        stats_lookup,
        salary_df,
        info_a.abbreviation,
        crosswalk,
        roster_ids_a,
    )
    players_b, picks_b = _parse_assets(
        sends_b,
        epm_df,
        stats_lookup,
        salary_df,
        info_b.abbreviation,
        crosswalk,
        roster_ids_b,
    )

    trade = Trade(
        team_a=_build_team(info_a, salary_df),
        team_b=_build_team(info_b, salary_df),
        team_a_sends=TradeAssets(players=players_a, picks=picks_a),
        team_b_sends=TradeAssets(players=players_b, picks=picks_b),
    )

    # Pick-ownership verification (on by default; --no-ownership-check disables).
    # The CLI applies presentation-layer leniency: NotOwner is a hard rejection,
    # but NoRecord is downgraded to a loud warning (the mirror may simply be
    # stale). The library API keeps NoRecord-as-rejection for programmatic use.
    if not no_ownership_check and _ownership_reject(trade):
        raise typer.Exit(code=1)

    result = grade_trade(
        trade,
        player_stats_df=stats_df,
        epm_df=epm_df,
        darko_df=darko_df,
        roster_ids_by_team={
            info_a.abbreviation: roster_ids_a,
            info_b.abbreviation: roster_ids_b,
        },
    )
    print_report(trade, result)


def _ownership_reject(trade: Trade) -> bool:
    """Run CLI-layer pick-ownership verification. Prints warnings (NoRecord +
    Indeterminate, each stamped with the mirror sync date) and any NotOwner
    rejections. Returns ``True`` if the trade should be rejected (a hard
    NotOwner), so the caller stops before grading.
    """
    try:
        registry = get_default_registry()
    except PickRegistryError as exc:
        typer.secho(
            f"Pick-ownership registry unavailable ({exc}). "
            "Re-sync the mirror or pass --no-ownership-check.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1) from exc

    stamp = f"mirror synced {registry.sync_date}" if registry.sync_date else "mirror sync date unknown"
    rejections: list[str] = []
    for sender_abbr, sender_canon, pick, verdict in iter_trade_pick_verdicts(trade, registry):
        if isinstance(verdict, NoRecord):
            typer.secho(
                f"⚠ {sender_abbr} {pick.label}: no record of this pick in the ownership "
                f"registry — {stamp}; verify manually or re-sync.",
                fg=typer.colors.YELLOW,
            )
        elif isinstance(verdict, Indeterminate):
            typer.secho(
                f'⚠ {sender_abbr} {pick.label}: ownership indeterminate (gapped pick) — '
                f'"{verdict.clause}" [{stamp}].',
                fg=typer.colors.YELLOW,
            )
        else:
            owner = verdict.owner if isinstance(verdict, Verified) else verdict.actual_owner
            if owner != sender_canon:
                rejections.append(f"{sender_abbr} cannot trade {pick.label}: controlled by {owner}.")

    if rejections:
        typer.echo()
        typer.secho("Trade rejected — invalid pick ownership:", fg=typer.colors.RED, bold=True)
        for r in rejections:
            typer.secho(f"  ✗ {r}", fg=typer.colors.RED)
        return True
    return False


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


# ---------------------------------------------------------------------------
# roster command helpers
# ---------------------------------------------------------------------------


def _salary_row_for_slug(
    salary_df: pd.DataFrame, slug: str, salary_abbr: str
) -> dict | None:
    """Find a contract row by BBRef slug (pure id join, no name matching).

    A player can appear on more than one row (dead money split across teams),
    so prefer the row whose team matches ``salary_abbr``; otherwise take the
    first. Returns ``None`` when no row carries the slug.
    """
    if salary_df is None or salary_df.empty or "bbref_slug" not in salary_df.columns:
        return None
    matches = salary_df[salary_df["bbref_slug"] == slug]
    if matches.empty:
        return None
    on_team = matches[matches["team"] == salary_abbr]
    chosen = on_team if not on_team.empty else matches
    return chosen.iloc[0].to_dict()


def _position_label(epm_df: pd.DataFrame, name: str) -> str:
    """Position from EPM if available, else ``-``."""
    epm_row = get_player_epm(epm_df, name)
    if epm_row is not None and epm_row.get("position"):
        return str(epm_row["position"])
    return "-"


def _contract_flags(contract) -> str:
    """Human-readable option/scale flags for a contract, or empty string."""
    flags = []
    if contract.has_player_option:
        flags.append("player option")
    if contract.has_team_option:
        flags.append("team option")
    if contract.is_rookie_scale:
        flags.append("rookie scale")
    return ", ".join(flags)


def _standing_line(label: str, line_value: int, payroll: int) -> str:
    """One salary-line row: the line value and payroll's signed distance to it."""
    distance = payroll - line_value
    direction = "above" if distance >= 0 else "below"
    magnitude = abs(distance) / _MILLION
    return f"  {label:<14} ${line_value:>13,}   ${magnitude:>6.1f}M {direction}"


@app.command()
def roster(
    team: str = typer.Option(
        ..., "--team", "-t", help="Team's 3-letter abbreviation (e.g. LAL)."
    ),
) -> None:
    """Show a team's roster, per-player contracts, and salary-line standing."""
    force_utf8_stdout()

    info = _resolve_team_or_exit(team)
    epm_df, _stats_df, salary_df = _load_core_data()
    crosswalk = _load_crosswalk_or_exit()
    roster_records = _fetch_team_roster_or_exit(info.abbreviation)

    # Join each current-roster player (nba id) to its contract via the crosswalk
    # (id -> slug -> salary row). Two-way / 10-day players have no contract row;
    # they are listed and marked, never dropped, and excluded from payroll.
    contracted: list[dict] = []
    no_contract: list[str] = []
    for rec in roster_records:
        name = rec["player_name"]
        slug = crosswalk.slug_for_nba_id(rec["nba_player_id"])
        salary_data = (
            _salary_row_for_slug(salary_df, slug, info.salary_abbreviation)
            if slug
            else None
        )
        if salary_data is None:
            no_contract.append(name)
            continue
        contract = build_contract(salary_data)
        contracted.append(
            {
                "name": name,
                "position": _position_label(epm_df, name),
                "salary": contract.salary,
                "years_remaining": contract.years_remaining,
                "flags": _contract_flags(contract),
            }
        )

    contracted.sort(key=lambda r: r["salary"], reverse=True)
    payroll = sum(r["salary"] for r in contracted)

    typer.echo()
    typer.echo(f"{info.name} ({info.abbreviation}) — current roster")
    typer.echo()
    for r in contracted:
        flags = f"  [{r['flags']}]" if r["flags"] else ""
        typer.echo(
            f"  {r['name']:<26} {r['position']:<5} ${r['salary']:>12,}  "
            f"{r['years_remaining']}yr{flags}"
        )
    for name in no_contract:
        typer.echo(f"  {name:<26} {'-':<5} {'— no contract data':>13}")

    typer.echo()
    typer.echo(f"Players with contract data: {len(contracted)}")
    if no_contract:
        typer.echo(
            f"Excluded from payroll (no contract data): {len(no_contract)} "
            f"({', '.join(no_contract)})"
        )
    typer.echo()
    typer.echo(f"Payroll: ${payroll:,}")
    typer.echo(f"Cap tier: {_cap_status_for(payroll).value.replace('_', ' ').title()}")
    typer.echo()
    typer.echo("Standing against salary lines:")
    typer.echo(_standing_line("Salary cap", SALARY_CAP, payroll))
    typer.echo(_standing_line("Luxury tax", LUXURY_TAX, payroll))
    typer.echo(_standing_line("First apron", FIRST_APRON, payroll))
    typer.echo(_standing_line("Second apron", SECOND_APRON, payroll))


@app.command()
def export(
    out: str = typer.Option(
        "-",
        "--out",
        help='Output path for the databallr JSON payload, or "-" for stdout.',
    ),
) -> None:
    """Export the databallr cap-data snapshot (salaries + projections) as JSON.

    Fetches salaries, EPM, DARKO, nba_api stats, and the crosswalk (each cached
    24h; salaries fall back to the committed CSV offline), builds the Control
    Runway / Trade Analyzer payload, and writes it as JSON. Progress goes to
    stderr so stdout stays a clean JSON document for the databallr sync script.
    """
    typer.echo("Building databallr cap-data export...", err=True)
    payload = build_export()
    text = json.dumps(payload.model_dump(by_alias=True), indent=2)

    if out == "-":
        typer.echo(text)
    else:
        Path(out).write_text(text, encoding="utf-8")
        typer.echo(f"Wrote {out}", err=True)

    meta = payload.metadata
    typer.echo(
        f"  salaries={meta.salary_rows} projections={len(payload.projections)} "
        f"epm={meta.epm_rows} darko={meta.darko_rows} stats={meta.stats_rows}",
        err=True,
    )


@app.command()
def ingest(
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Load sources, resolve names, evaluate guards and print the plan — write nothing.",
    ),
    accept_baseline: str = typer.Option(
        None,
        "--accept-baseline",
        metavar='"REASON"',
        help=(
            "SUPERVISED FIRST-RUNS AND KNOWN-BASELINE-RESETS ONLY. Downgrades the "
            "row-count/dollar collapse guards to report-only for this run (recorded "
            "in the run summary as baseline_overrides, with this operator reason). "
            "Use when the current DB state is a known-poisoned baseline — e.g. the "
            "Phase 1 seed's placeholder cap-hold tiers, which a correct first real "
            "ingest undercuts. Source-quality guards (empty source, missing "
            "SITE_DATA_ROOT) still block; the flag never bypasses them."
        ),
    ),
) -> None:
    """Refresh the databallr v3_* contract tables from living sources (Phase 2A).

    Connects via $CONTRACT_INGEST_DATABASE_URL (the restricted no-delete
    contract_ingest role) — refuses to run without it, never uses
    service_role. All writes are upserts; guards hard-block on row/dollar
    collapse or empty sources; a failed BBRef fetch is a FAILED run, never a
    silent fallback to stale committed data. Every run ends with the layer-1
    three-way salary verifier and is recorded in v3_ingest_runs.
    """
    # Local imports keep psycopg out of the legacy commands' import path.
    from nba_trade_analyzer.ingest.db import IngestDb, IngestDbError, connect_from_env
    from nba_trade_analyzer.ingest.runner import run_ingest

    # The flag REQUIRES an operator note — a baseline override without a
    # stated reason is not auditable. Validated before any DB connection.
    if accept_baseline is not None and not accept_baseline.strip():
        typer.echo(
            '✗ --accept-baseline requires a non-empty reason, e.g. '
            '--accept-baseline "first real ingest over seeded placeholder cap holds"',
            err=True,
        )
        raise typer.Exit(code=2)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        conn = connect_from_env()
    except IngestDbError as exc:
        typer.echo(f"✗ {exc}", err=True)
        raise typer.Exit(code=2) from exc

    try:
        result = run_ingest(
            IngestDb(conn),
            dry_run=dry_run,
            accept_baseline=accept_baseline.strip() if accept_baseline else None,
        )
    finally:
        conn.close()

    if result.status == "dry_run":
        typer.echo(json.dumps(result.summary, indent=2, default=str))
        return
    if result.status == "guard_blocked":
        typer.echo(f"✗ guard_blocked: {json.dumps(result.guard_failures, default=str)}", err=True)
        raise typer.Exit(code=3)
    if result.status == "failed":
        typer.echo(f"✗ ingest failed: {result.error}", err=True)
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
