"""Phase 7 demo: run four real 2025-26 trades through the trade grader.

Reuses the four scenarios and the data-loading / roster-building helpers from
``end_to_end_test.py``, then renders each :class:`TradeGrade` as a fan-readable
report: legality, per-team score + verdict, seven metric breakdowns, draft
capital, and a basketball-prose verdict.

Diagnostic only — not wired into the package or tests. Run from repo root:

    uv run python scripts/grade_trades.py
"""

from __future__ import annotations

import textwrap

# When run as a script, this file's directory (scripts/) is on sys.path, so the
# sibling end_to_end_test module imports directly.
from end_to_end_test import (
    SCENARIOS,
    TradeScenario,
    _augment_with_epm_position,
    _build_roster_entries,
    _build_team,
    _force_utf8_stdout,
)

from nba_trade_analyzer.data.darko import fetch_darko_data
from nba_trade_analyzer.data.epm import fetch_epm_data
from nba_trade_analyzer.data.players import fetch_player_stats
from nba_trade_analyzer.data.salaries import fetch_all_salaries
from nba_trade_analyzer.engine.grader import grade_trade
from nba_trade_analyzer.models.grade import MetricBreakdown, TeamGrade, TradeGrade
from nba_trade_analyzer.models.trade import Trade, TradeAssets

_WIDTH = 64
_HEAVY = "═" * _WIDTH
_LIGHT = "─" * 41


def _wrap(text: str, indent: str = "    ") -> str:
    return textwrap.fill(
        text,
        width=_WIDTH,
        initial_indent=indent,
        subsequent_indent=indent,
    )


def _receive_line(grade_side: TeamGrade, incoming: TradeAssets) -> str:
    parts = list(grade_side.players_acquired)
    parts.extend(pick.label for pick in incoming.picks)
    return ", ".join(parts) if parts else "cap relief"


def _asset_names(assets: TradeAssets) -> str:
    """Player names + pick labels in an asset package, for the send/receive lines."""
    parts = [entry.player.name for entry in assets.players]
    parts.extend(pick.label for pick in assets.picks)
    return ", ".join(parts) if parts else "nothing"


def _print_metric(title: str, mb: MetricBreakdown) -> None:
    print(f"  {title}")
    print(f"    {mb.raw_label}  ({mb.tier})")
    print(_wrap(f"→ {mb.explanation}", indent="    "))


def _print_team(grade_side: TeamGrade, incoming: TradeAssets) -> None:
    print(_LIGHT)
    short = grade_side.team_name.split()[-1].upper()
    print(f"{short} receive: {_receive_line(grade_side, incoming)}")
    print(f"Score: {grade_side.score} / 100 — {grade_side.verdict}")
    print(_LIGHT)
    print()
    _print_metric("IMPACT", grade_side.impact)
    print()
    _print_metric("CONTRACT", grade_side.contract)
    print()
    _print_metric("WIN CURVE", grade_side.win_curve)
    print()
    _print_metric("TIMELINE", grade_side.timeline)
    print()
    _print_metric("POSITIONAL FIT", grade_side.positional_fit)
    print()
    _print_metric("SPACING", grade_side.spacing)
    print()
    print("  DRAFT CAPITAL")
    for line in grade_side.draft_capital.picks_description:
        print(f"    • {line}")
    print(_wrap(f"→ {grade_side.draft_capital.explanation}", indent="    "))
    print()
    print("  VERDICT")
    print(_wrap(grade_side.prose, indent="    "))
    print()


def _print_report(scenario: TradeScenario, trade: Trade, grade: TradeGrade) -> None:
    a_label = trade.team_a.name.split()[-1]
    b_label = trade.team_b.name.split()[-1]
    print(_HEAVY)
    print(f"TRADE: {a_label} ↔ {b_label}")
    print(_HEAVY)
    print()
    if not grade.is_legal:
        # Show what each side would have moved, even though the deal is dead —
        # the asset names live on the Trade object regardless of legality.
        print(f"{a_label} send: {_asset_names(trade.team_a_sends)}")
        print(f"{a_label} receive: {_asset_names(trade.team_b_sends)}")
        print()
        print(f"{b_label} send: {_asset_names(trade.team_b_sends)}")
        print(f"{b_label} receive: {_asset_names(trade.team_a_sends)}")
        print()
        print(f"LEGALITY: ❌ Illegal — {grade.illegal_reason}")
        print()
        print("Trade evaluation stops here — this deal doesn't work under the CBA.")
        print()
        print(_HEAVY)
        print()
        return
    print("LEGALITY: ✅ Legal")
    print()
    # Team A receives team B's outgoing assets, and vice versa.
    _print_team(grade.team_a_grade, trade.team_b_sends)
    _print_team(grade.team_b_grade, trade.team_a_sends)
    print(_HEAVY)
    print()


def main() -> None:
    _force_utf8_stdout()
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
        _print_report(scenario, trade, grade)


if __name__ == "__main__":
    main()
