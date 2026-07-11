"""Non-guaranteed (NG) contract-year resolver — issue 3a, v1 Option 1.

Positive-confirmation: a contract season is treated as non-guaranteed (to be
marked, leaving committed salary untouched) ONLY when BOTH hold:

  (a) site_Data's ``salary_spread`` table codes that player-season ``NG``, and
  (b) the player-season is on the hand-verified allowlist
      (``data/guarantees/full_ng_allowlist.json``).

Anything not on the allowlist stays fully committed even if coded NG, and any
allowlisted year whose live spread code isn't NG does NOT fire. Matching is by
nba_id when present, else by normalized name + team (blank-id players like Cam
Christie must not silently fall through). NG may sit on ANY year, including
non-contiguous ones — nothing assumes a trailing year.

The spread (NOT the "current" table — that returns blank codes) is read from
``$SITE_DATA_ROOT/salary_spread.csv``. Both inputs are injectable for tests.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Repo-root-relative default allowlist path (src/nba_trade_analyzer/data/ -> repo root).
_REPO_ROOT = Path(__file__).resolve().parents[3]
ALLOWLIST_PATH = _REPO_ROOT / "data" / "guarantees" / "full_ng_allowlist.json"

_NG_CODE = "NG"
_SEASON_RE = re.compile(r"\d{4}-\d{2}")
# A salary string mis-loaded into an option column (e.g. "2028-29 $8,586,971") —
# Holmes II / Carter / Sarr. Never read these as a real option/guarantee code.
_MONEY_IN_CODE_RE = re.compile(r"[\$,]|\d{4,}")

# The current league year (season key). NG years AT OR BEFORE this are gated out
# of the fix — those guarantees are settled, not future commitments. The NBA
# league year rolls on July 1; **bump this single constant** at rollover (and it
# is overridable per-resolver for tests).
CURRENT_LEAGUE_YEAR = "2026-27"  # bumped 2026-07-11 (league-year rollover)

# Generational suffix tokens stripped when isolating a last name, so e.g.
# "Jaren Jackson Jr." buckets under "jackson", not "jr".
_NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}

# Minimum length of the SHORTER first name for the short/long prefix fallback to
# fire. Blocks 2-letter initials/abbreviations ("al", "aj") from prefix-matching
# a longer name; "cam" (3) is the shortest real truncation we want to bridge.
_MIN_PREFIX_LEN = 3


def _norm(value: str | None) -> str:
    return (value or "").strip().lower()


def _split_name(full: str | None) -> tuple[str, str]:
    """Split a full name into (first, last), normalized and suffix-stripped.

    Returns ("", "") for mononyms / unsplittable input, which callers treat as
    "no fallback possible" (errs toward NOT firing). Both inputs are normalized
    the same way, so the allowlist, the spread, and the query bucket together.
    """
    tokens = [t for t in re.split(r"\s+", _norm(full)) if t]
    # Drop trailing generational suffixes ("jr", "iii", with or without a dot),
    # but never strip the name down past a single token.
    while len(tokens) > 1 and tokens[-1].strip(".") in _NAME_SUFFIXES:
        tokens.pop()
    if len(tokens) < 2:
        return "", ""
    return tokens[0], tokens[-1]


def _first_name_compatible(a: str, b: str) -> bool:
    """True iff first names a/b are the same OR one is a prefix of the other and
    the shorter is at least _MIN_PREFIX_LEN chars (the short/long truncation
    class: "cam"/"cameron"). NOT a fuzzy match — pure prefix containment."""
    if a == b:
        return True
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    return len(shorter) >= _MIN_PREFIX_LEN and longer.startswith(shorter)


def _season_start_year(season: str | None) -> int | None:
    """Start year of a season key, e.g. '2026-27' -> 2026; None if malformed."""
    if season and _SEASON_RE.fullmatch(season):
        return int(season[:4])
    return None


def _to_int_id(value) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class NgAllowEntry:
    player: str
    nba_id: int | None
    team: str
    season: str
    salary: int | None
    spread_ng_confirmed: bool


def load_ng_allowlist(path: str | Path = ALLOWLIST_PATH) -> list[NgAllowEntry]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    out: list[NgAllowEntry] = []
    for e in payload.get("entries", []):
        out.append(
            NgAllowEntry(
                player=e["player"],
                nba_id=_to_int_id(e.get("nbaId")),
                team=e.get("team", ""),
                season=e["season"],
                salary=e.get("salary"),
                spread_ng_confirmed=bool(e.get("spreadNgConfirmed", False)),
            )
        )
    return out


def _id_key(nba_id: int | None, season: str) -> tuple | None:
    return ("id", nba_id, season) if nba_id is not None else None


def _name_key(player: str | None, team: str | None, season: str) -> tuple:
    return ("nameteam", _norm(player), _norm(team), season)


def build_allowlist_index(entries: list[NgAllowEntry]) -> set[tuple]:
    """Match keys for the allowlist: id-keyed when an id exists, plus a
    name+team key for every entry (so blank-id players still match)."""
    idx: set[tuple] = set()
    for e in entries:
        idk = _id_key(e.nba_id, e.season)
        if idk is not None:
            idx.add(idk)
        idx.add(_name_key(e.player, e.team, e.season))
    return idx


def load_spread_ng_codes(spread_path: str | Path) -> set[tuple]:
    """Set of NG-coded player-seasons from site_Data salary_spread.csv.

    Header-driven season detection. Option cells holding a mis-loaded salary
    string are skipped (never treated as a code). Each NG cell is recorded under
    both an id key and a name+team key so either match path works.
    """
    ng: set[tuple] = set()
    with open(spread_path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        return ng
    season_cols = [c for c in rows[0].keys() if c and _SEASON_RE.fullmatch(c)]
    for r in rows:
        nba_id = _to_int_id(r.get("nba_id"))
        name = r.get("Player")
        team = r.get("Team")
        for s in season_cols:
            code = (r.get(f"option_{s}", "") or "").strip()
            if _MONEY_IN_CODE_RE.search(code):  # malformed cell — skip
                continue
            if code.upper() == _NG_CODE:
                idk = _id_key(nba_id, s)
                if idk is not None:
                    ng.add(idk)
                ng.add(_name_key(name, team, s))
    return ng


def _name_buckets(index: set[tuple]) -> dict[tuple, dict[str, str]]:
    """Group an index's name-keys by (last, team, season) -> {first: full_norm}.

    Built from the SAME name-keys the exact path uses, so the fallback can only
    ever bridge to a player already present in that index. id-keys carry no name
    and are skipped.
    """
    buckets: dict[tuple, dict[str, str]] = {}
    for key in index:
        if not key or key[0] != "nameteam":
            continue
        _, full, team, season = key
        first, last = _split_name(full)
        if not first or not last:
            continue
        buckets.setdefault((last, team, season), {})[first] = full
    return buckets


class NonGuaranteeResolver:
    """Resolves whether a player-season should be marked as non-guaranteed."""

    def __init__(
        self,
        allowlist_index: set[tuple],
        spread_ng: set[tuple],
        current_league_year: str = CURRENT_LEAGUE_YEAR,
    ) -> None:
        self._allow = allowlist_index
        self._ng = spread_ng
        self._current_year = _season_start_year(current_league_year)
        # (last, team, season) -> {first_name: full_name_norm} buckets, derived
        # from the name-keys already in each index. Power the conservative
        # short/long first-name fallback (see is_non_guaranteed).
        self._allow_buckets = _name_buckets(allowlist_index)
        self._ng_buckets = _name_buckets(spread_ng)

    @classmethod
    def load(
        cls,
        spread_path: str | Path | None = None,
        allowlist_path: str | Path = ALLOWLIST_PATH,
        current_league_year: str = CURRENT_LEAGUE_YEAR,
    ) -> "NonGuaranteeResolver":
        if spread_path is None:
            root = os.environ.get("SITE_DATA_ROOT", os.path.expanduser("~/site_Data"))
            spread_path = os.path.join(root, "salary_spread.csv")
        allow = build_allowlist_index(load_ng_allowlist(allowlist_path))
        # Missing spread => empty NG set => nothing fires (safe default).
        spread_ng = load_spread_ng_codes(spread_path) if os.path.exists(spread_path) else set()
        return cls(allow, spread_ng, current_league_year)

    def is_non_guaranteed(
        self,
        season: str,
        *,
        nba_id: int | None = None,
        player: str | None = None,
        team: str | None = None,
    ) -> bool:
        # Gate (decision 2): only FUTURE seasons — strictly after the current
        # league year. Seasons at/before it are settled, not future commitments.
        start = _season_start_year(season)
        if start is None or (self._current_year is not None and start <= self._current_year):
            return False
        nba_id = _to_int_id(nba_id)
        idk = _id_key(nba_id, season)
        ntk = _name_key(player, team, season)
        spread_ng_exact = (idk is not None and idk in self._ng) or (ntk in self._ng)
        allowed_exact = (idk is not None and idk in self._allow) or (ntk in self._allow)
        if spread_ng_exact and allowed_exact:
            return True

        # Conservative short/long first-name fallback. Only reached after an
        # EXACT miss, so the exact path is unchanged for everyone already
        # matching. Handles blank-id name mismatches like the BBRef salary
        # source's "Cam Christie" vs the allowlist/spread's "Cameron Christie".
        # Guards: same team + same season + same last name (bucket key) + a
        # min-length first-name prefix + a uniqueness check (_bucket_match
        # returns None on an ambiguous bucket). Must satisfy BOTH indexes, so it
        # never fires on a player who isn't both allowlisted and spread-coded NG.
        first, last = _split_name(player)
        if not first or not last:
            return False
        team_norm = _norm(team)
        ng_match = self._bucket_match(first, last, team_norm, season, self._ng_buckets)
        allow_match = self._bucket_match(first, last, team_norm, season, self._allow_buckets)
        spread_ng = spread_ng_exact or ng_match is not None
        allowed = allowed_exact or allow_match is not None
        if spread_ng and allowed:
            # Audit: the exact path stays silent; every fallback bridge is logged
            # so a short/long match is never silent.
            logger.info(
                "NG short/long fallback bridged: %r -> %r (%s %s)",
                player,
                allow_match or ng_match,
                team,
                season,
            )
            return True
        return False

    def _bucket_match(
        self,
        first: str,
        last: str,
        team_norm: str,
        season: str,
        buckets: "dict[tuple, dict[str, str]]",
    ) -> str | None:
        """Return the matched full name if EXACTLY ONE first name in the
        (last, team, season) bucket is prefix-compatible with `first`, else None.
        The uniqueness requirement is the over-match guard: an ambiguous short
        name (bucket holds two distinct compatible first names) resolves to None.
        """
        bucket = buckets.get((last, team_norm, season))
        if not bucket:
            return None
        matched = [f for f in bucket if _first_name_compatible(first, f)]
        if len(matched) != 1:
            return None
        return bucket[matched[0]]
