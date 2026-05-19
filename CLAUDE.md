# NBA Trade Analyzer

An AI-powered tool that evaluates NBA trades between two teams,
combining salary cap validation, player impact modeling, and
team-context-aware value estimation.

## Status

Fresh uv scaffold. No domain code yet. Build from scratch.

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
│   ├── salaries.py     # contract/salary data (scraping)
│   └── cache.py        # local JSON/SQLite cache layer
├── models/             # pydantic domain models
│   ├── player.py       # Player, Contract
│   ├── team.py         # Team, Roster
│   └── trade.py        # Trade, TradeResult
├── engine/             # core evaluation logic
│   ├── salary_rules.py # CBA trade legality (basic matching)
│   ├── valuation.py    # player surplus value calculation
│   ├── team_context.py # win curve, roster fit, timeline
│   └── grader.py       # final trade grading (letter grades)
├── cli.py              # typer CLI entry point
└── __init__.py
```

## Data Sources

- **Player stats**: `nba_api` package (NBA.com endpoints)
- **Impact metrics**: DARKO projections (public Google Sheet),
  Basketball Reference (BPM, VORP, WS)
- **Salaries**: Spotrac or HoopsHype (scrape)
- **Draft pick values**: historical WAR-by-pick exponential decay curves

## Commands

```powershell
uv sync                          # install/lock deps into .venv
uv add <package>                 # add a runtime dependency
uv add --dev <package>           # add a dev dependency
uv run nba-trade-analyzer        # run the CLI
uv run pytest                    # run tests
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
- [ ] 4. Valuation — surplus value per player, draft pick value curves
- [ ] 5. Team context — win curve, roster fit, timeline scoring
- [ ] 6. Trade history model — train on historical trades to learn market
       realism and predicted surplus
- [ ] 7. Trade grader — combine heuristic valuation, team context, and
       model predictions into letter grades per team
- [ ] 8. Trade builder — given a target player and your team, find the
       optimal legal package that minimizes your cost while maximizing
       likelihood the other team accepts
- [ ] 9. CLI — typer interface: grade trades + build trades
- [ ] 10. Web app (future phase)