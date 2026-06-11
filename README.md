# NBA Trade Analyzer

An AI-powered NBA trade evaluation engine with CBA enforcement, EPM-based valuations, and plain-English explanations.

Feed it a proposed trade between two teams and it checks the deal against the 2025-26 collective bargaining agreement, values every player and pick, weighs each side's team context, and returns a 0-100 grade per team with a basketball-language write-up.

## Demo

```
$ uv run nba-trade-analyzer grade --team-a DAL --team-b MEM \
    --sends-a "Klay Thompson" --sends-b "Santi Aldama"

════════════════════════════════════════════════════════════════
TRADE: Mavericks ↔ Grizzlies
════════════════════════════════════════════════════════════════

LEGALITY: ✅ Legal

─────────────────────────────────────────
MAVERICKS receive: Santi Aldama
Score: 50 / 100 — Fair Trade
─────────────────────────────────────────

  IMPACT
    -0.3 EPM  (Below Average)
    → Santi Aldama's teams outscore opponents by -0.3 points per
    100 possessions when he's on the court.

  CONTRACT
    -$11.0M/yr deficit  (Overpay)
    → Santi Aldama is paid $18M but producing like a $8M player
    — a notable overpay.

  WIN CURVE
    0.5x  (Rebuilding)
    → The Mavericks are a 26-win team. Each win they add is
    worth 47% less than it would be for a .500 team.

  TIMELINE
    +8%  (Good Fit)
    → Santi Aldama is 25. The Mavericks' core averages 27. He'll
    peak alongside their core.

  POSITIONAL FIT
    -10%  (Roster Overlap)
    → The Mavericks already commit 204 minutes per game at F-C.
    Adding another F-C creates a minutes crunch with Cooper
    Flagg and P.J. Washington.

  SPACING
    +0%  (Neutral)
    → Santi Aldama shoots 35% from three on 4.7 attempts per
    game. Minimal impact on a team with poor existing spacing.

  DRAFT CAPITAL
    → No draft picks involved.

  VERDICT
    The Mavericks add Santi Aldama, below average on impact at
    -0.3 EPM. Santi Aldama is paid $18M but producing like a $8M
    player — a notable overpay. On the positive side: Santi
    Aldama is 25. The Mavericks' core averages 27. He'll peak
    alongside their core.

─────────────────────────────────────────
GRIZZLIES receive: Klay Thompson
Score: 50 / 100 — Fair Trade
─────────────────────────────────────────

  IMPACT
    -0.8 EPM  (Below Average)
    → Klay Thompson's teams outscore opponents by -0.8 points
    per 100 possessions when he's on the court.

  CONTRACT
    -$15.8M/yr deficit  (Bad Contract)
    → Klay Thompson's $17M salary is a major drag on the cap —
    his production doesn't come close to justifying the cost.

  WIN CURVE
    0.5x  (Rebuilding)
    → The Grizzlies are a 26-win team. Each win they add is
    worth 46% less than it would be for a .500 team.

  TIMELINE
    -11%  (Timeline Mismatch)
    → Klay Thompson is 36. The Grizzlies' core averages 24.
    He'll be 39 when their younger core hits its prime — the
    timelines don't align.

  POSITIONAL FIT
    -10%  (Roster Overlap)
    → The Grizzlies already commit 212 minutes per game at G.
    Adding another G creates a minutes crunch with Lucas
    Williamson and Ja Morant.

  SPACING
    +2%  (Mild Boost)
    → Klay Thompson shoots 38% from three on 7.6 attempts per
    game. That helps a Grizzlies squad ranked 14th in spacing.

  DRAFT CAPITAL
    → No draft picks involved.

  VERDICT
    The Grizzlies add Klay Thompson, below average on impact at
    -0.8 EPM. Klay Thompson's $17M salary is a major drag on the
    cap — his production doesn't come close to justifying the
    cost. On the downside: Klay Thompson is 36. The Grizzlies'
    core averages 24. He'll be 39 when their younger core hits
    its prime — the timelines don't align.

════════════════════════════════════════════════════════════════
```

## Features

