"""Positional-overlap diagnostic (issue: positional penalty saturation).

A verification harness, not a fix. Prints, for every incoming player across the
validate_trades trade set, the acquiring team's clean (post roster-fix)
minutes-by-bucket totals, the incoming player's resolved bucket, that bucket's
*relative share* (minutes-share ÷ fair-share) versus the need/logjam thresholds,
and the resulting positional modifier. The goal is to read, on sight, whether
the modifier distribution spreads (a healthy share metric) or collapses onto one
rail (the old bucket-sum units bug, which saturated every player at -MAX).

Run from repo root::

    uv run python scripts/diagnose_positional.py

Reuses validate_trades' loaders, trade list, and the exact grader roster path
(_roster_dicts -> filter_to_current_roster -> _minutes_by_position), so the
numbers reflect the genuinely-clean pipeline, not a fixture.
"""

from __future__ import annotations

from statistics import mean

import pandas as pd

from nba_trade_analyzer.cli import _build_team, _load_core_data, _stats_lookup
from nba_trade_analyzer.data.darko import fetch_darko_data
from nba_trade_analyzer.data.epm import normalize_name
from nba_trade_analyzer.data.players import fetch_roster_player_ids
from nba_trade_analyzer.engine.constants import (
    POSITIONAL_FAIR_SHARE,
    POSITIONAL_LOGJAM_MULT,
    POSITIONAL_MAX_ADJUSTMENT,
    POSITIONAL_NEED_MULT,
)
from nba_trade_analyzer.engine.grader import _roster_dicts
from nba_trade_analyzer.engine.team_context import (
    _coarse_position,
    _minutes_by_position,
    _trim_outgoing,
    calculate_positional_modifier,
    filter_to_current_roster,
    resolve_position,
)
from nba_trade_analyzer.report import force_utf8_stdout
from nba_trade_analyzer.teams import resolve_team

# Reuse the exact same trade set the smoke test exercises.
from validate_trades import TRADES  # noqa: E402

LOGJAM = POSITIONAL_LOGJAM_MULT
NEED = POSITIONAL_NEED_MULT
MAXADJ = POSITIONAL_MAX_ADJUSTMENT


def _epm_position(name: str, epm_df: pd.DataFrame) -> str | None:
    """Position label from the EPM feed (keyed by normalized name), or None."""
    if epm_df is None or epm_df.empty or "player_name_normalized" not in epm_df:
        return None
    match = epm_df[epm_df["player_name_normalized"] == normalize_name(name)]
    if match.empty:
        return None
    pos = match.iloc[0].get("position")
    if pos is None or (isinstance(pos, float) and pd.isna(pos)):
        return None
    return str(pos)


def _incoming_position(
    name: str, epm_df: pd.DataFrame, stats_lookup: dict[str, pd.Series]
) -> str | None:
    """Resolve a player's coarse position the way the context engine would.

    Mirrors ``_player_position``: prefers EPM's label, falls back to the stats
    feed, applies the per-player override (e.g. Luka -> G), coarsens to G/F/C.
    """
    raw = _epm_position(name, epm_df)
    if raw is None:
        row = stats_lookup.get(normalize_name(name))
        if row is not None:
            cand = row.get("position")
            if cand is not None and not (isinstance(cand, float) and pd.isna(cand)):
                raw = cand
    resolved = resolve_position(name, raw)
    buckets = _coarse_position(resolved)
    return buckets[0][0] if buckets else None


def _relative_share(bucket: str, bucket_sums: dict[str, float]) -> float | None:
    """Bucket's share of total team minutes ÷ its fair share, or None if N/A."""
    total = sum(bucket_sums.values())
    fair = POSITIONAL_FAIR_SHARE.get(bucket)
    if total <= 0 or not fair:
        return None
    return (bucket_sums.get(bucket, 0.0) / total) / fair


def _band_position(relative: float) -> str:
    """Human label for where a bucket's relative share lands in the band."""
    if relative >= LOGJAM:
        return f"LOGJAM    (rel {relative:.2f} >= {LOGJAM:.2f})  -> -{MAXADJ:.0%}"
    if relative <= NEED:
        return f"NEED      (rel {relative:.2f} <= {NEED:.2f})  -> +{MAXADJ:.0%}"
    return f"in-band   (rel {relative:.2f}, {NEED:.2f}-{LOGJAM:.2f})"


