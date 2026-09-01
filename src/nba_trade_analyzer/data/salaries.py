"""Player salary / contract fetcher backed by Basketball Reference.

The Basketball Reference contracts page packs every active NBA contract into
a single HTML table (``id="player-contracts"``), so one polite request yields
the whole league — no per-team fan-out. Each row gives a player, their team,
and year-by-year salary columns ``y1``..``y6`` (seasons 2026-27 .. 2031-32
since the 2026-07 league-year rollover),
from which we derive everything the frozen :class:`Contract` model needs:

- ``salary``           -> the current-season column (``y1`` for 2026-27)
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

import logging
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx
import pandas as pd
from bs4 import BeautifulSoup, Tag

from nba_trade_analyzer.data.cache import JsonCache
from nba_trade_analyzer.data.epm import NAME_ALIASES, normalize_name
from nba_trade_analyzer.models.player import Contract

logger = logging.getLogger(__name__)

_CONTRACTS_URL = "https://www.basketball-reference.com/contracts/players.html"
_TABLE_ID = "player-contracts"
_CACHE_TTL_HOURS = 24.0
_HTTP_TIMEOUT = 30.0
# The CURRENT league year — the label the parser anchors on in the contracts
# header. Rolled 2026-07-11; must roll together with export.season_keys()
# (the positional yearly->season mapping) every July. _season_to_year_stat
# FAILS LOUD when this label is absent, so next July aborts here instead of
# silently shifting labels.
_DEFAULT_SEASON = "2026-27"

# Basketball Reference asks for 3s between requests; we make exactly one, so a
# normal browser-ish UA is all the politeness required.
_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

EXPECTED_COLUMNS: tuple[str, ...] = (
    "player_name",
    "bbref_slug",
    "team",
    "salary",
    "years_remaining",
    "is_rookie_scale",
    "has_player_option",
    "has_team_option",
    "yearly_salaries",
)

# Per-year salaries are persisted (CSV + JSON cache) as a pipe-joined string of
# integers, current-season first — e.g. ``"59606817|62587158"`` — so the column
# survives a flat CSV/JSON round-trip without nested structures. ``build_contract``
# parses it back into a tuple.
_YEARLY_SEP = "|"

# Basketball Reference player-page links look like ``/players/d/doncilu01.html``;
# the trailing token (``doncilu01``) is the player's permanent BBRef id. It never
# goes stale (unlike the team column), so it is the stable join key the offline
# crosswalk uses to tie a contract to its nba_api identity.
_SLUG_RE = re.compile(r"/players/[a-z]/([^./]+)\.html")

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


def _extract_slug(name_cell: Tag) -> str:
    """Pull the permanent BBRef slug from a player cell's ``<a>`` href.

    Returns ``""`` when the cell has no recognizable player link (e.g. a plain
    text name); the crosswalk build flags any contract with an empty slug.
    """
    link = name_cell.find("a")
    href = link.get("href", "") if link is not None else ""
    m = _SLUG_RE.search(href or "")
    return m.group(1) if m else ""


def _cell_has_class(cell: Tag | None, cls: str) -> bool:
    return cell is not None and cls in (cell.get("class") or [])


def _season_to_year_stat(table: Tag, season: str) -> str:
    """Map a season label like ``"2026-27"`` to its ``y*`` column ``data-stat``.

    Keyed on the header's ACTUAL labels, and FAIL-LOUD (2026-07-11): when the
    expected current-season label is absent, raise — never guess a column.
    The old silent ``y1`` fallback is what shifted every salary one season
    early the night BBRef rolled to 2026-27 (only the collapse guards stopped
    the mislabeled write). Strict ingest turns this raise into a recorded
    FAILED run; the export path falls back to the committed CSV, loudly.
    """
    head = table.find("thead")
    seen: list[str] = []
    if head is not None:
        for cell in head.find_all(["th", "td"]):
            stat = cell.get("data-stat")
            if stat in _YEAR_STATS:
                label = cell.get_text(strip=True)
                seen.append(label)
                if label == season:
                    return stat
    raise RuntimeError(
        f"Basketball Reference contracts header has no {season!r} column "
        f"(year columns seen: {seen or 'none'}) — the page has likely rolled "
        "to a new league year. Roll _DEFAULT_SEASON and export.season_keys() "
        "together (see the 2026-07-11 rollover) instead of ingesting shifted "
        "labels."
    )


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
        bbref_slug = _extract_slug(name_cell)

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
        # Real per-year dollars, current season onward. Walk contiguously and
        # stop at the first empty year so the list is a clean run of guaranteed
        # current-onward salaries (almost always the full contract span). Option
        # years carry their listed figure as-is — no discount here.
        yearly: list[int] = []
        for stat in contract_stats:
            year_salary = _cell_salary(year_cells[stat])
            if year_salary is None:
                break
            yearly.append(int(year_salary))
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
                "bbref_slug": bbref_slug,
                "team": team_cell.get_text(strip=True) if team_cell else "",
                "salary": int(salary),
                "years_remaining": int(years_remaining),
                "is_rookie_scale": _detect_rookie_scale(
                    salary, years_remaining, has_team_option
                ),
                "has_player_option": has_player_option,
                "has_team_option": has_team_option,
                "yearly_salaries": _YEARLY_SEP.join(str(s) for s in yearly),
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
    # Pre-slug CSV snapshots lack the bbref_slug column; tolerate it so the
    # offline path keeps working, with empty slugs the crosswalk build flags.
    if "bbref_slug" not in df.columns:
        df["bbref_slug"] = ""
    df["bbref_slug"] = df["bbref_slug"].fillna("").astype(str)
    # Likewise tolerate snapshots predating per-year salaries: an empty string
    # parses to no per-year data, so valuation falls back to the flat salary.
    if "yearly_salaries" not in df.columns:
        df["yearly_salaries"] = ""
    df["yearly_salaries"] = df["yearly_salaries"].fillna("").astype(str)
    return df[list(EXPECTED_COLUMNS)]


def _dedupe_salary_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Collapse repeat contract lines that share a ``(team, bbref_slug)``.

    ROOT CAUSE: Basketball Reference's contracts ``tbody`` itself lists some
    players on MULTIPLE rows — min / two-way / dead-money players who signed
    several 10-day or rest-of-season minimum deals in one season, each rendered
    as its own line with the same salary (verified live: e.g. ``bradlto01`` is
    3 identical IND rows, ``robinje02`` is 3 identical IND rows + 1 DAL row).
    The parser faithfully emits one record per ``<tr>``, so these byte-identical
    rows flow downstream and double-count salary AND roster WAA, corrupting cap
    totals and surplus.

    We keep the FIRST row per ``(team, bbref_slug)`` and DELIBERATELY preserve
    the same player on DIFFERENT teams (legitimate two-stint / dead-money cap
    hits that belong on each team's books — e.g. ``robinje02`` stays on both IND
    and DAL). Rows with an empty slug can't be keyed reliably, so they are never
    collapsed. Applied at every ``fetch_all_salaries`` return path (live parse,
    warm cache, CSV fallback) so no source can reintroduce duplicates.
    """
    if df.empty or "bbref_slug" not in df.columns:
        return df
    slug = df["bbref_slug"].fillna("").astype(str)
    team = df["team"].fillna("").astype(str) if "team" in df.columns else ""
    keyable = slug != ""
    deduped_keyable = (
        df[keyable]
        .assign(_team=team[keyable], _slug=slug[keyable])
        .drop_duplicates(subset=["_team", "_slug"], keep="first")
        .drop(columns=["_team", "_slug"])
    )
    # Preserve original row order; keep all un-slugged rows as-is.
    out = pd.concat([deduped_keyable, df[~keyable]]).sort_index()
    return out.reset_index(drop=True)


