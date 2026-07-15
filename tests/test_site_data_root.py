"""Tests for the consolidated site_Data root helper (chore/site-data-root).

Seven code sites used to carry their own ``~/site_Data`` default (a dead path
that only worked through a symlink); ``ingest.site_data.site_data_root`` is
now the single source. These lock the precedence (env beats default), the
fail-loud not-found error naming BOTH locations tried, and that the helper
never fires when callers pass explicit paths.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nba_trade_analyzer.data import cap_holds, dead_money, guarantees
from nba_trade_analyzer.ingest import site_data
from nba_trade_analyzer.ingest.site_data import site_data_root


def test_env_var_beats_default(monkeypatch, tmp_path):
    monkeypatch.setenv("SITE_DATA_ROOT", str(tmp_path))
    # Poison the default so accidental fallback would be caught.
    monkeypatch.setattr(
        site_data, "DEFAULT_SITE_DATA_ROOT", str(tmp_path / "does-not-exist")
    )
    assert site_data_root() == tmp_path


def test_default_used_when_env_unset(monkeypatch, tmp_path):
    monkeypatch.delenv("SITE_DATA_ROOT", raising=False)
    monkeypatch.setattr(site_data, "DEFAULT_SITE_DATA_ROOT", str(tmp_path))
    assert site_data_root() == tmp_path


def test_missing_env_dir_raises_naming_both(monkeypatch, tmp_path):
    missing = tmp_path / "nowhere"
    monkeypatch.setenv("SITE_DATA_ROOT", str(missing))
    with pytest.raises(FileNotFoundError) as exc:
        site_data_root()
    message = str(exc.value)
    assert "SITE_DATA_ROOT" in message
    assert str(missing) in message
    assert site_data.DEFAULT_SITE_DATA_ROOT in message


def test_missing_default_raises_and_says_env_unset(monkeypatch, tmp_path):
    monkeypatch.delenv("SITE_DATA_ROOT", raising=False)
    missing = tmp_path / "nowhere"
    monkeypatch.setattr(site_data, "DEFAULT_SITE_DATA_ROOT", str(missing))
    with pytest.raises(FileNotFoundError) as exc:
        site_data_root()
    message = str(exc.value)
    assert "unset" in message
    assert str(missing) in message


def test_explicit_paths_bypass_the_helper(monkeypatch, tmp_path):
    """Loaders given an explicit path must never consult the helper — fixtures
    that pass paths directly are unaffected by a broken root."""
    monkeypatch.delenv("SITE_DATA_ROOT", raising=False)
    monkeypatch.setattr(
        site_data, "DEFAULT_SITE_DATA_ROOT", str(tmp_path / "does-not-exist")
    )

    holds_csv = tmp_path / "nba_cap_holds.csv"
    holds_csv.write_text("Player,Team,2026-27\nSomeone,GSW,20000000\n", encoding="utf-8")
    assert cap_holds.load_cap_holds(holds_csv) == {"GSW": {"2026-27": 20_000_000}}

    dead_csv = tmp_path / "nba_dead_money.csv"
    dead_csv.write_text("Player,Team,2026-27\nSomeone,MIL,666667\n", encoding="utf-8")
    rows = dead_money.load_dead_money(dead_csv)
    assert len(rows) == 1 and rows[0].amounts == {"2026-27": 666_667}

    spread_csv = tmp_path / "salary_spread.csv"
    spread_csv.write_text("Player,Team\n", encoding="utf-8")
    resolver = guarantees.NonGuaranteeResolver.load(spread_path=spread_csv)
    assert resolver is not None


def test_default_paths_route_through_helper(monkeypatch, tmp_path):
    monkeypatch.setenv("SITE_DATA_ROOT", str(tmp_path))
    assert dead_money.default_path() == str(Path(tmp_path) / "nba_dead_money.csv")
