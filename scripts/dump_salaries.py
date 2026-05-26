"""One-time salary snapshot dumper.

Fetches every active NBA contract from Basketball Reference and writes a CSV
snapshot next to ``data/salaries.py``. That committed CSV is the offline
fallback ``fetch_all_salaries`` uses when the live fetch fails, so anyone who
clones the repo gets working salary data without a network round-trip.

Run from repo root (re-run whenever you want to refresh the snapshot):
    uv run --native-tls python scripts/dump_salaries.py
"""

from __future__ import annotations

import httpx

from nba_trade_analyzer.data.salaries import (
    _DEFAULT_SEASON,
    _HEADERS,
    _HTTP_TIMEOUT,
    _CONTRACTS_URL,
    _csv_path,
    _parse_salary_html,
)


def main(season: str = _DEFAULT_SEASON) -> None:
    print(f"Fetching contracts from {_CONTRACTS_URL} ...")
    resp = httpx.get(
        _CONTRACTS_URL,
        headers=_HEADERS,
        follow_redirects=True,
        timeout=_HTTP_TIMEOUT,
    )
    resp.raise_for_status()

    df = _parse_salary_html(resp.text, season=season)
    out_path = _csv_path(season)
    df.to_csv(out_path, index=False)

    print(f"  parsed {len(df)} contracts")
    print(f"  wrote {out_path}")
    print(
        f"  player options: {int(df['has_player_option'].sum())}, "
        f"team options: {int(df['has_team_option'].sum())}, "
        f"rookie scale: {int(df['is_rookie_scale'].sum())}"
    )


if __name__ == "__main__":
    main()
