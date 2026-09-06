#!/bin/bash
# Auto-commits and pushes the Obsidian vault repo (obsidian-ws/Documents)
# whenever Obsidian is open and there are pending changes. No AI involved —
# purely mechanical, mirrors index_while_vscode_open.sh's structure but gates
# on Obsidian.app instead of VS Code and targets the vault repo instead of
# session-trace-data.

set -u

export PATH="/opt/homebrew/bin:$PATH"

VAULT_REPO="/Users/thanhndv212/Develop/obsidian-ws/Documents"
LOG_FILE="/Users/thanhndv212/Develop/odysseus/data/logs/vault_auto_commit.log"

mkdir -p "$(dirname "$LOG_FILE")"

if [ -f "$LOG_FILE" ] && [ "$(wc -c < "$LOG_FILE")" -gt 5000000 ]; then
  tail -n 2000 "$LOG_FILE" > "$LOG_FILE.tmp" && mv "$LOG_FILE.tmp" "$LOG_FILE"
fi

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG_FILE"
}

if ! pgrep -f "Obsidian.app" > /dev/null 2>&1; then
  exit 0  # Obsidian not open — silent no-op
fi

cd "$VAULT_REPO" || { log "ERROR: cannot cd to $VAULT_REPO"; exit 1; }

if [ -z "$(git status --porcelain)" ]; then
  exit 0  # nothing changed — silent no-op, don't spam the log every 15 min
fi

log "Obsidian is open, vault has pending changes — committing"

CHANGED_COUNT=$(git status --porcelain | wc -l | tr -d ' ')

git add -A >> "$LOG_FILE" 2>&1
git commit -m "vault: auto-sync ($CHANGED_COUNT file(s) changed)" >> "$LOG_FILE" 2>&1
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
