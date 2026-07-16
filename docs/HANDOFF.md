# NBA Trade Analyzer — Complete Handoff Document (V3, Post Phase 7/9 + Calibration)

## Project Overview

An AI-powered NBA trade evaluation engine that grades proposed trades for both
teams using salary cap validation, EPM-based player impact modeling,
team-context-aware value estimation, multi-year contract projections, and a
plain-English trade grader. The tool combines CBA compliance checking, player
surplus value calculations, team-aware draft pick valuation, and contextual
adjustments for team situation — all exposed through a typer CLI.

**Repo:** `github.com/Isingla/nba-trade-analyzer`
**Stack:** Python 3.12+, uv, pydantic, typer, httpx, pandas, BeautifulSoup, lxml, nba_api, pytest, ruff
**Current branch:** `main` (all feature work merged)
**Season:** 2025-26

**What changed since V2 (Post Phase 5.5):**
- **Salary data source** — real Basketball Reference scraper replaced hardcoded TODO salaries.
- **DraftPick model** — structured pydantic model with team-aware valuation replaced `list[str]`.
- **Trade grader (Phase 7)** — 0-100 scores, seven metric breakdowns, basketball prose.
- **CLI (Phase 9)** — `grade` and `lookup` commands over the engine.
- **Calibration sprint** — replacement-level pricing, position overrides, score scaling, dual-negative damping, current-roster filtering, realistic pick-slot mapping.
- **Tests** — 162 → **291 passing**, plus a 5-trade validation smoke test.

---

## Architecture

```
src/nba_trade_analyzer/
├── cli.py                   # typer CLI — `grade` and `lookup` commands (Phase 9)
├── report.py                # shared terminal renderer for a graded trade
├── teams.py                 # team abbreviation resolution (nba_api ↔ BRef forms)
├── data/
│   ├── cache.py             # Generic JSON file cache with 24h TTL
│   ├── players.py           # nba_api pipeline (stats, 3PT splits, team records, projected wins)
│   ├── epm.py               # EPM scraper from dunksandthrees.com (SvelteKit payload extraction)
│   ├── darko.py             # DARKO projections from public Google Sheet CSV export
│   ├── salaries.py          # Basketball Reference contracts scraper (+ committed CSV fallback)
│   └── salaries_2025_26.csv # offline salary snapshot
├── models/
│   ├── player.py            # Player, Contract (frozen pydantic)
│   ├── team.py              # CapStatus enum, Team, RosterEntry, Roster
│   ├── trade.py             # TradeAssets, Trade, TradeResult
│   ├── valuation.py         # PlayerValuation, YearProjection, MultiYearValuation
│   ├── team_context.py      # TeamContextValuation
│   ├── draft_pick.py        # structured DraftPick (team, year, round, protections, swap, via)
│   └── grade.py             # TradeGrade, TeamGrade, MetricBreakdown, DraftCapitalBreakdown
├── engine/
│   ├── constants.py         # All cap numbers, valuation params, aging rates, team context, pick params
│   ├── salary_rules.py      # CBA trade legality checker (all 4 cap tiers)
│   ├── valuation.py         # Single-season + multi-year surplus calculator
│   ├── draft_picks.py       # Team-aware pick value curves (record → slot → value)
│   ├── team_context.py      # Win curve, timeline, positional fit, spacing, roster filtering
│   ├── aging_curve.py       # EPM aging projections by age bracket
│   └── grader.py            # Phase 7 grader — scores, breakdowns, prose
scripts/
├── calibrate_epm.py         # Top-30 valuation output for factor tuning
├── grade_trades.py          # Demo trades rendered through the grader
├── validate_trades.py       # 5-trade validation smoke test (PASS/FAIL report)
├── end_to_end_test.py       # Real trade scenarios with full breakdowns + prose verdicts
├── stress_test_trades.py    # 62 scenarios across all system components
└── stress_test_multiyear.py # 478 scenarios for multi-year valuation edge cases
tests/
├── test_players.py          # Data pipeline tests
├── test_salaries.py         # Basketball Reference scraper + parsing tests
├── test_models.py           # Pydantic model validation tests
├── test_draft_pick_model.py # DraftPick model validation + label tests
├── test_salary_rules.py     # CBA legality tests (all 4 tiers + edge cases)
├── test_valuation.py        # Single-season valuation tests
├── test_draft_picks.py      # Pick value curve + team-aware valuation tests
├── test_epm.py              # EPM scraper + name normalization tests
├── test_darko.py            # DARKO fetcher tests
├── test_team_context.py     # Win curve, timeline, positional, spacing, roster filtering
├── test_aging_curve.py      # Aging factor tests
├── test_multiyear_valuation.py  # Multi-year projection tests
├── test_grader.py           # Phase 7 grader tests (scores, tiers, prose, zero-sum)
└── test_cli.py              # CLI parsing + command tests
```

