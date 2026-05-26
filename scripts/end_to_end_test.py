"""End-to-end diagnostic on four real 2025-26 trade scenarios.

Runs the full pipeline (salary matching → EPM valuation → team-context
adjustments) and prints a readable per-trade report. Diagnostic only —
not wired into the package or tests.

Run from repo root:
    uv run python scripts/end_to_end_test.py
"""

from __future__ import annotations

import io
import sys
from dataclasses import dataclass

import pandas as pd

from nba_trade_analyzer.data.epm import fetch_epm_data, get_player_epm, normalize_name
from nba_trade_analyzer.data.players import fetch_player_stats
from nba_trade_analyzer.engine.constants import (
    FIRST_APRON,
    LEAGUE_AVG_3PT_PCT,
    LEAGUE_AVG_3PT_RATE,
    SALARY_CAP,
    SECOND_APRON,
)
from nba_trade_analyzer.engine.draft_picks import calculate_pick_value_with_protections
from nba_trade_analyzer.engine.salary_rules import check_trade_legality
from nba_trade_analyzer.engine.team_context import (
    _effective_win_curve,
    evaluate_player_in_team_context,
)
from nba_trade_analyzer.engine.valuation import evaluate_player
from nba_trade_analyzer.models.player import Contract, Player
from nba_trade_analyzer.models.team import CapStatus, RosterEntry, Team
from nba_trade_analyzer.models.trade import Trade, TradeAssets


def _force_utf8_stdout() -> None:
    """Rewrap stdout in utf-8 — Windows console defaults to cp1252, which
    breaks engine error strings (em-dashes) and our own arrows. Called from
    ``main()`` only so importing this module doesn't disturb pytest's
    captured stdout (the filename matches pytest's ``*_test.py`` pattern)."""
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", line_buffering=True
        )


# ---------------------------------------------------------------------------
# HARDCODED 2025-26 SALARIES — TODO: replace once data/salaries.py exists.
# Approximate figures sourced from public salary trackers (Spotrac / HoopsHype).
# ---------------------------------------------------------------------------
SALARIES_2025_26: dict[str, int] = {
    "Julius Randle": 30_935_520,
    "Rudy Gobert": 43_827_586,
    "Kyle Kuzma": 23_500_000,
    "Rui Hachimura": 17_000_000,
    "Jimmy Butler": 52_400_000,
    "Andrew Wiggins": 26_270_000,
    "Kevon Looney": 8_000_000,
    "Mark Williams": 4_200_000,
    "Gordon Hayward": 31_500_000,
    "Kenrich Williams": 6_000_000,
}

# ---------------------------------------------------------------------------
# Approximate 2025-26 team total payrolls (USD). Drives apron-tier selection
# in the salary-matching engine. Realistic ballpark figures from public cap
# trackers — TODO: pull from a salaries data source.
# ---------------------------------------------------------------------------
TEAM_PAYROLLS_2025_26: dict[str, int] = {
    "NYK": 198_000_000,  # over first apron (hard-capped)
    "MIN": 205_000_000,  # near second apron
    "WAS": 168_000_000,  # over cap, under first apron
    "LAL": 192_000_000,  # at/just under first apron
    "MIA": 188_000_000,  # over cap, under first apron
    "GSW": 204_000_000,  # near second apron
    "CHA": 158_000_000,  # over cap, under first apron
    "OKC": 165_000_000,  # over cap, under first apron
}

TEAM_NAMES: dict[str, str] = {
    "NYK": "New York Knicks",
    "MIN": "Minnesota Timberwolves",
    "WAS": "Washington Wizards",
    "LAL": "Los Angeles Lakers",
    "MIA": "Miami Heat",
    "GSW": "Golden State Warriors",
    "CHA": "Charlotte Hornets",
    "OKC": "Oklahoma City Thunder",
}

# Per-task realistic 2025-26 win projections.
TEAM_PROJECTED_WINS: dict[str, float] = {
    "NYK": 50.0,
    "MIN": 45.0,
    "WAS": 20.0,
    "LAL": 48.0,
    "MIA": 46.0,
    "GSW": 47.0,
    "CHA": 25.0,
    "OKC": 60.0,
}

