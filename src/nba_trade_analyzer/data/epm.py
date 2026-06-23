"""EPM (Estimated Plus-Minus) fetcher for dunksandthrees.com.

The /epm page is rendered client-side, but SvelteKit serializes the full
player table into the HTML payload as a JavaScript object literal. We
extract the player records directly from that payload rather than executing
JavaScript or scraping a rendered DOM.

Columns exposed by ``fetch_epm_data``:

- ``player_name``, ``player_name_normalized``, ``team``
- ``epm`` (total), ``epm_off``, ``epm_def``
- ``mpg`` (sourced from ``p_mp_48`` — minutes per 48, ≈ MPG for starters)
- ``position``, ``age``

The page does not embed a per-player ``GP`` or ``estimated_wins`` value
(those are computed by the client from a separate data path). Downstream
code combines EPM with GP/MPG from ``nba_api`` for the wins_added calc.
"""

from __future__ import annotations

import os
import re
import unicodedata

import httpx
import pandas as pd

from nba_trade_analyzer.data.cache import JsonCache

# Current-season EPM is free on the public page (scraped from the embedded
# payload). PAST seasons are Premium-gated on the web page (top-5 only, rest
# "Locked Player"), but the Premium **API** returns the full population. We use
# the free scrape for the current season and the authenticated API for history.
_EPM_URL = "https://dunksandthrees.com/epm"
# Per-player, per-SEASON EPM (vs. /api/v1/epm which is per-game). Returns the
# full unlocked population as JSON, with player_id / gp / mpg included.
_EPM_API_URL = "https://dunksandthrees.com/api/v1/season-epm"
# Name of the env var holding the Dunks & Threes Premium+API key. The key is a
# SECRET: it is read from the environment only, never hardcoded or committed.
_API_KEY_ENV = "DUNKS_THREES_API_KEY"
_CACHE_KEY = "epm_dunksandthrees"
_CACHE_TTL_HOURS = 24.0
_HTTP_TIMEOUT = 30.0

EXPECTED_COLUMNS: tuple[str, ...] = (
    "player_name",
    "player_name_normalized",
    "team",
    "epm",
    "epm_off",
    "epm_def",
    "mpg",
    "position",
    "age",
)

# A single EPM record in the embedded payload starts with ``player_name:"..."``
# and continues with comma-separated ``key:value`` pairs until the closing
# brace. We only need a handful of fields; anchor on player_name and lazy-grab.
_RECORD_RE = re.compile(
    r'player_name:"(?P<name>[^"]+)",'
    r"team_id:\d+,"
    r'team_alias:"(?P<team>[^"]+)",'
    r"age:(?P<age>\d+),"
    r'inches:"\d+",'
    r"weight:\d+,"
    r"rookie_year:\d+,"
    r'position:"(?P<position>[^"]+)",'
    r"off:(?P<off>-?\d*\.?\d+),"
    r"def:(?P<def_>-?\d*\.?\d+),"
    r"tot:(?P<tot>-?\d*\.?\d+),"
    r"tot_change:[^,]+,"
    r"p_pct_start:-?\d*\.?\d+,"
    r"p_t_poss_48:-?\d*\.?\d+,"
    r"p_mp_48:(?P<mpg>-?\d*\.?\d+)",
)


# Strip generational suffixes — "Jr.", "Sr.", "II", "III", "IV", "V" — that
# users routinely omit when typing. Anchored to end-of-string and preceded by
# whitespace so it never eats part of a real surname. Verified against the
# current EPM dataset for collisions (e.g., two "Michael Porter"s); none exist.
_SUFFIX_RE = re.compile(r"\s+(?:jr|sr|ii|iii|iv|v)\.?$", re.IGNORECASE)


def normalize_name(name: str) -> str:
    """Aggressive normalization for fuzzy name matching.

    Strips diacritics, lowercases, removes periods, strips generational
    suffixes, and collapses whitespace. Examples:

    - "Nikola Jokić"        → "nikola jokic"
    - "P.J. Washington"     → "pj washington"
    - "Michael Porter Jr."  → "michael porter"
    - "Robert Williams III" → "robert williams"

    Same transform is applied to both the stored EPM rows and the query, so
    typing "Michael Porter" matches the canonical "Michael Porter Jr.".
    """
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_only = "".join(c for c in nfkd if not unicodedata.combining(c))
    s = ascii_only.casefold().strip()
    s = s.replace(".", "")
    s = _SUFFIX_RE.sub("", s)
    return " ".join(s.split())


# Colloquial → canonical name aliases. Both sides get ``normalize_name`` applied,
# so casing/diacritics/suffixes are forgiving on the caller side. Add an entry
# any time a commonly-used short name differs from the form Dunks & Threes uses.
NAME_ALIASES: dict[str, str] = {
    "Herb Jones": "Herbert Jones",
    "Cam Johnson": "Cameron Johnson",
    "Cam Payne": "Cameron Payne",
    "Nicolas Claxton": "Nic Claxton",
    "Alexandre Sarr": "Alex Sarr",
    "SGA": "Shai Gilgeous-Alexander",
}

_NORMALIZED_ALIASES: dict[str, str] = {
    normalize_name(k): normalize_name(v) for k, v in NAME_ALIASES.items()
}


