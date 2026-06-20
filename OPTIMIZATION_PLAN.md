# Odysseus Optimization Plan

> **Created:** 2026-06-20  
> **Status:** Proposed  
> **Owner:** @thanhndv212  
> **Reviewed by:** @oracle (architectural validation)

---

## Executive Summary

Odysseus is a single-user macOS-native FastAPI + vanilla JS SPA that has grown without optimization. The biggest wins — CSS minification and memory.json pagination — can together cut page-load transfer by **~4MB** with ~5 hours of work. This document prioritizes work by impact/effort, explains each decision, and provides a tracking checklist.

**Expected total savings:** ~4.3MB page load reduction, 38→5 HTTP requests, ~2s faster cold boot, long-term DB health.

---

## Current State: Baseline Metrics

Measure these before starting any work to track progress.

| Metric | Current | Target | Phase |
|--------|---------|--------|-------|
| `style.css` size | 1.1MB (37,789 lines) | ~300KB | 1 |
| Page-load HTTP requests | ~40 (35 JS modules + 2 CSS + inline) | ~10 | 3 |
| Page-load transfer size | ~1.5MB (gzipped) | ~500KB | 1+3 |
| `/api/memory` response size | 3.2MB raw / ~300KB gzipped | ~10KB (paginated) | 2 |
| Cold boot time | ~3-5s | ~2-3s | 2 |
| `app.db` size | 3.1MB | Stable at ~2MB | 1 |
| `highlight.min.js` | 119KB eager | 0KB (lazy) | 1 |

**Commands to record baselines:**
```bash
# Page load
curl -s -o /dev/null -w '%{size_download} bytes, %{num_redirects} redirects\n' \
  -H 'Accept-Encoding: gzip' http://localhost:7000/static/style.css

# /api/memory
curl -s -o /dev/null -w 'size: %{size_download} bytes, time: %{time_total}s\n' \
  http://localhost:7000/api/memory

# SQLite
ls -lh data/app.db

# Boot time
time ./venv/bin/python3 -c "import app" 2>&1 | tail -5
```

---

## Roadmap

```
Phase 1 ── CSS minification ── 1 hr ── -700KB transfer
   ├── Lazy-load highlight.js ── 0.5 hr ── -119KB transfer
   └── SQLite maintenance ── 0.5 hr ── long-term health
         │
Phase 2 ── memory.json pagination ── 4 hr ── 3.2MB→50KB per request
   └── Lazy TTS/STT init ── 1 hr ── -1.5s cold boot
         │
Phase 3 ── JS bundling (esbuild) ── 4 hr ── 35→5 HTTP requests
         │
Phase 4 ── Split tool_implementations.py ── 3 hr ── dev productivity
         │
Phase 5 ── macOS App Lifecycle ── 3 hr ── proper shutdown UX
```

---

## Phase 1: Quick Wins (~2 hours)

### 1.1 CSS Minification

**What:** Minify `static/style.css` (1.1MB, 37,789 lines) using safe CSS minification — whitespace + comment removal only. No structural changes that could break specificity.

**Why:**
- Single biggest file transferred on every page load
- `_RevalidatingStatic` forces `Cache-Control: no-cache` on `.css` files — the full bytes are re-sent on every conditional 304 check
- 37,789 lines of CSS is ~98% whitespace/comments in formatted source
- Zero behavioral risk with safe minification (no rule merging, no selector rewriting)

**Expected:** 1.1MB → ~300-400KB (60-75% reduction)

**How:**
```bash
# One-time: npm install -D cssnano postcss
npx cssnano static/style.css > static/style.min.css
```
Then update `index.html` to load `style.min.css` instead of `style.css`. Keep `style.css` in git as the source of truth for editing.

**Verification:**
- Visual diff: load app before/after, spot-check 10 pages
- CSS parse check: `npx cssnano --no-minify static/style.min.css > /dev/null` (should not error)
- Size check: `ls -lh static/style.min.css`

**Files:**
- `static/style.css` — source (unchanged, kept for editing)
- `static/style.min.css` — new, committed
- `static/index.html`, `static/login.html`, `static/backgrounds.html` — update `<link>` href

**Alternative considered & rejected:**
- ✗ `cssnano` with aggressive preset (rule merging, selector deduplication) — too risky for 37K lines without test coverage
- ✗ CSS-in-JS or Tailwind migration — refactor, not optimization; wrong scope
- ✗ Build step with watch mode — unnecessary for a project with no other build steps; just run once and commit

---

### 1.2 Lazy-load highlight.js

**What:** Move `highlight.min.js` (119KB) from eager `<script defer>` in `index.html` to dynamic load-on-demand.

**Why:**
- `highlight.js` is only needed when the chat renders code blocks
- Currently loaded on login page, settings, and every other page — even pages with no code
- The other 4 heavy libs (xlsx, html2pdf, docx, mammoth) already use this pattern in `document.js:8288-8313`

**How:**
```javascript
// Replace <script defer src="/static/lib/highlight.min.js"> in index.html
// with lazy-load wrapper in codeRunner.js or chatRenderer.js:

let hljsReady = false;
async function ensureHljs() {
    if (hljsReady) return;
    await new Promise((resolve, reject) => {
        const s = document.createElement('script');
        s.src = '/static/lib/highlight.min.js';
        s.onload = resolve;
        s.onerror = reject;
        document.head.appendChild(s);
    });
    hljsReady = true;
}
// Usage: await ensureHljs(); hljs.highlightElement(codeBlock);
```

