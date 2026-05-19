"""NBA salary-cap and apron figures.

Values are for the 2025-26 league year. These update annually with the cap;
refresh whenever the league publishes the new year's numbers.
"""

SALARY_CAP = 154_647_000
LUXURY_TAX = 187_895_000
FIRST_APRON = 195_945_000
SECOND_APRON = 207_824_000
EXPANDED_TPE_CUSHION = 8_527_000

# ---- Valuation Constants (tunable) ----
# Dollars per win above replacement, 2025-26 estimate.
# Derived from: total league salary (~$4.64B) / total league WAR (~1,300).
# This is a starting point — will be calibrated via backtesting.
DOLLARS_PER_WIN = 3_500_000

# Replacement level NET_RATING (points per 100 possessions).
# A replacement-level player is roughly -2.0 net rating relative to team.
# This defines the "zero" for wins above replacement.
REPLACEMENT_LEVEL_NET_RATING = -2.0

# Conversion factor: net rating points per 100 possessions to wins.
# Roughly, +1.0 adjusted net rating over a full season ≈ 2.5-3.0 wins.
# Source: historical correlation between team net rating and win totals.
NET_RATING_TO_WINS_FACTOR = 2.75

# Minutes baseline for a "full season" of production.
# Used to scale part-time players proportionally.
FULL_SEASON_MINUTES = 82 * 36  # 82 games × 36 minutes = 2,952

# ---- Draft Pick Value Constants ----
# Exponential decay parameters for draft pick surplus value.
# Fitted conceptually to EPM-based draft value research (Sports Appeal / Pelton).
# pick_value = PICK_VALUE_SCALE * exp(-PICK_VALUE_DECAY * (pick_number - 1))
PICK_VALUE_SCALE = 80_000_000    # ~$80M surplus value for pick 1 (over full rookie contract)
PICK_VALUE_DECAY = 0.065         # decay rate — pick 14 ≈ $33M, pick 30 ≈ $12M, pick 45 ≈ $4M
PICK_VALUE_SECOND_ROUND_PENALTY = 0.4  # second round picks (31-60) get 40% of curve value
