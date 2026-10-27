#!/bin/sh
# One command, one job: point git at the repo's own hooks so the "never commit on main" rule is enforced by
# the machine instead of by my memory (it failed twice on 2026-10-27 — see AUDIT.md F39).
#
#   sh tools/install_hooks.sh
set -e
root=$(git rev-parse --show-toplevel)
if [ ! -d "$root/tools/git-hooks" ]; then
  echo "no tools/git-hooks in $root — run this from the pc-eye repo root" >&2
  exit 1
fi
chmod +x "$root/tools/git-hooks/pre-commit" 2>/dev/null || true
git -C "$root" config core.hooksPath tools/git-hooks
echo "hooks: $root/tools/git-hooks (pre-commit refuses commits on main)"
git -C "$root" config --get core.hooksPath