def fetch_all_salaries(
    season: str = _DEFAULT_SEASON,
    cache: JsonCache | None = None,
    csv_path: Path | None = None,
    strict: bool = False,
) -> pd.DataFrame:
    """Fetch every active NBA contract from Basketball Reference.

    Cached for 24 hours. On any HTTP or parse failure, falls back to the
    committed CSV snapshot (``data/salaries_<season>.csv``) and prints a
    warning, so the project still works offline. Every return path is run
    through ``_dedupe_salary_frame`` so Basketball Reference's repeated
    contract lines never reach consumers (see that helper for the root cause).

    ``strict=True`` (the ingest path) DISABLES the CSV fallback: a failed
    fetch re-raises so the caller records a FAILED run instead of silently
    ingesting stale committed data (databallr Phase 0 flagged the silent
    fallback as a regression vector — the committed CSV was months stale).

    ``strict=False`` (the export path) keeps the fallback but LOUDLY: an
    unmissable multi-line stderr warning with the CSV's mtime, plus a
    ``bbref_fallback`` marker in ``df.attrs`` so ``build_export`` can stamp
    the payload's ``sourceNote`` — degradation must never be silent, but
    export must not hard-fail either (it feeds databallr's sync:cap-data).
    """
    cache = cache or JsonCache()
    cache_key = f"salaries_{season}"

    cached = cache.get(cache_key)
    if cached is not None:
        return _dedupe_salary_frame(
            pd.DataFrame(cached, columns=list(EXPECTED_COLUMNS))
        )

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
        if strict:
            raise
        fallback_path = csv_path or _csv_path(season)
        try:
            csv_mtime = (
                datetime.fromtimestamp(fallback_path.stat().st_mtime).date().isoformat()
            )
        except OSError:
            csv_mtime = "unknown"
        print(
            "\n".join(
                [
                    "=" * 72,
                    "!! BBREF FETCH FAILED — EXPORTING FROM COMMITTED CSV FALLBACK !!",
                    f"!! reason: {exc}",
                    f"!! fallback: {fallback_path} (dated {csv_mtime})",
                    "!! DATA MAY BE STALE — salaries reflect the CSV's vintage, not today.",
                    "=" * 72,
                ]
            ),
            file=sys.stderr,
        )
        df = _dedupe_salary_frame(_load_csv_fallback(fallback_path))
        # Marker for build_export -> payload sourceNote (attrs set on the FINAL
        # frame object; nothing mutates it between here and the caller).
        df.attrs["bbref_fallback"] = {"csv_mtime": csv_mtime, "reason": str(exc)}
        return df

    df = _dedupe_salary_frame(df)
    cache.set(cache_key, df.to_dict(orient="records"), ttl_hours=_CACHE_TTL_HOURS)
    return df


