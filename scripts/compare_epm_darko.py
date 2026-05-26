"""EPM vs DARKO DPM scale comparison.

Phase 5.5 uses DARKO DPM as the year-2 forward projection when year 1
came from EPM. That assumes the two metrics live on the same scale.
This script prints the top-N EPM players side by side with their DARKO
DPM, plus aggregate stats over the joined set, so we can eyeball whether
there's a systematic offset or compression that would distort the
multi-year projection.

It also dumps the raw DARKO column headers so we can confirm we're
reading the right column (the public sheet has multiple DPM flavors —
current, projected, etc.).

Run from repo root:
    uv run --native-tls python scripts/compare_epm_darko.py
"""

from __future__ import annotations

import io
import sys
from io import StringIO

import httpx
import pandas as pd

from nba_trade_analyzer.data.darko import (
    _DARKO_URL,
    fetch_darko_data,
    get_player_darko,
    normalize_name,
)
from nba_trade_analyzer.data.epm import fetch_epm_data


def _force_utf8_stdout() -> None:
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", line_buffering=True
        )


def _fetch_raw_darko_columns() -> list[str]:
    """Pull the raw CSV headers from the DARKO sheet, bypassing our shaped df."""
    resp = httpx.get(
        _DARKO_URL,
        headers={"User-Agent": "Mozilla/5.0"},
        follow_redirects=True,
        timeout=30.0,
    )
    resp.raise_for_status()
    raw = pd.read_csv(StringIO(resp.text))
    return list(raw.columns)


def main() -> None:
    _force_utf8_stdout()

    print("Raw DARKO sheet column headers:")
    try:
        cols = _fetch_raw_darko_columns()
        for i, c in enumerate(cols):
            print(f"  [{i:>2}] {c}")
    except Exception as exc:
        print(f"  (failed to fetch raw CSV: {exc})")
    print()

    print("Fetching shaped EPM + DARKO data...")
    epm = fetch_epm_data()
    darko = fetch_darko_data()
    print(f"  {len(epm)} EPM rows, {len(darko)} DARKO rows")
    print()

    # ----- top-20 EPM with DARKO DPM side by side --------------------------
    top20 = epm.sort_values("epm", ascending=False).head(20)
    print(
        f"{'rank':>4}  {'player':<28}  {'team':<4}  {'age':>3}  "
        f"{'EPM':>7}  {'DPM':>7}  {'Δ(DPM-EPM)':>11}"
    )
    print("-" * 80)

    deltas: list[float] = []
    missing: list[str] = []
    for i, (_, row) in enumerate(top20.iterrows(), 1):
        name = row["player_name"]
        epm_val = float(row["epm"])
        darko_row = get_player_darko(darko, name)
        if darko_row is None:
            print(
                f"{i:>4}  {name:<28}  {row['team']:<4}  {int(row['age']):>3}  "
                f"{epm_val:+7.2f}  {'—':>7}  {'—':>11}"
            )
            missing.append(name)
            continue
        dpm_val = float(darko_row["dpm"])
        delta = dpm_val - epm_val
        deltas.append(delta)
        print(
            f"{i:>4}  {name:<28}  {row['team']:<4}  {int(row['age']):>3}  "
            f"{epm_val:+7.2f}  {dpm_val:+7.2f}  {delta:+11.2f}"
        )

    print()
    if missing:
        print(f"  ({len(missing)} not found in DARKO: {missing})")
        print()

    # ----- aggregate stats over the full overlap ---------------------------
    epm_indexed = epm.set_index("player_name_normalized")
    darko_indexed = darko.set_index("player_name_normalized")
    joined = epm_indexed.join(
        darko_indexed[["dpm", "age"]],
        how="inner",
        rsuffix="_darko",
    )
    print(f"Joined set: {len(joined)} players present in BOTH EPM and DARKO.")

    epm_series = joined["epm"].astype(float)
    dpm_series = joined["dpm"].astype(float)
    diff = dpm_series - epm_series

    print()
    print(f"{'metric':<25}  {'EPM':>10}  {'DPM':>10}  {'DPM-EPM':>10}")
    print("-" * 65)
    print(
        f"{'mean':<25}  {epm_series.mean():+10.3f}  "
        f"{dpm_series.mean():+10.3f}  {diff.mean():+10.3f}"
    )
    print(
        f"{'median':<25}  {epm_series.median():+10.3f}  "
        f"{dpm_series.median():+10.3f}  {diff.median():+10.3f}"
    )
    print(
        f"{'stdev':<25}  {epm_series.std():10.3f}  "
        f"{dpm_series.std():10.3f}  {diff.std():10.3f}"
    )
    print(
        f"{'min':<25}  {epm_series.min():+10.3f}  "
        f"{dpm_series.min():+10.3f}  {diff.min():+10.3f}"
    )
    print(
        f"{'max':<25}  {epm_series.max():+10.3f}  "
        f"{dpm_series.max():+10.3f}  {diff.max():+10.3f}"
    )

    # Correlation
    corr = epm_series.corr(dpm_series)
    print(f"\nPearson correlation EPM vs DPM: {corr:.4f}")

    # Linear fit DPM = a*EPM + b — if scales match, slope≈1, intercept≈0.
    import numpy as np

    slope, intercept = np.polyfit(epm_series.values, dpm_series.values, 1)
    print(f"Linear fit DPM = {slope:.3f} * EPM + {intercept:+.3f}")
    if abs(slope - 1.0) > 0.10:
        print(
            f"  ⚠ Slope deviates from 1.0 by {abs(slope - 1.0):.3f} — "
            "DARKO appears to be on a compressed/expanded scale vs EPM."
        )
    if abs(intercept) > 0.15:
        print(
            f"  ⚠ Intercept is {intercept:+.3f} — DARKO is systematically "
            f"{'higher' if intercept > 0 else 'lower'} than EPM at the league mean."
        )

    # ----- top-EPM vs top-DPM tail behavior --------------------------------
    print()
    print("Top-5 by each metric (do the elites match?):")
    top5_epm = epm.sort_values("epm", ascending=False).head(5)["player_name"].tolist()
    top5_dpm = darko.sort_values("dpm", ascending=False).head(5)["player_name"].tolist()
    print(f"  top 5 EPM: {top5_epm}")
    print(f"  top 5 DPM: {top5_dpm}")
    overlap = set(map(normalize_name, top5_epm)) & set(map(normalize_name, top5_dpm))
    print(f"  overlap:   {len(overlap)} / 5")

    # ----- Cade-specific drill-down ----------------------------------------
    print()
    print("Cade Cunningham drill-down (Phase 5.5 canonical example):")
    cade_epm = epm[epm["player_name"] == "Cade Cunningham"]
    cade_dpm = darko[darko["player_name"] == "Cade Cunningham"]
    if not cade_epm.empty and not cade_dpm.empty:
        c_epm = float(cade_epm.iloc[0]["epm"])
        c_dpm = float(cade_dpm.iloc[0]["dpm"])
        print(f"  EPM (dunksandthrees, current):    {c_epm:+.3f}")
        print(f"  DPM (DARKO, sheet 'DPM' col):     {c_dpm:+.3f}")
        print(f"  Delta:                            {c_dpm - c_epm:+.3f}")
        # Note: DARKO's public sheet exposes "Current DPM"; the projection
        # column ("Projected DPM" / next-season forecast) is a separate field
        # and not in our column map. If it exists, our Phase 5.5 year-2 path
        # is reading the wrong column.


if __name__ == "__main__":
    main()
