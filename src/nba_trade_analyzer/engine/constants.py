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

# Controls how much of the team's net rating is subtracted from the player's.
# 1.0 = full subtraction (overcorrects for stars on good teams), 0.0 = no
# adjustment (ignores team context). 0.5 is a compromise until EPM integration
# replaces this approach.
TEAM_ADJUSTMENT_WEIGHT = 0.5

# Theoretical maximum wins a single player can add. Used in tanh curve to
# enforce diminishing returns. Historical best seasons (peak LeBron, peak
# Jordan) are roughly 15-18 WAR.
MAX_WINS_ADDED = 20.0

# Conversion factor: EPM (or DARKO DPM) points per 100 possessions to wins,
# scaled by minutes fraction. EPM already isolates individual impact via
# RAPM, so no team adjustment is applied. Calibrated 2026-05-19 against the
# top-30 EPM players: 5.0 compressed the top five to 17-18 tanh_wins (tanh
# was acting as a wall, not an outlier cap); 3.8 spread them but pulled
# mid-tier max contracts too far underwater. 4.2 keeps the top tier below
# the tanh ceiling while leaving room for Phase 5 team context to lift the
# second-tier max guys (Cade, Brunson, LeBron) back toward breakeven.
EPM_TO_WINS_FACTOR = 4.2

# ---- Team Context Constants (Phase 5) ----
# Win curve: sigmoid on projected team wins that rescales DOLLARS_PER_WIN.
# Marginal wins are most valuable near the playoff cutoff (where they swing
# postseason eligibility) and least valuable for teams locked into the lottery
# or a top seed. Multiplies DOLLARS_PER_WIN — applied before the additive
# timeline/positional/spacing adjustments.
WIN_CURVE_MIDPOINT = 42.0  # approximate playoff cutoff (historical avg 41-43 wins)
WIN_CURVE_STEEPNESS = 0.15  # how sharply value ramps around the cutoff
WIN_CURVE_MIN_MULTIPLIER = 0.4  # tanking-team floor (~20-win team)
WIN_CURVE_MAX_MULTIPLIER = 2.0  # contender ceiling

# Timeline alignment: penalize age gaps between an incoming player and the
# acquiring team's core. Exponential decay so a 3-year gap is a soft nudge
# and a 10-year gap is a real penalty. Capped at TIMELINE_MAX_ADJUSTMENT
# percent of context_value.
TIMELINE_LAMBDA = 0.12  # decay rate — gap=3 ≈ 70% alignment, gap=10 ≈ 30%
TIMELINE_MAX_ADJUSTMENT = 0.15  # ±15% of context_value
TIMELINE_CORE_SIZE = 5  # top-N players by minutes that define team core

# Positional fit: bonus when an incoming player addresses a thin position,
# penalty when they pile onto a logjam. Linear scaling between the two
# minute-load thresholds.
POSITIONAL_MAX_ADJUSTMENT = 0.10  # ±10% of context_value
POSITIONAL_MINUTES_THRESHOLD_HIGH = 40.0  # above this = logjam penalty
POSITIONAL_MINUTES_THRESHOLD_LOW = 25.0  # below this = positional need bonus

# Spacing fit: small additive modifier when a shooter joins a spacing-poor
# team (or a non-shooter joins one that needs spacing). Effect shrinks when
# the acquiring team already has elite spacing, since spacing isn't the
# binding constraint there.
SPACING_MAX_ADJUSTMENT = 0.08  # ±8% — weakest signal of the four
LEAGUE_AVG_3PT_PCT = 0.363  # 2024-25 league average 3P%; refresh annually
LEAGUE_AVG_3PT_RATE = 0.40  # 2024-25 league average 3PA/FGA; refresh annually

# ---- Draft Pick Value Constants ----
# Exponential decay parameters for draft pick surplus value.
# Fitted conceptually to EPM-based draft value research (Sports Appeal / Pelton).
# pick_value = PICK_VALUE_SCALE * exp(-PICK_VALUE_DECAY * (pick_number - 1))
PICK_VALUE_SCALE = (
    80_000_000  # ~$80M surplus value for pick 1 (over full rookie contract)
)
PICK_VALUE_DECAY = 0.065  # decay rate — pick 14 ≈ $33M, pick 30 ≈ $12M, pick 45 ≈ $4M
PICK_VALUE_SECOND_ROUND_PENALTY = (
    0.4  # second round picks (31-60) get 40% of curve value
)
