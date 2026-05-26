"""Player salary / contract fetcher backed by Basketball Reference.

The Basketball Reference contracts page packs every active NBA contract into
a single HTML table (``id="player-contracts"``), so one polite request yields
the whole league — no per-team fan-out. Each row gives a player, their team,
and year-by-year salary columns ``y1``..``y6`` (seasons 2025-26 .. 2030-31),
from which we derive everything the frozen :class:`Contract` model needs:

- ``salary``           -> the current-season column (``y1`` for 2025-26)
- ``years_remaining``  -> count of current-or-later columns that carry a value
- ``has_player_option``-> a year cell tagged with the ``salary-pl`` CSS class
- ``has_team_option``  -> a year cell tagged with the ``salary-tm`` CSS class
- ``is_rookie_scale``  -> current salary matches a published rookie-scale slot

**Parsing approach.** The handoff suggested ``pandas.read_html`` for the table
plus BeautifulSoup for option colors. After inspecting the live markup, a
single BeautifulSoup pass is both cleaner and more robust: every salary cell
carries a clean integer in its ``csk`` attribute (no ``$``/comma stripping),
empty years carry the ``iz`` class, and option years carry ``salary-pl`` /
``salary-tm`` — all of which we need to walk cell-by-cell anyway. Aligning a
positional ``read_html`` frame against the soup (which also contains repeated
mid-table header rows) would only add a class of index-drift bugs for no gain.

Cached for 24h via the shared :class:`JsonCache`. If the fetch or parse fails,
falls back to a committed CSV snapshot so the project works offline.
"""

from __future__ import annotations

import re
from pathlib import Path

import httpx
import pandas as pd
from bs4 import BeautifulSoup, Tag

from nba_trade_analyzer.data.cache import JsonCache
from nba_trade_analyzer.data.epm import NAME_ALIASES, normalize_name
from nba_trade_analyzer.models.player import Contract

_CONTRACTS_URL = "https://www.basketball-reference.com/contracts/players.html"
_TABLE_ID = "player-contracts"
_CACHE_TTL_HOURS = 24.0
_HTTP_TIMEOUT = 30.0
_DEFAULT_SEASON = "2025-26"

# Basketball Reference asks for 3s between requests; we make exactly one, so a
# normal browser-ish UA is all the politeness required.
_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

EXPECTED_COLUMNS: tuple[str, ...] = (
    "player_name",
    "team",
    "salary",
    "years_remaining",
    "is_rookie_scale",
    "has_player_option",
    "has_team_option",
)

# CSS classes Basketball Reference puts on option-year salary cells. Stated on
# the page itself: "Color Key: Player Option, Team Option".
_PLAYER_OPTION_CLASS = "salary-pl"
_TEAM_OPTION_CLASS = "salary-tm"
# Empty (no-contract) year cells carry this class and no ``csk`` value.
_EMPTY_CELL_CLASS = "iz"

# Published 2025-26 first-year rookie-scale salaries for first-round picks
# 1..30 (the amount a 2025 draftee actually earns — teams sign at the 120%
# figure almost universally). Source: Hoops Rumors / RealGM rookie-scale
# tables. Used only to flag ``is_rookie_scale``; see ``_is_rookie_scale_salary``.
# Refresh annually alongside the cap figures in engine/constants.py.
ROOKIE_SCALE_2025_26: tuple[int, ...] = (
    13_825_920,  # 1
    12_370_200,  # 2
    11_108_880,  # 3
    9_069_840,  # 4
    8_237_640,  # 5
    7_520_040,  # 6
    6_889_200,  # 7
    6_332_520,  # 8
    6_016_080,  # 9
    5_715_120,  # 10
    5_429_520,  # 11
    5_429_520,  # 12
    5_157_960,  # 13
    4_900_320,  # 14
    4_655_040,  # 15
    4_422_360,  # 16
    4_201_080,  # 17
    3_991_320,  # 18
    3_811_560,  # 19
    3_658_800,  # 20
    3_512_520,  # 21
    3_372_240,  # 22
    3_237_480,  # 23
    3_108_120,  # 24
    2_983_320,  # 25
    2_884_560,  # 26
    2_801_280,  # 27
    2_783_880,  # 28
    2_763_960,  # 29
    2_743_800,  # 30
)
# Relative tolerance for matching a salary to a rookie-scale slot. Rookies
# signed at the standard 120% match to the dollar; this tight band absorbs
# small figure errors without sweeping in veteran salaries that happen to sit
# near a (densely packed, at the low end) scale slot.
_ROOKIE_SCALE_TOLERANCE = 0.001
# A year-1 first-round rookie deal also carries the structural fingerprint of a
# rookie contract: a multi-year term with team options in its out-years. We
# require both signals so a veteran salary that merely lands near a scale slot
# isn't misflagged.
_ROOKIE_SCALE_MIN_YEARS = 3