- **CBA salary matching** — all four cap tiers under 2025-26 rules (under cap, over cap, first apron, second apron), including second-apron aggregation restrictions
- **EPM-based player impact** — Estimated Plus-Minus from dunksandthrees.com as the primary metric, with DARKO and NET_RATING fallbacks
- **Multi-year contract valuation** — values every remaining year with age-bracket aging curves, discounted for uncertainty
- **Team context** — win curve, timeline alignment, positional fit, and spacing, each adjusting value to the acquiring team's situation
- **Draft pick valuation** — team-aware (the originating team's record sets where the pick lands), with future-year regression and protection discounts
- **Pick-ownership verification** — before grading, the analyzer confirms each team actually controls the picks it's sending, backed by a verified 30-team registry (342 picks) with documented gaps for conditional/multi-team picks; an unowned pick is rejected with the real owner, a gapped pick warns but still grades
- **Trade grader** — 0-100 score per team with seven metric breakdowns and a plain-English verdict
- **CLI** — `grade` a trade and `lookup` a player from the terminal
- **405 tests** plus a 5-trade validation smoke test

## Architecture

```
src/nba_trade_analyzer/
├── cli.py                # typer entry point (grade + lookup)
├── report.py             # terminal report renderer
├── teams.py              # team abbreviation resolution
├── pick_ownership.py     # draft-pick ownership registry, verify(), trade integration
├── data/
│   ├── cache.py          # JSON file cache, 24h TTL
│   ├── players.py        # nba_api stats pipeline
│   ├── epm.py            # EPM scraper (dunksandthrees.com)
│   ├── darko.py          # DARKO projections (Google Sheet)
│   └── salaries.py       # Basketball Reference contract scraper
├── models/
│   ├── player.py         # Player, Contract
│   ├── team.py           # Team, Roster, CapStatus
│   ├── trade.py          # Trade, TradeAssets
│   ├── valuation.py      # PlayerValuation, MultiYearValuation
│   ├── team_context.py   # TeamContextValuation
│   ├── draft_pick.py     # structured DraftPick
│   └── grade.py          # TradeGrade, TeamGrade, breakdowns
├── engine/
│   ├── constants.py      # cap numbers, tunable parameters
│   ├── salary_rules.py   # CBA legality checker
│   ├── valuation.py      # surplus value (single + multi-year)
│   ├── draft_picks.py    # team-aware pick value curves
│   ├── team_context.py   # win curve, timeline, fit, spacing
│   ├── aging_curve.py    # EPM aging projections
│   └── grader.py         # scores, breakdowns, prose
scripts/
├── calibrate_epm.py      # top-30 valuation dump for tuning
├── grade_trades.py       # demo trades through the grader
├── validate_trades.py    # 5-trade smoke test
├── end_to_end_test.py    # full-pipeline trade diagnostics
├── stress_test_trades.py # 62 component scenarios
└── stress_test_multiyear.py  # 478 multi-year scenarios
```

## Usage

```powershell
# install dependencies (requires Python 3.12+)
uv sync

# grade a trade
uv run nba-trade-analyzer grade --team-a DAL --team-b MEM `
    --sends-a "Klay Thompson" --sends-b "Santi Aldama"

# trades can include picks and multi-asset packages
uv run nba-trade-analyzer grade --team-a DAL --team-b DET `
    --sends-a "Kyrie Irving" `
    --sends-b "Isaiah Stewart" --sends-b "Caris LeVert" `
    --sends-b "Ausar Thompson" --sends-b "2027 DET 1st unprotected"

# look up a single player's valuation
uv run nba-trade-analyzer lookup "Shai Gilgeous-Alexander"

# run the test suite
uv run pytest

# run the validation smoke test
uv run python scripts/validate_trades.py
```

## Pick Ownership

Before grading, the analyzer verifies that each team actually controls the picks it's sending. Ownership resolves against a verified 30-team registry (342 picks); conditional and multi-team picks that can't be pinned to a single owner are documented as gaps with their verbatim RealGM clauses. Three outcomes:

- **Not the owner** — the trade is rejected, naming the real controller (exit 1).
- **Gapped (indeterminate)** — a warning with the verbatim clause; the trade still grades.
- **No record** — the pick isn't in the registry at all (typo'd year, already-conveyed pick, invalid round) — a warning with the mirror's sync date; the trade still grades.