_TEAM_CONTRACTS_URL = "https://www.basketball-reference.com/contracts/{team}.html"
# Politeness gap between team-page requests (bounded fan-out: only teams
# involved in multi-stint slugs, ~5-15 pages).
_TEAM_FETCH_SLEEP_S = 3.0


def _parse_team_contract_amounts(
    html: str, slugs: set[str], season: str = _DEFAULT_SEASON
) -> dict[str, dict[str, int]]:
    """Read a TEAM contracts page's raw per-season amounts for ``slugs``.

    The league table prints a COMBINED season total in every stint row for a
    multi-team player; the team pages carry the real per-team figures
    (verified live 2026-08-31: Klay DAL 17,460,317 + MIA 5,600,000 = the
    league cell 23,060,317). Team-page quirks handled here, from the real
    markup: the player cell is a ``<th data-stat="player">`` whose ``csk``
    IS the slug, and waived players ride a ``partial_table`` section with
    the same cell shapes.
    """
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table", id="contracts")
    if table is None:
        raise RuntimeError(
            "team contracts table (id='contracts') not found — the page "
            "format may have changed."
        )
    thead = table.find("thead")
    if thead is None or not thead.find_all(attrs={"data-stat": True}):
        # FAIL LOUD (review finding): a silent y1-equals-current fallback is
        # the exact bug _season_to_year_stat exists to prevent — after a
        # season rollover it maps money one season off with no error. A
        # header-less page is a parse failure: warn-and-skip upstream.
        raise RuntimeError(
            "team contracts table has no data-stat header row — cannot map "
            "y1..y6 to seasons; the page format may have changed."
        )
    current_idx = _YEAR_STATS.index(_season_to_year_stat(table, season))
    season_start = int(season[:4])
    out: dict[str, dict[str, int]] = {}
    body = table.find("tbody")
    if body is None:
        raise RuntimeError(
            "team contracts table has no tbody — truncated or restructured "
            "page; treating as a parse failure."
        )
    for tr in body.find_all("tr"):
        if "thead" in (tr.get("class") or []):
            continue
        player_cell = tr.find("th", {"data-stat": "player"}) or tr.find(
            "td", {"data-stat": "player"}
        )
        if player_cell is None:
            continue
        slug = str(player_cell.get("csk") or "") or _extract_slug(player_cell)
        if slug not in slugs:
            continue
        amounts: dict[str, int] = {}
        for offset, stat in enumerate(_YEAR_STATS[current_idx:]):
            cell = tr.find("td", {"data-stat": stat})
            value = _cell_salary(cell)
            if value is not None and value > 0:
                year = season_start + offset
                amounts[f"{year}-{(year + 1) % 100:02d}"] = int(value)
        if amounts:
            # First row wins per slug (the league dedupe's keep-first rule);
            # a second same-team stint row would be byte-identical anyway.
            out.setdefault(slug, amounts)
    return out