**Test count:** 291 passing, ruff clean, plus a 5-trade validation smoke test
(`scripts/validate_trades.py`).

---

## What's Been Built

### Phase 1: Player Data Pipeline ✅
**Files:** `data/cache.py`, `data/players.py`, `tests/test_players.py`

- `fetch_player_stats()` pulls current-season stats via nba_api. Returns DataFrame with: `player_name, team, age, GP, MPG, PIE, USG_PCT, NET_RATING, OFF_RATING, W, L, FGA, FG3A, FG3_PCT, FG3_RATE`
- `get_team_net_rating(df, team_abbr)` — minutes-weighted team NET_RATING
- `get_team_projected_wins(df, team_abbr)` — extrapolates current W-L record to 82 games
- `get_team_3pt_stats(df, team_abbr)` — volume-weighted team 3PT efficiency
- Generic JSON file cache with 24h TTL at `~/.nba_trade_analyzer/cache/`
- Player names use Unicode (e.g., "Nikola Jokić")

### Phase 1.5: EPM & DARKO Integration ✅
**Files:** `data/epm.py`, `data/darko.py`, `tests/test_epm.py`, `tests/test_darko.py`

**EPM scraper (`data/epm.py`):**
- Scrapes dunksandthrees.com/epm by extracting the embedded SvelteKit data payload via regex (page is client-side rendered; the full table sits in the HTML as a JS object literal)
- Returns DataFrame: `player_name, team, epm, epm_off, epm_def, mpg, position, age`
- Name normalization: Unicode normalization, suffix stripping (Jr./Sr./II/III/IV/V), period removal, whitespace collapse
- `NAME_ALIASES` dict maps colloquial → canonical (e.g., "Herb Jones" → "Herbert Jones", "Cam Thomas" → "Cameron Thomas"). **Note:** "SGA" is *not* aliased — `lookup` needs the full name "Shai Gilgeous-Alexander".
- `get_player_epm(df, player_name)` — normalized name lookup, returns row or None

**DARKO fetcher (`data/darko.py`):**
- Fetches Kostya Medvedovsky's public Google Sheet via CSV export URL (Sheet ID `1mhwOLqPu2F9026EQiVxFPIN1t9RGafGpl-dokaIsm9c`)
- Returns DataFrame with DPM plus off/def/box/on-off splits
- Returns None gracefully on HTTP error so EPM stays the primary path

**DARKO-EPM Scale Gap (IMPORTANT, unchanged):**
- DARKO DPM is ~40% compressed vs EPM (linear fit: DPM = 0.608 × EPM + 0.026)
- We intentionally do NOT rescale — DARKO's regression-to-mean is a feature of its Kalman filter, not a scale artifact. `DARKO_TO_EPM_SLOPE` / `DARKO_TO_EPM_INTERCEPT` are stored for reference only.

**Valuation fallback chain:** EPM → DARKO → NET_RATING (tracked in `metric_source`). EPM/DARKO paths bypass the NET_RATING team adjustment entirely (RAPM already isolates impact).

### Phase 2: Pydantic Domain Models ✅
**Files:** `models/player.py`, `models/team.py`, `models/trade.py`, `tests/test_models.py`

- `Player` (name, team, age, stats dict) — frozen
- `Contract` (salary int > 0, years_remaining, is_rookie_scale, has_player_option, has_team_option) — frozen
- `CapStatus` enum: UNDER_CAP, OVER_CAP, FIRST_APRON, SECOND_APRON
- `Team` (name, abbreviation, total_payroll, cap_status)
- `RosterEntry` (Player + Contract paired) — prevents drift between parallel lists; critical for the salary rules engine
- `TradeAssets` (list[RosterEntry] + list[DraftPick]) — picks are now structured (see Phase 4)
- `Trade` (team_a, team_b, team_a_sends, team_b_sends)

### Phase 3: CBA Salary Rules Engine ✅
**Files:** `engine/salary_rules.py`, `engine/constants.py`, `tests/test_salary_rules.py`

**2025-26 cap numbers:**
```python
SALARY_CAP = 154_647_000
LUXURY_TAX = 187_895_000
FIRST_APRON = 195_945_000
SECOND_APRON = 207_824_000
EXPANDED_TPE_CUSHION = 8_527_000
```