**Risk:** `hljs` global is referenced by `codeRunner.js` and `chatRenderer.js`. Audit all call sites to ensure they `await ensureHljs()` before accessing `hljs`. If any call site is synchronous (e.g., in a `DOMContentLoaded` handler without async), refactor to gate behind readiness check.

**Verification:**
- Code blocks render with syntax highlighting (send a chat message with code)
- No `hljs is not defined` errors in console
- Network tab shows `highlight.min.js` only loads when code appears in chat

**Files:**
- `static/index.html` — remove `<script defer src="/static/lib/highlight.min.js">`
- `static/js/codeRunner.js` — add `ensureHljs()`
- `static/js/chatRenderer.js` — add `ensureHljs()`

---

### 1.3 SQLite Maintenance

**What:** Add `PRAGMA optimize` on startup, `VACUUM` on shutdown, and periodic old-session cleanup.

**Why:**
- `app.db` is 3.1MB and grows unboundedly with session history, messages, and API tokens
- `VACUUM` reclaims space from deleted rows (fragmentation accumulates over time)
- `PRAGMA optimize` updates the query planner statistics for faster queries
- No cleanup exists for old sessions — DB will grow forever

**How:**
```python
# In app.py _startup_event():
db = SessionLocal()
try:
    db.execute("PRAGMA optimize")
    db.commit()
finally:
    db.close()

# In _shutdown_event():
db = SessionLocal()
try:
    db.execute("VACUUM")
finally:
    db.close()

# In cleanup_routes.py or task_scheduler: auto-delete sessions older than 90 days
```

**Risk:** `VACUUM` locks the DB for a few seconds. Running it on shutdown (clean exit) avoids contention. Running it on startup adds <1s to cold boot.

**Files:**
- `app.py` — add to startup/shutdown events (or verify existing hooks in `_lifespan`)
- `routes/cleanup_routes.py` — add DB cleanup (currently handles session files only?)

**Verification:**
- `ls -lh data/app.db` before and after a clean shutdown
- Check logs for "PRAGMA optimize" and "VACUUM" execution
- After 1 week, confirm DB hasn't grown beyond initial size

---

## Phase 2: Data Efficiency (~5 hours)

### 2.1 memory.json Server-side Pagination

**What:** Don't transfer the full 3.2MB `memory.json` on every brain tab open. Instead, paginate via `?limit=50&offset=0` query params with a `has_more` sentinel.

**Why:**
- `GET /api/memory` returns the entire 3.2MB file — gzipped to ~300KB on wire, but still parsed entirely by the browser
- Brain tab already capped DOM rendering at 100 items (recent fix), but the raw JSON transfer is still unbounded
- 3.2MB JSON parse on every tab open blocks the main thread (~100-200ms on modern hardware)
- Search already loads the full file server-side (line 143 of `routes/memory_routes.py`) — pagination only affects the listing endpoint

**How:**

**Backend** (`routes/memory_routes.py`):
```python
@router.get("/memory")
async def list_memories(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    sort: str = Query("desc", pattern="^(asc|desc)$"),
):
    user = effective_user(request)
    # Load from memory.json once, cache with mtime invalidation
    entries = memory_manager.load(owner=user)
    if sort == "desc":
        entries = sorted(entries, key=lambda m: m.get("date", ""), reverse=True)
    else:
        entries = sorted(entries, key=lambda m: m.get("date", ""))
    
    total = len(entries)
    page = entries[offset : offset + limit]
    return {
        "memory": [format_entry(m) for m in page],
        "total": total,
        "has_more": (offset + limit) < total,
    }
```

**Backend cache** (avoid parsing 3.2MB JSON on every request):
```python
_memory_cache: dict = {}  # {owner: (mtime, entries)}

def _load_memory(owner: str) -> list:
    mem_file = os.path.join(DATA_DIR, "memory.json")
    mtime = os.path.getmtime(mem_file)
    cache_entry = _memory_cache.get(owner)
    if cache_entry and cache_entry[0] == mtime:
        return cache_entry[1]
    with open(mem_file) as f:
        all_entries = json.load(f)
    filtered = [e for e in all_entries if e.get("owner") == owner]
    _memory_cache[owner] = (mtime, filtered)
    return filtered
```

**Frontend** (`static/js/memory.js`):
- Replace `fetch('/api/memory')` with `fetch('/api/memory?limit=50&offset=0')`
- Store `total` and `has_more` in state
- Add IntersectionObserver "Load more" button or auto-load on scroll (already have the pattern from recent brain-tab fix)
- Update search to use server-side search with same pagination

**Risk assessment:**
- **Search UX:** If frontend currently does client-side filtering, it breaks with pagination. Move search to server-side.
- **Sorting:** If frontend sorts by column click, move sort params to API.
- **Memory count:** The `updateMemoryCount()` call needs to use `total` from the paginated response, not `response.length`.

**Verification:**
- `curl -s -w 'size: %{size_download}' 'localhost:7000/api/memory?limit=50'` — should be ~10-20KB
- Brain tab loads with first 50 entries visible
- Scroll to bottom, "Load more" appears and loads next 50
- Search returns correct results scoped to full dataset
- Memory count matches total entries