# Estimated landing pick numbers for the picks referenced in the trades. Pick
# numbers track team strength loosely (worse team → higher pick). Protections
# are encoded as the top-N "doesn't convey" cutoff used by the pick-value
# helper. TODO: integrate with a real pick-projection source.
PICK_ESTIMATES: dict[str, tuple[int, int | None]] = {
    "2027 LAL 1st (top-10 protected)": (17, 10),
    "2026 GSW 1st (unprotected)": (17, None),
    "2028 GSW 1st (top-5 protected)": (18, 5),
    "2027 OKC 2nd": (58, None),
}

# Fallback when a player has no GP/MPG row in nba_api (early season, injured,
# new acquisition). Picks a reasonable "regular starter" load so wins_added
# isn't pinned to zero.
DEFAULT_GP = 60
DEFAULT_MPG = 30.0


# ---------------------------------------------------------------------------
# Trade scenarios
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TradeScenario:
    title: str
    team_a_abbr: str
    team_b_abbr: str
    team_a_players: tuple[str, ...]
    team_b_players: tuple[str, ...]
    team_a_picks: tuple[str, ...] = ()
    team_b_picks: tuple[str, ...] = ()
    context_summary: str = ""


SCENARIOS: list[TradeScenario] = [
    TradeScenario(
        title="Trade 1 — Contender upgrade: Randle for Gobert",
        team_a_abbr="NYK",
        team_b_abbr="MIN",
        team_a_players=("Julius Randle",),
        team_b_players=("Rudy Gobert",),
        context_summary=(
            "Knicks ~50 wins, Wolves ~45. Tests win curve for two contenders, "
            "positional fit (NYK needs C, MIN needs F), spacing (Gobert is a "
            "non-shooter)."
        ),
    ),
    TradeScenario(
        title="Trade 2 — Rebuilder sells: Kuzma to Lakers",
        team_a_abbr="WAS",
        team_b_abbr="LAL",
        team_a_players=("Kyle Kuzma",),
        team_b_players=("Rui Hachimura",),
        team_b_picks=("2027 LAL 1st (top-10 protected)",),
        context_summary=(
            "Wizards ~20 wins (tanking), Lakers ~48 (contending). Tests win "
            "curve asymmetry and timeline: Kuzma (30) into older LAL core, "
            "Hachimura (27) into young WAS core."
        ),
    ),
    TradeScenario(
        title="Trade 3 — Superstar blockbuster: Butler to Warriors",
        team_a_abbr="MIA",
        team_b_abbr="GSW",
        team_a_players=("Jimmy Butler",),
        team_b_players=("Andrew Wiggins", "Kevon Looney"),
        team_b_picks=(
            "2026 GSW 1st (unprotected)",
            "2028 GSW 1st (top-5 protected)",
        ),
        context_summary=(
            "Heat ~46 wins, Warriors ~47. Tests large salary mismatch, draft "
            "pick valuation, and aging-star timeline."
        ),
    ),
    TradeScenario(
        title="Trade 4 — Salary dump: Williams + Hayward to Thunder",
        team_a_abbr="CHA",
        team_b_abbr="OKC",
        team_a_players=("Mark Williams", "Gordon Hayward"),
        team_b_players=("Kenrich Williams",),
        team_b_picks=("2027 OKC 2nd",),
        context_summary=(
            "Hornets ~25 wins, Thunder ~60. Tests salary-dump mechanics, "
            "positional fit (OKC center need), win-curve extremes."
        ),
    ),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cap_status_for(payroll: int) -> CapStatus:
    if payroll < SALARY_CAP:
        return CapStatus.UNDER_CAP
    if payroll < FIRST_APRON:
        return CapStatus.OVER_CAP
    if payroll < SECOND_APRON:
        return CapStatus.FIRST_APRON
    return CapStatus.SECOND_APRON


def _matching_rule(payroll: int) -> str:
    if payroll < SALARY_CAP:
        return "Room TPE (under cap)"
    if payroll < FIRST_APRON:
        return "Expanded TPE (over cap, under first apron)"
    if payroll < SECOND_APRON:
        return "First Apron — 100% matching, no $250K cushion"
    return "Second Apron — 100% matching, aggregation block"


def _build_team(abbr: str) -> Team:
    payroll = TEAM_PAYROLLS_2025_26[abbr]
    return Team(
        name=TEAM_NAMES[abbr],
        abbreviation=abbr,
        total_payroll=payroll,
        cap_status=_cap_status_for(payroll),
    )


def _money(amount: float) -> str:
    sign = "-" if amount < 0 else " "
    return f"{sign}${abs(amount) / 1_000_000:6.2f}M"


def _build_player(
    name: str,
    epm_row: pd.Series | None,
    stats_row: pd.Series | dict | None,
) -> Player:
    """Construct a Player. Pull team/age from EPM; GP/MPG from nba_api stats."""
    if epm_row is not None:
        team = str(epm_row["team"])
        age = int(epm_row["age"])
        epm_mpg = float(epm_row["mpg"])
    else:
        team = "FA"
        age = 0
        epm_mpg = DEFAULT_MPG

    if isinstance(stats_row, pd.Series):
        gp = int(stats_row.get("GP", 0) or 0)
        mpg = float(stats_row.get("MPG", 0.0) or 0.0)
        if "age" in stats_row.index and stats_row["age"] is not None:
            age = int(stats_row["age"] or age)
        net_rating = float(stats_row.get("NET_RATING", 0.0) or 0.0)
    else:
        gp = DEFAULT_GP
        mpg = epm_mpg if epm_mpg > 0 else DEFAULT_MPG
        net_rating = 0.0

    # If nba_api had no row (e.g. didn't play this season), fall back to a
    # reasonable starter load so wins_added isn't pinned to zero.
    if gp <= 0 or mpg <= 0:
        gp = DEFAULT_GP
        mpg = epm_mpg if epm_mpg > 0 else DEFAULT_MPG

    return Player(
        name=name,
        team=team,
        age=age,
        stats={"NET_RATING": net_rating, "GP": float(gp), "MPG": mpg},
    )


def _build_roster_entries(
    names: tuple[str, ...],
    epm_df: pd.DataFrame,
    stats_lookup: dict[str, pd.Series],
) -> list[RosterEntry]:
    entries: list[RosterEntry] = []
    for name in names:
        epm_row = get_player_epm(epm_df, name)
        stats_row = stats_lookup.get(normalize_name(name))
        player = _build_player(name, epm_row, stats_row)
        salary = SALARIES_2025_26[name]
        entries.append(
            RosterEntry(
                player=player,
                contract=Contract(salary=salary, years_remaining=1),
            )
        )
    return entries


def _team_roster_dicts(stats_df: pd.DataFrame, team_abbr: str) -> list[dict]:
    """Roster dicts for the team-context engine. Filter the league df by team."""
    team_df = stats_df[stats_df["team"] == team_abbr]
    return team_df.to_dict(orient="records")


def _augment_with_epm_position(
    stats_df: pd.DataFrame, epm_df: pd.DataFrame
) -> pd.DataFrame:
    """Attach EPM ``position`` onto the player_stats df via normalized name.

    nba_api doesn't expose a positional label, but team_context needs one for
    the positional-fit calc. EPM has it — merge on normalized name.
    """
    stats_df = stats_df.copy()
    stats_df["player_name_normalized"] = stats_df["player_name"].map(normalize_name)
    pos_lookup = dict(
        zip(epm_df["player_name_normalized"], epm_df["position"], strict=True)
    )
    stats_df["position"] = stats_df["player_name_normalized"].map(pos_lookup)
    return stats_df


def _pick_value(pick_label: str) -> float:
    pick_number, protection_top = PICK_ESTIMATES[pick_label]
    return calculate_pick_value_with_protections(pick_number, protection_top)


# ---------------------------------------------------------------------------
# Per-trade evaluation
# ---------------------------------------------------------------------------


def _evaluate_side(
    incoming_entries: list[RosterEntry],
    outgoing_entries: list[RosterEntry],
    incoming_picks: tuple[str, ...],
    outgoing_picks: tuple[str, ...],
    acquiring_team_abbr: str,
    epm_df: pd.DataFrame,
    stats_df: pd.DataFrame,
) -> dict:
    """Compute base + team-adjusted surplus deltas for one side of the trade.

    All evaluations use the acquiring team's roster context — that's the
    frame the trade is being judged from.
    """
    acquiring_wins = TEAM_PROJECTED_WINS[acquiring_team_abbr]
    acquiring_roster = _team_roster_dicts(stats_df, acquiring_team_abbr)
    # Same outgoing list for incoming and outgoing players: the trade vacates
    # those slots, so positional fit on both sides should reflect the gap.
    outgoing_names = [entry.player.name for entry in outgoing_entries]

    def _eval_player(entry: RosterEntry) -> tuple[float, dict]:
        base = evaluate_player(
            entry.player,
            entry.contract,
            epm_df=epm_df,
            darko_df=None,
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

        epm_row = get_player_epm(epm_df, entry.player.name)
        if epm_row is not None:
            epm_off = float(epm_row["epm_off"])
            epm_def = float(epm_row["epm_def"])
            position = str(epm_row.get("position") or "")
        else:
            epm_off = epm_def = 0.0
            position = ""

        stats_match = stats_df[
            stats_df["player_name_normalized"] == normalize_name(entry.player.name)
        ]
        # fg3_data_present gates the prose: when False, no league-average
        # fallback fools us into calling a player a "reliable shooter."
        fg3_data_present = False
        fg3a = 0.0
        if not stats_match.empty:
            srow = stats_match.iloc[0]
            raw_rate = srow.get("FG3_RATE")
            raw_pct = srow.get("FG3_PCT")
            raw_fg3a = srow.get("FG3A")
            rate_ok = raw_rate is not None and not pd.isna(raw_rate)
            pct_ok = raw_pct is not None and not pd.isna(raw_pct)
            fg3_data_present = rate_ok and pct_ok
            fg3_rate = float(raw_rate) if rate_ok else LEAGUE_AVG_3PT_RATE
            fg3_pct = float(raw_pct) if pct_ok else LEAGUE_AVG_3PT_PCT
            fg3a = (
                float(raw_fg3a)
                if raw_fg3a is not None and not pd.isna(raw_fg3a)
                else 0.0
            )
        else:
            fg3_rate, fg3_pct = LEAGUE_AVG_3PT_RATE, LEAGUE_AVG_3PT_PCT

        return base.surplus_value, {
            "name": entry.player.name,
            "salary": entry.contract.salary,
            "age": entry.player.age,
            "metric_source": base.metric_source,
            "wins_added": base.wins_added,
            "base_player_value": base.player_value,
            "base_surplus": base.surplus_value,
            "win_curve": adjusted.win_curve_multiplier,
            "effective_curve": _effective_win_curve(
                adjusted.win_curve_multiplier, base.wins_added
            ),
            "timeline": adjusted.timeline_modifier,
            "positional": adjusted.positional_modifier,
            "spacing": adjusted.spacing_modifier,
            "team_adjusted_value": adjusted.team_adjusted_value,
            "team_surplus": adjusted.team_surplus,
            "epm_off": epm_off,
            "epm_def": epm_def,
            "position": position,
            "fg3_rate": fg3_rate,
            "fg3_pct": fg3_pct,
            "fg3a": fg3a,
            "fg3_data_present": fg3_data_present,
        }

    incoming_player_details = [_eval_player(e) for e in incoming_entries]
    outgoing_player_details = [_eval_player(e) for e in outgoing_entries]

    incoming_base = sum(b for b, _ in incoming_player_details)
    outgoing_base = sum(b for b, _ in outgoing_player_details)
    incoming_adjusted = sum(d["team_surplus"] for _, d in incoming_player_details)
    outgoing_adjusted = sum(d["team_surplus"] for _, d in outgoing_player_details)

    incoming_pick_value = sum(_pick_value(p) for p in incoming_picks)
    outgoing_pick_value = sum(_pick_value(p) for p in outgoing_picks)

    return {
        "incoming_player_details": [d for _, d in incoming_player_details],
        "outgoing_player_details": [d for _, d in outgoing_player_details],
        "incoming_base": incoming_base,
        "outgoing_base": outgoing_base,
        "incoming_adjusted": incoming_adjusted,
        "outgoing_adjusted": outgoing_adjusted,
        "incoming_pick_value": incoming_pick_value,
        "outgoing_pick_value": outgoing_pick_value,
        "net_base": (incoming_base + incoming_pick_value)
        - (outgoing_base + outgoing_pick_value),
        "net_adjusted": (incoming_adjusted + incoming_pick_value)
        - (outgoing_adjusted + outgoing_pick_value),
    }


# ---------------------------------------------------------------------------
# Verdict prose helpers
# ---------------------------------------------------------------------------


# Short team labels used in the prose (less stilted than abbreviations).
TEAM_LABEL: dict[str, str] = {
    "NYK": "Knicks",
    "MIN": "Wolves",
    "WAS": "Wizards",
    "LAL": "Lakers",
    "MIA": "Heat",
    "GSW": "Warriors",
    "CHA": "Hornets",
    "OKC": "Thunder",
}


def _archetype(epm_off: float, epm_def: float) -> str:
    """One-phrase player archetype from EPM offense/defense splits."""
    if epm_off >= 3.0 and epm_def >= 1.0:
        return "two-way star"
    if epm_off >= 2.0 and epm_def >= 0.5:
        return "two-way wing"
    if epm_def >= 2.0 and epm_off < 1.0:
        return "defensive anchor"
    if epm_def >= 1.0 and epm_off < 0.5:
        return "defensive specialist"
    if epm_off >= 2.5:
        return "offensive engine"
    if epm_off >= 1.0:
        return "scoring forward"
    if epm_off >= 0 and epm_def >= 0:
        return "solid role player"
    if epm_off < -1.5 or epm_def < -1.5:
        return "below-replacement piece"
    return "fringe rotation player"


def _shooting_profile(rate: float, pct: float, fg3a: float, data_present: bool) -> str:
    """One-phrase shooting/spacing profile.

    When ``data_present`` is False the inputs are league-average fallbacks
    and carry no signal — never claim the player is a shooter on that basis.
    Volume gate (``fg3a < 1.0`` or ``rate < 0.15``) catches non-shooters
    with technically-valid but negligible 3PT samples (Gobert, Looney).
    """
    if not data_present:
        return "non-shooter (no shooting data)"
    if fg3a < 1.0 or rate < 0.15:
        return "non-shooter"
    if rate >= 0.45 and pct >= 0.37:
        return "elite floor spacer"
    if rate >= 0.35 and pct >= 0.36:
        return "reliable shooter"
    if rate >= 0.30 and pct < 0.32:
        return "high-volume, low-efficiency shooter"
    if rate < 0.25:
        return "low-volume shooter"
    return "average spacer"


def _positional_phrase(modifier: float, position: str | None) -> str:
    pos = position or "his position"
    if modifier >= 0.05:
        return f"fills a real need at {pos}"
    if modifier > 0:
        return f"a modest upgrade at {pos}"
    if modifier <= -0.05:
        return f"creates a logjam at {pos}"
    if modifier < 0:
        return f"slight positional overlap at {pos}"
    return f"positionally neutral at {pos}"


def _win_curve_phrase(mult: float) -> str:
    if mult >= 1.5:
        return "contender premium"
    if mult >= 1.15:
        return "playoff-team value"
    if mult >= 0.9:
        return "neutral team context"
    if mult <= 0.6:
        return "rebuilding discount"
    return "below-playoff discount"


def _timeline_phrase(modifier: float) -> str:
    if modifier >= 0.05:
        return "aligns with the core's timeline"
    if modifier <= -0.05:
        return "off-cycle with the team's window"
    if modifier > 0:
        return "roughly on the team's timeline"
    return "small timeline drag"


def _pick_headline(details: list[dict]) -> dict | None:
    if not details:
        return None
    return max(details, key=lambda d: abs(d["wins_added"]))


def _player_phrase(detail: dict) -> str:
    """Compact 'Name — archetype, shooting profile' phrase."""
    return (
        f"{detail['name']} — a {_archetype(detail['epm_off'], detail['epm_def'])} "
        f"({_shooting_profile(detail['fg3_rate'], detail['fg3_pct'], detail['fg3a'], detail['fg3_data_present'])})"
    )


def _natural_join(items: list[str]) -> str:
    """Oxford-comma join: 'a' / 'a and b' / 'a, b, and c'."""
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + f", and {items[-1]}"


def _outgoing_phrase(details: list[dict]) -> str:
    """One outgoing player gets the full archetype phrase; multiple are listed
    by name only and read naturally — descriptive context for the headline
    player still surfaces in the loser's-perspective sentence."""
    if not details:
        return "cap relief"
    if len(details) == 1:
        return _player_phrase(details[0])
    return _natural_join([d["name"] for d in details])


def _build_verdict(
    scenario: TradeScenario,
    side_a: dict,
    side_b: dict,
) -> list[str]:
    """Generate 3-5 sentences of basketball-prose trade commentary."""
    a = scenario.team_a_abbr
    b = scenario.team_b_abbr
    delta = side_a["net_adjusted"] - side_b["net_adjusted"]

    if abs(delta) < 1_000_000:
        return [
            f"{TEAM_LABEL[a]} and {TEAM_LABEL[b]} land within $1M of each other "
            f"on adjusted surplus — call it a wash.",
        ]

    if delta > 0:
        winner_abbr, loser_abbr = a, b
        winner_side, loser_side = side_a, side_b
        incoming_picks = scenario.team_b_picks
    else:
        winner_abbr, loser_abbr = b, a
        winner_side, loser_side = side_b, side_a
        incoming_picks = scenario.team_a_picks

    abs_delta_m = abs(delta) / 1_000_000
    winner_label = TEAM_LABEL[winner_abbr]

    headline_in = _pick_headline(winner_side["incoming_player_details"])
    out_phrase = _outgoing_phrase(winner_side["outgoing_player_details"])
    loser_headline_in = _pick_headline(loser_side["incoming_player_details"])

    sentences = [
        f"{winner_abbr} wins this trade by ${abs_delta_m:.1f}M adjusted surplus."
    ]

    # Sentence 2: what the winner gets, in basketball terms (+ picks if any).
    # Keep positional clause adjacent to the player name; tack picks on after.
    if headline_in is not None:
        in_segment = _player_phrase(headline_in)
        pos_clause = _positional_phrase(
            headline_in["positional"], headline_in["position"]
        )
        if pos_clause:
            in_segment += f" who {pos_clause}"
        other_incoming = [
            d for d in winner_side["incoming_player_details"] if d is not headline_in
        ]
        if other_incoming:
            in_segment += (
                f", along with {_natural_join([d['name'] for d in other_incoming])}"
            )
    else:
        in_segment = "a draft-only package"

    if incoming_picks:
        pick_value_m = winner_side["incoming_pick_value"] / 1_000_000
        in_segment += (
            f", plus {' + '.join(incoming_picks)} "
            f"(~${pick_value_m:.1f}M expected pick surplus)"
        )

    sentences.append(f"{winner_label} land {in_segment}, and ship out {out_phrase}.")

    # Sentence 3: why team context tilts it. Anchor on the loser's headline
    # incoming player — that's the piece they're "paying for" through the curve.
    if loser_headline_in is not None:
        wc = loser_headline_in["win_curve"]
        wc_phrase = _win_curve_phrase(wc)
        loser_wins = TEAM_PROJECTED_WINS[loser_abbr]
        if loser_headline_in["wins_added"] < 0:
            third = (
                f"{loser_abbr}'s {wc_phrase} (x{wc:.2f}) on a {loser_wins:.0f}-win "
                f"roster can't rescue paying {loser_headline_in['name']} "
                f"${loser_headline_in['salary'] / 1_000_000:.1f}M for negative "
                f"on-court impact — damping pulls the loss toward the flat unit "
                f"but doesn't erase it."
            )
        else:
            tl_clause = ""
            tl = loser_headline_in["timeline"]
            if tl <= -0.04:
                tl_clause = (
                    f", and the timeline misalignment ({tl:+.2f}) compounds the issue"
                )
            elif tl >= 0.04:
                tl_clause = (
                    f" — timeline alignment ({tl:+.2f}) softens the blow but "
                    f"doesn't close the gap"
                )
            third = (
                f"{loser_abbr}'s {wc_phrase} (x{wc:.2f}) on a {loser_wins:.0f}-win "
                f"roster lifts {loser_headline_in['name']}'s production value, "
                f"but his ${loser_headline_in['salary'] / 1_000_000:.1f}M salary "
                f"still leaves it underwater{tl_clause}."
            )
        sentences.append(third)

    # Sentence 4 (optional): if the winner's incoming player has a notable
    # contender boost OR if picks are the real story, call it out separately.
    if headline_in is not None:
        winner_wins = TEAM_PROJECTED_WINS[winner_abbr]
        winner_wc = headline_in["win_curve"]
        if headline_in["wins_added"] > 0 and winner_wc >= 1.15:
            sentences.append(
                f"On a {winner_wins:.0f}-win roster, {headline_in['name']}'s "
                f"impact gets the {_win_curve_phrase(winner_wc)} treatment "
                f"(x{winner_wc:.2f}), which is what tips the ledger for "
                f"{winner_label}."
            )
        elif incoming_picks and winner_side["incoming_pick_value"] >= 20_000_000:
            sentences.append(
                f"For {winner_label}, the draft capital does most of the lifting "
                f"— the player return is secondary to "
                f"${winner_side['incoming_pick_value'] / 1_000_000:.1f}M in expected "
                f"pick surplus."
            )

    return sentences


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _print_separator(char: str = "=", width: int = 96) -> None:
    print(char * width)


def _print_player_detail(d: dict, indent: str = "    ") -> None:
    print(
        f"{indent}{d['name']:<22} "
        f"src={d['metric_source']:<10} "
        f"wins_added={d['wins_added']:+5.2f}  "
        f"base_val={_money(d['base_player_value'])}  "
        f"salary={_money(d['salary'])}  "
        f"base_surplus={_money(d['base_surplus'])}"
    )
    print(
        f"{indent}  >> team_curve x{d['win_curve']:.2f}  "
        f"effective_curve x{d['effective_curve']:.2f}  "
        f"timeline {d['timeline']:+.3f}  "
        f"positional {d['positional']:+.3f}  "
        f"spacing {d['spacing']:+.3f}  "
        f"-> team_val={_money(d['team_adjusted_value'])}  "
        f"team_surplus={_money(d['team_surplus'])}"
    )


def _print_trade_report(
    scenario: TradeScenario,
    trade: Trade,
    side_a: dict,
    side_b: dict,
) -> None:
    _print_separator()
    print(scenario.title.replace("—", "--"))
    _print_separator()
    print(f"Context: {scenario.context_summary}")
    print()

    a_abbr = scenario.team_a_abbr
    b_abbr = scenario.team_b_abbr
    a_payroll = TEAM_PAYROLLS_2025_26[a_abbr]
    b_payroll = TEAM_PAYROLLS_2025_26[b_abbr]

    # --- (1) Salary matching ----------------------------------------------
    legality = check_trade_legality(trade)
    print("[1] SALARY MATCHING")
    out_a = trade.team_a_sends.total_salary
    out_b = trade.team_b_sends.total_salary
    print(f"    {a_abbr} payroll ${a_payroll:,} -> rule: {_matching_rule(a_payroll)}")
    print(f"        sending ${out_a:,}, receiving ${out_b:,}")
    print(f"    {b_abbr} payroll ${b_payroll:,} -> rule: {_matching_rule(b_payroll)}")
    print(f"        sending ${out_b:,}, receiving ${out_a:,}")
    if legality.legal:
        print("    Result: LEGAL")
    else:
        print(f"    Result: ILLEGAL -- {legality.error_reason}")
    print()

    # --- (2) Base surplus -------------------------------------------------
    print("[2] BASE SURPLUS (team-agnostic, pre-context)")
    for label, side in ((a_abbr, side_a), (b_abbr, side_b)):
        incoming_total = side["incoming_base"] + side["incoming_pick_value"]
        outgoing_total = side["outgoing_base"] + side["outgoing_pick_value"]
        print(
            f"    {label}: receives surplus {_money(incoming_total)} "
            f"(players {_money(side['incoming_base'])} + picks "
            f"{_money(side['incoming_pick_value'])}), "
            f"sends surplus {_money(outgoing_total)} "
            f"(players {_money(side['outgoing_base'])} + picks "
            f"{_money(side['outgoing_pick_value'])})"
        )
        print(f"        -> net base delta {_money(side['net_base'])}")
    print()

    # --- (3) Team-context breakdown per player ----------------------------
    print("[3] TEAM CONTEXT BREAKDOWN")
    print(
        f"    {a_abbr} acquiring (projected wins = {TEAM_PROJECTED_WINS[a_abbr]:.0f})"
    )
    if side_a["incoming_player_details"]:
        print(f"      Incoming to {a_abbr}:")
        for d in side_a["incoming_player_details"]:
            _print_player_detail(d, indent="        ")
    if side_a["outgoing_player_details"]:
        print(f"      Outgoing from {a_abbr} (loss valued in {a_abbr} context):")
        for d in side_a["outgoing_player_details"]:
            _print_player_detail(d, indent="        ")
    print(
        f"    {b_abbr} acquiring (projected wins = {TEAM_PROJECTED_WINS[b_abbr]:.0f})"
    )
    if side_b["incoming_player_details"]:
        print(f"      Incoming to {b_abbr}:")
        for d in side_b["incoming_player_details"]:
            _print_player_detail(d, indent="        ")
    if side_b["outgoing_player_details"]:
        print(f"      Outgoing from {b_abbr} (loss valued in {b_abbr} context):")
        for d in side_b["outgoing_player_details"]:
            _print_player_detail(d, indent="        ")
    print()

    # --- (4) Final team-adjusted surplus ----------------------------------
    print("[4] FINAL TEAM-ADJUSTED SURPLUS")
    for label, side in ((a_abbr, side_a), (b_abbr, side_b)):
        incoming_total = side["incoming_adjusted"] + side["incoming_pick_value"]
        outgoing_total = side["outgoing_adjusted"] + side["outgoing_pick_value"]
        print(
            f"    {label}: receives {_money(incoming_total)}, "
            f"sends {_money(outgoing_total)} "
            f"-> net adjusted delta {_money(side['net_adjusted'])}"
        )
    print()

    # --- (5) Verdict ------------------------------------------------------
    print("[5] VERDICT")
    title_prefix = scenario.title.split("—")[0].strip()
    print(
        f"    {title_prefix}: {a_abbr} net adjusted "
        f"{_money(side_a['net_adjusted'])}, "
        f"{b_abbr} net adjusted {_money(side_b['net_adjusted'])}."
    )
    for sentence in _build_verdict(scenario, side_a, side_b):
        print(f"    {sentence}")
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    _force_utf8_stdout()
    print("Fetching EPM table from dunksandthrees.com...")
    epm_df = fetch_epm_data()
    print(f"  {len(epm_df)} EPM rows loaded")

    print("Fetching player stats from nba_api...")
    stats_df = fetch_player_stats()
    print(f"  {len(stats_df)} player-stat rows loaded")

    stats_df = _augment_with_epm_position(stats_df, epm_df)
    stats_lookup = {
        row["player_name_normalized"]: row for _, row in stats_df.iterrows()
    }
    print()

    for scenario in SCENARIOS:
        team_a_entries = _build_roster_entries(
            scenario.team_a_players, epm_df, stats_lookup
        )
        team_b_entries = _build_roster_entries(
            scenario.team_b_players, epm_df, stats_lookup
        )

        team_a = _build_team(scenario.team_a_abbr)
        team_b = _build_team(scenario.team_b_abbr)

        trade = Trade(
            team_a=team_a,
            team_b=team_b,
            team_a_sends=TradeAssets(
                players=team_a_entries,
                picks=list(scenario.team_a_picks),
            ),
            team_b_sends=TradeAssets(
                players=team_b_entries,
                picks=list(scenario.team_b_picks),
            ),
        )

        # Team A's view: incoming = team_b_sends, outgoing = team_a_sends.
        side_a = _evaluate_side(
            incoming_entries=team_b_entries,
            outgoing_entries=team_a_entries,
            incoming_picks=scenario.team_b_picks,
            outgoing_picks=scenario.team_a_picks,
            acquiring_team_abbr=scenario.team_a_abbr,
            epm_df=epm_df,
            stats_df=stats_df,
        )
        side_b = _evaluate_side(
            incoming_entries=team_a_entries,
            outgoing_entries=team_b_entries,
            incoming_picks=scenario.team_a_picks,
            outgoing_picks=scenario.team_b_picks,
            acquiring_team_abbr=scenario.team_b_abbr,
            epm_df=epm_df,
            stats_df=stats_df,
        )

        _print_trade_report(scenario, trade, side_a, side_b)


if __name__ == "__main__":
    main()