def _parse_payload(html: str) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for m in _RECORD_RE.finditer(html):
        rows.append(
            {
                "player_name": m["name"],
                "player_name_normalized": normalize_name(m["name"]),
                "team": m["team"],
                "epm": float(m["tot"]),
                "epm_off": float(m["off"]),
                "epm_def": float(m["def_"]),
                "mpg": float(m["mpg"]),
                "position": m["position"],
                "age": int(m["age"]),
            }
        )
    return rows


def _f(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# Pinned field names from the live /api/v1/season-epm schema (verified against
# the 2016-17 payload). Total EPM is `tot`; offense/defense are `off`/`def`;
# position is `pos_text`. player_id (nba_id), gp and mp are carried through so
# callers can join by id and read games/minutes from the same source.
def _parse_api_payload(payload: object) -> list[dict[str, object]]:
    """Map the Premium season-EPM API JSON to rows (full unlocked population)."""
    records = payload.get("data", payload) if isinstance(payload, dict) else payload
    if not isinstance(records, list):
        raise RuntimeError(
            "Dunks & Threes season-EPM payload was not a JSON list of player "
            f"rows (got {type(records).__name__}) — the API schema may have changed."
        )

    rows: list[dict[str, object]] = []
    for row in records:
        if not isinstance(row, dict):
            continue
        name = row.get("player_name")
        if not name or row.get("tot") is None:
            continue
        rows.append(
            {
                "player_name": str(name),
                "player_name_normalized": normalize_name(str(name)),
                "player_id": int(_f(row.get("player_id"))),
                "team": str(row.get("team_alias") or ""),
                "epm": _f(row.get("tot")),
                "epm_off": _f(row.get("off")),
                "epm_def": _f(row.get("def")),
                "mpg": _f(row.get("mpg")),
                "gp": _f(row.get("gp")),
                "mp": _f(row.get("mp")),
                "position": str(row.get("pos_text") or ""),
                "age": int(_f(row.get("age"))),
            }
        )
    return rows


def fetch_epm_data(
    cache: JsonCache | None = None, season: int | None = None
) -> pd.DataFrame:
    """Fetch an EPM table from dunksandthrees.com.

    ``season`` is the season-ENDING year (``2025`` → 2024-25):
    - ``None`` (default): the CURRENT season, scraped free from the public
      ``/epm`` page — behavior and cache key unchanged.
    - set: a PAST season, fetched from the authenticated Premium **API**
      (``/api/v1/season-epm?season=YYYY``, with the raw key in the
      ``Authorization`` header — no ``Bearer`` prefix) because the web page gates
      history to a top-5 teaser. Requires the API key in ``$DUNKS_THREES_API_KEY``.

    Cached for 24 hours (per-season key so seasons never collide).
    """
    cache = cache or JsonCache()
    # Distinct cache namespace for the API source so it never reuses the earlier
    # (now-defunct) free-scrape season cache that only held the top-5 teaser.
    cache_key = _CACHE_KEY if season is None else f"{_CACHE_KEY}_api_{season}"

    cached = cache.get(cache_key)
    if cached is not None:
        return pd.DataFrame(cached)

    if season is None:
        resp = httpx.get(
            _EPM_URL,
            headers={"User-Agent": "Mozilla/5.0"},
            follow_redirects=True,
            timeout=_HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        rows = _parse_payload(resp.text)
        if not rows:
            raise RuntimeError(
                "EPM scraper found no player records in the dunksandthrees.com "
                f"payload (url={_EPM_URL}) — the page format may have changed."
            )
    else:
        api_key = os.environ.get(_API_KEY_ENV)
        if not api_key:
            raise RuntimeError(
                f"Historical EPM (season={season}) requires the Dunks & Threes "
                f"Premium API key in ${_API_KEY_ENV}, which is unset. Set it in "
                "the environment (it is never read from a committed file)."
            )
        resp = httpx.get(
            f"{_EPM_API_URL}?season={season}",
            # Dunks & Threes expects the raw key in the Authorization header
            # (NOT a `Bearer ` prefix).
            headers={"Authorization": api_key, "User-Agent": "Mozilla/5.0"},
            follow_redirects=True,
            timeout=_HTTP_TIMEOUT,
        )
        if resp.status_code in (401, 403):
            raise RuntimeError(
                f"Dunks & Threes API rejected the key ({resp.status_code}) for "
                f"season={season}: {resp.text[:160]}"
            )
        resp.raise_for_status()
        rows = _parse_api_payload(resp.json())
        if not rows:
            raise RuntimeError(
                f"Dunks & Threes API returned no EPM rows for season={season} — "
                "the key may lack API access or the schema changed."
            )
        # API rows carry extra columns (player_id, gp, mp); keep them.
        df = pd.DataFrame(rows)
        cache.set(cache_key, df.to_dict(orient="records"), ttl_hours=_CACHE_TTL_HOURS)
        return df

    df = pd.DataFrame(rows, columns=list(EXPECTED_COLUMNS))
    cache.set(cache_key, df.to_dict(orient="records"), ttl_hours=_CACHE_TTL_HOURS)
    return df


def get_player_epm(df: pd.DataFrame, player_name: str) -> pd.Series | None:
    """Look up a player by name. Forgiving of diacritics, case, periods,
    generational suffixes, and a small set of known colloquial nicknames
    (see ``NAME_ALIASES``).
    """
    if df.empty:
        return None
    key = normalize_name(player_name)
    key = _NORMALIZED_ALIASES.get(key, key)
    match = df[df["player_name_normalized"] == key]
    if match.empty:
        return None
    return match.iloc[0]