**Files:**
- `routes/memory_routes.py` — add pagination params, add mtime cache
- `static/js/memory.js` — paginated fetch, load-more UI, search adaptation

---

### 2.2 Lazy TTS/STT Initialization

**What:** Defer Text-to-Speech and Speech-to-Text service initialization from app boot to first use.

**Why:**
- TTS and STT are used on <5% of user sessions (most users type, don't speak)
- `get_tts_service()` (line 534) and `get_stt_service()` (line 652) run at module import time
- Each reads provider config, validates credentials, potentially reaches out to external APIs
- The pattern already exists: `get_rag_manager()` returns None immediately and defers connection

**How:**
```python
# Replace module-level:
# tts_service = get_tts_service()
# With:
_tts_service = None
def ensure_tts():
    global _tts_service
    if _tts_service is None:
        _tts_service = get_tts_service()
    return _tts_service

# Routes use `ensure_tts()` instead of `tts_service`
```

**Risk:**
- **First request latency:** The first user to trigger TTS/STT pays the init cost (~1s) instead of boot paying it. For single-user local use, this is a wash.
- **None handling:** Routes must handle `ensure_tts()` returning None (service misconfigured) gracefully — already handled today since `get_tts_service()` can return None.

**Files:**
- `app.py` — replace `tts_service =` and `stt_service =` with lazy wrappers
- `routes/tts_routes.py`, `routes/stt_routes.py` — use `ensure_tts()` / `ensure_stt()`
- Any other files that import `tts_service` from `app`

**Verification:**
- `time ./venv/bin/python3 -c "import app"` — cold boot should be ~1-2s faster
- TTS/STT still work when triggered from UI
- No `AttributeError: 'NoneType'` errors when TTS/STT is triggered

---

## Phase 3: JS Bundling (~4 hours)

### 3.1 JS Bundling with esbuild

**What:** Bundle ~35 ES module scripts into ~5 entry-point bundles using esbuild with code-splitting.

**Why:**
- 35 `<script type="module">` tags in `index.html` = 35 HTTP requests per page load
- `_RevalidatingStatic` adds `Cache-Control: no-cache` on all `.js` files — even 304 revalidation is 35 round-trips
- esbuild is zero-config, 100x faster than webpack, and preserves ESM dynamic `import()` as code-split points
- HTTP/2 multiplexing doesn't help because uvicorn doesn't support it (needs httptools + a proxy)

**Why NOT webpack/vite:**
- This project has no existing build step — adding a heavy build system creates ongoing maintenance burden
- esbuild works with a single CLI command, no config file required
- Vite is designed for SPA dev servers with HMR — overkill for a server-rendered app

**Why NOT nothing (status quo):**
- Acceptable for localhost-only use (38 round-trips is ~5ms)
- **Becomes a real problem** when accessing Odysseus over Tailscale, Cloudflared, or reverse proxy (each round-trip is 5-50ms → 175ms-2.5s for 35 requests)
- Users accessing from a phone/tablet on same network see noticeable delay

**Approach:**
```bash
npm install -D esbuild

# Production build (one command):
npx esbuild static/app.js \
  --bundle \
  --format=esm \
  --splitting \
  --outdir=static/dist/ \
  --minify \
  --sourcemap

# The --splitting flag preserves dynamic import() calls as separate chunks
# (e.g., document.js, settings.js, cookbook.js stay lazy-loaded)
```

**Bundle strategy:**
| Bundle | Contents | When loaded |
|--------|----------|-------------|
| `core.js` | `ui.js` + `storage.js` + `markdown.js` + `theme.js` | Always (every page) |
| `chat.js` | `chat.js` + `chatStream.js` + `chatRenderer.js` + `codeRunner.js` + `slashCommands.js` | Chat page |
| `memory.js` | `memory.js` + `memoryToNotes.js` | Brain tab open |
| Lazy chunks | `document.js`, `settings.js`, `cookbook.js`, `calendar.js`, etc. | On feature use (preserve dynamic imports) |

**CSP compatibility:**
The current CSP uses `script-src 'self' 'nonce-{nonce}'` for inline scripts. esbuild does NOT generate inline scripts by default — it outputs separate `.js` files loaded via `<script type="module">`. The CSP supports this (nonced inline scripts, `'self'` for separate files). No CSP changes needed.

**Risk:**
- **Dynamic imports:** Some modules import from `./chat.js?v=20260609ws` with cache-busting query params. esbuild may not resolve these. Clean up versioned imports before bundling, or add `--external:./chat.js*` exclusions.
- **Cookbook scripts:** `cookbookSchedule.js` is loaded as a non-module `<script>` (no `type="module"`). It must stay external.
- **Source maps:** Enable only for development builds. Production source maps are security-sensitive (expose source paths).
- **Manual import ordering:** `models.js` must load BEFORE `app.js`. The current `index.html` enforces this via script tag order. Bundling must preserve this — either bundle them together or mark `models.js` as an external dependency of the `app.js` bundle.

**Decision gate:** Only proceed if ANY of:
- Users access Odysseus remotely (Tailscale, Cloudflared, reverse proxy)
- Page load feels slow on mobile/tablet
- You or other users notice 35-request waterfall in DevTools

**Otherwise:** Skip Phase 3. The status quo is fine for localhost.

**Verification:**
- `ls static/dist/` — shows ~5 bundle files
- Page loads with no JS errors
- All features work (chat, memory, settings, document export, cookbook)
- Network tab shows <10 JS requests
- CSP doesn't block any script loads

**Files:**
- `static/index.html` — replace 35 script tags with 5 bundle tags + a few external
- `static/login.html` — same
- `package.json` — add `esbuild` devDependency + build script
- `static/dist/` — new directory (gitignored or committed)

---

## Phase 4: Code Health (~3 hours)

### 4.1 Split tool_implementations.py

**What:** Split `src/tool_implementations.py` (4,266 lines) into domain-specific tool modules: `tools/file_tools.py`, `tools/web_tools.py`, `tools/memory_tools.py`, etc.

**Why:**
- 4,266-line single file is hard to navigate, review, and avoid merge conflicts
- The dispatch table at the bottom naturally groups tools by domain
- Splitting encourages module boundaries — currently everything imports from one giant module

**Why NOT further:**
- `agent_loop.py` (3,190 lines) is a state machine — splitting makes control flow harder to follow. **Do NOT split.**
- `email_routes.py` (3,516 lines) could be split but has complex interdependencies. **Defer until after tool split proves the pattern.**

**Approach:**
```python
# src/tools/__init__.py
from .file_tools import read_file, write_file, edit_file, glob_tool, grep_tool
from .web_tools import web_search, web_fetch, github_search
from .memory_tools import create_memory, search_memory, delete_memory
from .system_tools import run_bash, run_python, git_commit
# ... etc

# Keep the dispatch table in __init__.py or a separate dispatch.py
TOOL_REGISTRY = {
    "read_file": read_file,
    "write_file": write_file,
    # ...
}
```

**Risk:**
- **Circular imports:** The #1 cause of bugs when splitting Python modules. Audit all imports first — identify which functions import from `tool_implementations.py` and whether the split modules would need to import each other.
- **Import cost:** Splitting into many files can marginally slow imports due to filesystem overhead (stat + read many small files vs one large file). For under 10 tool modules, this is negligible.

**Verification:**
- `python3 -m py_compile src/tools/*.py` — all modules compile
- `./venv/bin/python3 -c "from src.tools import TOOL_REGISTRY; print(len(TOOL_REGISTRY))"` — registry loads
- All existing tests pass: `pytest tests/ -x -q --tb=short`
- All tools work in chat (send a message that triggers each tool)

**Files:**
- `src/tool_implementations.py` → `src/tools/{file,web,memory,system,...}_tools.py`
- `src/tools/__init__.py` — re-exports + dispatch table
- All files importing from `src.tool_implementations` — update imports

---

## Phase 5: macOS App Lifecycle (~3 hours)

### Problem

When you launch Odysseus via the built `Odysseus.app`, it opens a Chromium app-window frontend. Closing that window does **not** stop the server — uvicorn keeps running, the bash launcher stays alive blocked on `wait`, and there is **no way to quit** except `kill` from Terminal or Activity Monitor.

**Root cause** in `build-macos-app.sh` (lines 122–146): The launcher script starts uvicorn in the background, opens the browser, then calls `wait $SERVER_PID`. This `wait` never returns because closing the browser window does not signal uvicorn to exit. The `trap 'kill $SERVER_PID' TERM INT` (line 130) would work if Dock "Quit" is clicked, but:
- On first run, the Dock icon may not be visible (the Chromium window takes focus)
- Even when visible, users don't know to right-click → Quit — they expect closing the window to stop the app, or at minimum a visible "Quit" control

**What launcher.py does (Windows-only, not used on macOS):**
- `launcher.py` has a pystray system tray icon with "Open Odysseus" and "Exit" menu items (lines 93–109)
- But this is gated behind `sys.frozen` (PyInstaller only) and uses tkinter, which isn't macOS-native
- The macOS launcher uses a completely separate bash script — none of this code runs

### 5.1 Add `/api/shutdown` Endpoint

**What:** A new API endpoint that gracefully stops the uvicorn server. This gives the frontend and any other client a clean way to trigger shutdown.

**Why:**
- The most fundamental missing piece: no programmatic way to stop the server
- Enables frontend shutdown button, beforeunload handler, and external tooling
- Trivially simple to implement — ~15 lines of Python

**How:**
```python
# In app.py or a new routes/shutdown_routes.py

@router.post("/api/shutdown")
async def shutdown_server(request: Request):
    """Gracefully shut down the Odysseus server.
    
    Requires authentication. Returns a response before exiting to avoid
    the caller getting a connection error.
    """
    # Only allow authenticated users or localhost callers
    user = effective_user(request)
    if not user and not _auth_disabled():
        raise HTTPException(401, "Authentication required")
    
    import signal, os as _os
    logger.warning("Shutdown requested by user=%s — stopping server", user or "anonymous")
    
    # Schedule shutdown after response is sent (avoid 5xx in caller)
    async def _delayed_shutdown():
        await asyncio.sleep(0.5)
        _os.kill(_os.getpid(), signal.SIGTERM)
    
    asyncio.create_task(_delayed_shutdown())
    return {"ok": True, "message": "Server shutting down..."}
```

**Verification:**
- `curl -X POST http://localhost:7000/api/shutdown` — server exits cleanly within 1 second
- Check logs for "Shutdown requested" message
- Server process is no longer running: `pgrep -f "uvicorn app:app"`

**Files:**
- `routes/shutdown_routes.py` — new file, or append to `app.py` existing routes

---

### 5.2 Add Frontend Shutdown Controls

**What:** A visible "Shut Down Odysseus" button in the settings or admin area, and optionally a `beforeunload` handler to prompt on window close.

**Why:**
- Users need a discoverable way to shut down — closing the browser window is the natural action, not right-clicking a Dock icon
- A confirmation dialog prevents accidental shutdown
- The `beforeunload` handler bridges the mental model gap: "closing window = quitting app"

**How:**

**A) Shutdown button in settings:**
```javascript
// In static/js/settings.js or app.js
async function shutdownServer() {
    const confirmed = confirm(
        'Shut down Odysseus?\n\n' +
        'The server will stop and the browser window will close.\n' +
        'To restart, open Odysseus.app again.'
    );
    if (!confirmed) return;
    try {
        await fetch('/api/shutdown', { method: 'POST' });
    } catch {
        // Expected — server dies before response completes
    }
    // Close the browser window
    window.close();
}
```

