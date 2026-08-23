# SPEC — Clean Engine (EPM → WAR → dollars)

> Status: SIGNED OFF (Russ, 08-19/20) with all constants ruled. This revision
> (08-23) folds the two rulings into the text: pointsPerWin 36.7 and
> dollarsPerWin = payroll ÷ production (gross, DARKO construction), which
> supersedes the $3.5M family and rewrites gate 4 as a derivation.
> Replaces the patched chain (4.2 / damper / tanh / full-season pin).

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

| constant        | value                                                        | tier     | derivation                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| --------------- | ------------------------------------------------------------ | -------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| R (replacement) | −2.1                                                         | measured | 25-26 sub-$2.5M non-rookie cohort (55 players), three-cut ladder −1.31 / −1.78 / −2.17, equal-weight −1.75, −0.35 disclosed selection/survivorship adjustment sized from ladder spacing. Ruled (Russ, 08-14). Bracket-verified both sides 08-18: signed sub-$2.5M cohort avg −1.75 (3 join methods agree ±0.03) / unsigned exit-pool median −2.75 (n=120); −2.1 sits at the market boundary (best castoff p75 −1.62 ≈ signed avg). Era-stable (2016-17 same bracket; 4-era proxy spread 0.24, no trend) — not annually remeasured. |
| pace            | 99.4 (2025-26)                                               | measured | BBRef league per-game table, read 2026-08-18. Per-season, remeasured annually (24-25: 98.8 · 23-24: 98.5). Replaces the 100 approximation.                                                                                                                                                                                                                                                                                                                                                                                         |
| pointsPerWin    | 36.7                                                         | measured | (PS/G − PA/G) × 82 vs wins, 90 modern team-seasons, R² 0.956, CI [35.1, 38.5], free and through-origin specs agree. Era-scoped: identical method on 2015-17 reproduces 31.7 [29.9, 33.8] — the textbook ~30 was a prior era's price. Remeasured annually. RULED (Russ, 08-20): 36.7, average-cost, consistent with the gross dollarsPerWin construction. 34.3 documented as the pythagorean marginal bracket (4×PPG/k, k=13.5).                                                                                                    |
| dollarsPerWin   | $7.86M (25-26) → $8.38M (26-27, cap-scaled ×164.961/154.647) | derived  | RULED (Russ, 08-20): payroll ÷ production, gross (DARKO construction, darko.app/about/fair-salary). Slug-joined 25-26: matched payroll $5.325B ÷ 677 matched WAR (365 players) = $7.86M/win. Brackets: net-of-min $6.73M, incl.-injured-pay $8.37M. Supersedes $3.5M/$3,733,428 — retired: it passed the old league-total check only because the patched engine minted ~2.2× the wins (verified both formulas, same players: $4.65B vs $2.12B). Re-derived annually from payroll and league WAR.                                   |
| cap growth      | 8.0%/yr                                                      | measured | Certified caps 2021-22 → 2026-27 (NBA PR, Sportico second source), growth 10 / 10 / 3.36 / 10 / 6.67, arithmetic avg. Ruled (delegated, 08-14).                                                                                                                                                                                                                                                                                                                                                                                    |

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

| gate                                                               | expected                                                        | tolerance |
| ------------------------------------------------------------------ | --------------------------------------------------------------- | --------- |
| Kuzma actual 25-26 (EPM −1.28, 1,806 min)                          | 0.84 WAR / value $6.6M @ $7.86M/win / surplus −$13.6M           | ±0.1 WAR  |
| Wemby actual (EPM 8.74, 1,866 min, salary $16,868,246 DB-verified) | 11.41 WAR / value $95.7M @ $8.38M/win / surplus +$78.8M         | ±0.1 WAR  |
| +7 EPM @ 2,800 min                                                 | 14.38 WAR / value $120.5M @ 26-27 rate                          | ±0.1 WAR  |
| dollarsPerWin reproduction                                         | slug-joined payroll ÷ matched WAR returns $7.86M (25-26 inputs) | ±$0.1M    |
| ΣWAA on actual minutes                                             | ≈ 0                                                             | ±5 wins   |
| pointsPerWin reproduction                                          | refit on the 90-season dataset returns 36.7                     | ±0.3      |

Dollar anchors computed 08-23 at the ruled rates (values above the max
contract are expected and correct — the max is a price ceiling; DARKO's
published values show the same shape). The old league-total gate is
retired: at gross $/win, league value ≡ payroll by construction, so the
check becomes reproducing the derivation, not comparing to it. ΣWAA ≈ 0
and the pointsPerWin refit keep their teeth unchanged.

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
- Annually remeasured: pointsPerWin, pace, cap growth, dollarsPerWin
  (re-derived from payroll ÷ league WAR). R is era-stable and exempt (§2).

## 7. Industry context

The chain matches the published BPM/VORP architecture ((rate − replacement)
× exposure ÷ constant). Replacement −2.1 sits within the published range
(−2.0 BPM/DARKO · −2.3 PIPM · −2.7 LEBRON · −2.75 RAPTOR) and is
cohort-measured like RAPTOR's; our unsigned exit-pool median (−2.75)
independently reproduces RAPTOR's measured two-way value. 36.7 pts/win
lands between BPM's implied ~30 (fixed 2.7 rule, older scoring era — our
own 2015-17 fits reproduce it) and RAPTOR's implied ~41 (fixed 2019
multiplier). The dollar construction IS DARKO's published construction
(league payroll ÷ league production, gross, re-anchored per season,
values not capped at the max salary). No public NBA system publishes a
marginal win price. Known simplifications, both disclosed: linear
points-to-wins (§5) and flat $/win (no superstar premium). D&T publishes
no wins methodology of its own. Full comparison:
research-industry-war-methods-2026-08-18.md.