```powershell
# rejected — DAL doesn't control LAC's 2026 first; OKC does
uv run nba-trade-analyzer grade --team-a DAL --team-b OKC `
    --sends-a "2026 LAC 1st" --sends-b "2031 OKC 1st"
```
```
Trade rejected — invalid pick ownership:
  ✗ DAL cannot trade 2026 LAC 1st (unprotected): controlled by OKC.
```
(exit code 1)

```powershell
# gapped pick — warns with the conditional clause, still grades
uv run nba-trade-analyzer grade --team-a SAS --team-b OKC `
    --sends-a "2027 SAS 1st" --sends-b "2031 OKC 1st"
```
```
⚠ SAS 2027 SAS 1st (unprotected): ownership indeterminate (gapped pick) —
  "1-16 to SAC; 17-30 to OKC (via SAC)" [mirror synced 2026-06-10].

LEGALITY: ✅ Legal
... (full grade report follows)
```

```powershell
# --no-ownership-check skips verification — the escape hatch when the mirror
# lags reality (e.g. a real trade happened the registry hasn't absorbed yet)
uv run nba-trade-analyzer grade --team-a DAL --team-b OKC `
    --sends-a "2026 LAC 1st" --sends-b "2031 OKC 1st" --no-ownership-check
#   → grades normally; the ownership rejection is skipped
```

```powershell
# a pick with no registry record — warns and stamps the mirror's sync date, still grades
uv run nba-trade-analyzer grade --team-a DAL --team-b OKC `
    --sends-a "2025 DAL 1st" --sends-b "2031 OKC 1st"
```
```
⚠ DAL 2025 DAL 1st (unprotected): no record of this pick in the ownership registry —
  mirror synced 2026-06-10; verify manually or re-sync.
```

### From Python

```python
from nba_trade_analyzer.pick_ownership import get_default_registry

registry = get_default_registry()
registry.verify("LAC", 2026, 1)   # NotOwner(actual_owner="OKC")
registry.verify("WAS", 2026, 1)   # Verified(owner="WAS")
registry.verify("SAS", 2027, 1)   # Indeterminate(clause="1-16 to SAC; 17-30 to OKC (via SAC)")
```

`verify(..., swap=True)` resolves a pick-**swap** right to its holder. Pass the registry into `grade_trade(trade, ..., registry=registry)` to enforce ownership at the library level — there, unlike the CLI, a `NoRecord` pick is a hard rejection (the CLI deliberately softens it to a warning).

### Data provenance

The registry mirrors a hand-verified seed maintained in a companion project: every pick was read from both teams' RealGM pages, then run through load-time validators (no contradictory owners, no orphaned swaps) and a 30-team coverage census. ~166 conditional/multi-team picks are deliberately excluded and documented with verbatim clauses in `data/draft_picks/KNOWN_GAPS.md`. The mirror's sync date lives in the file headers and is stamped into every staleness warning, so a lagging mirror is self-diagnosing — re-copy from the source and re-run.

## Data Sources

| Source | What | Module |
|--------|------|--------|
| [Dunks & Threes](https://dunksandthrees.com/epm) | EPM (current-season impact, off/def splits, position) | `data/epm.py` |
| [DARKO](https://darko.app/) | Forward-looking DPM projections (Google Sheet export) | `data/darko.py` |
| [nba_api](https://github.com/swar/nba_api) | Player stats, team records, 3PT splits | `data/players.py` |
| [Basketball Reference](https://www.basketball-reference.com/contracts/players.html) | Contracts and salaries | `data/salaries.py` |

All sources are cached locally for 24 hours. The salary scraper falls back to a committed CSV snapshot if the live fetch fails, so the tool works offline.

## Grading Scale

The score is zero-sum: a 0-100 grade per team, where 50 is an even deal. One side's gain is the other's loss.

| Score | Verdict |
|-------|---------|
| 85-100 | Highway Robbery |
| 70-84 | Clear Win |
| 55-69 | Smart Deal |
| 45-54 | Fair Trade |
| 35-44 | Slight Overpay |
| 20-34 | Overpay |
| 0-19 | Fleeced |

## Built With

Python 3.12, pydantic, typer, httpx, pandas, pytest
