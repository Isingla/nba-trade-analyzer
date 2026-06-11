"""Draft-pick ownership verification.

A Python port of the databallr_v3 seed parser/validator
(``lib/draft-picks/seed/parse.ts``). It loads the mirrored ``draft-picks.csv`` +
``KNOWN_GAPS.md`` (canonical source is databallr; see the mirror headers), runs
the same load-time integrity checks so a stale/corrupt mirror fails LOUDLY, and
exposes :meth:`PickRegistry.verify` so the trade validator can reject a team from
trading a pick it does not control.

Ownership is the question the valuation engine never asked: ``engine/draft_picks``
prices a pick by its *originating* team's record, but nothing checked whether the
*sending* team actually owns it. This module closes that gap.

Verdicts (:class:`PickVerification`):

- :class:`Verified` — determinate owner, and it IS the originating team (it kept
  its own pick): ``verify("WAS", 2026, 1) -> Verified("WAS")``.
- :class:`NotOwner` — determinate owner, but a DIFFERENT team holds it:
  ``verify("LAC", 2026, 1) -> NotOwner("OKC")``.
- :class:`Indeterminate` — the pick sits inside a gapped conditional/multi-team
  chain; carries the verbatim RealGM clause from ``KNOWN_GAPS.md``. Warn-only.
- :class:`NoRecord` — the key is absent from BOTH files. The coverage census
  guarantees every real team has rows, so an absent key means the pick likely
  does not exist (typo, already-conveyed 2025 pick, invalid round). REJECTION,
  not a warning — a $0-plus-warning would let typos through grading.
"""

from __future__ import annotations

import csv
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from nba_trade_analyzer.teams import VALID_ABBREVIATIONS, resolve_team

# --- mirror locations (canonical source is databallr_v3) --------------------
_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "draft_picks"
DEFAULT_CSV_PATH = _DATA_DIR / "draft-picks.csv"
DEFAULT_GAPS_PATH = _DATA_DIR / "KNOWN_GAPS.md"

# --- team canonicalization (reuse the crosswalk; mirror the TS SOURCE_ALIASES)
# RealGM diverges from nba_api for five teams; Basketball-Reference for two more.
# resolve_team() already handles the BR salary-abbreviation codes (BRK/CHO/PHO),
# but NOT the RealGM-only codes (GOS/PHL/SAN/UTH) — so we pre-map all alt codes,
# then delegate to resolve_team() for the canonical nba_api abbreviation.
SOURCE_ALIASES: dict[str, str] = {
    "BRK": "BKN",  # RealGM + Basketball-Reference -> Brooklyn
    "GOS": "GSW",  # RealGM -> Golden State
    "PHL": "PHI",  # RealGM -> Philadelphia
    "SAN": "SAS",  # RealGM -> San Antonio
    "UTH": "UTA",  # RealGM -> Utah
    "CHO": "CHA",  # Basketball-Reference -> Charlotte
    "PHO": "PHX",  # Basketball-Reference -> Phoenix
}

EARLIEST_YEAR = 2025
LATEST_YEAR = 2035


class PickRegistryError(RuntimeError):
    """Raised when the mirror is stale/corrupt — a load-time integrity failure."""


def canonicalize_team(code: str) -> str:
    """Resolve any alt-source code to the canonical nba_api abbreviation.

    Applies the RealGM/BR alias pre-map, then the shared :func:`resolve_team`
    crosswalk. Raises :class:`PickRegistryError` on an unrecognized code.
    """
    raw = (code or "").strip().upper()
    aliased = SOURCE_ALIASES.get(raw, raw)
    team = resolve_team(aliased)
    if team is None:
        raise PickRegistryError(f"unrecognized team code {code!r}")
    return team.abbreviation


# The 30 canonical nba_api abbreviations (BKN/CHA/PHX, not BRK/CHO/PHO).
CANONICAL_TEAMS: frozenset[str] = frozenset(
    resolve_team(a).abbreviation  # type: ignore[union-attr]  # all 30 resolve
    for a in VALID_ABBREVIATIONS
)


