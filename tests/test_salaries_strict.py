"""fetch_all_salaries strict mode (Phase 2A): no silent CSV fallback for ingest."""

from __future__ import annotations

import httpx
import pytest

from nba_trade_analyzer.data import salaries as salaries_mod
from nba_trade_analyzer.data.salaries import fetch_all_salaries


class _NoCache:
    """Cache stub: always miss, never store."""

    def get(self, key):
        return None

    def set(self, key, value, ttl_hours):
        pass


def _boom(*args, **kwargs):
    raise httpx.ConnectError("network down")


def test_strict_reraises_instead_of_falling_back(monkeypatch):
    monkeypatch.setattr(salaries_mod.httpx, "get", _boom)
    with pytest.raises(httpx.HTTPError):
        fetch_all_salaries(cache=_NoCache(), strict=True)


def test_default_path_falls_back_LOUDLY_with_staleness_marker(monkeypatch, capsys):
    # The export path still degrades to the committed CSV (never brick
    # sync:cap-data) — but LOUDLY: multi-line stderr banner + df.attrs marker.
    monkeypatch.setattr(salaries_mod.httpx, "get", _boom)
    df = fetch_all_salaries(cache=_NoCache())
    assert len(df) > 0
    err = capsys.readouterr().err
    assert "BBREF FETCH FAILED" in err
    assert "COMMITTED CSV FALLBACK" in err
    assert "DATA MAY BE STALE" in err
    assert "dated" in err  # the CSV mtime is named
    marker = df.attrs["bbref_fallback"]
    assert marker["reason"] == "network down"
    assert marker["csv_mtime"] != "unknown"  # committed CSV exists -> real date


def test_export_payload_carries_source_note_on_fallback():
    # The df.attrs marker becomes the payload's additive `sourceNote` field —
    # a degraded export is self-describing; a live one carries None.
    import pandas as pd

    from nba_trade_analyzer.data.crosswalk import Crosswalk, CrosswalkEntry
    from nba_trade_analyzer.export import build_export

    cols = [
        "player_name", "bbref_slug", "team", "salary", "years_remaining",
        "is_rookie_scale", "has_player_option", "has_team_option", "yearly_salaries",
    ]
    row = {
        "player_name": "Stephen Curry", "bbref_slug": "curryst01", "team": "GSW",
        "salary": 59606817, "years_remaining": 2, "is_rookie_scale": False,
        "has_player_option": False, "has_team_option": False,
        "yearly_salaries": "59606817",
    }
    empty = {
        "epm_df": pd.DataFrame(columns=["player_name", "player_name_normalized", "team", "epm"]),
        "darko_df": pd.DataFrame(columns=["player_name", "player_name_normalized", "dpm"]),
        "stats_df": pd.DataFrame(
            columns=["nba_player_id", "player_name", "team", "age", "GP", "MPG", "NET_RATING"]
        ),
    }
    cw = Crosswalk(
        [CrosswalkEntry(nba_id=201939, nba_name="Stephen Curry", bbref_slug="curryst01", bbref_name="Stephen Curry")]
    )

    degraded = pd.DataFrame([row], columns=cols)
    degraded.attrs["bbref_fallback"] = {"csv_mtime": "2026-06-24", "reason": "boom"}
    export = build_export(salary_df=degraded, crosswalk=cw, cap_holds={}, **empty)
    dumped = export.model_dump(by_alias=True)
    assert "sourceNote" in dumped
    assert "2026-06-24" in dumped["sourceNote"]
    assert "STALE" in dumped["sourceNote"]

    live = pd.DataFrame([row], columns=cols)
    assert build_export(salary_df=live, crosswalk=cw, cap_holds={}, **empty).source_note is None


def test_live_path_carries_no_fallback_marker(monkeypatch):
    # A successful (cache-served) fetch must NOT be marked degraded.
    class _WarmCache(_NoCache):
        def get(self, key):
            from nba_trade_analyzer.data.salaries import EXPECTED_COLUMNS

            return [dict.fromkeys(EXPECTED_COLUMNS, "x") | {
                "player_name": "A", "bbref_slug": "a01", "team": "BOS",
                "salary": 1, "years_remaining": 1, "is_rookie_scale": False,
                "has_player_option": False, "has_team_option": False,
                "yearly_salaries": "1",
            }]

    df = fetch_all_salaries(cache=_WarmCache())
    assert "bbref_fallback" not in df.attrs