**B) Optional beforeunload behavior:**
```javascript
// In static/app.js, guarded by a setting
let SHUTDOWN_ON_CLOSE = false; // default off — most users just close windows

if (SHUTDOWN_ON_CLOSE) {
    window.addEventListener('beforeunload', () => {
        navigator.sendBeacon('/api/shutdown');
    });
}
```
Keep `SHUTDOWN_ON_CLOSE = false` by default. Auto-shutdown on close is surprising behavior — most macOS apps don't quit when you close the window. The explicit button is the primary control.

**Verification:**
- "Shut Down" button visible in settings/admin area
- Click → confirmation dialog → server stops, window closes
- No `beforeunload` popup by default (setting off)

**Files:**
- `static/js/settings.js` or `static/app.js` — add `shutdownServer()` and button
- `static/index.html` or `static/settings.html` — add button markup

---

### 5.3 Enhance Bash Launcher: Watchdog + Graceful Exit

**What:** Improve `build-macos-app.sh`'s embedded launcher script so the app process cleanly exits when the server stops — for any reason.

**Why:**
- After 5.1, the server CAN be stopped (via `/api/shutdown`). The launcher must respond correctly.
- If uvicorn crashes, the launcher should exit too (not hang forever on `wait`)
- The Dock icon should reflect the app's state (present when running, gone when stopped)

