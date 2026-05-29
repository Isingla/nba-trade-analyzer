"""The NBA-ID <-> Basketball-Reference-slug crosswalk — the single chokepoint.

The project pulls salaries from Basketball Reference (keyed by permanent
*slug*, e.g. ``doncilu01``) and everything else — stats, roster membership,
impact metrics — from nba_api (keyed by numeric *player id*). The two sources
share no id system. This module is the one place those ids are joined.

**All fuzzy / name-based matching happens once, offline, in
``scripts/build_crosswalk.py`` and is frozen into ``data/player_crosswalk.json``
(committed, human-auditable).** At runtime the join is a deterministic
dictionary lookup on ids only — no name matching, ever. Loading the crosswalk
validates it (no duplicate ids, no duplicate slugs, no many-to-one collisions
in either direction) and raises :class:`CrosswalkError` on any violation, so a
corrupt artifact fails loud at startup rather than silently mis-joining.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Repo-root ``data/player_crosswalk.json`` (committed). This module lives at
# ``src/nba_trade_analyzer/data/crosswalk.py``; parents[3] is the repo root.
DEFAULT_CROSSWALK_PATH = (
    Path(__file__).resolve().parents[3] / "data" / "player_crosswalk.json"
)

# Bumped only on a breaking schema change; load asserts the file matches.
SCHEMA_VERSION = 1


class CrosswalkError(Exception):
    """Raised when the crosswalk file is malformed or internally inconsistent."""


@dataclass(frozen=True)
class CrosswalkEntry:
    nba_id: int
    nba_name: str
    bbref_slug: str
    bbref_name: str


class Crosswalk:
    """Bidirectional, validated NBA-id <-> BBRef-slug map.

    Construction validates the entries and raises :class:`CrosswalkError` on any
    duplicate id, duplicate slug, or many-to-one collision — the guard is in the
    constructor so every load path (and every test fixture) is checked.
    """

    def __init__(self, entries: list[CrosswalkEntry]) -> None:
        nba_to_slug: dict[int, str] = {}
        slug_to_nba: dict[str, int] = {}
        for e in entries:
            if e.nba_id in nba_to_slug:
                raise CrosswalkError(
                    f"duplicate nba_id {e.nba_id} "
                    f"(slugs {nba_to_slug[e.nba_id]!r} and {e.bbref_slug!r})"
                )
            if e.bbref_slug in slug_to_nba:
                raise CrosswalkError(
                    f"duplicate bbref_slug {e.bbref_slug!r} "
                    f"(nba_ids {slug_to_nba[e.bbref_slug]} and {e.nba_id})"
                )
            nba_to_slug[e.nba_id] = e.bbref_slug
            slug_to_nba[e.bbref_slug] = e.nba_id
        self._entries = list(entries)
        self._nba_to_slug = nba_to_slug
        self._slug_to_nba = slug_to_nba
        self._by_nba = {e.nba_id: e for e in entries}
        self._by_slug = {e.bbref_slug: e for e in entries}

    @property
    def entries(self) -> list[CrosswalkEntry]:
        return list(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def slug_for_nba_id(self, nba_id: int) -> str | None:
        return self._nba_to_slug.get(nba_id)

    def nba_id_for_slug(self, slug: str) -> int | None:
        return self._slug_to_nba.get(slug)

    def entry_for_nba_id(self, nba_id: int) -> CrosswalkEntry | None:
        return self._by_nba.get(nba_id)

    def entry_for_slug(self, slug: str) -> CrosswalkEntry | None:
        return self._by_slug.get(slug)


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise CrosswalkError(msg)


def _parse_entry(raw: Any, idx: int) -> CrosswalkEntry:
    _require(isinstance(raw, dict), f"entry {idx} is not an object")
    for field in ("nba_id", "nba_name", "bbref_slug", "bbref_name"):
        _require(field in raw, f"entry {idx} missing required field {field!r}")
    _require(
        isinstance(raw["nba_id"], int) and not isinstance(raw["nba_id"], bool),
        f"entry {idx} nba_id must be an integer",
    )
    _require(
        isinstance(raw["bbref_slug"], str) and bool(raw["bbref_slug"]),
        f"entry {idx} bbref_slug must be a non-empty string",
    )
    return CrosswalkEntry(
        nba_id=int(raw["nba_id"]),
        nba_name=str(raw["nba_name"]),
        bbref_slug=str(raw["bbref_slug"]),
        bbref_name=str(raw["bbref_name"]),
    )


def crosswalk_from_dict(data: Any) -> Crosswalk:
    """Validate a parsed-JSON payload and build a :class:`Crosswalk`."""
    _require(isinstance(data, dict), "crosswalk root must be a JSON object")
    _require(
        data.get("schema_version") == SCHEMA_VERSION,
        f"crosswalk schema_version must be {SCHEMA_VERSION}, "
        f"got {data.get('schema_version')!r}",
    )
    entries = data.get("entries")
    _require(isinstance(entries, list), "crosswalk 'entries' must be a list")
    parsed = [_parse_entry(raw, i) for i, raw in enumerate(entries)]
    return Crosswalk(parsed)  # constructor enforces uniqueness/collision guards


def load_crosswalk(path: Path | str | None = None) -> Crosswalk:
    """Load and validate the committed crosswalk from ``path``.

    Raises :class:`CrosswalkError` if the file is missing, not valid JSON, the
    schema is wrong, or any id/slug duplicate or collision is present.
    """
    p = Path(path) if path is not None else DEFAULT_CROSSWALK_PATH
    if not p.exists():
        raise CrosswalkError(f"crosswalk file not found: {p}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CrosswalkError(f"crosswalk file is not valid JSON: {exc}") from exc
    return crosswalk_from_dict(data)


def dump_crosswalk(entries: list[CrosswalkEntry], season: str) -> dict[str, Any]:
    """Serialize entries (sorted by slug) into the on-disk JSON schema."""
    ordered = sorted(entries, key=lambda e: e.bbref_slug)
    return {
        "schema_version": SCHEMA_VERSION,
        "season": season,
        "entries": [
            {
                "nba_id": e.nba_id,
                "nba_name": e.nba_name,
                "bbref_slug": e.bbref_slug,
                "bbref_name": e.bbref_name,
            }
            for e in ordered
        ],
    }