# --- rows -------------------------------------------------------------------
@dataclass(frozen=True)
class PickRow:
    """One CSV row, with team codes canonicalized to nba_api form."""

    team: str  # ORIGINATING team (sets value)
    owner_team: str  # CONTROLLING team / swap holder
    year: int
    round: int
    protections: str | None
    swap: bool
    via_team: str | None

    @property
    def key(self) -> tuple[str, int, int]:
        return (self.team, self.year, self.round)


def _parse_rows(csv_text: str) -> list[PickRow]:
    rows: list[PickRow] = []
    reader = csv.reader(csv_text.splitlines())
    for lineno, cells in enumerate(reader, start=1):
        if not cells:
            continue
        first = cells[0].strip()
        if not first or first.startswith("#"):
            continue
        if first == "team":  # column header
            continue
        if len(cells) != 7:
            raise PickRegistryError(
                f"row {lineno}: expected 7 comma-separated columns, got {len(cells)}: {cells!r}"
            )
        team, owner, year_s, round_s, protections, swap_s, via = (c.strip() for c in cells)
        try:
            year = int(year_s)
            rnd = int(round_s)
        except ValueError as exc:
            raise PickRegistryError(f"row {lineno}: non-integer year/round: {cells!r}") from exc
        if not EARLIEST_YEAR <= year <= LATEST_YEAR:
            raise PickRegistryError(f"row {lineno}: year {year} out of range")
        if rnd not in (1, 2):
            raise PickRegistryError(f"row {lineno}: round must be 1 or 2, got {rnd}")
        rows.append(
            PickRow(
                team=canonicalize_team(team),
                owner_team=canonicalize_team(owner),
                year=year,
                round=rnd,
                protections=protections or None,
                swap=swap_s.lower() == "true",
                via_team=canonicalize_team(via) if via else None,
            )
        )
    return rows


# --- gaps -------------------------------------------------------------------
# Matches a numbered KNOWN_GAPS entry: `N. **TEAM · YYYY · RX** — "clause"`.
# `[^*]*` tolerates suffixes like "R1 swap component". Resolved (`~~...~~`)
# entries never match because `~~` sits between the number and the `**`.
_GAP_RE = re.compile(
    r'^\s*\d+\.\s+\*\*([A-Z]{2,3})\s*·\s*(\d{4})\s*·\s*R([12])[^*]*\*\*\s*—\s*"([^"]*)"'
)


_SYNC_RE = re.compile(r"Synced:\s*(\d{4}-\d{2}-\d{2})")


def _parse_sync_date(text: str) -> str | None:
    """Pull the ``# Synced: YYYY-MM-DD`` date out of the mirror's provenance header."""
    m = _SYNC_RE.search(text)
    return m.group(1) if m else None


def _parse_gaps(md_text: str) -> dict[tuple[str, int, int], list[str]]:
    gaps: dict[tuple[str, int, int], list[str]] = {}
    for line in md_text.splitlines():
        m = _GAP_RE.match(line)
        if not m:
            continue
        team, year_s, round_s, clause = m.groups()
        key = (canonicalize_team(team), int(year_s), int(round_s))
        gaps.setdefault(key, []).append(clause.strip())
    return gaps


# --- verdicts ---------------------------------------------------------------
@dataclass(frozen=True)
class Verified:
    """The originating team controls its own pick. ``owner == queried team``."""

    owner: str


@dataclass(frozen=True)
class NotOwner:
    """A different team controls the pick (outright owner or swap holder)."""

    actual_owner: str


@dataclass(frozen=True)
class Indeterminate:
    """The pick sits in a gapped chain; carries the verbatim RealGM clause(s)."""

    clause: str


@dataclass(frozen=True)
class NoRecord:
    """No row and no gap for this key — likely a non-existent pick (REJECTION)."""

    team: str
    year: int
    round: int


PickVerification = Verified | NotOwner | Indeterminate | NoRecord