**Current behavior (lines 122–146):**
```bash
"$UVICORN" app:app ... &      # Start server in background
SERVER_PID=$!
trap 'kill $SERVER_PID; exit 0' TERM INT
# ... wait for readiness ...
open_ui                         # Open browser
wait "$SERVER_PID"              # Block forever
```

**Only change needed:** After the server exits (wait returns), exit the script. This already happens implicitly. But add:
```bash
wait "$SERVER_PID"
EXIT_CODE=$?
echo "$(date): uvicorn exited with code $EXIT_CODE" >> "$LOG"
exit $EXIT_CODE
```

And ensure the `trap` covers `EXIT` too (clean up server if script is killed):
```bash
cleanup() { kill "$SERVER_PID" 2>/dev/null; }
trap cleanup TERM INT EXIT
```

**Additional hardening — watchdog:**
```bash
# If browser window is closed but server is still up, don't auto-quit.
# Instead, poll the server health endpoint. If server is unreachable for
# 30+ seconds, assume it crashed and exit.
(
    while kill -0 "$SERVER_PID" 2>/dev/null; do
        if ! curl -s -o /dev/null --max-time 2 "$URL/api/health" 2>/dev/null; then
            FAILS=$((FAILS + 1))
            if [ "$FAILS" -ge 15 ]; then
                echo "$(date): server unreachable for 30s — exiting" >> "$LOG"
                kill "$SERVER_PID" 2>/dev/null
                exit 1
            fi
        else
            FAILS=0
        fi
        sleep 2
    done
) &
WATCHDOG_PID=$!
trap 'kill $WATCHDOG_PID 2>/dev/null; cleanup' TERM INT EXIT
```

**Verification:**
- Build app: `./build-macos-app.sh`
- Launch app: `open dist/Odysseus.app`
- Call shutdown: `curl -X POST http://localhost:7860/api/shutdown`
- App process exits, Dock icon disappears
- Kill server manually: `kill $(pgrep -f "uvicorn app:app")`
- App process exits, Dock icon disappears

**Files:**
- `build-macos-app.sh` — enhance launcher template (lines 70–147)

---

### 5.4 macOS Menu Bar App with rumps

**What:** Replace the bash-only launcher with a tiny Python menu bar app that shows an Odysseus icon in the macOS menu bar, with menu items: "Open Odysseus", "Shut Down Server", "Quit".

**Why:**
- This is the **proper macOS UX** for a server-backed app. Users expect a menu bar icon or Dock menu for server apps.
- Provides discoverable Quit at all times (even when browser window is closed)
- Pure Python via `rumps` — no Swift/Xcode/compilation needed
- `launcher.py` already has this concept (pystray on Windows) — we're bringing it to macOS