def main() -> None:
    force_utf8_stdout()
    print("Loading data sources (stats, EPM, salaries, DARKO)...")
    epm_df, stats_df, salary_df = _load_core_data()
    _ = fetch_darko_data()  # warm cache / parity with smoke test
    stats_lookup = _stats_lookup(stats_df)

    print()
    print("=" * 78)
    print(
        f"POSITIONAL DIAGNOSTIC   fair-share={POSITIONAL_FAIR_SHARE}  "
        f"need<={NEED:.2f}  logjam>={LOGJAM:.2f}  max=+/-{MAXADJ:.0%}"
    )
    print("=" * 78)

    all_modifiers: list[float] = []
    all_relatives: list[float] = []
    saturated_high = 0
    saturated_low = 0
    in_band = 0
    sat_eps = 1e-9

    for spec in TRADES:
        if not spec.get("expected_legal", False):
            continue  # illegal trades short-circuit before context eval

        info_a = resolve_team(spec["team_a"])
        info_b = resolve_team(spec["team_b"])
        if info_a is None or info_b is None:
            continue

        team_a = _build_team(info_a, salary_df)
        team_b = _build_team(info_b, salary_df)
        ids_a = fetch_roster_player_ids(info_a.abbreviation)
        ids_b = fetch_roster_player_ids(info_b.abbreviation)

        print(f"\n--- {spec['name']} ---")

        # team A receives team B's outgoing players, and vice versa.
        for recv_team, recv_info, recv_ids, send_team, incoming_names in (
            (team_a, info_a, ids_a, team_b, spec["sends_b"]),
            (team_b, info_b, ids_b, team_a, spec["sends_a"]),
        ):
            roster = _roster_dicts(stats_df, recv_info.abbreviation)
            roster = filter_to_current_roster(roster, recv_ids)
            # outgoing for THIS receiving team = the players IT sends out
            outgoing = spec["sends_a"] if recv_team is team_a else spec["sends_b"]
            roster_trimmed = _trim_outgoing(roster, outgoing)
            bucket_sums = _minutes_by_position(roster_trimmed)

            print(
                f"  {recv_info.abbreviation} clean roster bucket minutes: "
                + "  ".join(f"{b}={m:6.1f}" for b, m in bucket_sums.items())
            )

            # only player assets are position-relevant (picks skipped)
            for name in incoming_names:
                # crude pick filter: pick strings contain a 4-digit year
                if any(tok.isdigit() and len(tok) == 4 for tok in name.split()):
                    continue
                bucket = _incoming_position(name, epm_df, stats_lookup)
                if bucket is None:
                    # No resolvable position → the engine returns 0.0 (no
                    # positional signal); count it as in-band, not saturated.
                    print(f"    {name:<22} bucket=?    (no position label) mod=+0.0%")
                    continue
                relative = _relative_share(bucket, bucket_sums)
                mod = calculate_positional_modifier(bucket, roster_trimmed)
                all_modifiers.append(mod)
                if relative is not None:
                    all_relatives.append(relative)
                # Classify on the modifier itself: saturated only at the ±MAX rails.
                if mod <= -MAXADJ + sat_eps:
                    saturated_high += 1
                elif mod >= MAXADJ - sat_eps:
                    saturated_low += 1
                else:
                    in_band += 1
                rel_str = f"{relative:.2f}" if relative is not None else "n/a"
                print(
                    f"    {name:<22} bucket={bucket}  "
                    f"{recv_info.abbreviation} {bucket}-share rel={rel_str}  "
                    f"mod={mod:+.1%}   "
                    f"{_band_position(relative) if relative is not None else 'n/a'}"
                )

    # ---- distribution summary ----
    n = len(all_modifiers)
    print()
    print("=" * 78)
    print("DISTRIBUTION SUMMARY")
    print("=" * 78)
    if n == 0:
        print("No position-bearing incoming players found.")
        return
    print(f"  incoming players scored:        {n}")
    print(
        f"  saturated at HIGH (-{MAXADJ:.0%} penalty): "
        f"{saturated_high:3d}  ({saturated_high / n:5.1%})"
    )
    print(
        f"  saturated at LOW  (+{MAXADJ:.0%} bonus):   "
        f"{saturated_low:3d}  ({saturated_low / n:5.1%})"
    )
    print(f"  in-band (carries signal):       {in_band:3d}  ({in_band / n:5.1%})")
    print()
    if all_relatives:
        print(
            f"  relative share        min/mean/max: "
            f"{min(all_relatives):.2f} / {mean(all_relatives):.2f} / "
            f"{max(all_relatives):.2f}"
        )
    print(
        f"  modifier              min/mean/max: "
        f"{min(all_modifiers):+.1%} / {mean(all_modifiers):+.1%} / "
        f"{max(all_modifiers):+.1%}"
    )
    print()
    print("  READ: a healthy distribution spreads — most incoming players land")
    print("  in-band (relative share between need and logjam) and carry a real,")
    print("  unsaturated signal; only genuine logjams or needs hit the ±MAX rails.")
    print("  A return to ~100% at one rail means the share metric has regressed.")


if __name__ == "__main__":
    main()
