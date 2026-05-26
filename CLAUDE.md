# NBA Trade Analyzer

An AI-powered tool that evaluates NBA trades between two teams,
combining salary cap validation, player impact modeling, and
team-context-aware value estimation.

## Status

Phases 1–5.5, 7, and 9 complete, plus a calibration sprint. The engine
fetches live data (EPM, DARKO, nba_api stats, Basketball Reference
salaries), checks CBA legality across all four cap tiers, values players
over their full contracts with aging curves, applies team context, and
grades trades 0-100 with plain-English breakdowns — all exposed through a
typer CLI (`grade` + `lookup`). 291 tests pass plus a 5-trade validation
smoke test. **Phase 6 (ML trade history model) is next.**

## Project Goals

- Build a trade evaluation engine that grades proposed NBA trades
  for both teams using player impact metrics, contract surplus value,
  and team context (win curve, roster fit, timeline).
- Ship as a CLI first, web app later.
- **Meta-goal**: author is leveling up on GitHub workflow (branching,
  PRs, issues, project boards) and Claude Code usage. Prefer clean
  architecture and good commit hygiene over speed.

## Tech Stack & Conventions

- **Language**: Python 3.12+
- **Package manager**: uv (never pip directly)
- **CLI**: typer
- **HTTP**: httpx (for NBA API, scraping salary data)
- **Data**: pandas for tabular work, pydantic for domain models
- **Testing**: pytest
- **Lint/format**: ruff
- **Types**: pyright

## Architecture

```
src/nba_trade_analyzer/
├── data/               # data fetching and caching
│   ├── players.py      # nba_api wrapper for player stats
│   ├── epm.py          # EPM scraper (dunksandthrees.com)
│   ├── darko.py        # DARKO projections (Google Sheet)
│   ├── salaries.py     # Basketball Reference contract scraper
│   └── cache.py        # local JSON cache layer (24h TTL)
├── models/             # pydantic domain models
│   ├── player.py       # Player, Contract
│   ├── team.py         # Team, Roster, CapStatus
│   ├── trade.py        # Trade, TradeAssets, TradeResult
│   ├── valuation.py    # PlayerValuation, MultiYearValuation
│   ├── team_context.py # TeamContextValuation
│   ├── draft_pick.py   # structured DraftPick
│   └── grade.py        # TradeGrade, TeamGrade, breakdowns
├── engine/             # core evaluation logic
│   ├── constants.py    # cap numbers + tunable parameters
│   ├── salary_rules.py # CBA trade legality (all 4 tiers)
│   ├── valuation.py    # surplus value (single + multi-year)
│   ├── draft_picks.py  # team-aware pick value curves
│   ├── team_context.py # win curve, fit, timeline, spacing
│   ├── aging_curve.py  # EPM aging projections
│   └── grader.py       # 0-100 scores, breakdowns, prose
├── cli.py              # typer CLI entry point (grade + lookup)
├── report.py           # terminal report renderer
├── teams.py            # team abbreviation resolution
└── __init__.py
```

## Data Sources

- **Player stats**: `nba_api` package (NBA.com endpoints)
- **Impact metrics**: EPM (dunksandthrees.com, primary), DARKO
  projections (public Google Sheet, secondary), NET_RATING (fallback)
- **Salaries**: Basketball Reference contracts page (scrape), with a
  committed CSV snapshot as an offline fallback
- **Draft pick values**: team-aware exponential decay curves
  (originating team's record sets the pick slot)

## Commands

```powershell
uv sync                          # install/lock deps into .venv
uv add <package>                 # add a runtime dependency
uv add --dev <package>           # add a dev dependency
uv run nba-trade-analyzer --help # CLI help (grade + lookup commands)
uv run pytest                    # run tests
uv run python scripts/validate_trades.py  # 5-trade smoke test
uv run ruff check                # lint
uv run ruff format               # format
```

## Git Workflow

- Feature branches for all work (e.g. `feat/player-data-pipeline`,
  `feat/salary-rules-engine`).
- Claude Code commits to the current branch — never switches branches.
- Conventional Commits: `feat:`, `fix:`, `chore:`, `docs:`,
  `refactor:`, `test:`.
- PRs with squash-and-merge into main. Reference issues in PR
  descriptions (e.g. `closes #3`).
- Each commit leaves the project in a working state.

## Phases

- [x] 1. Data pipeline — fetch player stats + salaries, cache locally
- [x] 2. Domain models — Player, Contract, Team, Trade (pydantic)
- [x] 3. Salary rules — CBA trade legality checker (all four apron tiers)
- [x] 4. Valuation — surplus value per player, draft pick value curves
- [x] 5. Team context — win curve, roster fit, timeline, spacing scoring
- [x] 5.5. Multi-year valuation — full-contract projection with aging curves
- [ ] 6. Trade history model — train on historical trades to learn market
       realism and predicted surplus **(NEXT)**
- [x] 7. Trade grader — combine heuristic valuation and team context into
       0-100 scores + plain-English breakdowns per team
- [ ] 8. Trade builder — given a target player and your team, find the
       optimal legal package that minimizes your cost while maximizing
       likelihood the other team accepts
- [x] 9. CLI — typer interface: `grade` a trade + `lookup` a player
- [ ] 10. Web app (future phase)

A **calibration sprint** followed Phase 9: EPM replacement-level offset,
position overrides, score scaling by multi-year magnitude, dual-negative
damping, current-roster filtering, and realistic pick-slot mapping.
See `NBA_TRADE_ANALYZER_HANDOFF_V3.md` for the full record.

## Known Limitations

1. **No ML calibration yet.** Every constant (EPM-to-wins factor, win
   curve, aging rates, damping thresholds) is hand-tuned against intuition
   and a handful of real players. Phase 6 replaces this with a model
   trained on historical trade outcomes.
2. **Salary escalation not modeled.** Multi-year valuation uses a flat
   current-year salary for all years; real contracts escalate ~5-8%/yr.
3. **Fragile scraping.** EPM parses a SvelteKit payload and salaries parse
   Basketball Reference markup — both break if the source format changes
   (salaries fall back to a committed CSV; EPM does not).
4. **No playoff or injury adjustment.** EPM is regular-season only, and
   beyond current-season GP there's no forward-looking availability model.
5. **Pick protections are a lookup table.** Protection discounts are a
   static map, not a probabilistic convey/no-convey integral over the
   team's standing distribution.
6. **0 MPG edge case.** A player with no current-season minutes falls back
   to a default starter load rather than a career-average projection.