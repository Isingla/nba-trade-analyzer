"""Phase 7 demo: run real 2025-26 trades through the trade grader.

Reuses the four scenarios and the data-loading / roster-building helpers from
``end_to_end_test.py``, then renders each :class:`TradeGrade` via the shared
``nba_trade_analyzer.report`` formatter (the same one the CLI uses).

Diagnostic only — not wired into the package or tests. Run from repo root:

    uv run python scripts/grade_trades.py
"""

from __future__ import annotations

from dataclasses import dataclass, field

# When run as a script, this file's directory (scripts/) is on sys.path, so the
# sibling end_to_end_test module imports directly.
from end_to_end_test import (
    SCENARIOS,
    _augment_with_epm_position,
    _build_player,
    _build_roster_entries,
    _build_team,
    _cap_status_for,
)

from nba_trade_analyzer.data.darko import fetch_darko_data
from nba_trade_analyzer.data.epm import fetch_epm_data, get_player_epm, normalize_name
from nba_trade_analyzer.data.players import fetch_player_stats
from nba_trade_analyzer.data.salaries import (
    build_contract,
    fetch_all_salaries,
    get_player_salary,
)
from nba_trade_analyzer.engine.grader import grade_trade
from nba_trade_analyzer.models.draft_pick import DraftPick
from nba_trade_analyzer.models.player import Contract
from nba_trade_analyzer.models.team import RosterEntry, Team
from nba_trade_analyzer.models.trade import Trade, TradeAssets
from nba_trade_analyzer.report import force_utf8_stdout, print_report

# ---------------------------------------------------------------------------
# Manually-specified trades
#
# The SCENARIOS path (shared with end_to_end_test) hardcodes every contract to
# one year remaining. These trades carry real multi-year terms and options, so
# they exercise the multi-year valuation properly — exactly what differentiates
# an expiring deal from a long-term commitment in the grade.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ManualPlayer:
    name: str
    fallback: Contract  # used only if the salary scraper has no row for the player


@dataclass(frozen=True)
class _ManualSide:
    players: list[_ManualPlayer] = field(default_factory=list)
    picks: list[DraftPick] = field(default_factory=list)


@dataclass(frozen=True)
class _ManualTrade:
    title: str
    team_a_name: str
    team_a_abbr: str
    team_a_payroll: int
    team_b_name: str
    team_b_abbr: str
    team_b_payroll: int
    team_a_sends: _ManualSide
    team_b_sends: _ManualSide


MANUAL_TRADES: list[_ManualTrade] = [
    _ManualTrade(
        title="Trade 5: Lakers ↔ Mavericks (Gafford)",
        team_a_name="Los Angeles Lakers",
        team_a_abbr="LAL",
        team_a_payroll=192_000_000,  # over cap, under the first apron
        team_b_name="Dallas Mavericks",
        team_b_abbr="DAL",
        team_b_payroll=178_000_000,  # over cap, under the first apron
        team_a_sends=_ManualSide(
            players=[
                _ManualPlayer(
                    "Jarred Vanderbilt",
                    Contract(
                        salary=11_571_429, years_remaining=3, has_player_option=True
                    ),
                ),
                _ManualPlayer(
                    "Dalton Knecht",
                    Contract(salary=4_010_160, years_remaining=3, has_team_option=True),
                ),
            ],
            picks=[DraftPick(team="LAL", year=2026, round=1)],
        ),
        team_b_sends=_ManualSide(
            players=[
                _ManualPlayer(
                    "Daniel Gafford",
                    Contract(salary=14_386_320, years_remaining=4),
                ),
            ],
        ),
    ),
]


def _manual_entry(
    mp: _ManualPlayer,
    epm_df,
    stats_lookup: dict,
    salary_df,
) -> RosterEntry:
    """Build a RosterEntry, preferring the scraped contract over the fallback."""
    epm_row = get_player_epm(epm_df, mp.name)
    stats_row = stats_lookup.get(normalize_name(mp.name))
    player = _build_player(mp.name, epm_row, stats_row)
    salary_data = get_player_salary(salary_df, mp.name)
    contract = build_contract(salary_data) if salary_data is not None else mp.fallback
    return RosterEntry(player=player, contract=contract)


def _manual_assets(
    side: _ManualSide, epm_df, stats_lookup: dict, salary_df
) -> TradeAssets:
    return TradeAssets(
        players=[
            _manual_entry(mp, epm_df, stats_lookup, salary_df) for mp in side.players
        ],
        picks=list(side.picks),
    )


def _manual_team(name: str, abbr: str, payroll: int) -> Team:
    return Team(
        name=name,
        abbreviation=abbr,
        total_payroll=payroll,
        cap_status=_cap_status_for(payroll),
    )


def main() -> None:
    force_utf8_stdout()
    print("Loading data (EPM, nba_api stats, salaries, DARKO)...")
    epm_df = fetch_epm_data()
    stats_df = fetch_player_stats()
    salary_df = fetch_all_salaries()
    darko_df = fetch_darko_data()
    stats_df = _augment_with_epm_position(stats_df, epm_df)
    stats_lookup = {
        row["player_name_normalized"]: row for _, row in stats_df.iterrows()
    }
    print()

    for scenario in SCENARIOS:
        team_a_entries = _build_roster_entries(
            scenario.team_a_players, epm_df, stats_lookup, salary_df
        )
        team_b_entries = _build_roster_entries(
            scenario.team_b_players, epm_df, stats_lookup, salary_df
        )
        trade = Trade(
            team_a=_build_team(scenario.team_a_abbr),
            team_b=_build_team(scenario.team_b_abbr),
            team_a_sends=TradeAssets(
                players=team_a_entries, picks=list(scenario.team_a_picks)
            ),
            team_b_sends=TradeAssets(
                players=team_b_entries, picks=list(scenario.team_b_picks)
            ),
        )
        grade = grade_trade(
            trade,
            player_stats_df=stats_df,
            epm_df=epm_df,
            darko_df=darko_df,
            salary_df=salary_df,
        )
        print_report(trade, grade)

    for manual in MANUAL_TRADES:
        trade = Trade(
            team_a=_manual_team(
                manual.team_a_name, manual.team_a_abbr, manual.team_a_payroll
            ),
            team_b=_manual_team(
                manual.team_b_name, manual.team_b_abbr, manual.team_b_payroll
            ),
            team_a_sends=_manual_assets(
                manual.team_a_sends, epm_df, stats_lookup, salary_df
            ),
            team_b_sends=_manual_assets(
                manual.team_b_sends, epm_df, stats_lookup, salary_df
            ),
        )
        grade = grade_trade(
            trade,
            player_stats_df=stats_df,
            epm_df=epm_df,
            darko_df=darko_df,
            salary_df=salary_df,
        )
        print_report(trade, grade, title=manual.title)


if __name__ == "__main__":
    main()
