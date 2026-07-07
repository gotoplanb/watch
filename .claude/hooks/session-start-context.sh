#!/usr/bin/env bash
# SessionStart hook (startup|resume|compact): re-surface the build-mode operating context from
# CLAUDE.md so it survives a conversation compaction/summary. Stdout is injected into the model's
# context. Single source of truth: the "## Operating mode" section of CLAUDE.md — edit that, not this.
set -euo pipefail
root="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
md="$root/CLAUDE.md"
[ -f "$md" ] || exit 0
echo "[operating-mode reminder — re-read CLAUDE.md; key context that a summary may have dropped]"
awk '
  /^## Operating mode/ {inblk=1; print; next}
  inblk && /^## /       {exit}
  inblk                 {print}
' "$md"
