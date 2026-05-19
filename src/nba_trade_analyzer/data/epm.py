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

import re
import unicodedata

import httpx
import pandas as pd

from nba_trade_analyzer.data.cache import JsonCache

_EPM_URL = "https://dunksandthrees.com/epm"
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


def fetch_epm_data(cache: JsonCache | None = None) -> pd.DataFrame:
    """Fetch the current EPM table from dunksandthrees.com.

    Cached for 24 hours; data on the source refreshes nightly.
    """
    cache = cache or JsonCache()

    cached = cache.get(_CACHE_KEY)
    if cached is not None:
        return pd.DataFrame(cached)

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
            "payload — the page format may have changed."
        )

    df = pd.DataFrame(rows, columns=list(EXPECTED_COLUMNS))
    cache.set(_CACHE_KEY, df.to_dict(orient="records"), ttl_hours=_CACHE_TTL_HOURS)
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