**Four-tier CBA rules:**
1. **Under cap:** incoming ≤ salary cap space + outgoing salary + $250K
2. **Over cap, below first apron (Expanded TPE):**
   - Outgoing < $7,250,000: incoming ≤ 200% × outgoing + $250K
   - Outgoing $7,250,000–$29,000,000: incoming ≤ outgoing + $8,527,000
   - Outgoing > $29,000,000: incoming ≤ 125% × outgoing (NO $250K cushion)
   - Aggregation: ALLOWED
3. **First apron:** incoming ≤ 100% of outgoing. No cushion. Aggregation: ALLOWED.
4. **Second apron:** incoming ≤ 100% of outgoing. No cushion. Aggregation: BLOCKED.
   - **Exception:** if the trade drops the team below the second apron post-trade, aggregation allowed.

**Critical:** Rules check the POST-TRADE cap position, not pre-trade.
**Sources verified:** CBA Guide (cbaguide.com), Hoops Rumors, NBA.com official cap numbers.

### Phase 4: Player Valuation + Draft Picks ✅
**Files:** `engine/valuation.py`, `engine/draft_picks.py`, `models/valuation.py`, `models/draft_pick.py`, `tests/test_valuation.py`, `tests/test_draft_picks.py`, `tests/test_draft_pick_model.py`

**Core pipeline (EPM/DARKO path):**
```python
adjusted_impact = player_epm  # no team adjustment when using EPM
minutes_fraction = (gp * mpg) / FULL_SEASON_MINUTES
# Calibration sprint: price against replacement level, not 0.0 (league average)
raw_wins_added = (adjusted_impact - EPM_REPLACEMENT_LEVEL) * minutes_fraction * EPM_TO_WINS_FACTOR
wins_added = MAX_WINS_ADDED * tanh(raw_wins_added / MAX_WINS_ADDED)  # Option F
player_value = wins_added * DOLLARS_PER_WIN
surplus_value = player_value - contract.salary
```

**EPM replacement-level offset (calibration sprint, NEW):** EPM 0.0 is *league
average*, not replacement level. RAPM centers on the average rotation player —
a real, rosterable contributor — not a freely-available scrap-heap body.
Pricing against 0.0 made the median $15M starter read as a ~$13M overpay.
Subtracting `EPM_REPLACEMENT_LEVEL = -1.0` before scaling wins prices an
average starter near fair value, a constant shift that preserves relative
ordering. Recalibrated targets: Jokić ≈ +$6M/yr, SGA ≈ +$22M/yr, Wembanyama ≈
+$44M/yr, a 0.0-EPM $15M starter ≈ break-even.

**`DraftPick` model (NEW, replaces `list[str]`):**
```python
class DraftPick(BaseModel):  # frozen
    team: str            # 3-letter originating team — its record sets the slot
    year: int            # 2025–2035
    round: int           # 1 or 2
    protections: str | None = None   # "top-4 protected", "lottery protected", ...
    swap: bool = False               # pick-swap right vs. outright pick
    via_team: str | None = None      # previously-traded ("via") pick
    # .label property reconstructs "2027 LAL 1st (top-4 protected) via BRK"
```

**Team-aware pick valuation (`evaluate_draft_pick`, NEW):**
1. Estimate landing slot from the originating team's projected wins, regressed toward the mean for future years.
2. Map slot → dollar value via the exponential decay curve (second-round picks shifted into slots 31-60 so the second-round penalty applies).
3. Discount for years out, protection, and swap rights.

`estimate_pick_position` anchors the wins→slot map on the *realistic* win range
(15 wins → pick 1, 62 → pick 30), not 0-82 — see the calibration sprint note.

**Option F (tanh curve) is PERMANENT infrastructure — never remove or modify.**

### Phase 5: Team Context ✅
**Files:** `engine/team_context.py`, `models/team_context.py`, `tests/test_team_context.py`

Makes valuations team-aware. Same player = different value to different teams.

**Formula:**
```python
context_value = wins_added * (DOLLARS_PER_WIN * effective_win_curve_multiplier)
timeline_adj   = context_value * timeline_modifier     # capped ±15%
positional_adj = context_value * positional_modifier   # capped ±10%
spacing_adj    = context_value * spacing_modifier      # capped ±8%
team_adjusted_value = context_value + timeline_adj + positional_adj + spacing_adj
team_surplus = team_adjusted_value - contract.salary
```

**Win curve is multiplicative; timeline/positional/spacing are additive.**

- **5a. Win curve** — sigmoid centered on the playoff cutoff (42 wins), returns a multiplier in [0.4, 2.0] on DOLLARS_PER_WIN. Damped toward 1.0 for negative wins_added so contender multipliers don't amplify bad contracts.
- **5b. Timeline alignment** — exponential decay on the age gap between the incoming player and the team core (top 5 by minutes), `λ = 0.16`, ±15% cap.
- **5c. Positional fit** — minutes-by-position analysis. Penalty for logjams (>40 min filled), bonus for needs (<25 min). `_trim_outgoing()` removes departing players' minutes before scoring.
- **5d. Spacing** — volume-weighted 3PT contribution × scarcity. Direction tracks the player (shooter = positive), magnitude tracks team scarcity.