**Why rumps over pystray:**
- `pystray` requires a running event loop and doesn't integrate cleanly with macOS menu bar
- `rumps` is purpose-built for macOS menu bar apps, uses native `NSStatusBar` via `pyobjc`
- Zero UI dependencies beyond `pyobjc` (which ships with macOS Python)

**How:**
```python
# menu_bar_app.py — runs alongside uvicorn, provides menu bar controls
import rumps
import webbrowser
import requests
import os

PORT = int(os.getenv("APP_PORT", "7000"))
URL = f"http://127.0.0.1:{PORT}"

class OdysseusBarApp(rumps.App):
    def __init__(self):
        super().__init__(
            name="Odysseus",
            title="⛵",
            icon=None,  # Use emoji title for simplicity
            quit_button=None,  # We handle quit ourselves
        )
    
    @rumps.clicked("Open Odysseus")
    def open_browser(self, _):
        webbrowser.open(URL)
    
    @rumps.clicked("Shut Down Server")
    def shutdown_server(self, _):
        try:
            requests.post(f"{URL}/api/shutdown", timeout=2)
        except requests.exceptions.ConnectionError:
            pass  # Expected — server dies during response
        rumps.quit_application()
    
    @rumps.clicked("Quit")
    def quit_app(self, _):
        # Try graceful shutdown first, then force kill
        try:
            requests.post(f"{URL}/api/shutdown", timeout=2)
        except Exception:
            os._exit(0)
        rumps.quit_application()

if __name__ == "__main__":
    OdysseusBarApp().run()
```

**Update build script** to copy `menu_bar_app.py` into the app bundle and launch it alongside uvicorn:
```bash
# In build-macos-app.sh launcher template:
"$UVICORN" app:app ... &
SERVER_PID=$!
"$INSTALL_DIR/venv/bin/python" "$INSTALL_DIR/menu_bar_app.py" &
BAR_PID=$!
trap 'kill $SERVER_PID $BAR_PID 2>/dev/null; exit 0' TERM INT EXIT
wait $SERVER_PID
kill $BAR_PID 2>/dev/null
```

**Decision gate:** This item is **optional**. The bash launcher improvements (5.3) + shutdown endpoint (5.1) + frontend button (5.2) already solve the core problem. The menu bar app is polish — do it if you want a premium macOS feel, skip it if you're satisfied with Dock behavior + frontend button.

**Verification:**
- Menu bar shows Odysseus icon (⛵)
- Click → menu with "Open Odysseus", "Shut Down Server", "Quit"
- "Shut Down Server" → server exits, menu bar icon disappears
- "Quit" → force-kills everything, menu bar icon disappears
- "Open Odysseus" → browser opens to Odysseus URL

**Files:**
- `menu_bar_app.py` — new file in repo root
- `build-macos-app.sh` — launch menu bar app alongside uvicorn
- `requirements.txt` — add `rumps` and `pyobjc-framework-Cocoa` (or install rumps which pulls pyobjc)

---

### Phase 5 Summary

| Item | What | Impact | Effort |
|------|------|--------|--------|
| 5.1 | `/api/shutdown` endpoint | Programmatic shutdown | 30 min |
| 5.2 | Frontend shutdown button | Discoverable Quit UX | 45 min |
| 5.3 | Bash launcher hardening | Clean exit on server stop | 45 min |
| 5.4 | Menu bar app (rumps) | Premium macOS UX | 60 min |

**Minimum viable fix:** 5.1 + 5.2 + 5.3 = **2 hours**. Menu bar app (5.4) is polish.

---

## Risk Register

| Risk | Phase | Severity | Mitigation |
|------|-------|----------|------------|
| CSS minification breaks layout | 1.1 | Medium | Safe preset only (no rule merging). Visual spot-check 10 pages. |
| highlight.js lazy-load breaks code blocks | 1.2 | Medium | Audit all `hljs` call sites. Gate behind `ensureHljs()`. |
| VACUUM locks DB during request | 1.3 | Low | Run on shutdown, not during serving. |
| memory.json pagination breaks search | 2.1 | Medium | Move search server-side. Back-compat: return full list if no `limit` param. |
| memory.json mtime cache serves stale data | 2.1 | Low | File mtime is reliable on macOS. Invalidate on write. |
| Lazy TTS/STT causes None-reference crash | 2.2 | Medium | All route handlers already handle None from `get_tts_service()`. |
| esbuild breaks dynamic imports | 3.1 | High | `--splitting` preserves `import()`. Test before committing. |
| esbuild + CSP nonce incompatibility | 3.1 | Low | esbuild doesn't generate inline scripts. No CSP change needed. |
| Circular imports when splitting tool_implementations.py | 4.1 | High | Audit import graph first. Start with leaf modules (least dependencies). |
| `/api/shutdown` called by unauthorized user | 5.1 | Medium | Require auth (effective_user) or localhost exemption. Never expose unauthenticated. |
| Shutdown endpoint kills server before response sent | 5.1 | Low | Use `asyncio.create_task` with 0.5s delay — response completes, then SIGTERM. |
| beforeunload auto-shutdown is surprising | 5.2 | Low | Default OFF. Only enable with explicit user opt-in in settings. |
| Bash watchdog false-positive kills healthy server | 5.3 | Low | Use 30s threshold (15 failures × 2s interval). Server unreachable for 30s is a real crash. |
| rumps + pyobjc install fails on some Macs | 5.4 | Medium | rumps is optional (5.4. is polish). If install fails, skip menu bar — 5.1-5.3 still work. |

