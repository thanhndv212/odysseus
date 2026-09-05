#!/bin/bash
# Daily session-distillation pipeline: raw session capture -> CSF -> memory.json -> Obsidian notes.
#
# Runs under launchd (see ~/Library/LaunchAgents/com.thanhndv212.odysseus-session-pipeline.plist),
# which does not source the user's shell profile, so all paths are absolute here.
#
# Each step is independent: a failure in one (e.g. distill.py finding no usable LLM
# credentials) does not block the others from running.

set -u

# launchd runs with a minimal PATH that lacks Homebrew's bin dir, which is where
# node/npx live — without this, npx's `#!/usr/bin/env node` shebang fails silently.
export PATH="/opt/homebrew/bin:$PATH"

ODYSSEUS_DIR="/Users/thanhndv212/Develop/odysseus"
CSF_DIR="/Users/thanhndv212/Develop/canonical-session-format"
PYTHON="$ODYSSEUS_DIR/venv/bin/python3"
NPX="/opt/homebrew/bin/npx"
LOG_FILE="$ODYSSEUS_DIR/data/logs/session_pipeline.log"

mkdir -p "$(dirname "$LOG_FILE")"

# Keep the log from growing unbounded over months of daily runs.
if [ -f "$LOG_FILE" ] && [ "$(wc -c < "$LOG_FILE")" -gt 5000000 ]; then
  tail -n 2000 "$LOG_FILE" > "$LOG_FILE.tmp" && mv "$LOG_FILE.tmp" "$LOG_FILE"
fi

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG_FILE"
}

log "=== Pipeline run started ==="

log "--- migrate-traces.ts (raw session JSON -> CSF) ---"
( cd "$CSF_DIR" && "$NPX" tsx scripts/migrate-traces.ts ) >> "$LOG_FILE" 2>&1
log "migrate-traces.ts exited $?"

log "--- distill.py --all (CSF -> memory.json, LLM calls, default limit) ---"
( cd "$ODYSSEUS_DIR" && "$PYTHON" scripts/distill.py --all ) >> "$LOG_FILE" 2>&1
log "distill.py exited $?"

log "--- memory_to_notes.py (memory.json -> Obsidian project notes) ---"
( cd "$ODYSSEUS_DIR" && "$PYTHON" scripts/ingest/memory_to_notes.py ) >> "$LOG_FILE" 2>&1
log "memory_to_notes.py exited $?"

log "--- memory_to_notes.py --digest (Recent Activity rollup) ---"
( cd "$ODYSSEUS_DIR" && "$PYTHON" scripts/ingest/memory_to_notes.py --digest ) >> "$LOG_FILE" 2>&1
log "memory_to_notes.py --digest exited $?"

log "=== Pipeline run finished ==="