def _owner_of(verdict: PickVerification) -> str | None:
    """The controlling team for a determinate verdict, else ``None``."""
    if isinstance(verdict, Verified):
        return verdict.owner
    if isinstance(verdict, NotOwner):
        return verdict.actual_owner
    return None


# --- registry ---------------------------------------------------------------
class PickRegistry:
    """Loaded, validated draft-pick ownership index."""

    def __init__(
        self,
        rows: list[PickRow],
        gaps: dict[tuple[str, int, int], list[str]],
        *,
        sync_date: str | None = None,
    ):
        self._rows = rows
        self._gaps = gaps
        self.sync_date = sync_date
        """The mirror's ``# Synced: YYYY-MM-DD`` date, for staleness diagnostics."""
        self._by_key: dict[tuple[str, int, int], list[PickRow]] = {}
        for r in rows:
            self._by_key.setdefault(r.key, []).append(r)
        self._validate()

    # -- construction --
    @classmethod
    def load(
        cls, csv_path: Path | str = DEFAULT_CSV_PATH, gaps_path: Path | str = DEFAULT_GAPS_PATH
    ) -> PickRegistry:
        csv_text = Path(csv_path).read_text(encoding="utf-8")
        rows = _parse_rows(csv_text)
        gaps = _parse_gaps(Path(gaps_path).read_text(encoding="utf-8"))
        return cls(rows, gaps, sync_date=_parse_sync_date(csv_text))

    # -- load-time integrity (ported from findPickCollisions + coverage census) --
    def _validate(self) -> None:
        problems: list[str] = []
        for key, group in self._by_key.items():
            label = f"{key[0]} {key[1]} R{key[2]}"
            outright = [r for r in group if not r.swap]
            swaps = [r for r in group if r.swap]
            outright_owners = {r.owner_team for r in outright}
            # contradiction: >1 distinct outright owner for the same pick
            if len(outright_owners) > 1:
                problems.append(f"contradiction: {label} claimed by {sorted(outright_owners)}")
            # multiple-swaps: >1 swap right over the same pick
            if len(swaps) > 1:
                problems.append(
                    f"multiple-swaps: {label} has {len(swaps)} swap rights "
                    f"({sorted(r.owner_team for r in swaps)})"
                )
            # orphan-swap: a swap right with no outright pick to reference.
            # (own + swap sharing a key is VALID — not flagged.)
            if swaps and not outright:
                problems.append(f"orphan-swap: {label} swap has no outright referent")
        # 30-team coverage census: every team must control >=1 of its own picks.
        with_own = {r.team for r in self._rows if r.team == r.owner_team}
        missing = sorted(CANONICAL_TEAMS - with_own)
        if missing:
            problems.append(f"coverage: {len(missing)} team(s) have no own-pick row: {missing}")
        if problems:
            raise PickRegistryError(
                "draft-pick mirror failed integrity checks (stale/corrupt?):\n  "
                + "\n  ".join(problems)
            )

    # -- public API --
    def verify(self, team: str, year: int, round: int, *, swap: bool = False) -> PickVerification:
        """Resolve who controls the pick originating from ``team`` in ``year``/``round``.

        ``swap=True`` resolves against the swap-right rows (the owner is the swap
        HOLDER). Unknown team codes and absent keys return :class:`NoRecord`.
        """
        try:
            canon = canonicalize_team(team)
        except PickRegistryError:
            return NoRecord(team, year, round)
        key = (canon, year, round)
        matching = [r for r in self._by_key.get(key, []) if r.swap == swap]
        if matching:
            owner = matching[0].owner_team  # validator guarantees uniqueness
            return Verified(owner) if owner == canon else NotOwner(owner)
        clauses = self._gaps.get(key)
        if clauses:
            return Indeterminate(" | ".join(clauses))
        return NoRecord(canon, year, round)

    # -- introspection (handy for tests / debugging) --
    @property
    def row_count(self) -> int:
        return len(self._rows)

    @property
    def gap_count(self) -> int:
        return len(self._gaps)

    def teams_missing_own_picks(self) -> list[str]:
        with_own = {r.team for r in self._rows if r.team == r.owner_team}
        return sorted(CANONICAL_TEAMS - with_own)


