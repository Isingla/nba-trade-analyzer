# NBA Trade Analyzer

CLI that evaluates NBA trades against CBA salary rules and a player-valuation engine.

## Commands
- Install / sync deps: `uv sync`  — **IMPORTANT: use uv, never pip.**
- Run the CLI: `uv run <entrypoint> ...`   # set the real entrypoint after running /init
- Tests: `uv run pytest`  (single: `uv run pytest path::test_name -x`)
- Lint: `uv run ruff check .`   Format: `uv run ruff format .`

## Conventions
- Prefer pydantic models over loose dicts for structured data.
- Keep the CBA salary-rules engine and the valuation engine decoupled — a change
  to one shouldn't require touching the other.
- New behavior gets a test. Match the existing module structure; don't reorganize
  files unprompted.

## Repo etiquette
- Feature branches off main; squash-merge PRs.
- **IMPORTANT: never add AI co-author or "Generated with" trailers to commits.**
- Run `ruff check` and `pytest` before claiming a task is done.

## Don't
- Don't add dependencies without asking.
- Don't refactor for style alone during a feature change — keep diffs reviewable.
- Don't hardcode data paths or API keys.
