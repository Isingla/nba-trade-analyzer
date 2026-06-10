# Draft Pick Port Spec

Portable specification of how `nba-trade-analyzer` (Python) **stores** and **values** draft
picks, so the DataBallr Contract Outlook (TypeScript) port can be parity-checked against this
engine. **Python is the oracle.** Every constant, formula, and expected value below is pulled
verbatim from the source on the date of writing; file/line refs are given throughout.

> **Anchored to `CURRENT_SEASON_START = 2026`** (league year rolling over July 1). All worked
> values below were regenerated against the 2026 anchor — a pick's `year_offset` is
> `max(0, pick.year - 2026)`, so the 2026 draft conveys at `year_offset 0` (no future discount).

Source files:
- `src/nba_trade_analyzer/models/draft_pick.py` — the `DraftPick` model
- `src/nba_trade_analyzer/models/trade.py` — `TradeAssets` (how picks attach to a trade)
- `src/nba_trade_analyzer/cli.py` — pick-string parsing (`parse_pick`, `PICK_PATTERN`)
- `src/nba_trade_analyzer/engine/draft_picks.py` — the valuation pipeline
- `src/nba_trade_analyzer/engine/constants.py` — the numeric constants
- `src/nba_trade_analyzer/engine/grader.py` / `engine/valuation.py` — where pick value enters the grade
- `tests/test_draft_picks.py`, `tests/test_draft_pick_model.py` — parity cases

---

## 1. STORAGE

### 1.1 The `DraftPick` model (`models/draft_pick.py`)

A **frozen / immutable** pydantic model (`model_config = ConfigDict(frozen=True)`, line 19) — once
constructed, fields can't be mutated.

| Field | Type | Default | Notes |
|---|---|---|---|
| `team` | `str` | *required* | 3-letter uppercase abbreviation of the **originating** team — whose record sets where the pick lands, and thus its value (lines 21-23). |
| `year` | `int` | *required* | Draft year. |
| `round` | `int` | *required* | 1 or 2. |
| `protections` | `str \| None` | `None` | **Free-text** human string, e.g. `"top-4 protected"`, `"lottery protected"`. `None` = unprotected (lines 27-29). |
| `swap` | `bool` | `False` | True = a pick-swap right, not an outright pick (lines 31-32). |
| `via_team` | `str \| None` | `None` | Set when previously traded: `team` is the current obligor, `via_team` the original routing team (lines 34-36). |

**Validators:**
- `team` **and** `via_team` (shared validator, lines 38-47): `None` is allowed (for `via_team`);
  otherwise must satisfy `len == 3 and isalpha() and isupper()`, else `ValueError`
  *"team must be a 3-letter uppercase abbreviation"*. (So `"lal"`, `"LA"`, `"LALX"`, `"L4L"` all reject.)
- `round` (lines 49-54): must be in `(1, 2)`, else `ValueError`.
- `year` (lines 56-64): must satisfy `EARLIEST_PICK_YEAR (2025) <= year <= LATEST_PICK_YEAR (2035)`
  (module constants, lines 14-15), else `ValueError`.

**Label format (`label` property + `__str__`, lines 66-85):**
```
ordinal = "1st" if round == 1 else "2nd"
text    = f"{year} {team} {ordinal}"
if swap:            text += " swap"
else:               text += f" ({protections or 'unprotected'})"
if via_team:        text += f" via {via_team}"
```
Examples (from `tests/test_draft_pick_model.py:68-86`):
- `DraftPick("LAL", 2027, 1, protections="top-4 protected")` → `"2027 LAL 1st (top-4 protected)"`
- `DraftPick("WAS", 2026, 2)` → `"2026 WAS 2nd (unprotected)"`
- `DraftPick("OKC", 2028, 1, swap=True)` → `"2028 OKC 1st swap"`
- `DraftPick("LAL", 2027, 1, via_team="NOP")` → `"2027 LAL 1st (unprotected) via NOP"`

**Known wart (flagged in the model docstring + valuation TODO):** `protections` is a free-text string
that is later keyed against a lookup table (`PROTECTION_DISCOUNT`, §2.4). The match is **exact and
case-sensitive** — `"Top-4 Protected"` or a stray space will *not* match `"top-4 protected"` and
silently falls back to `DEFAULT_PROTECTION_DISCOUNT = 0.70`. There is no structured protection
model. (The `swap` and `via_team` fields, by contrast, are real booleans/strings.)