**Position overrides (calibration sprint, NEW):** `POSITION_OVERRIDES` corrects
wrong source labels for high-profile players (e.g. Luka Dončić → "G");
`resolve_position(name, raw)` applies the override or passes through. Dual-position
labels ("G-F", "F-C") split minutes evenly across both coarse buckets, so
positional totals stay conserved (sum of buckets == sum of player MPG).

**Current-roster filtering (calibration sprint, NEW):** nba_api season stats
list *every* player who logged minutes for a team, including ones traded away
mid-season — which inflated position groups to impossible totals (Memphis 336
guard minutes vs. a 240 ceiling). `filter_to_current_roster(roster,
salary_team_abbr, salary_df)` drops players whose salary-data team differs from
the one being evaluated. The grader maps the nba_api abbreviation to the
Basketball Reference form (BRK/CHO/PHO) first. Without `salary_df`, behavior is
unchanged. Result: Memphis guards 336→212, Washington forwards 233→129, all
teams under 240. Scores are unaffected (team-agnostic).

### Phase 5.5: Multi-Year Contract Valuation ✅
**Files:** `engine/aging_curve.py`, `engine/valuation.py`, `models/valuation.py`, `tests/test_aging_curve.py`, `tests/test_multiyear_valuation.py`

Values all remaining contract years, not just the current season.

**Projection tiers:** Year 1 = current EPM. Year 2 = DARKO DPM (raw, not rescaled). Years 3+ = aging curve applied to the year-2 anchor, compounding per year.

**Aging curve rates (annual EPM change by age bracket):**
```python
AGING_GROWTH_RATE_20_24 = 0.04      # +4%/yr growth
AGING_GROWTH_RATE_25_27 = 0.015     # +1.5%/yr approaching peak
AGING_PLATEAU_RATE_28_29 = 0.0      # stable at peak
AGING_DECLINE_RATE_30_32 = -0.03    # -3%/yr early decline
AGING_DECLINE_RATE_33_35 = -0.065   # -6.5%/yr steeper decline
AGING_DECLINE_RATE_36_PLUS = -0.10  # -10%/yr sharp decline
```

**Per-year formula:** project EPM → minutes_fraction → raw_wins → tanh →
dollar_value → `(dollar_value - year_salary) * discount_factor`, where
`discount_factor = 1 / (1 + PROJECTION_DISCOUNT_RATE) ** year_offset`. Total
contract surplus = sum of all years' discounted surplus. `evaluate_trade_assets*`
use `total_contract_surplus` as the primary trade metric.

### Phase 7: Trade Grader ✅ (NEW)
**Files:** `engine/grader.py`, `models/grade.py`, `report.py`, `tests/test_grader.py`

A pure *consumer* layer on top of the existing engines — it modifies none of
them. Pipeline:

1. **Legality** — illegal trades short-circuit to `TradeGrade(is_legal=False)` with both team grades `None`. No surplus analysis runs on an illegal trade.
2. **Valuation** — per-player base / team-context / multi-year views plus team-aware pick values for both sides.
3. **Translation** — each side gets a 0-100 score, a verdict label, seven `MetricBreakdown`s (impact, contract, win curve, timeline, positional fit, spacing) + a `DraftCapitalBreakdown`, and a short basketball-prose write-up.

**Zero-sum score by construction.** The score is driven by team-agnostic
multi-year surplus plus team-agnostic pick values, so what one side gains the
other gives up (`score_a + score_b == 100`). Team context (win curve, fit,
spacing) shapes the *breakdowns and prose* — the texture of the deal — not the
headline number.

```python
delta_a = incoming_surplus - outgoing_surplus
delta_a *= _dual_negative_damping(incoming_surplus, outgoing_surplus)
score = 50 + (delta / max(total_gross, 1)) * SCORE_SCALING_FACTOR   # clamped [0,100]
```

**Verdict labels (`get_verdict`):**

| Score | Verdict |
|-------|---------|
| 85-100 | Highway Robbery |
| 70-84 | Clear Win |
| 55-69 | Smart Deal |
| 45-54 | Fair Trade |
| 35-44 | Slight Overpay |
| 20-34 | Overpay |
| 0-19 | Fleeced |

