#!/bin/bash
# Daily non-AI chat-session index: runs the session-look-up CLI indexer
# (VS Code Copilot / Claude Code / Codex / OpenCode stores -> trace tree in
# session-trace-data) with --no-ai, then commits and pushes the trace repo.
#
# This is the step that discovers NEW chats — index_while_vscode_open.sh only
# converts already-captured traces to CSF. Scheduled at 05:30 so the 06:00
# run_daily_pipeline.sh picks up the fresh traces.
#
# Runs under launchd (see ~/Library/LaunchAgents/com.thanhndv212.session-daily-index.plist),
# which does not source the user's shell profile, so all paths are absolute here.

set -u

# launchd runs with a minimal PATH that lacks Homebrew's bin dir, which is where node lives.
export PATH="/opt/homebrew/bin:$PATH"

INDEXER="/Users/thanhndv212/Develop/session-knowledge/extensions/session-look-up/cli/index.js"
TRACE_REPO="/Users/thanhndv212/Develop/session-trace-data"
NODE="/opt/homebrew/bin/node"
LOG_FILE="/Users/thanhndv212/Develop/odysseus/data/logs/daily_index.log"

mkdir -p "$(dirname "$LOG_FILE")"

if [ -f "$LOG_FILE" ] && [ "$(wc -c < "$LOG_FILE")" -gt 5000000 ]; then
  tail -n 2000 "$LOG_FILE" > "$LOG_FILE.tmp" && mv "$LOG_FILE.tmp" "$LOG_FILE"
fi

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG_FILE"
}

log "=== Daily index started ==="

cd "$TRACE_REPO" || { log "ERROR: cannot cd to $TRACE_REPO"; exit 1; }

# Pull first so the commit lands on top of other machines' syncs.
if ! git pull --rebase --autostash >> "$LOG_FILE" 2>&1; then
  log "ERROR: pull failed, aborting so we don't commit on a diverged tree"
  exit 1
fi

# Indexer prints every session title; keep only the summary lines in the log.
"$NODE" "$INDEXER" --traces-dir "$TRACE_REPO" --no-ai 2>&1 \
  | grep -E "^(New:|Written to|Error)|indexed$|refreshed$" >> "$LOG_FILE"
status=${PIPESTATUS[0]}
log "indexer exited $status"
[ "$status" -eq 0 ] || exit 1

if [ -z "$(git status --porcelain)" ]; then
  log "Nothing new to commit"
  exit 0
fi

MACHINE=$(ioreg -rd1 -c IOPlatformExpertDevice | awk -F'"' '/IOPlatformUUID/{print substr($4,1,8)}')
added=$(git status --porcelain | grep -c '^??')
updated=$(git status --porcelain | grep -c '^ M')

git add -A >> "$LOG_FILE" 2>&1
git commit -m "sync(${MACHINE}): daily index, +${added} new, ${updated} updated" >> "$LOG_FILE" 2>&1
log "committed (+$added new, $updated updated)"

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