@lru_cache(maxsize=1)
def get_default_registry() -> PickRegistry:
    """Load (once) the registry from the committed mirror."""
    return PickRegistry.load()


# --- trade integration ------------------------------------------------------
@dataclass(frozen=True)
class PickOwnershipReport:
    """Outcome of verifying every pick asset in a proposed trade."""

    rejections: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.rejections


def iter_trade_pick_verdicts(
    trade, registry: PickRegistry
) -> Iterator[tuple[str, str, object, PickVerification]]:
    """Yield ``(sender_abbr, sender_canonical, pick, verdict)`` for every pick asset.

    The shared iteration so callers can apply their own policy (the library treats
    :class:`NoRecord` as a rejection; the CLI downgrades it to a warning).
    """
    for sender, assets in (
        (trade.team_a, trade.team_a_sends),
        (trade.team_b, trade.team_b_sends),
    ):
        try:
            sending = canonicalize_team(sender.abbreviation)
        except PickRegistryError:
            sending = sender.abbreviation
        for pick in assets.picks:
            verdict = registry.verify(pick.team, pick.year, pick.round, swap=pick.swap)
            yield sender.abbreviation, sending, pick, verdict


def verify_trade_pick_ownership(trade, registry: PickRegistry) -> PickOwnershipReport:
    """Verify that each team sends only picks it controls.

    - A pick controlled by a team OTHER than the sender -> rejection (real owner).
    - :class:`NoRecord` (pick not in the registry) -> rejection.
    - :class:`Indeterminate` (gapped pick) -> warning with the verbatim clause; the
      trade is NOT hard-failed (a human resolves the conditional).
    """
    rejections: list[str] = []
    warnings: list[str] = []
    for sender_abbr, sender_canon, pick, verdict in iter_trade_pick_verdicts(trade, registry):
        if isinstance(verdict, Indeterminate):
            warnings.append(
                f'{pick.label}: ownership indeterminate (gapped) — "{verdict.clause}"'
            )
        elif isinstance(verdict, NoRecord):
            rejections.append(
                f"{sender_abbr} cannot trade {pick.label}: no such pick in the "
                f"ownership registry (typo / already conveyed / invalid round)."
            )
        elif _owner_of(verdict) != sender_canon:
            rejections.append(
                f"{sender_abbr} cannot trade {pick.label}: controlled by {_owner_of(verdict)}."
            )
    return PickOwnershipReport(rejections=rejections, warnings=warnings)


# --- stub (NOT implemented) -------------------------------------------------
def stepien_check(trade) -> list[str]:
    """STUB — Ted Stepien rule: a team may not be left without a controllable
    first-round pick in consecutive future drafts.

    A full implementation would, for each team in the trade, project its
    post-trade slate of CONTROLLED first-rounders (own picks not conveyed, plus
    incoming firsts, accounting for swaps/protections via this registry) and flag
    any pair of consecutive future years where the team would control zero firsts.

    Returns a list of violation messages (empty = compliant).

    TODO: implement once the registry exposes a per-team forward first-round
    slate (and the trade mutation is modeled). For now, always returns [].
    """
    return []  # TODO: not yet implemented


__all__ = [
    "PickRegistry",
    "PickRegistryError",
    "PickRow",
    "PickVerification",
    "Verified",
    "NotOwner",
    "Indeterminate",
    "NoRecord",
    "PickOwnershipReport",
    "verify_trade_pick_ownership",
    "iter_trade_pick_verdicts",
    "stepien_check",
    "canonicalize_team",
    "get_default_registry",
    "CANONICAL_TEAMS",
    "SOURCE_ALIASES",
    "DEFAULT_CSV_PATH",
    "DEFAULT_GAPS_PATH",
]
