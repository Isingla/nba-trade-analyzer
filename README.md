# NBA Trade Analyzer

An NBA trade evaluation engine that grades proposed trades using salary cap validation, player impact modeling, and team-context-aware value estimation. Combines CBA compliance checking, EPM-based player surplus value calculations, multi-year contract projections, and contextual adjustments for team situation.

## What It Does

Feed the analyzer a proposed trade between two teams and it will:

1. **Check salary legality** — validates the trade against the full 2025-26 CBA salary matching rules across all four cap tiers (under cap, over cap, first apron, second apron), including second-apron aggregation restrictions
2. **Value each player** — calculates surplus value using EPM (Estimated Plus-Minus) as the primary impact metric, with DARKO projections and adjusted NET_RATING as fallbacks
3. **Apply team context** — adjusts player value based on the acquiring team's win curve position, timeline alignment with the team core, positional fit, and spacing needs
4. **Project across contract years** — values the full remaining contract using EPM for the current season, DARKO for next season, and aging curve projections for years 3+, discounted for uncertainty
5. **Grade the trade** — determines which side wins and by how much, with basketball-language explanations referencing offensive/defensive splits, shooting profiles, and fit

## Quick Start

```bash
# requires python 3.12+
git clone https://github.com/Isingla/nba-trade-analyzer.git
cd nba-trade-analyzer

# install dependencies
pip install uv
uv sync

# run tests
uv run pytest

# run the calibration script (prints top-30 EPM valuations)
uv run python scripts/calibrate_epm.py

# run the end-to-end trade diagnostic
uv run python scripts/end_to_end_test.py

# run the 62-scenario stress test
uv run python scripts/stress_test_trades.py
```

## Architecture

```
src/nba_trade_analyzer/
├── data/
│   ├── cache.py          # Generic JSON cache with 24h TTL
│   ├── players.py        # nba_api stats pipeline (stats, 3PT splits, team records)
│   ├── epm.py            # EPM scraper from dunksandthrees.com
│   └── darko.py          # DARKO projections from public Google Sheet
├── models/
│   ├── player.py         # Player, Contract (frozen pydantic models)
│   ├── team.py           # CapStatus, Team, RosterEntry, Roster
│   ├── trade.py          # TradeAssets, Trade, TradeResult
│   ├── valuation.py      # PlayerValuation, YearProjection, MultiYearValuation
│   └── team_context.py   # TeamContextValuation
├── engine/
│   ├── constants.py      # All cap numbers, valuation params, aging curve rates
│   ├── salary_rules.py   # CBA trade legality checker (all 4 tiers)
│   ├── valuation.py      # Surplus value calculator (single + multi-year)
│   ├── draft_picks.py    # Pick value curves by draft position
│   ├── team_context.py   # Win curve, timeline, positional fit, spacing
│   └── aging_curve.py    # EPM aging projections by age bracket
scripts/
├── calibrate_epm.py      # Top-30 valuation output for factor tuning
├── end_to_end_test.py    # 4 real trade scenarios with full breakdowns
└── stress_test_trades.py # 62 scenarios across all system components
```

## How Valuation Works

**Player impact → Wins added → Dollar value → Subtract salary → Surplus**

The pipeline:

1. **EPM** (primary) measures points per 100 possessions attributable to the individual player, isolated from teammate and opponent effects via RAPM. DARKO (secondary) provides forward-looking projections. NET_RATING (fallback) is used when neither is available.

2. **Wins added** converts EPM to wins using a calibrated factor (EPM_TO_WINS_FACTOR = 4.2), scaled by minutes fraction and compressed through a tanh curve to cap outliers at MAX_WINS_ADDED = 20.

3. **Dollar value** multiplies wins added by DOLLARS_PER_WIN ($3.5M), adjusted by the acquiring team's win curve multiplier — contending teams near the playoff cutoff get a higher multiplier (~1.5-1.8x), tanking teams get a lower one (~0.4-0.5x).

4. **Team context** applies additive adjustments for timeline alignment (±15%), positional fit (±10%), and spacing (±8%), each capped to prevent wild swings.

5. **Multi-year projection** sums discounted surplus across all remaining contract years. Year 1 uses current EPM, year 2 uses DARKO projections, years 3+ apply aging curve decay. Future years are discounted at 12% annually.

## Data Sources

| Source | What | Update frequency |
|--------|------|------------------|
| [Dunks & Threes](https://dunksandthrees.com/epm) | EPM (current season impact) | Nightly |
| [DARKO](https://apanacea.com/darko) | Forward projections (Kalman filter) | Daily |
| [nba_api](https://github.com/swar/nba_api) | Player stats, team records, 3PT splits | Live |
| [CBA Guide](https://cbaguide.com) | Salary matching rules | As CBA changes |

All data is cached locally for 24 hours at `~/.nba_trade_analyzer/cache/`.

## CBA Rules Implemented

Full 2025-26 salary matching across four tiers:

- **Under cap**: Can absorb salary into cap space without matching
- **Over cap / below first apron**: Expanded TPE brackets — 200%+$250K (under $7.25M), +$8.527M ($7.25M-$29M), 125%+$250K (over $29M)
- **First apron**: 100% match, no cushion, no simultaneous sign-and-trade
- **Second apron**: 100% match, no aggregation (no 2-for-1 or 3-for-1 trades)

Cap numbers: $154.6M salary cap, $187.9M luxury tax, $195.9M first apron, $207.8M second apron.

## Roadmap

- [x] Phase 1: Player data pipeline
- [x] Phase 1.5: EPM & DARKO integration
- [x] Phase 2: Pydantic domain models
- [x] Phase 3: CBA salary rules engine
- [x] Phase 4: Valuation engine (tanh curve, draft picks)
- [x] Phase 5: Team context (win curve, timeline, positional fit, spacing)
- [x] Phase 5.5: Multi-year contract valuation with aging curves
- [ ] Phase 6: ML trade history model (calibrate against real trades)
- [ ] Phase 7: Trade grader (letter grades, prose explanations)
- [ ] Phase 8: Trade builder / suggestion engine
- [ ] Phase 9: CLI (typer)
- [ ] Phase 10: Web app

## Tech Stack

Python 3.12+, uv, pydantic, httpx, pandas, BeautifulSoup, nba_api, pytest, ruff

## Testing

```bash
# full test suite
uv run pytest

# specific module
uv run pytest tests/test_epm.py

# with verbose output
uv run pytest -v
```

127+ tests covering salary matching edge cases, valuation paths, metric fallback chains, team context components, and aging curve projections. The stress test script runs 62 scenarios across every combination of system components.

## License

MIT