def fetch_multi_stint_raw_amounts(
    salary_df: pd.DataFrame,
    season: str = _DEFAULT_SEASON,
    cache: JsonCache | None = None,
) -> dict[str, dict[str, dict[str, int]]]:
    """Fetch per-team RAW amounts for every multi-stint slug in the frame.

    Returns ``{slug: {team: {season: dollars}}}``. Bounded fan-out: only the
    teams involved in multi-stint slugs are fetched, one polite request per
    team, cached under ``salaries_teamraw_{season}`` with the league cache's
    TTL. Vintage skew between this cache and the league cache is caught at
    ATTACH time by the sum invariant (:func:`attach_raw_amounts`) — a raw
    set fetched against yesterday's league cells fails today's sums and
    falls back, never silently mixes.

    A failed team fetch/parse logs a WARNING and simply yields no raw data
    for that team's slugs — the ingest then behaves exactly as today for
    those players (never worse than now).
    """
    cache = cache or JsonCache()
    cache_key = f"salaries_teamraw_{season}"
    cached = cache.get(cache_key)
    if isinstance(cached, dict) and all(isinstance(v, dict) for v in cached.values()):
        return cached

    if salary_df.empty or "bbref_slug" not in salary_df.columns:
        return {}
    by_slug: dict[str, set[str]] = {}
    for _, row in salary_df.iterrows():
        slug = str(row.get("bbref_slug") or "")
        team = str(row.get("team") or "")
        if slug and team:
            by_slug.setdefault(slug, set()).add(team)
    multi = {slug: teams for slug, teams in by_slug.items() if len(teams) > 1}
    if not multi:
        # NOT cached (review finding): a slug-less fallback frame passing
        # through here would poison the cache with {} for a full TTL while
        # real multi-stint slugs exist.
        return {}

    teams_needed = sorted({t for teams in multi.values() for t in teams})
    slugs_needed = set(multi)
    per_team: dict[str, dict[str, dict[str, int]]] = {}
    fetched_ok: set[str] = set()
    for i, team in enumerate(teams_needed):
        try:
            if i > 0:
                time.sleep(_TEAM_FETCH_SLEEP_S)
            resp = httpx.get(
                _TEAM_CONTRACTS_URL.format(team=team),
                headers=_HEADERS,
                follow_redirects=True,
                timeout=_HTTP_TIMEOUT,
            )
            resp.raise_for_status()
            parsed = _parse_team_contract_amounts(resp.text, slugs_needed, season)
        except (httpx.HTTPError, RuntimeError) as exc:
            logger.warning(
                "team contracts fetch failed for %s (%s) — affected "
                "multi-stint players keep today's blended behavior",
                team,
                exc,
            )
            continue
        for slug, amounts in parsed.items():
            if team in multi.get(slug, set()):
                per_team.setdefault(slug, {})[team] = amounts
        fetched_ok.add(team)

    # Cache ONLY a complete, successful sweep (review finding: caching a
    # transient all-teams failure — a rate-limit night — silently served {}
    # for 24h; the loud degrade became silent after the first run). A
    # partial result is used this run but NOT cached, so the next run
    # retries the failed teams.
    if per_team and fetched_ok == set(teams_needed):
        cache.set(cache_key, per_team, ttl_hours=_CACHE_TTL_HOURS)
    elif not per_team and fetched_ok == set(teams_needed):
        # COMPLETE but empty: every page fetched and parsed cleanly yet no
        # needed slug appeared — parse/row drift, not a network problem (a
        # markup change that breaks parsing raises and lands in the
        # incomplete branch below instead).
        logger.warning(
            "team contracts sweep returned no rows for any needed slug "
            "(all %d team(s) fetched OK) — result NOT cached, so the next "
            "run retries; check for a team-page row-shape change",
            len(fetched_ok),
        )
    else:
        logger.warning(
            "team contracts sweep incomplete (%d/%d teams) — result used "
            "this run but NOT cached, so the next run retries",
            len(fetched_ok),
            len(teams_needed),
        )
    return per_team