**Tier maps** (also in `grader.py`): `_epm_tier` (Elite/All-Star/Above
Average/Average/Below Average/Replacement Level), `_contract_tier` (Elite
Value … Bad Contract), `_win_curve_tier` (Championship Contender …
Rebuilding), `_timeline_tier`, `_positional_tier`, `_spacing_tier`.

**Prose** is deterministic and score-aware: it frames the strongest fit signal
by the *overall* grade (a positive metric never reads as a "win" when the trade
is an overpay), references outgoing players (a grade is the delta between what
you got and gave up), and never quotes a raw multiplier or an absurd deficit.
Salary-dump verdicts quote the *shed* value (sent surplus minus received
surplus), tiered so small figures are dropped and only > $30M reads as a major
cap win.

### Phase 9: CLI ✅ (NEW)
**Files:** `cli.py`, `report.py`, `teams.py`, `tests/test_cli.py`

A thin typer shell over the engine. Two commands:

```bash
# grade a trade — players and picks both go through --sends-{a,b}, repeated per asset
uv run nba-trade-analyzer grade --team-a DAL --team-b MEM \
    --sends-a "Klay Thompson" --sends-b "Santi Aldama"

# multi-asset package with a pick
uv run nba-trade-analyzer grade --team-a DAL --team-b DET \
    --sends-a "Kyrie Irving" \
    --sends-b "Isaiah Stewart" --sends-b "Caris LeVert" \
    --sends-b "Ausar Thompson" --sends-b "2027 DET 1st unprotected"

# look up a single player (full name)
uv run nba-trade-analyzer lookup "Shai Gilgeous-Alexander"
```

- `parse_pick` recognizes a pick string by the pattern `{year} {TEAM} {1st|2nd} [protections]` (e.g. "2027 DET 1st unprotected"); anything else is treated as a player name.
- Salary data is the source of truth for contracts; a player miss is fatal with a spelling hint and `difflib` fuzzy suggestions.
- Team payroll is summed from the salary feed to derive `CapStatus`.
- `--quick` skips the DARKO fetch (EPM → NET_RATING only) for a faster grade.
- `force_utf8_stdout()` rewraps stdout so the box-drawing/arrow/✓✗ characters survive the Windows cp1252 console.
- Rendering is shared with `scripts/grade_trades.py` via `report.py`.

---

## Calibration Sprint (Post Phase 9)

A focused pass fixing valuation and presentation issues surfaced by the full
pipeline. Every fix is documented in the constants and code comments.

1. **EPM replacement-level offset** — price against `EPM_REPLACEMENT_LEVEL = -1.0`, not 0.0. (See Phase 4.)
2. **Dual-position minutes split** — dual-eligible players ("G-F", "F-C") split evenly across both coarse buckets; regression tests assert minutes are conserved.
3. **Score scaling by multi-year magnitude** — `_asset_totals` now measures gross production over the *same* projected, discounted horizon as surplus. Previously a multi-year surplus was divided by a single-season gross, so a longer contract inflated the ratio (two similar players differing only in years left scored ~26/74). Similar swaps now land in the 40-60 band; star-for-scrub stays decisive (80/20).
4. **Capped surplus in verdict prose** — salary-dump verdicts no longer quote raw multi-year deltas ("shedding roughly $56M"); the figure is tiered (dropped < $10M, "significant" $10-30M, "major cap win" > $30M).
5. **Dual-negative damping** — when *both* sides receive net-negative-surplus packages, the swing is compressed in proportion to how negative the better (less-bad) package is. A 2yr-bad-deal-for-4yr-bad-deal no longer reads as Highway Robbery. Klay/Kispert 96/4 → 64/36, Klay/Aldama 60/40 → 53/47, star-for-scrub stays 80/20. Trades with any positive-surplus side are untouched.
6. **Shed value = surplus delta, not total** — a team moving off a -$73M contract for a -$28M one improves by ~$45M, not $73M: `abs(sent_surplus) - abs(received_surplus)`.
7. **Implied per-year production in the contract verdict** — quote `salary + per_year_surplus` for the "producing like" figure, matching the multi-year tier (a player with a strong current season but a multi-year overpay no longer self-contradicts).
8. **Current-roster filtering** — drop traded-away players via the salary feed before any roster-based context. (See Phase 5.)
9. **Realistic pick-slot mapping** — anchor `estimate_pick_position` on 15-62 wins, not 0-82. A 60-win top seed now maps to ~slot 29 (was ~22); its 2027 pick lands ~24 after regression (was ~19). (See Phase 4.)
10. **Honest spacing prose for missing data** — a player with no current-season stats row (injured/absent, e.g. Kyrie post-ACL) is reported as "No current shooting data … treated as neutral" instead of falsely "isn't a three-point threat (0.0 attempts/game)".

