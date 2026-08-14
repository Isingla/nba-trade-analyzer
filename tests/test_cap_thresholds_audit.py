"""Certification audit for the per-season cap thresholds (Cap Sheet, Stage 1).

AUDIT ARTIFACT (verification-only — never edits engine/constants/reference).

``CAP_THRESHOLDS_BY_SEASON`` carries two kinds of seasons and this audit holds
each to its own standard:

  * CERTIFIED seasons (2025-26, 2026-27) must match the ``verified: true``
    figures in the CBA reference YAML exactly — 2025-26 against the original
    ``cap_levels`` block (via the engine scalars the constant audit already
    certifies), 2026-27 against the new ``cap_levels_2026_27`` block.
  * PROJECTED seasons (2027-28 onward) must be tagged ``certified: False`` and
    must equal the certified 2026-27 levels scaled by the documented growth
    rate — i.e. thresholds scale with the projected cap, never by a separate
    per-line formula. Projections must NEVER appear in the reference YAML.

Follows the existing audit convention: the YAML is regex-parsed (PyYAML is
intentionally not a dependency).
"""

from __future__ import annotations

import re
from pathlib import Path

from nba_trade_analyzer.engine import constants
from nba_trade_analyzer.engine.constants import (
    CAP_THRESHOLD_PROJECTED_GROWTH,
    CAP_THRESHOLDS_BY_SEASON,
)

REFERENCE_PATH = (
    Path(__file__).resolve().parents[1] / "reference" / "cba_reference_2025-26.yaml"
)
_YAML_TEXT = REFERENCE_PATH.read_text()

CERTIFIED_SEASONS = ("2025-26", "2026-27")
PROJECTED_SEASONS = ("2027-28", "2028-29", "2029-30")
LEVEL_KEYS = (
    "salary_cap",
    "minimum_team_salary",
    "luxury_tax",
    "first_apron",
    "second_apron",
)

# Official June-2026 NBA announcement figures (the certification target).
OFFICIAL_2026_27 = {
    "salary_cap": 164_961_000,
    "minimum_team_salary": 148_465_000,
    "luxury_tax": 200_428_000,
    "first_apron": 209_015_000,
    "second_apron": 221_686_000,
}

# constants-table key -> YAML key inside cap_levels_2026_27.
_YAML_KEY = {
    "salary_cap": "salary_cap",
    "minimum_team_salary": "minimum_team_salary",
    "luxury_tax": "luxury_tax_line",
    "first_apron": "first_apron",
    "second_apron": "second_apron",
}


def _2026_27_block() -> str:
    """The cap_levels_2026_27 block text, so key lookups can't leak into the
    2025-26 ``cap_levels`` block (several keys are shared)."""
    m = re.search(
        r"^cap_levels_2026_27:\s*$\n(?P<block>(?:^\s+.*\n?)+)", _YAML_TEXT, re.MULTILINE
    )
    assert m is not None, "cap_levels_2026_27 block not found in reference"
    return m.group("block")


def _entry_2026_27(key: str) -> tuple[int, bool]:
    """Parse ``key: { value: N, verified: bool, ... }`` (brace may follow a
    line break, matching the split-line floor entry style)."""
    block = _2026_27_block()
    m = re.search(
        rf"^\s*{re.escape(key)}:\s*\{{(?P<body>[^}}]*)\}}", block, re.MULTILINE
    )
    assert m is not None, f"{key!r} not found in cap_levels_2026_27"
    body = m.group("body")
    vm = re.search(r"value:\s*(\d+)", body)
    verm = re.search(r"verified:\s*(true|false)", body)
    assert vm is not None and verm is not None, f"malformed {key!r} entry"
    return int(vm.group(1)), verm.group(1) == "true"


# ---- certified seasons ------------------------------------------------------


