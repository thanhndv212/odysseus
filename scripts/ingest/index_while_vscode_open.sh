#!/bin/bash
# Runs the (non-AI) session-indexing step — raw JSON -> CSF via migrate-traces.ts —
# and commits+pushes the result, but ONLY while VS Code is actually open.
# Deliberately does NOT call distill.py (that's the AI/LLM step); this is just the
# mechanical indexing + git sync half of the pipeline, on a tighter cadence than the
# once-a-day full pipeline (see run_daily_pipeline.sh) so freshly captured sessions
# get committed while you're actively working, not just once at 6am.
#
# Triggered on an interval by launchd (see
# com.thanhndv212.odysseus-index-while-vscode.plist) rather than a VS Code hook,
# since launchd has no native "while process X is running" trigger — this script
# is the guard that makes each tick a no-op when VS Code isn't open.

set -u

# launchd runs with a minimal PATH that lacks Homebrew's bin dir, which is where
# node/npx live — without this, npx's `#!/usr/bin/env node` shebang fails silently.
export PATH="/opt/homebrew/bin:$PATH"

CSF_DIR="/Users/thanhndv212/Develop/canonical-session-format"
TRACE_REPO="/Users/thanhndv212/Develop/session-trace-data"
NPX="/opt/homebrew/bin/npx"
LOG_FILE="/Users/thanhndv212/Develop/odysseus/data/logs/index_while_vscode.log"

mkdir -p "$(dirname "$LOG_FILE")"

if [ -f "$LOG_FILE" ] && [ "$(wc -c < "$LOG_FILE")" -gt 5000000 ]; then
  tail -n 2000 "$LOG_FILE" > "$LOG_FILE.tmp" && mv "$LOG_FILE.tmp" "$LOG_FILE"
fi

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG_FILE"
}

if ! pgrep -f "Visual Studio Code.app" > /dev/null 2>&1; then
  exit 0  # VS Code not open — silent no-op, don't spam the log every 15 min
fi

log "VS Code is open — running index step"

( cd "$CSF_DIR" && "$NPX" tsx scripts/migrate-traces.ts ) >> "$LOG_FILE" 2>&1

cd "$TRACE_REPO" || { log "ERROR: cannot cd to $TRACE_REPO"; exit 1; }

if [ -z "$(git status --porcelain)" ]; then
  log "Nothing new to commit"
  exit 0
fi

git add -A >> "$LOG_FILE" 2>&1
git commit -m "index: convert new sessions to CSF" >> "$LOG_FILE" 2>&1
log "committed"

if git push >> "$LOG_FILE" 2>&1; then
  log "pushed"
else
  log "push rejected, retrying with pull --rebase"
  if git pull --rebase >> "$LOG_FILE" 2>&1 && git push >> "$LOG_FILE" 2>&1; then
    log "pushed after rebase"
  else
    log "ERROR: push still failing, leaving for next run"
  fi
fi
