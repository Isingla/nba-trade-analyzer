"""Positional-overlap diagnostic (issue: positional penalty saturation).

NOT a fix. Prints, for every incoming player across the validate_trades trade
set, the acquiring team's clean (post roster-fix) minutes-by-bucket totals, the
incoming player's resolved bucket, where that bucket sits relative to the
POSITIONAL_MINUTES_THRESHOLD_LOW / _HIGH band, and the resulting positional
modifier. The goal is to read, on sight, whether the penalty saturates because
the thresholds are simply mis-set for bucket-sum units (units-mismatch
hypothesis) or for some subtler reason.

Run from repo root::

    uv run python scripts/diagnose_positional.py

Reuses validate_trades' loaders, trade list, and the exact grader roster path
(_roster_dicts -> filter_to_current_roster -> _minutes_by_position), so the
numbers reflect the genuinely-clean pipeline, not a fixture.
"""

from __future__ import annotations

from statistics import mean
from typing import Any

import pandas as pd

from nba_trade_analyzer.cli import _build_team, _load_core_data, _stats_lookup
from nba_trade_analyzer.data.darko import fetch_darko_data
from nba_trade_analyzer.data.epm import normalize_name
from nba_trade_analyzer.data.players import fetch_roster_player_ids
from nba_trade_analyzer.engine.constants import (
    POSITIONAL_MAX_ADJUSTMENT,
    POSITIONAL_MINUTES_THRESHOLD_HIGH,
    POSITIONAL_MINUTES_THRESHOLD_LOW,
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

HIGH = POSITIONAL_MINUTES_THRESHOLD_HIGH
LOW = POSITIONAL_MINUTES_THRESHOLD_LOW
MAXADJ = POSITIONAL_MAX_ADJUSTMENT


def _incoming_position(
    name: str, epm_df: pd.DataFrame, stats_lookup: dict[str, pd.Series]
) -> str | None:
    """Resolve a player's coarse position the way the context engine would.

    Prefers EPM's position label, falls back to the stats feed, applies the
    per-player override (e.g. Luka -> G), then coarsens to G/F/C.
    """
    key = normalize_name(name)
    row = stats_lookup.get(key)
    raw = None
    if row is not None:
        raw = row.get("position")
        if raw is not None and isinstance(raw, float) and pd.isna(raw):
            raw = None
    resolved = resolve_position(name, raw if raw is not None else None)
    buckets = _coarse_position(resolved)
    return buckets[0][0] if buckets else None


def _band_position(minutes: float) -> str:
    """Human label for where a bucket-minute total lands in the band."""
    if minutes >= HIGH:
        return f"SATURATED HIGH (>= {HIGH:.0f})  -> -{MAXADJ:.0%}"
    if minutes <= LOW:
        return f"SATURATED LOW  (<= {LOW:.0f})  -> +{MAXADJ:.0%}"
    return f"in-band ({LOW:.0f}-{HIGH:.0f})"


def main() -> None:
    force_utf8_stdout()
    print("Loading data sources (stats, EPM, salaries, DARKO)...")
    epm_df, stats_df, salary_df = _load_core_data()
    _ = fetch_darko_data()  # warm cache / parity with smoke test
    stats_lookup = _stats_lookup(stats_df)

    print()
    print("=" * 78)
    print(
        f"POSITIONAL DIAGNOSTIC   thresholds: LOW={LOW:.0f}  HIGH={HIGH:.0f}  "
        f"max=+/-{MAXADJ:.0%}"
    )
    print("=" * 78)

    all_modifiers: list[float] = []
    all_bucket_sums: list[float] = []
    saturated_high = 0
    saturated_low = 0
    in_band = 0

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
        for (recv_team, recv_info, recv_ids, send_team, incoming_names) in (
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
                    print(f"    {name:<22} bucket=?    (no position label)")
                    continue
                bucket_min = bucket_sums.get(bucket, 0.0)
                mod = calculate_positional_modifier(bucket, roster_trimmed)
                all_modifiers.append(mod)
                all_bucket_sums.append(bucket_min)
                if bucket_min >= HIGH:
                    saturated_high += 1
                elif bucket_min <= LOW:
                    saturated_low += 1
                else:
                    in_band += 1
                print(
                    f"    {name:<22} bucket={bucket}  "
                    f"{recv_info.abbreviation} {bucket}-minutes={bucket_min:6.1f}  "
                    f"mod={mod:+.1%}   {_band_position(bucket_min)}"
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
    print(f"  bucket-minute totals  min/mean/max: "
          f"{min(all_bucket_sums):.1f} / {mean(all_bucket_sums):.1f} / "
          f"{max(all_bucket_sums):.1f}")
    print(f"  modifier              min/mean/max: "
          f"{min(all_modifiers):+.1%} / {mean(all_modifiers):+.1%} / "
          f"{max(all_modifiers):+.1%}")
    print()
    print("  READ: if nearly all incoming players saturate HIGH and bucket-minute")
    print("  totals cluster well above the HIGH threshold, the thresholds are in")
    print("  the wrong units for three-bucket sums (units-mismatch). If totals")
    print("  straddle the band but signal is still flat, the metric itself is too")
    print("  coarse and wants minutes-share or five-bucket resolution.")


if __name__ == "__main__":
    main()
