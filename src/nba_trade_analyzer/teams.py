"""NBA team registry: abbreviations, full names, and cross-source aliases.

The project pulls from sources that disagree on three abbreviations:

- nba_api / EPM use ``BKN``, ``CHA``, ``PHX``
- Basketball Reference (the salary source) uses ``BRK``, ``CHO``, ``PHO``

The grader filters the nba_api player-stats frame by ``Team.abbreviation`` for
roster / win-curve / spacing context, while payroll is summed from the salary
frame — so a team needs *both* forms. :class:`TeamInfo` carries the nba_api
``abbreviation`` (canonical, used everywhere downstream) plus the
``salary_abbreviation`` for the payroll lookup. :func:`resolve_team` accepts
either convention as input so the CLI is forgiving about which a user types.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TeamInfo:
    abbreviation: str
    """Canonical nba_api / EPM form — what ``Team.abbreviation`` is set to so the
    grader's roster and team-context lookups resolve."""

    name: str
    """Full team name, e.g. ``"Los Angeles Lakers"``."""

    salary_abbreviation: str
    """Basketball Reference form, used to sum payroll from the salary frame."""


# (nba_api abbr, full name, Basketball Reference abbr). The three teams whose
# forms diverge are BKN/BRK, CHA/CHO, PHX/PHO; the rest are identical.
_TEAMS: tuple[TeamInfo, ...] = (
    TeamInfo("ATL", "Atlanta Hawks", "ATL"),
    TeamInfo("BOS", "Boston Celtics", "BOS"),
    TeamInfo("BKN", "Brooklyn Nets", "BRK"),
    TeamInfo("CHA", "Charlotte Hornets", "CHO"),
    TeamInfo("CHI", "Chicago Bulls", "CHI"),
    TeamInfo("CLE", "Cleveland Cavaliers", "CLE"),
    TeamInfo("DAL", "Dallas Mavericks", "DAL"),
    TeamInfo("DEN", "Denver Nuggets", "DEN"),
    TeamInfo("DET", "Detroit Pistons", "DET"),
    TeamInfo("GSW", "Golden State Warriors", "GSW"),
    TeamInfo("HOU", "Houston Rockets", "HOU"),
    TeamInfo("IND", "Indiana Pacers", "IND"),
    TeamInfo("LAC", "Los Angeles Clippers", "LAC"),
    TeamInfo("LAL", "Los Angeles Lakers", "LAL"),
    TeamInfo("MEM", "Memphis Grizzlies", "MEM"),
    TeamInfo("MIA", "Miami Heat", "MIA"),
    TeamInfo("MIL", "Milwaukee Bucks", "MIL"),
    TeamInfo("MIN", "Minnesota Timberwolves", "MIN"),
    TeamInfo("NOP", "New Orleans Pelicans", "NOP"),
    TeamInfo("NYK", "New York Knicks", "NYK"),
    TeamInfo("OKC", "Oklahoma City Thunder", "OKC"),
    TeamInfo("ORL", "Orlando Magic", "ORL"),
    TeamInfo("PHI", "Philadelphia 76ers", "PHI"),
    TeamInfo("PHX", "Phoenix Suns", "PHO"),
    TeamInfo("POR", "Portland Trail Blazers", "POR"),
    TeamInfo("SAC", "Sacramento Kings", "SAC"),
    TeamInfo("SAS", "San Antonio Spurs", "SAS"),
    TeamInfo("TOR", "Toronto Raptors", "TOR"),
    TeamInfo("UTA", "Utah Jazz", "UTA"),
    TeamInfo("WAS", "Washington Wizards", "WAS"),
)

# Every accepted input form (both conventions) -> its canonical record.
_BY_ALIAS: dict[str, TeamInfo] = {}
for _t in _TEAMS:
    _BY_ALIAS[_t.abbreviation] = _t
    _BY_ALIAS[_t.salary_abbreviation] = _t

# The documented set we advertise in help / error text. Uses the abbreviations
# from the CLI spec (Basketball Reference style for BRK/PHO, nba_api CHA).
VALID_ABBREVIATIONS: tuple[str, ...] = (
    "ATL", "BOS", "BRK", "CHA", "CHI", "CLE", "DAL", "DEN", "DET", "GSW",
    "HOU", "IND", "LAC", "LAL", "MEM", "MIA", "MIL", "MIN", "NOP", "NYK",
    "OKC", "ORL", "PHI", "PHO", "POR", "SAC", "SAS", "TOR", "UTA", "WAS",
)  # fmt: skip


def resolve_team(raw: str) -> TeamInfo | None:
    """Resolve a user-typed abbreviation to a :class:`TeamInfo`, or ``None``.

    Case-insensitive and accepts either source's convention (``BKN`` or
    ``BRK``, ``CHA`` or ``CHO``, ``PHX`` or ``PHO``).
    """
    if not raw:
        return None
    return _BY_ALIAS.get(raw.strip().upper())


def format_valid_teams(per_line: int = 10) -> str:
    """Render the valid abbreviations as wrapped, comma-separated lines."""
    abbrs = list(VALID_ABBREVIATIONS)
    lines = [", ".join(abbrs[i : i + per_line]) for i in range(0, len(abbrs), per_line)]
    return "\n".join(lines)
