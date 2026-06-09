"""Deterministic CBA constant audit.

Regex-extracts the ``verified: true`` dollar values from the CBA reference YAML
(PyYAML is intentionally NOT a dependency) and asserts the engine constants
match them. This is an AUDIT artifact: a failing assertion here is a finding to
report, not a number to silence.

Only ``verified: true`` reference entries may certify an engine constant.

The three MLE values (non_taxpayer_mle, taxpayer_mle, room_mle) are
``verified: true`` in the reference but the engine exposes NO constant for them.
That absence is itself a finding: there is a primary-sourced value with nothing
to certify against. The tests below assert the reference entries are internally
``verified: true`` and that the engine truly has no such constant, recording the
gap rather than faking a pass.
"""

from __future__ import annotations

import re
from pathlib import Path

from nba_trade_analyzer.engine import constants

REFERENCE_PATH = (
    Path(__file__).resolve().parents[1] / "reference" / "cba_reference_2025-26.yaml"
)
_YAML_TEXT = REFERENCE_PATH.read_text()


def _inline_entry(key: str) -> tuple[int, bool]:
    """Parse an inline ``key: { value: N, verified: bool, ... }`` mapping.

    Used for the cap_levels block whose entries are single-line flow mappings.
    Returns (value, verified).
    """
    pattern = rf"^\s*{re.escape(key)}:\s*\{{(?P<body>[^}}]*)\}}"
    m = re.search(pattern, _YAML_TEXT, re.MULTILINE)
    assert m is not None, f"{key!r} not found as an inline mapping in reference"
    body = m.group("body")
    vm = re.search(r"value:\s*(\d+)", body)
    verm = re.search(r"verified:\s*(true|false)", body)
    assert vm is not None, f"no value: in {key!r} entry"
    assert verm is not None, f"no verified: in {key!r} entry"
    return int(vm.group(1)), verm.group(1) == "true"


def _block_entry(key: str) -> tuple[int, bool]:
    """Parse a block-style entry:

        key:
          value: N
          verified: bool
          ...

    Returns (value, verified) reading the first value:/verified: under the key.
    """
    # Anchor on the key line, then scan the indented block that follows.
    pattern = rf"^(?P<indent>\s*){re.escape(key)}:\s*$\n(?P<block>(?:^\s+.*\n?)+)"
    m = re.search(pattern, _YAML_TEXT, re.MULTILINE)
    assert m is not None, f"{key!r} not found as a block mapping in reference"
    block = m.group("block")
    vm = re.search(r"^\s*value:\s*(\d+)", block, re.MULTILINE)
    verm = re.search(r"^\s*verified:\s*(true|false)", block, re.MULTILINE)
    assert vm is not None, f"no value: in {key!r} block"
    assert verm is not None, f"no verified: in {key!r} block"
    return int(vm.group(1)), verm.group(1) == "true"


# --- Cap levels: verified:true in the reference, must match the engine -------


def test_salary_cap_matches_verified_reference():
    value, verified = _inline_entry("salary_cap")
    assert verified, "reference salary_cap must be verified:true to certify"
    assert constants.SALARY_CAP == value, (
        f"engine SALARY_CAP={constants.SALARY_CAP:,} != reference {value:,}"
    )


def test_luxury_tax_matches_verified_reference():
    value, verified = _inline_entry("luxury_tax_line")
    assert verified, "reference luxury_tax_line must be verified:true to certify"
    assert constants.LUXURY_TAX == value, (
        f"engine LUXURY_TAX={constants.LUXURY_TAX:,} != reference {value:,}"
    )


def test_first_apron_matches_verified_reference():
    value, verified = _inline_entry("first_apron")
    assert verified, "reference first_apron must be verified:true to certify"
    assert constants.FIRST_APRON == value, (
        f"engine FIRST_APRON={constants.FIRST_APRON:,} != reference {value:,}"
    )


def test_second_apron_matches_verified_reference():
    value, verified = _inline_entry("second_apron")
    assert verified, "reference second_apron must be verified:true to certify"
    assert constants.SECOND_APRON == value, (
        f"engine SECOND_APRON={constants.SECOND_APRON:,} != reference {value:,}"
    )


# --- MLEs: verified:true in the reference, but NO engine constant exists ------
# Each test confirms (a) the reference entry is verified:true and (b) the engine
# genuinely exposes no constant to certify. The absence is the finding.


def _engine_has_constant_for(*candidate_names: str) -> bool:
    return any(hasattr(constants, name) for name in candidate_names)


def test_non_taxpayer_mle_is_verified_but_engine_has_no_constant():
    value, verified = _block_entry("non_taxpayer_mle")
    assert verified, "reference non_taxpayer_mle must be verified:true"
    assert value == 14104000  # pin the reference value being recorded
    assert not _engine_has_constant_for(
        "NON_TAXPAYER_MLE", "NON_TAX_MLE", "MLE", "NTMLE", "NON_TAXPAYER_MLE_VALUE"
    ), "engine unexpectedly exposes a non-taxpayer MLE constant — update audit"


def test_taxpayer_mle_is_verified_but_engine_has_no_constant():
    value, verified = _block_entry("taxpayer_mle")
    assert verified, "reference taxpayer_mle must be verified:true"
    assert value == 5685000
    assert not _engine_has_constant_for(
        "TAXPAYER_MLE", "TAX_MLE", "TPMLE", "TAXPAYER_MLE_VALUE"
    ), "engine unexpectedly exposes a taxpayer MLE constant — update audit"


def test_room_mle_is_verified_but_engine_has_no_constant():
    value, verified = _block_entry("room_mle")
    assert verified, "reference room_mle must be verified:true"
    assert value == 8781000
    assert not _engine_has_constant_for(
        "ROOM_MLE", "ROOM_EXCEPTION", "ROOM_MLE_VALUE"
    ), "engine unexpectedly exposes a room MLE constant — update audit"