### 1.2 How picks attach to a trade (`models/trade.py`)

`TradeAssets` (frozen, lines 11-26) is one side's outgoing bundle:
- `players: list[RosterEntry]` (default `[]`)
- `picks: list[DraftPick]` (default `[]`, lines 18-22)
- `total_salary` property (lines 24-26) sums **player** salaries only — **picks carry no salary** and
  do not contribute to salary-matching. Picks affect *valuation/grade*, not trade legality.

`Trade` holds `team_a` / `team_b` and `team_a_sends` / `team_b_sends` (each a `TradeAssets`).

### 1.3 CLI pick-string parsing (`cli.py`)

```python
PICK_PATTERN = re.compile(r"^(\d{4})\s+([A-Z]{3})\s+(1st|2nd)\s*(.*)$", re.IGNORECASE)   # line 88
```
`parse_pick(s)` (lines 96-122):
1. `s.strip()`, match against `PICK_PATTERN`. **No match → return `None`** (caller treats the token
   as a player name — this is how the CLI disambiguates picks from player names).
2. `year = int(group1)`; `team = group2.upper()`; `round = 1 if group3.lower()=="1st" else 2`;
   `protections = group4.strip() or None`.
3. If `protections.lower() == "unprotected"` → set `protections = None`.
4. Construct `DraftPick(...)`. On `ValidationError` (e.g. year out of 2025-2035) → print a yellow
   stderr warning *"looks like a pick but couldn't be parsed. Treating as player name."* and return
   `None`.

**Accepted string forms** (regex: 4-digit year, 3-letter team, `1st`/`2nd`, optional trailing text,
case-insensitive):
- `"2027 DET 1st unprotected"` → `DraftPick("DET", 2027, 1, protections=None)`
- `"2027 DAL 1st top-4 protected"` → `protections="top-4 protected"`
- `"2026 LAL 1st"` (no trailing text) → unprotected
- `"2026 lal 1ST lottery protected"` → team upper-cased to `"LAL"`, `protections="lottery protected"`

