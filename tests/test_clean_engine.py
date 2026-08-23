"""Tests for the clean valuation engine (docs/SPEC-clean-engine.md).

Every expected value here is a LITERAL from the spec's acceptance table or the
ruled constants — never recomputed through the function under test. The old
engine (tanh / damper / full-season pin) is deliberately absent: the clean
chain is WAR = (EPM − R) × (minutes × pace/48) / 100 ÷ pointsPerWin.
"""

from __future__ import annotations

import pytest

from nba_trade_analyzer.engine.clean_engine import (
    CLEAN_CONSTANTS,
    calculate_surplus,
    calculate_value,
    calculate_war,
    derive_dollars_per_win,
    pace_for_season,
    scale_dollars_per_win,
)


# ----- ruled constants -----------------------------------------------------


def test_ruled_constants():
    assert CLEAN_CONSTANTS.replacement_level == -2.1
    assert CLEAN_CONSTANTS.points_per_win == 36.7
    assert CLEAN_CONSTANTS.pace_by_season["2025-26"] == 99.4
    assert CLEAN_CONSTANTS.dollars_per_win_2025_26 == 7_860_000


# ----- acceptance pins (spec §4, values are spec literals) -----------------


def test_kuzma_pin_actual_2025_26():
    # Spec gate 1: Kuzma actual 25-26 — EPM −1.28, 1,806 min, pace 99.4,
    # salary $20,194,392 → 0.84 WAR / $6.6M value @ $7.86M/win / −$13.6M.
    war = calculate_war(epm=-1.28, minutes=1806, pace=99.4)
    assert war == pytest.approx(0.84, abs=0.1)
    value = calculate_value(war, dollars_per_win=7_860_000)
    assert value == pytest.approx(6.6e6, abs=0.5e6)
    assert calculate_surplus(value, salary=20_194_392) == pytest.approx(
        -13.6e6, abs=0.5e6
    )


def test_wemby_pin_actual_2025_26_priced_at_2026_27_rate():
    # Spec gate 2: Wemby actual — EPM 8.74, 1,866 min → 11.41 WAR; value
    # $95.7M @ the 26-27 cap-scaled rate $8.38M/win; salary $16,868,246
    # (DB-verified) → surplus +$78.8M.
    war = calculate_war(epm=8.74, minutes=1866, pace=99.4)
    assert war == pytest.approx(11.41, abs=0.1)
    rate_26_27 = scale_dollars_per_win(7_860_000, 154_647_000, 164_961_000)
    value = calculate_value(war, dollars_per_win=rate_26_27)
    assert value == pytest.approx(95.7e6, abs=0.5e6)
    assert calculate_surplus(value, salary=16_868_246) == pytest.approx(
        78.8e6, abs=0.5e6
    )


def test_plus_seven_pin():
    # Spec gate 3: +7 EPM @ 2,800 min → 14.38 WAR / value $120.5M @ the
    # 26-27 cap-scaled rate ($8.38M/win). Both expectations are spec §4
    # literals.
    war = calculate_war(epm=7.0, minutes=2800, pace=99.4)
    assert war == pytest.approx(14.38, abs=0.1)
    rate_26_27 = scale_dollars_per_win(7_860_000, 154_647_000, 164_961_000)
    assert calculate_value(war, dollars_per_win=rate_26_27) == pytest.approx(
        120.5e6, abs=0.5e6
    )


# ----- dollars-per-win derivation and scaling ------------------------------


def test_derive_dollars_per_win_reproduces_ruled_rate():
    # Spec §2: matched payroll $5.325B ÷ 677 matched WAR = $7.86M/win.
    assert derive_dollars_per_win(5.325e9, 677) == pytest.approx(
        7.86e6, abs=0.1e6
    )


def test_scale_dollars_per_win_cap_ratio():
    # 25-26 → 26-27 certified caps: 7.86M × 164,961/154,647 = 8.38M.
    assert scale_dollars_per_win(
        7_860_000, 154_647_000, 164_961_000
    ) == pytest.approx(8.38e6, abs=0.01e6)


# ----- pace rule -----------------------------------------------------------


def test_unmeasured_season_uses_latest_measured_pace():
    # '2026-27' has no measured pace; the rule is LATEST measured (99.4),
    # never the 100 approximation.
    assert pace_for_season("2026-27") == 99.4


def test_measured_season_uses_its_own_pace():
    assert pace_for_season("2025-26") == 99.4


# ----- identities and edge behavior ----------------------------------------


def test_replacement_identity_exact_zero_at_any_minutes():
    # EPM exactly at R → (EPM − R) = 0 → WAR exactly 0, regardless of minutes.
    # Exact == 0.0 is INTENTIONAL (not float-tolerance abuse): epm == R makes
    # the first factor a literal 0.0, and 0.0 × anything is exactly 0.0.
    for minutes in (0, 500, 1806, 2800, 4000):
        assert calculate_war(epm=-2.1, minutes=minutes, pace=99.4) == 0.0


def test_zero_minutes_zero_war_no_error():
    assert calculate_war(epm=5.0, minutes=0, pace=99.4) == 0.0


# ----- extreme-input policy (spec §5): loud warning, output untouched ------


def test_extreme_war_warns_loudly_and_output_unmodified():
    # Synthetic: EPM 10.0 over 3,000 min at pace 99.4 → hand-worked
    # (10 + 2.1) × (3000 × 99.4/48)/100 / 36.7 = 12.1 × 62.125 / 36.7 = 20.48.
    # Above the 20-WAR flag line: a warning must fire naming the player, and
    # the output must be the unclamped 20.48 — no tanh, no cap.
    with pytest.warns(UserWarning, match="Synthetic Star"):
        war = calculate_war(
            epm=10.0, minutes=3000, pace=99.4, player_name="Synthetic Star"
        )
    assert war == pytest.approx(20.48, abs=0.05)


def test_no_warning_inside_normal_range(recwarn):
    calculate_war(epm=7.0, minutes=2800, pace=99.4, player_name="Normal Star")
    assert len(recwarn) == 0