---

## All Constants in `engine/constants.py`

```python
# 2025-26 Cap numbers
SALARY_CAP = 154_647_000
LUXURY_TAX = 187_895_000
FIRST_APRON = 195_945_000
SECOND_APRON = 207_824_000
EXPANDED_TPE_CUSHION = 8_527_000

# Valuation
DOLLARS_PER_WIN = 3_500_000
EPM_TO_WINS_FACTOR = 4.2
EPM_REPLACEMENT_LEVEL = -1.0          # NEW — price above a min-salary body, not league avg
REPLACEMENT_LEVEL_NET_RATING = -2.0   # NET_RATING fallback path
NET_RATING_TO_WINS_FACTOR = 2.75      # NET_RATING fallback path
FULL_SEASON_MINUTES = 2952            # 82 × 36
TEAM_ADJUSTMENT_WEIGHT = 0.5          # NET_RATING fallback path
MAX_WINS_ADDED = 20.0                 # tanh cap (Option F) — PERMANENT

# Win curve
WIN_CURVE_MIDPOINT = 42.0
WIN_CURVE_STEEPNESS = 0.15
WIN_CURVE_MIN_MULTIPLIER = 0.4
WIN_CURVE_MAX_MULTIPLIER = 2.0

# Timeline alignment
TIMELINE_LAMBDA = 0.16
TIMELINE_MAX_ADJUSTMENT = 0.15
TIMELINE_CORE_SIZE = 5

# Positional fit
POSITIONAL_MAX_ADJUSTMENT = 0.10
POSITIONAL_MINUTES_THRESHOLD_HIGH = 40.0
POSITIONAL_MINUTES_THRESHOLD_LOW = 25.0

# Spacing
SPACING_MAX_ADJUSTMENT = 0.08
LEAGUE_AVG_3PT_PCT = 0.363
LEAGUE_AVG_3PT_RATE = 0.40

# Aging curve
AGING_GROWTH_RATE_20_24 = 0.04
AGING_GROWTH_RATE_25_27 = 0.015
AGING_PLATEAU_RATE_28_29 = 0.0
AGING_DECLINE_RATE_30_32 = -0.03
AGING_DECLINE_RATE_33_35 = -0.065
AGING_DECLINE_RATE_36_PLUS = -0.10

# Multi-year valuation
PROJECTION_DISCOUNT_RATE = 0.12
MAX_PROJECTION_YEARS = 5
PROJECTED_GP_HEALTHY = 72
PROJECTED_GP_CAP = 75

# DARKO-EPM scale relationship (reference only — intentionally not used for rescaling)
DARKO_TO_EPM_SLOPE = 0.608
DARKO_TO_EPM_INTERCEPT = 0.026

# Draft picks — base curve
PICK_VALUE_SCALE = 80_000_000
PICK_VALUE_DECAY = 0.065
PICK_VALUE_SECOND_ROUND_PENALTY = 0.4

# Draft picks — team-aware valuation (NEW)
PICK_YEAR_DISCOUNT_RATE = 0.05        # 5%/yr into the future
PICK_REGRESSION_RATE = 0.75           # regression_factor = 1 - rate ** year_offset
LEAGUE_MEAN_WINS = 41.0
CURRENT_SEASON_START = 2025           # year_offset = pick.year - this; bump each season
PICK_SWAP_VALUE_FRACTION = 0.40
```

**Grader constants (`engine/grader.py`):**
```python
SCORE_SCALING_FACTOR = 30.0                 # surplus-ratio → score sensitivity
DUAL_NEGATIVE_DAMPING_THRESHOLD = 40_000_000.0
```

**Salary scraper constants (`data/salaries.py`):** `ROOKIE_SCALE_2025_26`
(picks 1-30 first-year salaries), `_ROOKIE_SCALE_TOLERANCE = 0.001`,
`_ROOKIE_SCALE_MIN_YEARS = 3`.

**Position overrides (`engine/team_context.py`):** `POSITION_OVERRIDES`
(currently `{Luka Dončić: "G"}`).

---

## Data Sources

| Source | Module | What | Cache |
|--------|--------|------|-------|
| nba_api | `data/players.py` | Player stats, 3PT splits, team records, W-L, projected wins | 24h |
| dunksandthrees.com/epm | `data/epm.py` | EPM, off/def splits, position, age | 24h |
| DARKO Google Sheet | `data/darko.py` | DPM projections, off/def/box/on-off | 24h |
| Basketball Reference | `data/salaries.py` | Every active contract: salary, years, options, rookie-scale flag | 24h |
| CBA Guide (cbaguide.com) | `engine/salary_rules.py` | Salary matching rules | Hardcoded |

