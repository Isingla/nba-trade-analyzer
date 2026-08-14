"""NBA salary-cap and apron figures.

Values are for the 2025-26 league year. These update annually with the cap;
refresh whenever the league publishes the new year's numbers.
"""

SALARY_CAP = 154_647_000
MINIMUM_TEAM_SALARY = 139_182_000  # 2025-26 salary floor (NBA PR); 90% of the cap
LUXURY_TAX = 187_895_000
FIRST_APRON = 195_945_000
SECOND_APRON = 207_824_000
EXPANDED_TPE_CUSHION = 8_527_000

# ---- Per-season cap thresholds (Cap Sheet export, Stage 1) ----
# Every value is a TEAM-SALARY level in whole dollars: the official salary cap,
# minimum team salary (floor), luxury-tax level, first apron, and second apron
# that a team's TOTAL salary is measured against for that league year. These
# are the announced dollar lines themselves — not cap percentages, not
# exception amounts, and not any alternative salary basis.
#
# 2025-26 and 2026-27 are CERTIFIED: primary-sourced from the NBA's league
# announcements and audited against reference/cba_reference_2025-26.yaml
# (which, per its own rules, carries ONLY certified figures — projections
# never go in the yaml).
#
# Out-years (2027-28 onward) are PROJECTED, certified=False: the cap grows at
# 8.0%/yr — past-5-season certified cap growth avg (NBA PR), ruled 2026-08-14
# (supersedes the ~5.5%/yr league-guidance figure; 164,961,000 x 1.08 =
# 178,157,880). Every other line is the certified 2026-27 value scaled by the
# SAME factor — i.e. by projectedCap[season] / cap[2026-27]. Tax/apron lines
# are CBA functions of the cap, so deriving them from the projected cap keeps
# the whole set ratio-consistent instead of inventing a separate per-line
# growth rate.
CAP_THRESHOLD_PROJECTED_GROWTH = 0.08  # past-5-season certified cap growth avg (NBA PR), ruled 2026-08-14

_CERTIFIED_LEVELS_2025_26: dict[str, int] = {
    "salary_cap": SALARY_CAP,
    "minimum_team_salary": MINIMUM_TEAM_SALARY,
    "luxury_tax": LUXURY_TAX,
    "first_apron": FIRST_APRON,
    "second_apron": SECOND_APRON,
}

# NBA.com league press release, June 2026 (2026-27 league year).
_CERTIFIED_LEVELS_2026_27: dict[str, int] = {
    "salary_cap": 164_961_000,
    "minimum_team_salary": 148_465_000,
    "luxury_tax": 200_428_000,
    "first_apron": 209_015_000,
    "second_apron": 221_686_000,
}


def _projected_levels(years_past_2026_27: int) -> dict[str, int]:
    """Certified 2026-27 levels scaled by (1 + growth)^n — see block comment."""
    factor = (1 + CAP_THRESHOLD_PROJECTED_GROWTH) ** years_past_2026_27
    return {k: int(round(v * factor)) for k, v in _CERTIFIED_LEVELS_2026_27.items()}


_PROJECTED_SOURCE = (
    "Projected: certified 2026-27 levels x 1.08^n "
    "(past-5-season certified cap growth avg (NBA PR), ruled 2026-08-14; "
    "thresholds scale with the cap)"
)

# season key -> {five threshold levels..., "certified": bool, "source": str}.
# Covers the databallr export window (see export.season_keys()).
CAP_THRESHOLDS_BY_SEASON: dict[str, dict[str, int | bool | str]] = {
    "2025-26": {
        **_CERTIFIED_LEVELS_2025_26,
        "certified": True,
        "source": "NBA PR 2025-26",
    },
    "2026-27": {
        **_CERTIFIED_LEVELS_2026_27,
        "certified": True,
        "source": "NBA.com league press release, June 2026",
    },
    "2027-28": {**_projected_levels(1), "certified": False, "source": _PROJECTED_SOURCE},
    "2028-29": {**_projected_levels(2), "certified": False, "source": _PROJECTED_SOURCE},
    "2029-30": {**_projected_levels(3), "certified": False, "source": _PROJECTED_SOURCE},
    # Added at the 2026-07-11 league-year rollover: season_keys() now ends at
    # 2030-31, and export fails loud when the window outgrows this table.
    # Same projected-growth rule as the other non-certified seasons.
    "2030-31": {**_projected_levels(4), "certified": False, "source": _PROJECTED_SOURCE},
}

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

# ---- Minutes & Availability Projection (issue 2.2) ----
# Total playing time is projected as TWO independent models multiplied:
#     projected_minutes = projected_games * projected_mpg
# Keeping games and MPG separable lets "injury-prone" (games) and "reduced
# role" (mpg) move independently. v1 prices a flat per-minute impact across
# those minutes; within-game effectiveness degradation (fatigue / minute
# restrictions) is a documented future TODO, NOT modeled here.
REGULAR_SEASON_GAMES = 82