# Colloquial -> canonical aliases are shared with the EPM source; rebuild the
# normalized map here from the public dict (avoids importing a private symbol).
_NORMALIZED_ALIASES: dict[str, str] = {
    normalize_name(k): normalize_name(v) for k, v in NAME_ALIASES.items()
}

# Year columns on the contracts table, in chronological order.
_YEAR_STATS: tuple[str, ...] = ("y1", "y2", "y3", "y4", "y5", "y6")

_MONEY_RE = re.compile(r"[^\d]")


def _parse_money(text: str) -> int | None:
    """Parse a salary string like ``"$46,097,561"`` into ``46097561``.

    Returns ``None`` for empty/whitespace-only cells (no contract that year).
    """
    digits = _MONEY_RE.sub("", text or "")
    return int(digits) if digits else None


def _salary_matches_rookie_slot(salary: int) -> bool:
    """True if ``salary`` is within tolerance of a published scale slot."""
    return any(
        abs(salary - slot) <= slot * _ROOKIE_SCALE_TOLERANCE
        for slot in ROOKIE_SCALE_2025_26
    )


def _detect_rookie_scale(
    salary: int, years_remaining: int, has_team_option: bool
) -> bool:
    """Conservatively flag a first-round rookie-scale contract.

    Combines two signals so coincidental veteran salaries near a (densely
    packed) low-end scale slot aren't misflagged: the current salary matches a
    published 2025-26 rookie-scale slot, *and* the deal has the structural
    fingerprint of a rookie contract — a multi-year term with team options in
    its out-years (years 3-4). Validated against the live page: this lands on
    the 2025 first round almost exactly.

    Conservative by design — it reliably catches current-class first-round
    deals while defaulting to ``False`` for everyone else (years 2-4 of a
    rookie deal pay a higher, non-scale amount and aren't detected here).
    The frozen ``Contract`` field is currently informational; precise
    multi-year detection would need each player's draft year.
    """
    return (
        _salary_matches_rookie_slot(salary)
        and has_team_option
        and years_remaining >= _ROOKIE_SCALE_MIN_YEARS
    )


def _cell_salary(cell: Tag | None) -> int | None:
    """Extract the integer salary from a year cell, or ``None`` if empty.

    Prefers the ``csk`` sort-key attribute (already a clean integer); falls
    back to parsing the visible ``$1,234,567`` text if ``csk`` is absent.
    """
    if cell is None:
        return None
    csk = cell.get("csk")
    if csk:
        try:
            return int(float(csk))
        except ValueError:
            pass
    return _parse_money(cell.get_text(strip=True))


def _cell_has_class(cell: Tag | None, cls: str) -> bool:
    return cell is not None and cls in (cell.get("class") or [])


def _season_to_year_stat(table: Tag, season: str) -> str:
    """Map a season label like ``"2025-26"`` to its ``y*`` column ``data-stat``.

    Reads the table header so the scraper keeps working when Basketball
    Reference rolls the page forward a year (``y1`` becomes 2026-27, etc.).
    Falls back to ``y1`` if the label is not found.
    """
    head = table.find("thead")
    if head is not None:
        for cell in head.find_all(["th", "td"]):
            stat = cell.get("data-stat")
            if stat in _YEAR_STATS and cell.get_text(strip=True) == season:
                return stat
    return "y1"