**Salary scraper (`data/salaries.py`, NEW — closes the V2 gap):** the
Basketball Reference contracts page packs every active contract into one HTML
table (`id="player-contracts"`), so a single polite request yields the whole
league. A BeautifulSoup pass reads each salary cell's `csk` attribute (a clean
integer — no `$`/comma stripping), the `iz` class for empty years, and the
`salary-pl` / `salary-tm` classes for player/team options. From the year
columns it derives `salary` (current season), `years_remaining`,
`has_player_option`, `has_team_option`, and a conservative `is_rookie_scale`
flag (current salary matches a published scale slot AND the deal has a rookie
contract's structural fingerprint). `_season_to_year_stat` reads the header so
the scraper survives Basketball Reference rolling the page forward a year. On
any HTTP/parse failure it falls back to the committed
`data/salaries_2025_26.csv` snapshot, so the project works offline.

---

## Diagnostic Scripts

- **`scripts/calibrate_epm.py`** — full valuation for the top 30 EPM players (raw/tanh wins, value, surplus, source, multi-year columns) plus a validation mini-table.
- **`scripts/grade_trades.py`** — runs a set of demo trades through the Phase 7 grader and renders them via `report.py`.
- **`scripts/validate_trades.py`** — the 5-trade smoke test (see below).
- **`scripts/end_to_end_test.py`** — real trade scenarios with full breakdowns + basketball-prose verdicts.
- **`scripts/stress_test_trades.py`** — 62 scenarios across 8 categories (salary matching, valuation paths, win curve, timeline, positional, spacing, combined context, real blockbusters).
- **`scripts/stress_test_multiyear.py`** — 478 scenarios for aging-curve shape, contract-length edges, projection-source chain, discount math, extreme EPM, real players, minutes projection, and team-context integration.

### Validation Smoke Test (`scripts/validate_trades.py`, NEW)

A lightweight smoke test — **not** a calibration or grading-quality validation.
It runs five predefined trades (defined as *data*, not code) through the full
grading pipeline and checks for obvious data-pipeline failures and sanity
violations: a broken data source, empty prose, scores that don't sum to 100, a
contender "giving up" a lottery pick, an absent player branded a non-shooter,
position minutes over the 240 ceiling. Loads every source once, reuses it, and
prints a PASS/FAIL report. Exits 0 if every trade passes, 1 otherwise.

The five trades:
1. **Klay/Aldama (DAL↔MEM)** — near-even swap, both sides in the 45-55 fair band.
2. **Klay/Kispert (DAL↔ATL)** — modest value gap, still inside a reasonable band (the dual-negative-damping regression case).
3. **Kyrie to DET (DAL↔DET)** — multi-asset + pick; checks the absent-player spacing prose and that DET's own 1st projects late.
4. **Randle/Gobert (NYK↔MIN)** — illegal (single-for-single salaries don't match); no grades produced.
5. **Kuzma dump (WAS↔DET)** — salary dump; WAS clearly wins, DET's own near-future 1st must project late.

---

## Git Workflow

- Feature branches: `feat/`, `fix/`, `chore/`, `docs/`
- Conventional commits: `feat:`, `fix:`, `chore:`, `docs:`, `refactor:`, `test:`
- PRs with squash-merge into main, reference issues (`closes #N`)
- `.claude/` is in `.gitignore` — never commit Claude Code local settings
- `ruff check` and `ruff format`, and `pytest`, before every commit
- **No AI attribution trailer** in commits (no `Co-Authored-By`)
- `uv` is invoked with `--native-tls` in this environment (corporate cert chain)

---

## Key Design Decisions (with rationale)

1. **EPM over BPM/VORP/WS/PER** — EPM won the Dunks & Threes retrodiction study; RAPM + tracking data isolates individual impact.
2. **DARKO raw DPM, not rescaled** — its ~40% compression is intentional Bayesian conservatism; rescaling over-inflated elite projections.
3. **Tanh curve (Option F)** — caps wins_added at MAX_WINS_ADDED = 20. Permanent infrastructure.
4. **Win curve multiplicative, context additive** — the win curve rescales what a win is worth (different unit, so it multiplies); timeline/positional/spacing are adjustments within the same unit (so they add, with caps).
5. **Price against replacement level (−1.0 EPM), not league average** — RAPM's 0.0 is the average rotation player, who is rosterable; replacement level is the min-salary floor. This stops average starters reading as overpays. (Calibration sprint.)
6. **Score is team-agnostic and zero-sum** — surplus + pick value drive the number; team context only shapes the prose. Keeps the score interpretable and `score_a + score_b == 100`.
7. **Dual-negative damping** — two bad contracts swapping is "pick your poison," usually a contract-length gap, not a quality edge; don't grade it as a fleecing. (Calibration sprint.)
8. **Measure gross over the same horizon as surplus** — comparing a multi-year surplus to a single-season gross made long deals look lopsided. (Calibration sprint.)
9. **Filter to the current roster via the salary feed** — season stats include traded-away players; the salary feed is the live roster. (Calibration sprint.)
10. **Team-aware pick values** — a pick's worth depends on the originating team's record (regressed for future years), protections, and swap status, not a fixed slot.
11. **Grader is a pure consumer** — it reads the engines and translates to a verdict; it never mutates valuation/context logic, keeping calibration in one place.

---

## Known Limitations & Tech Debt

1. **No ML calibration yet (Phase 6).** Every constant is hand-tuned against intuition and a handful of real players. Phase 6 replaces this with a model trained on historical trade outcomes.
2. **Salary escalation not modeled.** Multi-year valuation uses a flat current-year salary for all years; real contracts escalate ~5-8%/yr.
3. **Pick protections are a static lookup table.** `PROTECTION_DISCOUNT` maps a protection string to a flat multiplier; it should be a probabilistic convey/no-convey integral over the team's standing distribution (Travis Chen's approach).
4. **Pick swaps are a flat 40% of outright value.** A swap conveys only the *difference* between two teams' picks — `PICK_SWAP_VALUE_FRACTION` is a placeholder until a probabilistic swap model exists.
5. **Fragile scraping.** EPM parses a SvelteKit payload and salaries parse Basketball Reference markup; both break if the source format changes (salaries fall back to the committed CSV; EPM does not).
6. **No playoff adjustment.** EPM is regular-season only; some players (Butler, LeBron) have materially different playoff impact.
7. **No injury/availability discount.** Beyond current-season GP, no forward-looking availability risk is modeled.
8. **0 MPG edge case.** A player with no current-season minutes falls back to a default starter load (CLI/grader) rather than a career-average projection; in raw multi-year valuation a 0 MPG row can zero out future projections.
9. **`is_rookie_scale` is conservative and current-year only.** It reliably catches 2025 first-round deals but defaults to False for years 2-4 of a rookie deal (which pay a higher, non-scale amount); precise detection needs each player's draft year. The field is currently informational.
10. **Win curve absolute multiplier at 42 wins is ~1.2x**, not the ideal 1.5-1.8x — the *derivative* peaks there (correct), but the absolute value is lower than desired. Fixable by shifting the midpoint or raising the floor; deferred.
11. **`CURRENT_SEASON_START = 2025` and all cap/rookie-scale/league-average numbers are season-stamped.** They must be bumped each league year.

---

## Remaining Roadmap

### Phase 6: ML Trade History Model (NEXT)
Train a model on historical NBA trades to calibrate the hand-tuned constants
against real outcomes — the phase where the system learns from data instead of
intuition.

**What's needed:**
- Historical trade dataset (last 5-10 seasons): players, salaries, team records, and outcomes (win delta, playoff results 1-3 years post-trade).
- Feature engineering: compute the same features the current model uses (EPM/equivalent at time of trade, contract details, team context) for each historical trade.
- Training target: win-delta, surplus-predicted-vs-actual, or a composite.
- Model: likely gradient-boosted trees (XGBoost/LightGBM), either calibrating the existing constants or learning trade outcomes end-to-end.
- Output: calibrated weights for `EPM_TO_WINS_FACTOR`, win curve params, `TIMELINE_LAMBDA`, positional/spacing caps, discount rate, aging rates, and the grader's `SCORE_SCALING_FACTOR` / damping threshold.

**Key questions to decide:**
- Where to source historical trade data (Basketball Reference transaction logs, NBA.com, manual collection)?
- How far back to go (CBA rules changed significantly in 2023-24)?
- What's the training target?
- Calibrate the existing formula's constants, or learn a new model end-to-end?

### Phase 8: Trade Builder / Suggestion Engine
Given a target player and your team, find the optimal legal package that
minimizes your cost while maximizing the likelihood the other team accepts.

### Phase 10: Web App
Frontend for the trade analyzer.

---

## How to Resume in a New Chat

1. Share this handoff document.
2. Say "I'm working on Phase 6: ML trade history model."
3. Reference `CLAUDE.md` in the repo for additional architecture context.
4. Run `uv run pytest` to confirm all 291 tests pass, and `uv run python scripts/validate_trades.py` for the smoke test, before starting new work.
5. Create a new feature branch: `git checkout -b feat/trade-history-ml`.
6. The detailed Phase 6 implementation plan still needs to be drafted — the scope above has the key questions, not a step-by-step plan.
