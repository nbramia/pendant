#!/bin/sh
# One-time setup of a checkout: use the tracked hooks (shared by every worktree of it) and link
# the agent instructions in. Safe to rerun.
#   PENDANT_PRIVATE_AGENTS   your private AGENTS.md (default ~/.config/pendant/AGENTS.md)
set -e
cd "$(git rev-parse --show-toplevel)"
git config core.hooksPath tools/git-hooks
sh tools/git-hooks/post-checkout
echo "hooks: tools/git-hooks; AGENTS.md -> $(readlink AGENTS.md 2>/dev/null || echo 'copied from AGENTS.example.md')"
