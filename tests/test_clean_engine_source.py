"""EPM source rule (spec §6): the clean engine reads ONLY the Premium API
cache (actuals, with gp/mp). The scrape caches serve Expected (modeled)
values and must be impossible to load silently."""

from __future__ import annotations

import json

import pytest

from nba_trade_analyzer.engine.clean_engine import load_epm_api_cache


def _write_cache(tmp_path, name, rows):
    (tmp_path / name).write_text(json.dumps({"expires_at": 9e12, "value": rows}))


def test_loads_api_cache_with_gp_mp(tmp_path):
    _write_cache(
        tmp_path,
        "epm_dunksandthrees_api_2026.json",
        [
            {
                "player_id": 1,
                "player_name": "A Player",
                "epm": 1.0,
                "gp": 50,
                "mp": 1500.0,
                "mpg": 30.0,
            }
        ],
    )
    rows = load_epm_api_cache(season=2026, cache_dir=tmp_path)
    assert rows[0]["player_name"] == "A Player"


def test_scrape_shaped_records_raise_naming_the_file(tmp_path):
    # A scrape cache lacks gp/mp — if such records ever arrive at the loader
    # (wrong file contents), it must raise immediately, naming the file, and
    # never fall back.
    _write_cache(
        tmp_path,
        "epm_dunksandthrees_api_2026.json",
        [{"player_id": 1, "player_name": "Expected Ghost", "epm": 1.0}],
    )
    with pytest.raises(ValueError, match="epm_dunksandthrees_api_2026.json"):
        load_epm_api_cache(season=2026, cache_dir=tmp_path)


def test_missing_file_raises_not_falls_back(tmp_path):
    # The demoted scrape cache sitting beside a MISSING api file must not be
    # picked up: the loader knows only the _api_ name and raises.
    _write_cache(
        tmp_path,
        "epm_dunksandthrees.json",
        [{"player_id": 1, "player_name": "Scrape Row", "epm": 1.0}],
    )
    with pytest.raises(FileNotFoundError, match="_api_"):
        load_epm_api_cache(season=2026, cache_dir=tmp_path)
