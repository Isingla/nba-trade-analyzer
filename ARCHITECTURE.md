# Architecture — NBA Trade Analyzer

High-level map of how the system fits together, so a fresh session can orient in a
few hundred tokens instead of re-reading the whole tree. Kept deliberately
high-level: modules, boundaries, and where things live — not line-by-line. Update it
when the *shape* changes, not on every feature.

> Items marked (?) are inferred — correct them to match the real repo.

## What it is

A Python CLI that evaluates NBA trades against CBA salary rules and a player-
valuation model, and reports whether a trade is legal and who "wins" it.

## Data flow

```
external sources            ingestion           domain models         engines                 CLI
─────────────────           ─────────           ─────────────         ───────                 ───
Basketball-Reference  ─►  scraper/fetch  ─►  pydantic models  ─┬─►  CBA salary-rules engine ─┐
Dunks & Threes (EPM)  ─►  (httpx + pandas)                     └─►  valuation engine        ─┴─►  typer commands
                                                                    (tanh curve + picks)          (e.g. `roster`, trade eval)
```

Raw data comes in, gets normalized into validated pydantic models, and the two
engines consume those models independently. The CLI is the entry point that wires
them together and presents results.

## Major components

**Ingestion / data pipeline (?)** — fetches and normalizes player, salary, and
metric data from Basketball-Reference and the EPM source. Uses `httpx` for fetching
and `pandas` for wrangling. Output: clean data ready to become models.

**Domain models** — pydantic models for the structured entities (players, salaries,
contracts, trades, picks). The single source of truth for shape/validation;
everything downstream takes these, not loose dicts.

**CBA salary-rules engine** — encodes the collective-bargaining-agreement logic:
salary matching, cap/apron checks, trade legality. Knows the rules; does not know
about player value.

**Valuation engine** — scores players and trades (tanh value curve, draft-pick
valuation, positional-share metrics, salary escalation). Knows value; does not know
about CBA legality.

**CLI** — `typer` commands (e.g. `roster`) that orchestrate the above and format
output for the terminal.

## The boundary that matters

The **salary-rules engine and the valuation engine are intentionally decoupled.**
One answers "is this trade legal?", the other answers "is this trade good?" They
should not import each other or share state. The CLI composes them; they don't reach
into each other. Preserve this — it's the core architectural invariant.

## Where things live (?)

```
src/<package>/
├── <ingestion>/     # scraper / data fetching + normalization
├── models/          # pydantic domain models
├── cba/             # salary-rules engine
├── valuation/       # valuation engine (curve, picks, escalation)
└── cli/             # typer commands
tests/               # pytest; mirrors the module layout
```

(Replace with the actual package/module names.)

## External data sources

- **Basketball-Reference** — player, salary, contract data (scraped).
- **Dunks & Threes (EPM)** — advanced metric; replacing the older NET_RATING input.

Both are stubbed in tests — no live network calls in the suite.

## Stack & conventions

Python 3.12+ · uv (not pip) · pydantic · typer · httpx · pandas · pytest · ruff.
Feature branches → squash-merge PRs. See `CLAUDE.md` for the full rule set.

## Extending it

- New data field → add/extend the pydantic model first, then thread it through.
- New valuation factor → lives in the valuation engine only; must not touch CBA logic.
- New CBA rule → lives in the salary-rules engine only; must not touch valuation.
- New behavior → add tests mirroring the existing layout.

## Maintenance

This file is a *map*, not a mirror. If you find yourself updating it for a small
change, it's gotten too detailed — keep it at the level of "what talks to what."
Re-read and prune it when the module structure or the data flow actually changes.
