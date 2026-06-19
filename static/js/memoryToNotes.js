// memoryToNotes.js — Notes Sync tab for the Brain modal
// SSE consumer for memory-to-notes pipeline with live log + project breakdown.
//
// Imported lazily when the Notes tab is first clicked inside the Brain modal.
// Requires: the #memory-modal with a [data-memory-panel="notes"] panel.

const API_PREFIX = '/api/memory-to-notes';

let _running = false;
let _abortController = null;

// ── DOM refs (lazy) ──────────────────────────────────────────────────────────

function $statusBar()   { return document.getElementById('notes-sync-status'); }
function $btnRun()       { return document.getElementById('notes-sync-run'); }
function $btnDryRun()    { return document.getElementById('notes-sync-dry-run'); }
function $btnFull()      { return document.getElementById('notes-sync-full'); }
function $log()          { return document.getElementById('notes-sync-log'); }
function $progressBar()  { return document.getElementById('notes-sync-progress-bar'); }
function $progressText() { return document.getElementById('notes-sync-progress-text'); }
function $progressWrap() { return document.getElementById('notes-sync-progress-wrap'); }
function $projects()     { return document.getElementById('notes-sync-projects'); }

// ── Status bar ───────────────────────────────────────────────────────────────

async function loadStatus() {
  try {
    const res = await fetch(`${API_PREFIX}/status`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    renderStatus(data);
    renderProjects(data.projects || {});
  } catch (e) {
    console.warn('memoryToNotes: status fetch failed', e);
    if ($statusBar()) $statusBar().textContent = 'Status unavailable';
  }
}

function renderStatus(data) {
  const bar = $statusBar();
  if (!bar) return;
  if (data.last_run) {
    const dt = new Date(data.last_run).toLocaleString();
    const total = data.total_entries?.toLocaleString() || '?';
    const processed = data.total_entries_processed?.toLocaleString() || '0';
    bar.innerHTML = `Last run: <strong>${dt}</strong> &bull; ${processed} entries indexed &bull; ${total} total in memory`;
  } else {
    bar.textContent = 'Last run: — (never run)';
  }
}

// ── Project breakdown ────────────────────────────────────────────────────────

function renderProjects(projects) {
  const el = $projects();
  if (!el) return;
  const entries = Object.entries(projects);
  if (!entries.length) {
    el.innerHTML = '<div class="notes-sync-empty">No projects matched yet. Run a sync.</div>';
    return;
  }
  const maxCount = Math.max(1, ...entries.map(([, p]) => p.entries || 0));
  el.innerHTML = entries.map(([slug, p]) => {
    const count = p.entries || 0;
    const barW = Math.max(2, Math.round((count / maxCount) * 100));
    const name = p.name || slug;
    const noteIcon = p.has_note ? '📄' : '📝';
    return `<div class="notes-project-row">
      <span class="notes-project-name" title="${slug}">${noteIcon} ${name}</span>
      <span class="notes-project-bar-wrap"><span class="notes-project-bar" style="width:${barW}%"></span></span>
      <span class="notes-project-count">${count.toLocaleString()}</span>
    </div>`;
  }).join('');
}

// ── SSE runner ───────────────────────────────────────────────────────────────

async function startSync(mode) {
  if (_running) {
    logLine('⚠️ Already running. Wait for the current sync to finish.', 'warning');
    return;
  }
  _running = true;
  _abortController = new AbortController();
  disableButtons(true);
  clearLog();
  showProgress(true);

  logLine(`🚀 Starting ${mode} sync...`, 'info');

  try {
    const url = `${API_PREFIX}/start?mode=${encodeURIComponent(mode)}`;
    const res = await fetch(url, {
      method: 'POST',
      signal: _abortController.signal,
    });

    if (!res.ok) {
      const text = await res.text().catch(() => '');
      logLine(`❌ Server returned ${res.status}: ${text}`, 'error');
      return;
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop(); // keep incomplete line
      for (const line of lines) {
        if (line.startsWith('event: ')) {
          // SSE event type line — stored for next data line
          continue;
        }
        if (line.startsWith('data: ')) {
          try {
            const event = JSON.parse(line.slice(6));
            handleEvent(event);
          } catch (e) {
            // non-JSON data line, ignore
          }
        }
      }
    }
  } catch (e) {
    if (e.name === 'AbortError') {
      logLine('⏹ Sync cancelled.', 'warning');
    } else {
      logLine(`❌ Connection failed: ${e.message}`, 'error');
    }
  } finally {
    _running = false;
    _abortController = null;
    disableButtons(false);
    showProgress(false);
    await loadStatus();
  }
}

// ── Event handlers ───────────────────────────────────────────────────────────

function handleEvent(event) {
  const type = event.event;
  switch (type) {
    case 'phase':
      logLine(`📂 ${event.message}`, 'info');
      break;
    case 'progress':
      updateProgress(event.current, event.total, event.percent);
      break;
    case 'entry_matched':
      // too frequent to log individually
      break;
    case 'entry_skipped':
      logLine(`⏭ ${event.entry_id || '?'} — ${event.reason || 'skipped'}`, 'debug');
      break;
    case 'project_written': {
      const added = event.entries_added;
      const action = event.action;
      if (action === 'unchanged') {
        logLine(`📄 ${event.name || event.project} — no changes`, 'debug');
      } else if (added > 0) {
        logLine(`✅ ${event.name || event.project} — +${added} entries`, 'success');
      } else {
        logLine(`📄 ${event.name || event.project} — ${action}`, 'info');
      }
      // Refresh project breakdown
      loadStatus();
      break;
    }
    case 'summary':
      logLine(`✨ Done! ${event.new_entries || event.total_entries || '?'} entries across ${event.projects_updated || '?'} projects.`, 'success');
      break;
    case 'error':
      logLine(`❌ ${event.message}`, 'error');
      break;
    case 'done':
      logLine('✔ Stream ended.', 'info');
      break;
    default:
      logLine(`[${type}] ${JSON.stringify(event)}`, 'debug');
  }
}

// ── UI helpers ───────────────────────────────────────────────────────────────

function logLine(text, level = 'default') {
  const log = $log();
  if (!log) return;
  const div = document.createElement('div');
  div.className = `log-line log-line-${level}`;
  const ts = new Date().toLocaleTimeString();
  div.innerHTML = `<span class="log-timestamp">${ts}</span> ${text}`;
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
}

function clearLog() {
  const log = $log();
  if (log) log.innerHTML = '';
}

function updateProgress(current, total, percent) {
  const bar = $progressBar();
  const text = $progressText();
  const wrap = $progressWrap();
  if (wrap) wrap.style.display = '';
  if (bar) bar.style.width = `${percent}%`;
  if (text) text.textContent = `${(current || 0).toLocaleString()} / ${(total || 0).toLocaleString()} (${percent}%)`;
}

function showProgress(show) {
  const wrap = $progressWrap();
  if (wrap) wrap.style.display = show ? '' : 'none';
}

function disableButtons(disabled) {
  [$btnRun(), $btnDryRun(), $btnFull()].forEach(b => {
    if (b) b.disabled = disabled;
  });
}

function cancelSync() {
  if (_abortController) {
    _abortController.abort();
  }
}

// ── Init ─────────────────────────────────────────────────────────────────────

export function initNotesTab() {
  // Wire buttons
  $btnRun()?.addEventListener('click', () => startSync('incremental'));
  $btnDryRun()?.addEventListener('click', () => startSync('dry-run'));
  $btnFull()?.addEventListener('click', () => {
    if (confirm('Full rebuild will rewrite ALL project notes from scratch. Continue?')) {
      startSync('full');
    }
  });

  // Load initial status
  loadStatus();
}

// Auto-detect: if the notes panel is already visible (e.g. page loaded on Notes tab),
// initialize immediately.
if (document.readyState === 'complete') {
  const panel = document.querySelector('[data-memory-panel="notes"]');
  if (panel && !panel.classList.contains('hidden')) {
    initNotesTab();
  }
} else {
  window.addEventListener('load', () => {
    const panel = document.querySelector('[data-memory-panel="notes"]');
    if (panel && !panel.classList.contains('hidden')) {
      initNotesTab();
    }
  });
}
