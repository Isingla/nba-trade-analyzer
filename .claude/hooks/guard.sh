#!/usr/bin/env bash
# .claude/hooks/guard.sh
# PreToolUse (matcher: Bash) — block obviously destructive commands.
# Exit 2 + stderr tells Claude the command was blocked and why, so it
# reconsiders instead of running it. This is a speed-bump, not a sandbox:
# it catches common foot-guns, not every possible spelling.
set -uo pipefail

input=$(cat)
cmd=$(printf '%s' "$input" | sed -n 's/.*"command"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')

deny() {
  printf 'Blocked by guard hook (%s). Reconsider before running:\n  %s\n' "$1" "$cmd" >&2
  exit 2
}

# Recursive force delete, in the common flag orders.
case "$cmd" in
  *"rm -rf"*|*"rm -fr"*|*"rm -r -f"*|*"rm -f -r"*) deny "recursive force delete" ;;
esac

# Git operations that throw away work or rewrite shared history.
case "$cmd" in
  *"git reset --hard"*) deny "hard reset — discards uncommitted changes" ;;
  *"git clean -f"*|*"git clean -xf"*|*"git clean -fd"*|*"git clean -xfd"*) deny "git clean — deletes untracked files" ;;
esac

# Force push, but allow the safer --force-with-lease.
case "$cmd" in
  *"git push"*)
    case "$cmd" in
      *"force-with-lease"*) : ;;                       # safe, allow
      *"--force"*|*" -f "*|*" -f") deny "git force push" ;;
    esac
    ;;
esac

# Destroying the secrets file.
case "$cmd" in
  *"rm "*".env"*|*"> .env"*|*">.env"*) deny "modifying/removing .env" ;;
esac

# Disk-level and permission foot-guns.
case "$cmd" in
  *"chmod -R 777"*) deny "world-writable recursive chmod" ;;
  *"mkfs"*|*"dd if="*) deny "disk-level operation" ;;
esac

exit 0
