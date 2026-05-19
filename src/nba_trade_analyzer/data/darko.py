"""DARKO projections fetcher from the public Google Sheet.

DARKO (Daily Adjusted and Regressed Kalman Optimized projections) by
Kostya Medvedovsky lives in a public Google Sheet. The first tab exports
cleanly as CSV via the standard Sheets export URL — no auth, no scraping.

Columns exposed by ``fetch_darko_data``:

- ``player_name``, ``player_name_normalized``
- ``dpm`` (Daily Plus-Minus, the per-100-possessions impact projection)
- ``dpm_off``, ``dpm_def``
- ``box_dpm_off``, ``box_dpm_def``, ``onoff_dpm_off``, ``onoff_dpm_def``
- ``position``, ``age``

``dpm`` is the primary impact metric — equivalent role to EPM. The
valuation engine treats it the same way: per-100-possessions impact that
needs to be scaled by minutes played.
"""

from __future__ import annotations

import unicodedata

import httpx
import pandas as pd

from nba_trade_analyzer.data.cache import JsonCache

_DARKO_SHEET_ID = "1mhwOLqPu2F9026EQiVxFPIN1t9RGafGpl-dokaIsm9c"
_DARKO_URL = (
    f"https://docs.google.com/spreadsheets/d/{_DARKO_SHEET_ID}/export?format=csv"
)
_CACHE_KEY = "darko_kmedved"
_CACHE_TTL_HOURS = 24.0
_HTTP_TIMEOUT = 30.0

EXPECTED_COLUMNS: tuple[str, ...] = (
    "player_name",
    "player_name_normalized",
    "dpm",
    "dpm_off",
    "dpm_def",
    "box_dpm_off",
    "box_dpm_def",
    "onoff_dpm_off",
    "onoff_dpm_def",
    "position",
    "age",
)

# Map raw CSV headers → our snake_case schema.
_COLUMN_MAP = {
    "Player Name": "player_name",
    "Position": "position",
    "Age": "age",
    "DPM": "dpm",
    "Offensive DPM": "dpm_off",
    "Defensive DPM": "dpm_def",
    "Box Only O-DPM": "box_dpm_off",
    "Box Only D-DPM": "box_dpm_def",
    "On Off O-DPM": "onoff_dpm_off",
    "On Off D-DPM": "onoff_dpm_def",
}


def normalize_name(name: str) -> str:
    """Strip diacritics and lower-case for fuzzy name matching."""
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_only = "".join(c for c in nfkd if not unicodedata.combining(c))
    return ascii_only.casefold().strip()


def _shape(raw: pd.DataFrame) -> pd.DataFrame:
    missing = [c for c in _COLUMN_MAP if c not in raw.columns]
    if missing:
        raise RuntimeError(
            f"DARKO sheet missing expected columns: {missing}. "
            "The sheet schema may have changed."
        )
    df = raw.rename(columns=_COLUMN_MAP)[list(_COLUMN_MAP.values())].copy()
    df["player_name_normalized"] = df["player_name"].map(normalize_name)
    return df[list(EXPECTED_COLUMNS)]


def fetch_darko_data(cache: JsonCache | None = None) -> pd.DataFrame | None:
    """Fetch the latest DARKO projections.

    Returns ``None`` (with a printed note) if the sheet is unreachable so
    the EPM path remains usable as the primary impact source.
    """
    cache = cache or JsonCache()

    cached = cache.get(_CACHE_KEY)
    if cached is not None:
        return pd.DataFrame(cached)

    try:
        resp = httpx.get(
            _DARKO_URL,
            headers={"User-Agent": "Mozilla/5.0"},
            follow_redirects=True,
            timeout=_HTTP_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        print(f"DARKO fetch failed: {exc}. Falling back without DARKO data.")
        return None

    from io import StringIO

    raw = pd.read_csv(StringIO(resp.text))
    df = _shape(raw)

    cache.set(_CACHE_KEY, df.to_dict(orient="records"), ttl_hours=_CACHE_TTL_HOURS)
    return df


def get_player_darko(df: pd.DataFrame | None, player_name: str) -> pd.Series | None:
    """Look up a player in DARKO data (diacritics-insensitive)."""
    if df is None or df.empty:
        return None
    key = normalize_name(player_name)
    match = df[df["player_name_normalized"] == key]
    if match.empty:
        return None
    return match.iloc[0]