def _parse_salary_html(html: str, season: str = _DEFAULT_SEASON) -> pd.DataFrame:
    """Parse the Basketball Reference contracts table into the salary schema."""
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table", id=_TABLE_ID)
    if table is None:
        raise RuntimeError(
            f"Basketball Reference contracts table (id={_TABLE_ID!r}) not found "
            "— the page format may have changed."
        )

    current_stat = _season_to_year_stat(table, season)
    current_idx = _YEAR_STATS.index(current_stat)
    # Only the current season and beyond count toward an active contract.
    contract_stats = _YEAR_STATS[current_idx:]

    body = table.find("tbody")
    rows: list[dict[str, object]] = []
    for tr in body.find_all("tr"):
        # Skip the repeated mid-table header rows (class includes "thead").
        if "thead" in (tr.get("class") or []):
            continue

        name_cell = tr.find("td", {"data-stat": "player"})
        team_cell = tr.find("td", {"data-stat": "team_id"})
        if name_cell is None:
            continue
        player_name = name_cell.get_text(strip=True)
        if not player_name:
            continue

        year_cells = {
            stat: tr.find("td", {"data-stat": stat}) for stat in contract_stats
        }

        salary = _cell_salary(year_cells.get(current_stat))
        # Contract requires salary > 0; rows with no current-season figure
        # (e.g. an unsigned/two-way line) carry no usable contract — skip.
        if not salary or salary <= 0:
            continue

        years_remaining = sum(
            1 for stat in contract_stats if _cell_salary(year_cells[stat]) is not None
        )
        has_player_option = any(
            _cell_has_class(year_cells[stat], _PLAYER_OPTION_CLASS)
            for stat in contract_stats
        )
        has_team_option = any(
            _cell_has_class(year_cells[stat], _TEAM_OPTION_CLASS)
            for stat in contract_stats
        )

        rows.append(
            {
                "player_name": player_name,
                "team": team_cell.get_text(strip=True) if team_cell else "",
                "salary": int(salary),
                "years_remaining": int(years_remaining),
                "is_rookie_scale": _detect_rookie_scale(
                    salary, years_remaining, has_team_option
                ),
                "has_player_option": has_player_option,
                "has_team_option": has_team_option,
            }
        )

    if not rows:
        raise RuntimeError(
            "Basketball Reference contracts table parsed to zero rows — the "
            "page format may have changed."
        )
    return pd.DataFrame(rows, columns=list(EXPECTED_COLUMNS))


def _csv_path(season: str) -> Path:
    """Packaged CSV-fallback path for a season, co-located with this module."""
    return Path(__file__).with_name(f"salaries_{season.replace('-', '_')}.csv")


def _load_csv_fallback(path: Path) -> pd.DataFrame:
    """Load the committed CSV snapshot and coerce it to the salary schema."""
    df = pd.read_csv(path)
    df["salary"] = df["salary"].astype(int)
    df["years_remaining"] = df["years_remaining"].astype(int)
    for col in ("is_rookie_scale", "has_player_option", "has_team_option"):
        df[col] = df[col].astype(bool)
    return df[list(EXPECTED_COLUMNS)]


def fetch_all_salaries(
    season: str = _DEFAULT_SEASON,
    cache: JsonCache | None = None,
    csv_path: Path | None = None,
) -> pd.DataFrame:
    """Fetch every active NBA contract from Basketball Reference.

    Cached for 24 hours. On any HTTP or parse failure, falls back to the
    committed CSV snapshot (``data/salaries_<season>.csv``) and prints a
    warning, so the project still works offline.
    """
    cache = cache or JsonCache()
    cache_key = f"salaries_{season}"

    cached = cache.get(cache_key)
    if cached is not None:
        return pd.DataFrame(cached, columns=list(EXPECTED_COLUMNS))

    try:
        resp = httpx.get(
            _CONTRACTS_URL,
            headers=_HEADERS,
            follow_redirects=True,
            timeout=_HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        df = _parse_salary_html(resp.text, season=season)
    except (httpx.HTTPError, RuntimeError) as exc:
        print(f"Basketball Reference fetch failed ({exc}), using local CSV fallback")
        return _load_csv_fallback(csv_path or _csv_path(season))

    cache.set(cache_key, df.to_dict(orient="records"), ttl_hours=_CACHE_TTL_HOURS)
    return df


def get_player_salary(df: pd.DataFrame, player_name: str) -> dict | None:
    """Look up a player's contract row by name, returning a plain dict.

    Name matching reuses the EPM normalization layer (diacritics, case,
    periods, generational suffixes, and known colloquial aliases), so
    "Nikola Jokic" finds "Nikola Jokić". Returns ``None`` if not found.
    """
    if df is None or df.empty:
        return None
    key = normalize_name(player_name)
    key = _NORMALIZED_ALIASES.get(key, key)
    normalized = df["player_name"].map(normalize_name)
    match = df[normalized == key]
    if match.empty:
        return None
    return match.iloc[0].to_dict()


def build_contract(salary_data: dict) -> Contract:
    """Adapt a ``get_player_salary`` result into the frozen ``Contract`` model.

    A straight field mapping — the scraper has already derived every field the
    model needs. ``years_remaining`` is clamped to a 1 floor since any row we
    keep has a positive current-season salary (i.e. at least one year left).
    """
    return Contract(
        salary=int(salary_data["salary"]),
        years_remaining=max(1, int(salary_data["years_remaining"])),
        is_rookie_scale=bool(salary_data.get("is_rookie_scale", False)),
        has_player_option=bool(salary_data.get("has_player_option", False)),
        has_team_option=bool(salary_data.get("has_team_option", False)),
    )
