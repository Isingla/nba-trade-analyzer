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


def test_default_path_still_falls_back_to_committed_csv(monkeypatch, capsys):
    # The legacy export path is UNCHANGED: fetch failure -> committed CSV + warning.
    monkeypatch.setattr(salaries_mod.httpx, "get", _boom)
    df = fetch_all_salaries(cache=_NoCache())
    assert len(df) > 0
    assert "using local CSV fallback" in capsys.readouterr().out
