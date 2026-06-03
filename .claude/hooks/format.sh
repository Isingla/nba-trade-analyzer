#!/usr/bin/env bash
# .claude/hooks/format.sh
# PostToolUse (matcher: Edit|Write) — tidy the file Claude just touched.
# Runs ruff's autofix + formatter on that one file so style stays clean
# continuously, instead of piling up for the Stop gate to catch.
# Always exits 0: this is cleanup, never a blocker.
set -uo pipefail

input=$(cat)

# Pull the edited file path out of the tool input JSON.
file=$(printf '%s' "$input" | sed -n 's/.*"file_path"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')

# Only act on Python files that actually exist.
case "$file" in
  *.py) ;;
  *) exit 0 ;;
esac
[ -f "$file" ] || exit 0

cd "${CLAUDE_PROJECT_DIR:-.}" || exit 0

uv run ruff check --fix "$file" >/dev/null 2>&1
uv run ruff format "$file"      >/dev/null 2>&1

exit 0