# Games-played model: recency-weighted games-missed % + an age durability term.
# Recency weights decay geometrically toward older seasons — the latest season
# carries weight 1.0 and each season further back is multiplied by
# GAMES_RECENCY_DECAY (so the most recent availability dominates, never a flat
# multi-season average). 0.55 puts ~57% of the weight on the latest of 3 seasons.
GAMES_RECENCY_DECAY = 0.55
# Durability falls with age: every year a player is older than the pivot adds a
# small flat games-missed increment, evaluated at the projected season's age so
# it compounds naturally for out-years.
GAMES_AGE_DURABILITY_PIVOT = 31
GAMES_AGE_MISSED_PER_YEAR = 0.012  # +1.2% of the season missed per year past pivot
# Bounds so neither model tail produces an absurd season.
PROJECTED_GAMES_FLOOR = 0.0
PROJECTED_GAMES_CEILING = 78.0  # not even iron-men are projected to a full 82
# Games projected when a player has no usable GP history (rookies, long
# absentees): start from the healthy ceiling and let the age term haircut it.
PROJECTED_GAMES_NO_HISTORY = 70.0

# Minutes-per-game model: a recency-weighted prior MPG nudged by impact and
# salary. Both nudge terms are deliberately SMALL so prior role dominates; the
# backtest's salary guard checks whether the salary term overweights
# overpaid-but-low-impact players (Russ's condition for keeping salary in).
MPG_RECENCY_DECAY = 0.55
MPG_IMPACT_REF = 0.0    # league-average EPM/DPM impact anchor (~0)
MPG_IMPACT_COEF = 0.9   # MPG added per +1.0 impact above the reference
MPG_SALARY_REF = 0.10   # salary as a share of the cap: ~10% is a mid-rotation deal
MPG_SALARY_COEF = 6.0   # MPG added per +1.0 of cap-share above the reference.
# Capped from 12 -> 6 after the issue-2.2 salary guard, re-run on EPM/DARKO
# impact (not NET_RATING), confirmed salary materially overweighted genuinely
# overpaid-but-low-impact players: +64 min of projected minutes on that cohort
# at coef 12, halving to ~+32 min at coef 6 (under the materiality line) while
# keeping salary's positive "coaches play who they pay" signal. See
# scripts/backtest_minutes.py and databallr docs/CONTROL_RUNWAY.md.
MPG_FLOOR = 0.0
# Realistic modern max sustainable MPG. Re-tuned 38 -> 36: in 2024-25 (>= 40 GP)
# NO player averaged 38, only 4 reached >= 37, and 14 reached >= 36 — so 36 sits
# right at the genuine high-minute-star load while a 38 ceiling was above every
# real player and PINNED stars flat, masking the aging curve. 36 is slightly
# conservative on purpose so the aging curve drives year-over-year movement
# instead of the model re-clamping a declining star at the cap every season.
MPG_CEILING = 36.0

# ---- Minutes aging curve: year-over-year ΔMPG by age ----
# Fitted by scripts/fit_minutes_aging.py (DELTA METHOD): for every player in two
# CONSECUTIVE seasons, ΔMPG = mpg_later − mpg_earlier, keyed by age in the later
# season, mean-by-age, then 3-point smoothed.
#
#   FIT WINDOW: 2013-14 .. 2024-25 (modern load-management era; don't reach older).
#   FIT FILTER: a delta pair counts only when BOTH endpoint seasons cleared
#     >= 15 MPG and >= 20 GP (n = 2,784 pairs). Low-minute role churn is roster
#     chaos, not aging — it would distort the fit. The curve is still APPLIED to
#     every player regardless of their minutes.
#   TAIL GUARD (age >= 31): forced NON-POSITIVE and NON-INCREASING (most-negative
#     value carried forward). Survivorship — declining players get waived, so the
#     survivors look like they barely decline — otherwise flattens or ticks the
#     old-age curve UP and re-creates the "old players gain minutes" bug. The
#     guard pinned ages 38-40 to the age-37 value (raw age-40 was even +0.371).
#   KNOWN BIAS (accepted for v1): survivorship still makes the late-decline
#     MAGNITUDE conservative (gentler than reality) for players who survive.
#
# APPLICATION (minutes.py): CUMULATIVE — sum the per-year delta from the player's
# current age forward. Ages outside [20, 40] clamp to the nearest fitted end, so
# a 41+ vet holds the steepest -2.747/yr decline and a teenager holds +1.898.
MPG_AGE_DELTA = {
    20: 1.898, 21: 1.694, 22: 1.661, 23: 1.073, 24: 0.793,
    25: 0.354, 26: 0.276, 27: -0.052, 28: -0.401, 29: -0.721,
    30: -1.036, 31: -1.238, 32: -1.539, 33: -1.671, 34: -1.925,
    35: -2.407, 36: -2.565, 37: -2.747, 38: -2.747, 39: -2.747,
    40: -2.747,
}
MPG_AGE_DELTA_MIN_AGE = 20
MPG_AGE_DELTA_MAX_AGE = 40

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
