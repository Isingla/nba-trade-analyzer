"""Fan-readable rendering of a :class:`TradeGrade`.

Extracted from ``scripts/grade_trades.py`` so the demo script and the CLI share
one formatter — the terminal report looks identical from either entry point.
``print_report`` renders legality, each team's score + verdict, the seven metric
breakdowns, draft capital, and the basketball-prose verdict.
"""

from __future__ import annotations

import io
import sys
import textwrap

from nba_trade_analyzer.models.grade import MetricBreakdown, TeamGrade, TradeGrade
from nba_trade_analyzer.models.trade import Trade, TradeAssets

_WIDTH = 64
_HEAVY = "═" * _WIDTH
_LIGHT = "─" * 41


def force_utf8_stdout() -> None:
    """Rewrap stdout in utf-8.

    The Windows console defaults to cp1252, which can't encode the box-drawing
    characters, arrows, and check/cross marks this report prints — without this
    the first such character raises ``UnicodeEncodeError``. No-op where stdout
    has no underlying byte buffer (e.g. pytest's captured stdout).
    """
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", line_buffering=True
        )


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


def print_report(trade: Trade, grade: TradeGrade, title: str | None = None) -> None:
    """Render a graded trade to stdout — legality, both team grades, verdicts."""
    a_label = trade.team_a.name.split()[-1]
    b_label = trade.team_b.name.split()[-1]
    print(_HEAVY)
    print(title if title else f"TRADE: {a_label} ↔ {b_label}")
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
