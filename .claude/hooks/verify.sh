#!/usr/bin/env bash
# .claude/hooks/verify.sh
# Stop gate: block the turn from ending while lint or tests fail.
# Exit 2 + stderr  -> Claude keeps working and sees the failure.
# Exit 0           -> turn is allowed to end.
set -uo pipefail

input=$(cat)

# Loop guard: if this stop was already triggered by a prior block, let it stop.
# (Claude Code also force-ends after 8 consecutive blocks, but this exits sooner.)
if printf '%s' "$input" | grep -q '"stop_hook_active":[[:space:]]*true'; then
  exit 0
fi

cd "${CLAUDE_PROJECT_DIR:-.}" || exit 0

# Only gate when there are uncommitted changes — skip pure Q&A / no-op turns
# so the suite doesn't run every time you just ask a question.
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  if git diff --quiet && git diff --cached --quiet; then
    exit 0
  fi
fi

if ! out=$(uv run ruff check . 2>&1); then
  printf 'Lint failed — fix before finishing:\n%s\n' "$out" >&2
  exit 2
fi

if ! out=$(uv run pytest -q 2>&1); then
  printf 'Tests failed — fix before finishing:\n%s\n' "$out" >&2
  exit 2
fi

exit 0
