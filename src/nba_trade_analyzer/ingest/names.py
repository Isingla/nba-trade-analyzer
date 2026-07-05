"""Crosswalk-backed name resolution for site_Data CSVs (Phase 2A).

site_Data files key players by display name only (no ids), in two shapes:
plain ("Bradley Beal") and Spotrac waived-marker style ("Lillard Damian" —
surname first, after the WAIVED strip). The committed crosswalk carries both
a BBRef name and an nba_api name per player, so it is the name authority.

Resolution ladder (most exact first, ambiguity always loses):
  1. exact normalized match on either crosswalk name
  2. exact match with word order flipped (surname-first inputs) — both the
     two-token flip ("Lillard Damian") and the last-token-to-front flip for
     multi-word surnames / double given names ("Da Silva Tristan" ->
     "Tristan Da Silva", "Konan Niederhauser Yanic" -> "Yanic Konan
     Niederhauser")
  3. unique last-name bucket + first-name prefix (>=3 chars, either direction)
     — the Cam/Cameron Christie and Svi/Sviatoslav Mykhailiuk bridge, same
     guard shape as the NG resolver's short/long fallback.

Normalization is MATCH-ONLY and folds to ASCII (NFKD + strip combining marks
+ casefold): site_Data carries ASCII names (Nikola Jokic) while the
BBRef-derived crosswalk carries accented forms (Jokić) — both sides fold to
the same key. Stored names are never rewritten; only comparison keys fold.

Any bucket with more than one candidate returns None — the caller records an
``unverifiable`` verdict rather than guessing (never silently skipped).
"""

from __future__ import annotations

import logging
import re
import unicodedata

from nba_trade_analyzer.data.crosswalk import Crosswalk

logger = logging.getLogger(__name__)

_MIN_PREFIX_LEN = 3
_NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def _ascii_fold(value: str) -> str:
    """Jokić -> Jokic: NFKD-decompose, drop combining marks."""
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _norm(value: str | None) -> str:
    s = _ascii_fold((value or "").strip()).casefold()
    s = re.sub(r"[.'’]", "", s)  # J.D. -> jd, O'Neale -> oneale
    return re.sub(r"\s+", " ", s)


def _tokens(value: str | None) -> list[str]:
    toks = _norm(value).split(" ")
    while len(toks) > 1 and toks[-1] in _NAME_SUFFIXES:
        toks.pop()
    return [t for t in toks if t]


class NameResolver:
    """Deterministic name -> bbref_slug resolution over the crosswalk."""

    def __init__(self, crosswalk: Crosswalk) -> None:
        self._exact: dict[str, str | None] = {}  # normalized name -> slug (None = ambiguous)
        self._last_buckets: dict[str, list[tuple[str, str]]] = {}  # last -> [(first, slug)]
        for e in crosswalk.entries:
            for name in (e.bbref_name, e.nba_name):
                toks = _tokens(name)
                if not toks:
                    continue
                key = " ".join(toks)
                if key not in self._exact:
                    self._exact[key] = e.bbref_slug
                elif self._exact[key] != e.bbref_slug:
                    # Two different players share a name: exact match is unsafe.
                    if self._exact[key] is not None:
                        logger.warning("names: ambiguous exact name %r", name)
                    self._exact[key] = None
                if len(toks) >= 2:
                    first, last = toks[0], toks[-1]
                    bucket = self._last_buckets.setdefault(last, [])
                    if (first, e.bbref_slug) not in bucket:
                        bucket.append((first, e.bbref_slug))

    def resolve(self, raw_name: str | None) -> str | None:
        toks = _tokens(raw_name)
        if not toks:
            return None

        # 1. exact, as written.
        hit = self._exact.get(" ".join(toks), None)
        if hit is not None:
            return hit

        # 2. exact, surname-first flipped. Two variants, exact-map hits only:
        #    first-token-to-end  "Lillard Damian"           -> "Damian Lillard"
        #    last-token-to-front "Da Silva Tristan"         -> "Tristan Da Silva"
        #                        "Konan Niederhauser Yanic" -> "Yanic Konan Niederhauser"
        if len(toks) >= 2:
            for flipped_toks in ([*toks[1:], toks[0]], [toks[-1], *toks[:-1]]):
                hit = self._exact.get(" ".join(flipped_toks), None)
                if hit is not None:
                    return hit

        # 3. unique last-name bucket + first-name prefix, trying both word orders.
        if len(toks) >= 2:
            for first, last in ((toks[0], toks[-1]), (toks[-1], toks[0])):
                if len(first) < _MIN_PREFIX_LEN:
                    continue
                bucket = self._last_buckets.get(last, [])
                matches = [
                    slug
                    for cand_first, slug in bucket
                    if len(cand_first) >= _MIN_PREFIX_LEN
                    and (cand_first.startswith(first) or first.startswith(cand_first))
                ]
                unique = sorted(set(matches))
                if len(unique) == 1:
                    logger.info(
                        "names: prefix fallback bridged %r -> %s", raw_name, unique[0]
                    )
                    return unique[0]
                if len(unique) > 1:
                    logger.warning(
                        "names: ambiguous fallback for %r (%s)", raw_name, unique
                    )
                    return None
        return None