---

## Tracking Checklist

Copy this section into each phase's PR description.

### Phase 1: Quick Wins

- [x] **1.1 CSS minification**
  - [x] Install cssnano: `npm install -D cssnano postcss`
  - [x] Generate: `npx cssnano static/style.css > static/style.min.css`
  - [x] Update `<link>` in `index.html`, `login.html`, `backgrounds.html`
  - [x] Verify: `ls -lh static/style.min.css` — under 400KB
  - [x] Verify: visual spot-check 10 pages
  - [x] Record: new transfer size

- [x] **1.2 Lazy-load highlight.js**
  - [x] Remove `<script defer>` from `index.html`
  - [x] Add `ensureHljs()` helper to `codeRunner.js`
  - [x] Gate all `hljs.` calls behind `await ensureHljs()`
  - [x] Verify: code blocks render with syntax highlighting
  - [x] Verify: no `hljs is not defined` in console
  - [x] Record: highlight.js only loads when code appears

- [x] **1.3 SQLite maintenance**
  - [x] Add `PRAGMA optimize` to startup event
  - [x] Add `VACUUM` to shutdown event
  - [x] Add old-session cleanup (90+ days) to `cleanup_routes.py`
  - [x] Verify: `ls -lh data/app.db` before/after shutdown
  - [x] Verify: check logs for PRAGMA/VACUUM execution

### Phase 2: Data Efficiency

- [x] **2.1 memory.json pagination**
  - [x] Add `?limit=` and `?offset=` params to `GET /api/memory`
  - [x] Add mtime-based in-memory cache for memory.json loading
  - [x] Return `{memory, total, has_more}` response shape
  - [x] Update `static/js/memory.js` to paginated fetch
  - [x] Add "Load more" or IntersectionObserver infinite scroll
  - [x] Move search to server-side with same pagination
  - [x] Verify: `/api/memory?limit=50` returns ~10-20KB
  - [x] Verify: load more works, search works, count is correct

- [x] **2.2 Lazy TTS/STT init**
  - [x] Replace module-level `tts_service = get_tts_service()` with `ensure_tts()`
  - [x] Replace module-level `stt_service = get_stt_service()` with `ensure_stt()`
  - [x] Update route handlers to use `ensure_*()` wrappers
  - [x] Verify: cold boot time reduced
  - [x] Verify: TTS/STT still works from UI

### Phase 3: Network Efficiency

- [x] **3.1 JS bundling with esbuild**
  - [x] Install esbuild: `npm install -D esbuild`
  - [x] Define bundle entry points (core, chat, memory)
  - [x] Run esbuild with `--bundle --format=esm --splitting --minify`
  - [x] Update `index.html` to load bundles instead of 35 modules
  - [x] Fix cache-busting query params (`?v=...`) in imports
  - [x] Keep `cookbookSchedule.js` as external (non-module script)
  - [x] Verify: page loads with <10 JS requests
  - [x] Verify: all features work (chat, memory, settings, export, cookbook)
  - [x] Verify: CSP doesn't block any script loads

### Phase 4: Code Health

- [x] **4.1 Split tool_implementations.py**
  - [x] Audit import graph: identify leaf modules
  - [x] Create `src/tools/` package with `__init__.py`
  - [x] Extract tool functions into domain modules
  - [x] Re-export from `__init__.py` with dispatch table
  - [x] Update all imports in consuming files
  - [x] Verify: `py_compile` all modules
  - [x] Verify: `pytest tests/ -x -q --tb=short`
  - [x] Verify: all tools work in chat

### Phase 5: macOS App Lifecycle

- [x] **5.1 Add `/api/shutdown` endpoint**
  - [x] Create `routes/shutdown_routes.py` with `POST /api/shutdown`
  - [x] Require authentication (effective_user or localhost)
  - [x] Use `asyncio.create_task` + 0.5s delay → `os.kill(SIGTERM)`
  - [x] Register router in `app.py`
  - [x] Verify: `curl -X POST http://localhost:7000/api/shutdown` kills server
  - [x] Verify: logs show "Shutdown requested" before exit

- [x] **5.2 Add frontend shutdown button**
  - [x] Add `shutdownServer()` function to `settings.js` or `app.js`
  - [x] Add "Shut Down Odysseus" button with confirmation dialog
  - [x] After shutdown, call `window.close()` to close browser
  - [x] Keep `SHUTDOWN_ON_CLOSE` default OFF (no auto-shutdown)
  - [x] Verify: button visible in settings, click → confirm → server stops

- [x] **5.3 Enhance bash launcher**
  - [x] Add `trap ... EXIT` to ensure server cleanup on any exit path
  - [x] Add watchdog: poll health endpoint, exit if server unreachable for 30s
  - [x] Log server exit code to `logs/odysseus-app.log`
  - [x] Rebuild app: `./build-macos-app.sh`
  - [x] Verify: launch app, call `/api/shutdown` → app process exits cleanly
  - [x] Verify: kill uvicorn manually → app process exits within 30s
  - [x] Verify: closing browser window does NOT kill server (intentional)