**Parser limitation to carry into the port:** `parse_pick` only fills `year/team/round/protections`.
It does **not** parse `swap` or `via_team` from the string — those can only be set via direct model
construction. (The `label` property can *render* swap/via, but the CLI can't *parse* them back.)

---

## 2. VALUATION (`engine/draft_picks.py`)

### 2.1 Constants (exact values, `engine/constants.py` + `draft_picks.py` module level)

| Constant | Value | Source |
|---|---|---|
| `PICK_VALUE_SCALE` | `80_000_000` | constants.py:149 |
| `PICK_VALUE_DECAY` | `0.065` | constants.py:152 |
| `PICK_VALUE_SECOND_ROUND_PENALTY` | `0.4` | constants.py:153 |
| `PICK_YEAR_DISCOUNT_RATE` | `0.05` | constants.py:161 |
| `PICK_REGRESSION_RATE` | `0.75` | constants.py:166 |
| `LEAGUE_MEAN_WINS` | `41.0` | constants.py:167 |
| `CURRENT_SEASON_START` | `2026` | constants.py:171 (rolls over July 1) |
| `PICK_SWAP_VALUE_FRACTION` | `0.40` | constants.py:172 |
| `FIRST_PICK` | `1` | draft_picks.py:25 |
| `LAST_PICK` | `60` | draft_picks.py:26 |
| `SECOND_ROUND_START` | `31` | draft_picks.py:27 |
| `FIRST_ROUND_PICKS` | `30` | draft_picks.py:28 |
| `PICK_MAP_WORST_WINS` | `15.0` | draft_picks.py:35 |
| `PICK_MAP_BEST_WINS` | `62.0` | draft_picks.py:36 |
| `DEFAULT_PROTECTION_DISCOUNT` | `0.70` | draft_picks.py:56 |

`PROTECTION_DISCOUNT` table (draft_picks.py:46-55):

| Key (`pick.protections`) | Multiplier |
|---|---|
| `None` | `1.00` |
| `"unprotected"` | `1.00` |
| `"top-1 protected"` | `0.95` |
| `"top-3 protected"` | `0.85` |
| `"top-4 protected"` | `0.80` |
| `"top-5 protected"` | `0.75` |
| `"top-10 protected"` | `0.55` |
| `"lottery protected"` (= top-14) | `0.45` |
| *anything else (unrecognized)* | `0.70` (default) |

### 2.2 `evaluate_draft_pick(pick, team_projected_wins)` — the full pipeline (lines 135-155)

```
1. year_offset   = max(0, pick.year - CURRENT_SEASON_START)              # 2026
2. position      = estimate_pick_position(team_projected_wins, year_offset)   # → [1, 30]
3. if pick.round == 2:  position += FIRST_ROUND_PICKS                     # → [31, 60]
4. position_slot = clamp(round(position), FIRST_PICK, LAST_PICK)          # [1, 60]
5. value         = calculate_pick_value(position_slot)                    # decay curve (+2nd-rd penalty)
6. value        *= 1.0 / (1.0 + PICK_YEAR_DISCOUNT_RATE) ** year_offset   # future-year discount
7. value        *= PROTECTION_DISCOUNT.get(pick.protections, DEFAULT)     # protection discount
8. if pick.swap:  value *= PICK_SWAP_VALUE_FRACTION                       # swap fraction
   return value
```

**Step 2 — `estimate_pick_position(wins, year_offset)` (lines 96-132):**
```
if year_offset > 0:
    regression_factor = 1.0 - PICK_REGRESSION_RATE ** year_offset
    adjusted_wins     = wins + (LEAGUE_MEAN_WINS - wins) * regression_factor
else:
    adjusted_wins     = wins
win_span = PICK_MAP_BEST_WINS - PICK_MAP_WORST_WINS                       # 62 - 15 = 47
position = 1.0 + (adjusted_wins - PICK_MAP_WORST_WINS) / win_span * (FIRST_ROUND_PICKS - 1)  # *29
return clamp(position, FIRST_PICK, FIRST_ROUND_PICKS)                     # [1.0, 30.0]
```
More projected wins ⇒ *later* (higher-numbered, less valuable) pick. Future years regress the record
toward the league mean (41) before mapping: `regression_factor` rises with `year_offset`, so a
60-win team projects to ~49 wins three years out. The map spans the **realistic** win range (15→62),
not 0→82.

**Step 5 — `calculate_pick_value(pick_number)` (lines 59-68):**
```
if pick_number < 1 or pick_number > 60: raise ValueError
curve = PICK_VALUE_SCALE * exp(-PICK_VALUE_DECAY * (pick_number - 1))     # 80M * e^(-0.065*(n-1))
if pick_number >= SECOND_ROUND_START (31): curve *= PICK_VALUE_SECOND_ROUND_PENALTY (0.4)
return curve
```

> **Parity gotcha — Python `round()` is banker's rounding (round-half-to-even).** Step 4 uses
> `round(position)`. Python: `round(0.5)=0`, `round(1.5)=2`, `round(2.5)=2`. JS `Math.round` rounds
> half **up** (`Math.round(2.5)=3`) and differs on negatives. Positions here are positive, and exact
> `.5` landings are rare, but to guarantee bit-for-bit parity the TS port must implement
> round-half-to-even (see §4.2).

### 2.3 Worked dollar-value table — `calculate_pick_value(slot)` at `year_offset = 0`

(No discounts; pure curve. Computed from the engine.)

| Slot | Value ($) |
|---|---|
| 1 | 80,000,000.00 |
| 5 | 61,684,126.86 |
| 10 | 44,568,468.94 |
| 14 | 34,364,588.66 |
| 20 | 23,266,780.99 |
| 30 | 12,146,324.75 |
| 31 | 4,552,770.29 ← second-round penalty (×0.4) kicks in |
| 45 | 1,832,600.33 |
| 60 | 691,242.83 |

### 2.4 Worked end-to-end example — **2028 first, 55-win team, top-4 protected**

| Step | Computation | Result |
|---|---|---|
| `year_offset` | `2028 - 2026` | `2` |
| `regression_factor` | `1 - 0.75² = 1 - 0.5625` | `0.4375` |
| `adjusted_wins` | `55 + (41 - 55) × 0.4375` | `48.875` |
| `position` | `1 + (48.875 - 15)/47 × 29` | `21.9016` |
| `position_slot` | `round(21.9016)` | `22` |
| base curve @ 22 | `80M × e^(-0.065×21)` | `20,430,454.08` |
| × year discount | `× 1/1.05² = × 0.907029` | `18,531,024.11` |
| × top-4 (×0.80) | `× 0.80` | **`14,824,819.29`** |

`evaluate_draft_pick(DraftPick(team=…, year=2028, round=1, protections="top-4 protected"), 55.0)`
= **`14,824,819.29`**. (The `team` abbreviation does not enter the math — only the
`team_projected_wins` the caller supplies; see §4.3.)

### 2.5 Documented limitations / TODOs (verbatim intent)

- **Sharp-cutoff protections (no convey-probability integral).** A *separate* helper
  `calculate_pick_value_with_protections(pick_number, protection_top)` (lines 71-89) models protection
  as a hard cutoff: if the realized slot `<= protection_top` it returns `0.0` (doesn't convey), else
  full value. Its TODO (lines 79-82): *"this is a sharp cutoff; a future version should integrate over
  the probability distribution of where the pick actually lands (e.g. weighted lottery odds ×
  convey/no-convey × value at each slot) instead of taking a single realized pick_number as input."*
  **⚠ Important for the port:** this cutoff function is **not** used by `evaluate_draft_pick` — the live
  pipeline uses the multiplicative `PROTECTION_DISCOUNT` table (step 7). `calculate_pick_value_with_protections`
  is exercised only by tests. Mirror the **table**, not the cutoff, for parity with the grade.
- **Linear expected-value slot map (no lottery variance).** `estimate_pick_position` docstring
  (lines 100-102): *"intentionally a simple linear expected-value map; the lottery adds variance
  around it, but the expectation is still monotonic with record."*
- **Free-text protection lookup.** `PROTECTION_DISCOUNT` TODO (lines 43-45): *"replace this lookup
  table with a structured PickProtection model and a probabilistic convey/no-convey integral over the
  team's standing distribution (Travis Chen's approach) once a pick-projection source exists."*
- **Swap fraction is a placeholder.** `PICK_SWAP_VALUE_FRACTION` comment (constants.py:169-172):
  *"A pick swap conveys only the difference between two teams' picks… 40% of the outright value is a
  rough placeholder until a probabilistic swap model (Travis Chen's approach) replaces it."*
- **July-1 rollover quirk.** Known quirk: between the draft (late June) and July 1, current-draft
  picks carry `year_offset 1` (~5% extra discount). Accepted — the proper fix is a continuous
  time-to-convey discount, not a draft-day rollover (which would split the season convention).

---

## 3. HOW PICK VALUE ENTERS THE GRADE

### 3.1 The DRAFT CAPITAL breakdown (`grader.py:_draft_capital`, lines 762-805)

Per acquired pick: `value = evaluate_draft_pick(pick, _pick_team_wins(pick, stats_df))`, accumulated
into `total`. Each pick gets a description line
`"{year} {team} {ordinal} (projected pick {number}) — ${value/1e6:.1f}M value"` where the projected
number comes from `_projected_pick_number` (lines 263-268, same slot math as the valuation). Returns
a `DraftCapitalBreakdown` (`models/grade.py:35`): `picks_description: list[str]`,
`total_pick_value: float`, `explanation: str`.

### 3.2 Units — **same surplus-value dollars as players (confirmed).**

Pick value is summed into the *same accumulators* as player contract surplus, in two places:
- `grader.py:_asset_totals` (lines 1002-1034): `for pick … : value = evaluate_draft_pick(…); surplus
  += value; gross += value` — alongside each player's `multi.total_contract_surplus`. The score is
  then `_score(surplus_delta, total_value)` = `clamp(round(50 + (surplus_delta/max(total_value,1)) ×
  SCORE_SCALING_FACTOR), 0, 100)` (lines 1037-1040).
- `valuation.py:evaluate_trade_assets` (lines 485-486): `total += evaluate_draft_pick(pick,
  _draft_pick_team_wins(pick, pick_stats_df))` — added to the same `total` as player surplus.

So a pick's value is **directly comparable to and additive with player contract-surplus dollars** —
the engine treats "$15M of draft capital" and "$15M of player surplus" as the same unit. For the
Contract Outlook display, that means a pick's `evaluate_draft_pick` dollar value sits on the same
axis as the player `contractSurplus`/`surplus` figures already shown.

---

## 4. PORT NOTES FOR DATABALLR (TypeScript)

### 4.1 Suggested TS interface mirroring `DraftPick`

```ts
export interface DraftPick {
  team: string;              // 3-letter UPPERCASE (validate: /^[A-Z]{3}$/)
  year: number;              // integer, 2025..2035 inclusive
  round: 1 | 2;
  protections: string | null; // free-text key into PROTECTION_DISCOUNT; null = unprotected
  swap: boolean;             // default false
  viaTeam: string | null;    // default null; same 3-letter rule when set
}

export function pickLabel(p: DraftPick): string {
  const ordinal = p.round === 1 ? "1st" : "2nd";
  let text = `${p.year} ${p.team} ${ordinal}`;
  text += p.swap ? " swap" : ` (${p.protections ?? "unprotected"})`;
  if (p.viaTeam != null) text += ` via ${p.viaTeam}`;
  return text;
}
```

### 4.2 The formulas as pure functions (constants inlined)

```ts
// --- Constants (engine/constants.py + draft_picks.py) ---
const PICK_VALUE_SCALE = 80_000_000;
const PICK_VALUE_DECAY = 0.065;
const PICK_VALUE_SECOND_ROUND_PENALTY = 0.4;
const PICK_YEAR_DISCOUNT_RATE = 0.05;
const PICK_REGRESSION_RATE = 0.75;
const PICK_SWAP_VALUE_FRACTION = 0.40;
const LEAGUE_MEAN_WINS = 41.0;
const CURRENT_SEASON_START = 2026;            // ⚠ stateful: bump each July 1 (see §4.3)
const FIRST_PICK = 1, LAST_PICK = 60, SECOND_ROUND_START = 31, FIRST_ROUND_PICKS = 30;
const PICK_MAP_WORST_WINS = 15.0, PICK_MAP_BEST_WINS = 62.0;
const DEFAULT_PROTECTION_DISCOUNT = 0.70;
const PROTECTION_DISCOUNT: Record<string, number> = {
  "unprotected": 1.0, "top-1 protected": 0.95, "top-3 protected": 0.85,
  "top-4 protected": 0.80, "top-5 protected": 0.75, "top-10 protected": 0.55,
  "lottery protected": 0.45,
};

// Python round() is round-half-to-even — Math.round is NOT. Replicate exactly.
function roundHalfToEven(x: number): number {
  const f = Math.floor(x), d = x - f;
  if (d < 0.5) return f;
  if (d > 0.5) return f + 1;
  return f % 2 === 0 ? f : f + 1;
}
const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v));

export function calculatePickValue(pickNumber: number): number {
  if (pickNumber < FIRST_PICK || pickNumber > LAST_PICK) throw new RangeError(`${pickNumber}`);
  let curve = PICK_VALUE_SCALE * Math.exp(-PICK_VALUE_DECAY * (pickNumber - 1));
  if (pickNumber >= SECOND_ROUND_START) curve *= PICK_VALUE_SECOND_ROUND_PENALTY;
  return curve;
}

export function estimatePickPosition(teamProjectedWins: number, yearOffset = 0): number {
  let adjusted = teamProjectedWins;
  if (yearOffset > 0) {
    const regression = 1.0 - PICK_REGRESSION_RATE ** yearOffset;
    adjusted = teamProjectedWins + (LEAGUE_MEAN_WINS - teamProjectedWins) * regression;
  }
  const winSpan = PICK_MAP_BEST_WINS - PICK_MAP_WORST_WINS;            // 47
  const position = 1.0 + ((adjusted - PICK_MAP_WORST_WINS) / winSpan) * (FIRST_ROUND_PICKS - 1);
  return clamp(position, FIRST_PICK, FIRST_ROUND_PICKS);              // [1, 30]
}

export function evaluateDraftPick(pick: DraftPick, teamProjectedWins: number): number {
  const yearOffset = Math.max(0, pick.year - CURRENT_SEASON_START);
  let position = estimatePickPosition(teamProjectedWins, yearOffset);
  if (pick.round === 2) position += FIRST_ROUND_PICKS;
  const slot = clamp(roundHalfToEven(position), FIRST_PICK, LAST_PICK);
  let value = calculatePickValue(slot);
  value *= 1.0 / (1.0 + PICK_YEAR_DISCOUNT_RATE) ** yearOffset;
  const disc = pick.protections == null
    ? 1.0
    : (PROTECTION_DISCOUNT[pick.protections] ?? DEFAULT_PROTECTION_DISCOUNT);
  value *= disc;
  if (pick.swap) value *= PICK_SWAP_VALUE_FRACTION;
  return value;
}
```
(Note `protections == null → 1.0` reproduces Python's `None: 1.0` table entry; `"unprotected"` also
maps to `1.0`. Keep the lookup **case-sensitive exact-match** to match the Python wart.)

### 4.3 Stateful / data-dependent inputs DataBallr must supply

- **`teamProjectedWins`** — the single biggest external dependency. In Python it's derived from the
  originating team's record: `grader._pick_team_wins` → `_team_wins(stats_df, pick.team)` and
  `valuation._draft_pick_team_wins` → `get_team_projected_wins(player_stats_df, pick.team)`. **Fallback
  rule to replicate:** when no stats are supplied, or the team isn't found / returns `0.0`, fall back
  to `LEAGUE_MEAN_WINS = 41.0` (valuation.py:413-416) — *not* 0 (0 would mis-read as a worst-team
  number-one slot). DataBallr must feed projected wins for `pick.team` from its own data layer and
  apply the same league-mean fallback.
- **`CURRENT_SEASON_START = 2026`** — hardcoded "current season" that sets `year_offset`. **Rolls over
  July 1** (new CBA league year, same as the cap numbers). It must be kept in sync with the Python
  engine each season (or sourced from the same config), or future-year discounts/regression will
  diverge.
- **Protection strings** — DataBallr must store the protection key in the **exact** spelling the table
  expects, or accept the `0.70` default. Consider normalizing on input.
- Everything else (`calculatePickValue`, `estimatePickPosition`, the discounts) is pure and
  deterministic given `(pick, teamProjectedWins)`.

### 4.4 Ready-made parity test cases

Curve (`calculatePickValue`, from `tests/test_draft_picks.py` + computed):

| Input | Expected | Test ref |
|---|---|---|
| `calculatePickValue(1)` | `80_000_000` (approx) | test_pick_1_is_approximately_80m |
| `calculatePickValue(30)` | `12,146,324.75` (in [12M,15M]) | test_pick_30_in_expected_range |
| `calculatePickValue(31)` | `4,552,770.29` (< pick30 × 0.5) | test_second_round_penalty_at_pick_31 |
| `calculatePickValue(60)` | `691,242.83` (in (0, 2M)) | test_pick_60_is_near_zero_but_positive |
| `calculatePickValue(0)` / `(61)` | throw | test_pick_value_rejects_zero / _61 |
| curve has 60 entries, all >0 & strictly decreasing | — | test_pick_value_curve_* |

Position (`estimatePickPosition`, computed from engine):

| Input | Expected position |
|---|---|
| `(15, 0)` | `1.0000` |
| `(20, 0)` | `4.0851` |
| `(41, 0)` (league mean) | `17.0426` (test asserts 14–18) |
| `(60, 0)` (contender) | `28.7660` (test asserts 28–30; "BUG-3 regression") |
| `(62, 0)` | `30.0000` |
| `(55, 2)` | `21.9016` |
| league-mean team: `(41, 0) == (41, 4)` | identical (no regression) |

End-to-end (`evaluateDraftPick`, computed from engine):

| Pick | wins | Expected $ |
|---|---|---|
| `{WAS, 2026, r1, unprotected}` | 20 | `65,826,772.64` (slot 4, `year_offset 0`, no discounts) |
| `{BOS, 2028, r1, "top-4 protected"}` | 55 | `14,824,819.29` (the §2.4 worked example, `year_offset 2`) |

Relational invariants the port should also satisfy (from `tests/test_draft_pick_model.py`):
- protected = unprotected × `PROTECTION_DISCOUNT[key]` (e.g. top-5 ⇒ ×0.75).
- unknown protection string ⇒ × `0.70`.
- future-year (yo=3, league-mean team) = now × `1/1.05³` (regression-neutral, isolates the discount).
- swap = outright × `0.40`.
- second-round pick value > 0 and < first-round × 0.3.
- a tanking team's first (wins 20) > a contender's first (wins 60) × 1.3.

> Parity workflow (Python = oracle, mirroring the CBA verification approach): run each TS function
> against these inputs and assert equality to the Python values within a small float tolerance
> (`1e-6` relative). Any divergence at a `.5` slot boundary almost certainly means `Math.round`
> slipped in where `roundHalfToEven` is required (§4.2).