def test_2026_27_yaml_block_is_fully_verified_and_matches_official_figures():
    for const_key, yaml_key in _YAML_KEY.items():
        value, verified = _entry_2026_27(yaml_key)
        assert verified, f"cap_levels_2026_27.{yaml_key} must be verified:true"
        assert value == OFFICIAL_2026_27[const_key], (
            f"cap_levels_2026_27.{yaml_key} = {value:,} does not match the "
            f"official June-2026 figure {OFFICIAL_2026_27[const_key]:,}"
        )


def test_2026_27_constants_match_yaml_and_are_certified():
    season = CAP_THRESHOLDS_BY_SEASON["2026-27"]
    assert season["certified"] is True
    for const_key, yaml_key in _YAML_KEY.items():
        value, verified = _entry_2026_27(yaml_key)
        assert verified
        assert season[const_key] == value, (
            f"CAP_THRESHOLDS_BY_SEASON['2026-27'][{const_key!r}] = "
            f"{season[const_key]:,} != reference {value:,}"
        )


def test_2025_26_row_matches_the_audited_engine_scalars():
    # The scalar constants are themselves certified against the YAML by
    # test_cba_constant_audit; the table row must agree with them exactly.
    season = CAP_THRESHOLDS_BY_SEASON["2025-26"]
    assert season["certified"] is True
    assert season["salary_cap"] == constants.SALARY_CAP
    assert season["minimum_team_salary"] == constants.MINIMUM_TEAM_SALARY
    assert season["luxury_tax"] == constants.LUXURY_TAX
    assert season["first_apron"] == constants.FIRST_APRON
    assert season["second_apron"] == constants.SECOND_APRON


# ---- projected seasons ------------------------------------------------------


def test_projected_seasons_are_tagged_projected_and_follow_cap_growth():
    base = CAP_THRESHOLDS_BY_SEASON["2026-27"]
    for n, season_key in enumerate(PROJECTED_SEASONS, start=1):
        season = CAP_THRESHOLDS_BY_SEASON[season_key]
        assert season["certified"] is False, f"{season_key} must be projected"
        factor = (1 + CAP_THRESHOLD_PROJECTED_GROWTH) ** n
        for key in LEVEL_KEYS:
            expected = int(round(base[key] * factor))
            assert season[key] == expected, (
                f"{season_key}.{key} = {season[key]:,} != certified 2026-27 "
                f"level x {factor:.6f} = {expected:,}"
            )


def test_projected_thresholds_keep_cba_ratios_to_the_cap():
    # Deriving every line from the same cap factor must preserve each line's
    # ratio to the cap (within integer rounding) — the CBA-consistency claim.
    base = CAP_THRESHOLDS_BY_SEASON["2026-27"]
    for season_key in PROJECTED_SEASONS:
        season = CAP_THRESHOLDS_BY_SEASON[season_key]
        for key in LEVEL_KEYS:
            base_ratio = base[key] / base["salary_cap"]
            season_ratio = season[key] / season["salary_cap"]
            assert abs(season_ratio - base_ratio) < 1e-6, (
                f"{season_key}.{key} ratio drifted from the certified "
                f"2026-27 ratio ({season_ratio} vs {base_ratio})"
            )


def test_projections_never_enter_the_reference_yaml():
    # The reference carries certified figures only. No projected out-year
    # dollar level may appear anywhere in it.
    for season_key in PROJECTED_SEASONS:
        season = CAP_THRESHOLDS_BY_SEASON[season_key]
        for key in LEVEL_KEYS:
            assert str(season[key]) not in _YAML_TEXT, (
                f"projected value {season[key]} ({season_key}.{key}) found in "
                "the reference YAML — projections must stay out of it"
            )


def test_projected_growth_is_8_percent_ruled_2026_08_14():
    # Past-5-season certified cap growth avg (NBA PR), ruled 2026-08-14 —
    # supersedes the ~5.5%/yr league-guidance figure. Hand-worked pin:
    # 164,961,000 x 1.08 = 178,157,880.
    assert CAP_THRESHOLD_PROJECTED_GROWTH == 0.08
    assert CAP_THRESHOLDS_BY_SEASON["2027-28"]["salary_cap"] == 178_157_880