- [ ] **5.4 (Optional) Menu bar app with rumps**
  - [x] Install rumps: `pip install rumps` (or add to requirements.txt)
  - [x] Create `menu_bar_app.py` with "Open Odysseus", "Shut Down", "Quit"
  - [x] Update `build-macos-app.sh` to launch menu bar app alongside uvicorn
  - [x] Rebuild and verify: ⛵ icon in menu bar, all menu items work
  - [x] Verify: "Shut Down" → server exits, icon disappears
  - [x] Verify: "Quit" → force-kills everything

---

## Decisions & Arguments

### Why CSS minification before JS bundling
CSS is 1.1MB and loaded on every page. JS modules total ~5MB but are spread across 35 files loaded on-demand (ES modules with dynamic import). The CSS is one monolithic transfer. Minifying it requires no build pipeline, no config, and has zero behavioral risk.

### Why pagination before compression
`GZipMiddleware` already compresses `/api/memory` from 3.2MB to ~300KB on wire. Further compression (Brotli, pre-compression) saves maybe 20-30% more. Pagination saves **99.7%** (50KB→~5KB). The math is clear.

### Why esbuild over webpack/vite
This project has no build step. webpack requires config files, loaders, plugins, and ongoing maintenance. esbuild works with a single command. The zero-config principle of the project should be preserved.

### Why NOT a Service Worker for caching
A Service Worker could cache static assets aggressively, eliminating all revalidation requests. However:
- It adds significant complexity (caching strategy, update signalling, debugging)
- It can mask bugs (stale code served after deploy)
- The current `_RevalidatingStatic` + ETag approach is simple and correct
- Service Workers would be a better investment if/when offline support is needed

### Why NOT splitting agent_loop.py
`agent_loop.py` is a state machine (orchestration loop for AI agent execution). Splitting state machines across files makes the control flow harder to follow — readers must jump between files to understand what happens next. The current single-file structure is intentional and correct for this pattern.

### Why `/api/shutdown` instead of SIGTERM-only
Sending SIGTERM to the uvicorn process from outside requires knowing the PID and having terminal access. An API endpoint is discoverable (frontend button), auth-gated, and works from any context (browser, curl, automation). The endpoint triggers the same SIGTERM internally — same shutdown path, better UX.

### Why `SHUTDOWN_ON_CLOSE` defaults to OFF
macOS users expect closing a window ≠ quitting an app. Safari, Mail, Notes, and most Mac apps keep running when you close the window. Auto-shutdown on close would be surprising and potentially lose in-progress work. The explicit "Shut Down" button is the primary control.

### Why rumps over Swift/Electron tray
`rumps` is a pure-Python wrapper around macOS `NSStatusBar`. No Xcode, no compilation, no additional languages in the codebase. It's the simplest way to add a menu bar icon to a Python project. The alternative — a full Swift menu bar app — would require a separate Xcode project, bridging code, and a different build pipeline. Overkill for a launcher.

### What we deliberately did NOT include
- **CDN for static assets:** Single-user local app — CDN adds latency, external dependency, and CSP complexity. No benefit.
- **Image optimization:** The app generates images dynamically via Cookbook. No static image bloat.
- **Database migration to PostgreSQL:** SQLite is correct for single-user local app. Zero-config, zero-admin.
- **Redis/memcached:** Single-user, in-process — no distributed caching benefit. Memory cache in Python dict is sufficient.
- **HTTP/2 on uvicorn:** uvicorn doesn't support H2 natively. Would need nginx/caddy reverse proxy. Overkill for localhost.
- **Brotli precompression:** GZip is already enabled. Brotli saves ~15% more but adds build complexity. Not worth it for localhost.
- **Auto-shutdown on window close:** macOS convention is window close ≠ quit. Explicit button is clearer.
- **Swift/Electron launcher:** Adds a second language + build pipeline. Python rumps is simpler.

---

## Metrics Dashboard

Track these after each phase to measure progress.

| Metric | Baseline | After P1 | After P2 | After P3 | Target |
|--------|----------|----------|----------|----------|--------|
| CSS transfer size | ___ KB | | — | — | <400 KB |
| Page JS requests | ___ | | — | ___ | <10 |
| Page total transfer | ___ KB | ___ KB | ___ KB | ___ KB | <600 KB |
| `/api/memory` size | ___ KB | — | ___ KB | — | <20 KB |
| Cold boot time | ___ s | — | ___ s | — | <3 s |
| `app.db` size | ___ MB | ___ MB | | — | Stable |
| `highlight.js` eager? | yes | no | — | — | no |
| Tool file line count | 4,266 | — | — | 4,266 | <500/file |
| App has shutdown UX | no | — | — | — | yes (P5) |
| App exits on server stop | no | — | — | — | yes (P5) |

---

## Iteration Plan

This document is a living plan. After each phase:
1. **Measure** — fill in the metrics dashboard
2. **Review** — did the optimization deliver what was expected?
3. **Adjust** — reprioritize remaining phases based on new data
4. **Document** — add notes on what worked, what didn't, surprises

If a phase shows less benefit than expected, **question whether subsequent phases are worth it.** The effort estimates are ranges — stop early if diminishing returns kick in.
