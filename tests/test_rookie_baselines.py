"""Tier-baseline pure logic + season-EPM API field mapping (rookie auto-fill)."""

from __future__ import annotations

import os
import sys

from nba_trade_analyzer.data.epm import _parse_api_payload

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import compute_rookie_baselines as rb  # noqa: E402


def test_tier_of_boundaries():
    assert rb.tier_of(1) == "top3"
    assert rb.tier_of(3) == "top3"
    assert rb.tier_of(4) == "lottery"
    assert rb.tier_of(14) == "lottery"
    assert rb.tier_of(15) == "mid_late_1st"
    assert rb.tier_of(30) == "mid_late_1st"
    assert rb.tier_of(31) == "early_2nd"
    assert rb.tier_of(45) == "early_2nd"
    assert rb.tier_of(46) == "late_2nd"
    assert rb.tier_of(60) == "late_2nd"
    assert rb.tier_of(61) is None
    assert rb.tier_of(0) is None


def test_war_from_washout_contributes_zero():
    # A washout window-year is (impact, minutes) = (0, 0) -> zero WAR, which is
    # exactly the expected-value pull the baseline relies on.
    assert rb.war_from(0.0, 0.0) == 0.0
    # Any impact with zero minutes is also zero (no playing time, no WAR).
    assert rb.war_from(5.0, 0.0) == 0.0
    # At replacement level (-1.0) WAR is zero regardless of minutes.
    assert rb.war_from(-1.0, 2000.0) == 0.0


def test_war_from_monotonic_in_impact_and_minutes():
    assert rb.war_from(2.0, 1500.0) > rb.war_from(0.0, 1500.0)
    assert rb.war_from(2.0, 2000.0) > rb.war_from(2.0, 1000.0)
    assert rb.war_from(-3.0, 1500.0) < 0.0  # below replacement -> negative WAR


def test_parse_api_payload_pins_the_real_schema():
    rows = _parse_api_payload(
        [
            {
                "player_name": "Brandon Ingram",
                "player_id": 1627742,
                "tot": -3.86,
                "off": -2.19,
                "def": -1.67,
                "mpg": 28.85,
                "gp": 79,
                "mp": 2279,
                "pos_text": "F",
                "team_alias": "LAL",
                "age": 19,
            }
        ]
    )
    assert len(rows) == 1
    r = rows[0]
    assert r["epm"] == -3.86  # tot -> epm
    assert r["epm_off"] == -2.19 and r["epm_def"] == -1.67
    assert r["mpg"] == 28.85 and r["gp"] == 79.0
    assert r["player_id"] == 1627742  # joins to DraftHistory PERSON_ID
    assert r["position"] == "F"  # pos_text -> position
    assert r["player_name"] == "Brandon Ingram"


def test_parse_api_payload_drops_rows_without_name_or_epm():
    assert _parse_api_payload([{"player_name": "X"}]) == []  # no tot
    assert _parse_api_payload([{"tot": 1.0}]) == []  # no name