def attach_raw_amounts(
    salary_df: pd.DataFrame,
    raw: dict[str, dict[str, dict[str, int]]],
    season: str = _DEFAULT_SEASON,
) -> pd.DataFrame:
    """Attach per-team raw amounts to multi-stint rows, invariant-enforced.

    THE INVARIANT: for every season a slug's league rows print, the sum of
    the per-team raw amounts must EQUAL the blended league cell (Klay:
    17,460,317 + 5,600,000 = 23,060,317). A mismatch means vintage skew or
    a parse error — that slug gets NO raw amounts (falls back to today's
    behavior) and a WARNING names the numbers. Never a silent pick.
    """
    if salary_df.empty or not raw:
        out = salary_df.copy()
        out["raw_amounts"] = None
        return out
    season_start = int(season[:4])

    def league_cells(yearly: str) -> dict[str, int]:
        cells: dict[str, int] = {}
        for offset, part in enumerate(str(yearly).split(_YEARLY_SEP)):
            if part.strip().isdigit() and int(part) > 0:
                year = season_start + offset
                cells[f"{year}-{(year + 1) % 100:02d}"] = int(part)
        return cells

    out = salary_df.copy()

    def slug_verified(slug: str, team_raw: dict[str, dict[str, int]]) -> bool:
        """The full invariant (review-hardened): checked against EVERY league
        row of the slug (divergent sibling cells fail), empty/NaN cells FAIL
        rather than vacuously pass, raw seasons must be a subset of the
        league seasons (no raw-only season can attach), and every league
        season's per-team sum must equal the cell."""
        rows = out[out["bbref_slug"].fillna("").astype(str) == slug]
        raw_seasons = {s for a in team_raw.values() for s in a}
        for _, r in rows.iterrows():
            cells = league_cells(r.get("yearly_salaries") or "")
            if not cells:
                logger.warning(
                    "raw-amount invariant FAILED for %s: league row (%s) has "
                    "no parseable cells — keeping today's blended behavior",
                    slug,
                    r.get("team"),
                )
                return False
            if not raw_seasons <= set(cells):
                logger.warning(
                    "raw-amount invariant FAILED for %s: raw carries "
                    "season(s) %s the league row does not print — vintage "
                    "skew; keeping today's blended behavior",
                    slug,
                    sorted(raw_seasons - set(cells)),
                )
                return False
            for season, cell in cells.items():
                total = sum(a.get(season, 0) for a in team_raw.values())
                if total != cell:
                    logger.warning(
                        "raw-amount invariant FAILED for %s %s: per-team sum "
                        "%d != league cell %d — vintage skew or parse error; "
                        "keeping today's blended behavior for this player",
                        slug,
                        season,
                        total,
                        cell,
                    )
                    return False
        return True

    attached: list[dict[str, int] | None] = []
    verified: dict[str, bool] = {}
    for _, row in out.iterrows():
        slug = str(row.get("bbref_slug") or "")
        team = str(row.get("team") or "")
        team_raw = raw.get(slug, {})
        if not slug or team not in team_raw:
            attached.append(None)
            continue
        if slug not in verified:
            verified[slug] = slug_verified(slug, team_raw)
        attached.append(dict(team_raw[team]) if verified[slug] else None)
    out["raw_amounts"] = attached
    return out


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


def _parse_yearly_salaries(value: object) -> tuple[int, ...]:
    """Parse a ``yearly_salaries`` cell into a tuple of ints, current year first.

    Accepts the persisted pipe-joined string (``"a|b|c"``), an already-parsed
    list/tuple of numbers, or an empty/missing value (``None``, ``NaN``, ``""``)
    which yields ``()`` so valuation falls back to the flat salary.
    """
    if value is None or isinstance(value, float):  # float covers pandas NaN
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(int(v) for v in value)
    text = str(value).strip()
    if not text:
        return ()
    return tuple(int(part) for part in text.split(_YEARLY_SEP) if part)


def build_contract(salary_data: dict) -> Contract:
    """Adapt a ``get_player_salary`` result into the frozen ``Contract`` model.

    A straight field mapping — the scraper has already derived every field the
    model needs. ``years_remaining`` is clamped to a 1 floor since any row we
    keep has a positive current-season salary (i.e. at least one year left).
    ``yearly_salaries`` carries the real per-year dollars (empty when a snapshot
    predates the column, in which case valuation holds the salary flat).
    """
    return Contract(
        salary=int(salary_data["salary"]),
        years_remaining=max(1, int(salary_data["years_remaining"])),
        is_rookie_scale=bool(salary_data.get("is_rookie_scale", False)),
        has_player_option=bool(salary_data.get("has_player_option", False)),
        has_team_option=bool(salary_data.get("has_team_option", False)),
        yearly_salaries=_parse_yearly_salaries(salary_data.get("yearly_salaries")),
    )
