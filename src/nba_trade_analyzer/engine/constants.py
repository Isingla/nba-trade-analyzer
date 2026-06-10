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

# EPM replacement level: the production available for a minimum salary.
# EPM 0.0 = league average, NOT replacement level — RAPM centers the metric
# on the average rotation player, who is a real, rosterable contributor, not
# a freely-available scrap-heap body. Replacement level is roughly -1.0 to
# -1.5 EPM (the floor you can sign off waivers for the minimum). Pricing a
# player against 0.0 made the median $15M starter look like a $13M overpay;
# pricing against replacement level fixes that without touching relative
# ordering. Player value on the EPM/DARKO path is therefore:
#   (EPM - EPM_REPLACEMENT_LEVEL) * minutes_fraction * EPM_TO_WINS_FACTOR
EPM_REPLACEMENT_LEVEL = -1.0

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
TIMELINE_LAMBDA = 0.16  # decay rate — gap=3 ≈ 62% alignment, gap=10 ≈ 20%
TIMELINE_MAX_ADJUSTMENT = 0.15  # ±15% of context_value
TIMELINE_CORE_SIZE = 5  # top-N players by minutes that define team core

# Positional fit: bonus when an incoming player addresses a thin position,
# penalty when they pile onto a logjam. Measured as a bucket's *share* of the
# team's total minutes versus its fair share, so the metric is independent of
# roster size/shape (raw minute sums ran 2-5x the old fixed thresholds, which
# saturated every incoming player at the penalty floor).
POSITIONAL_MAX_ADJUSTMENT = 0.10  # ±10% of context_value
# Fair share of total minutes per coarse bucket: 2 guard / 2 forward / 1 center
# floor slots out of 5 starters → G=0.40, F=0.40, C=0.20.
POSITIONAL_FAIR_SHARE = {"G": 0.40, "F": 0.40, "C": 0.20}
# Relative share (actual ÷ fair) at/above this is a full logjam penalty; at/below
# the need threshold is a full positional-need bonus; linear between the two.
POSITIONAL_LOGJAM_MULT = 1.3  # relative share at/above this -> full penalty
POSITIONAL_NEED_MULT = 0.7  # relative share at/below this -> full bonus

# Spacing fit: small additive modifier when a shooter joins a spacing-poor
# team (or a non-shooter joins one that needs spacing). Effect shrinks when
# the acquiring team already has elite spacing, since spacing isn't the
# binding constraint there.
SPACING_MAX_ADJUSTMENT = 0.08  # ±8% — weakest signal of the four
LEAGUE_AVG_3PT_PCT = 0.363  # 2024-25 league average 3P%; refresh annually
LEAGUE_AVG_3PT_RATE = 0.40  # 2024-25 league average 3PA/FGA; refresh annually

# ---- Aging Curve Constants (Phase 5.5) ----
# Annual EPM change rates by age bracket — used by engine/aging_curve.py to
# project a player's impact forward year-by-year. Brackets follow established
# NBA aging research: growth into mid-20s, prime around 27-29, then steepening
# decline. The yearly rate is applied compounding-style (each year of aging is
# a separate multiplier, picked from the bracket the player is in *that* year),
# which keeps the curve continuous across bracket boundaries.
AGING_GROWTH_RATE_20_24 = 0.04  # +4% per year — growth phase
AGING_GROWTH_RATE_25_27 = 0.015  # +1.5% per year — approaching peak
AGING_PLATEAU_RATE_28_29 = 0.0  # stable at peak
AGING_DECLINE_RATE_30_32 = -0.03  # -3% per year — early decline
AGING_DECLINE_RATE_33_35 = -0.065  # -6.5% per year — steeper decline
AGING_DECLINE_RATE_36_PLUS = -0.10  # -10% per year — sharp decline

# Multi-year valuation: discount future-year surplus to reflect both time value
# and projection uncertainty. Year 2 is discounted 12%, year 3 ~23% (compounding),
# year 4 ~33%, year 5 ~43% — naturally devalues speculative far-future production.
PROJECTION_DISCOUNT_RATE = 0.12
# Cap projection horizon — beyond 5 years out, uncertainty dominates signal and
# any contract that long is being mis-modeled anyway (player options, ETOs, etc.).
MAX_PROJECTION_YEARS = 5

# DARKO DPM is ~40% compressed vs EPM (slope=0.608 across 491 players).
# We intentionally do NOT rescale because DARKO's regression-to-mean
# is a feature, not a scale artifact — its Kalman filter projects
# conservative estimates that correctly discount variance in
# single-season EPM peaks. Rescaling over-inflates elite projections.
# See stress_test_multiyear.py analysis for details.
DARKO_TO_EPM_SLOPE = 0.608
DARKO_TO_EPM_INTERCEPT = 0.026

# Future-year availability assumptions for multi-year valuation:
# - PROJECTED_GP_HEALTHY: league-average healthy-season games played, used as
#   the year 2+ baseline GP before applying the aging haircut.
# - PROJECTED_GP_CAP: hard ceiling so aging-curve > 1.0 cases don't project a
#   player into 80+ games next year.
PROJECTED_GP_HEALTHY = 72
PROJECTED_GP_CAP = 75

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

# ---- Team-Aware Draft Pick Valuation ----
# A pick's value depends on the originating team's record (worse team → earlier,
# more valuable pick), how far in the future it is (uncertainty), and any
# protections. These parameters drive engine/draft_picks.py:evaluate_draft_pick.
PICK_YEAR_DISCOUNT_RATE = 0.05  # 5% per year into the future (time + uncertainty)
# Per-year retention of a team's deviation from the mean when projecting future
# picks. regression_factor = 1 - PICK_REGRESSION_RATE ** year_offset, so a team
# 19 wins above the mean keeps ~42% of that edge 3 years out (a 60-win team
# projects to ~49 wins). Captures that today's extremes drift toward average.
PICK_REGRESSION_RATE = 0.75
LEAGUE_MEAN_WINS = 41.0  # 82-game schedule split evenly across the league
# year_offset = pick.year - this. Rolls over on July 1 (start of the new CBA
# league year), the same date the cap/apron numbers roll — bump it then each
# year so a pick conveying in the current league year gets year_offset 0.
# Known quirk: between the draft (late June) and July 1, current-draft picks
# carry year_offset 1 (~5% extra discount). Accepted — the proper fix is a
# continuous time-to-convey discount, not a draft-day rollover (which would
# split the season convention).
CURRENT_SEASON_START = 2026
# A pick swap conveys only the *difference* between two teams' picks, not a whole
# pick. 40% of the outright value is a rough placeholder until a probabilistic
# swap model (Travis Chen's approach) replaces it.
PICK_SWAP_VALUE_FRACTION = 0.40
