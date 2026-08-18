# SPEC — Clean Engine (EPM → WAR → dollars)

> Status: DRAFT for Russ sign-off. 2026-08-18.
> Replaces the patched chain (4.2 / damper / tanh / full-season pin).
> One open ruling is flagged inline (§2, pointsPerWin).

## 1. The chain

    WAR     = (EPM − R) × (minutes × pace/48) / 100 ÷ pointsPerWin
    dollars = WAR × dollarsPerWin
    surplus = dollars − salary

| step | operation           | units in → out                                |
| ---- | ------------------- | --------------------------------------------- |
| 1    | EPM − R             | pts/100 poss → pts/100 poss above replacement |
| 2    | minutes × pace/48   | minutes → possessions played                  |
| 3    | (1) × (2) ÷ 100     | rate × exposure → marginal points             |
| 4    | (3) ÷ pointsPerWin  | points → wins (WAR)                           |
| 5    | (4) × dollarsPerWin | wins → dollars                                |

Replacement enters at step 1 (the rate), never after step 4: subtracting
replacement _wins_ at the end would scale the offset by each player's own
exposure incorrectly.

_Footnote: 48 = regulation game length in minutes (4 × 12). Pace is defined
as possessions per 48 minutes, so pace/48 = possessions per minute and
minutes × pace/48 = possessions played. Not a tuned constant — a unit
conversion. Overtime is ignored; pace's per-48 normalization absorbs it at
our precision._

## 2. Constants (provenance-tiered)

| constant        | value                | tier      | derivation                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| --------------- | -------------------- | --------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| R (replacement) | −2.1                 | measured  | 25-26 sub-$2.5M non-rookie cohort (55 players), three-cut ladder −1.31 / −1.78 / −2.17, equal-weight −1.75, −0.35 disclosed selection/survivorship adjustment sized from ladder spacing. Ruled (Russ, 08-14).                                                                                                                                                                                                                                                                                                                                                                                                |
| pace            | 99.4 (2025-26)       | measured  | BBRef league per-game table, read 2026-08-18. Per-season, remeasured annually (24-25: 98.8 · 23-24: 98.5). Replaces the 100 approximation.                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| pointsPerWin    | 36.7                 | measured  | (PS/G − PA/G) × 82 vs wins, 90 modern team-seasons, R² 0.956, CI [35.1, 38.5], free and through-origin specs agree. Era-scoped: identical method on 2015-17 reproduces 31.7 [29.9, 33.8] — the textbook ~30 was a prior era's price. Remeasured annually. **OPEN RULING:** 36.7 is the average cost of a point (league as it stands); the pythagorean marginal cost at .500 is 34.3 (4×PPG/k, k=13.5 fit on the same 90 seasons, Morey published 13.91). Recommendation: 36.7 as the engine constant, 34.3 documented as the marginal bracket. No public NBA system publishes a marginal win price (see §7). |
| dollarsPerWin   | $3,733,428 (2026-27) | validated | $3.5M market anchor scaled by cap. League-total check: Σ value $5.05B vs $5.37B payroll (≈ market-consistent; old engine read $2.64B).                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| cap growth      | 8.0%/yr              | measured  | Certified caps 2021-22 → 2026-27 (NBA PR, Sportico second source), growth 10 / 10 / 3.36 / 10 / 6.67, arithmetic avg. Ruled (delegated, 08-14).                                                                                                                                                                                                                                                                                                                                                                                                                                                              |

## 3. Deliberate absences

Each was scaffolding around one ~2×-inflated constant (the 4.2). Removed at
the root, none is needed:

- **Full-season pin (2,952 / 2,786):** no reference workload exists in the
  chain. A player's own minutes are his exposure. Changing a pin only ever
  rescaled a coefficient to keep outputs identical — it carried no
  basketball meaning.
- **The 4.2:** origin unknown, ~2× the dimensionally derivable value.
  Replaced by the explicit rate → points → wins chain.
- **The 0.40 damper:** a patch compressing the 4.2's inflation. Inflation
  gone, patch goes.
- **The tanh:** a second patch hiding absurd tails. Extreme outputs were a
  minutes-input problem, not a formula problem (§5).

## 4. Acceptance gates (all must pass before swap)

| gate                                                                     | expected                                           | tolerance |
| ------------------------------------------------------------------------ | -------------------------------------------------- | --------- |
| Kuzma actual 25-26 (EPM −1.28, 1,806 min)                                | 0.84 WAR / −$17.1M surplus                         | ±0.1 WAR  |
| Wemby actual (EPM 8.74, 1,866 min, salary $16,868,246 DB-verified 08-18) | 11.4 WAR / +$26M surplus                           | ±0.1 WAR  |
| +7 EPM @ 2,800 min                                                       | 14.4 WAR                                           | ±0.1 WAR  |
| League total                                                             | Σ value ≈ league payroll ($5.05B vs $5.37B family) | ±10%      |
| ΣWAA on actual minutes                                                   | ≈ 0                                                | ±5 wins   |
| pointsPerWin reproduction                                                | refit on the 90-season dataset returns 36.7        | ±0.3      |

Anchors restated at measured pace 99.4 (Wemby 11.48 → 11.41; all within
tolerance — executed 08-18).

## 5. Extreme-input policy

The engine never clamps or squashes outputs. Absurd WAR comes from absurd
_inputs_ (projected minutes nobody will play). The fix lives in the minutes
model (P4 lane: asymmetric governor + team normalization), not in the
formula. An output exceeding historical WAR ranges is flagged loudly, not
silenced.

Pythagorean nonlinearity (our measured k=13.5) is a documented future
consideration, deliberately deferred: it makes player value
team-context-dependent, which conflicts with contract pricing. The 34.3
marginal bracket (§2) is the same curve's slope at .500.

## 6. Data sources

- **EPM: Dunks & Threes Premium API — source of record** (actuals, with
  gp/mp). Ruled 2026-08-18 after verification: the free page carries an
  Expected/Actual toggle and the scraper was reading Expected (modeled)
  values — evidence: 67 never-played players incl. 2026 draftees, no gp
  field, stars regressed toward mean. Scrape demoted to fallback-only with
  a loud warning that it may serve Expected values.
- **Salary / cap: BBRef** (authority of record), Spotrac cross-check.
- **Standings: BBRef**, transcribed + checksummed (wins sum 1,230/season;
  PS/G means match published to 0.05).
- **Pace: BBRef** league per-game table, per-season, annual refresh.
- **CBA rules: cbaguide.com** (operative verb quoted, never asserted).
- Annually remeasured: pointsPerWin, pace, cap growth. Era-scoped by design.

## 7. Industry context

The chain matches the published BPM/VORP architecture ((rate − replacement)
× exposure ÷ constant). Replacement −2.1 sits within the published range
(−2.0 BPM/DARKO · −2.3 PIPM · −2.7 LEBRON · −2.75 RAPTOR) and is
cohort-measured like RAPTOR's. 36.7 pts/win lands between BPM's implied ~30
(fixed 2.7 rule, older scoring era — our own 2015-17 fits reproduce it) and
RAPTOR's implied ~41 (fixed 2019 multiplier). The average-cost, cap-scaled
dollar anchor follows DARKO's published construction (league payroll ÷
league production, re-anchored per season). No public NBA system publishes
a marginal win price. Known simplifications, both disclosed: linear
points-to-wins (§5) and flat $/win (no superstar premium). D&T publishes no
wins methodology of its own. Full comparison:
research-industry-war-methods-2026-08-18.md.
