#!/usr/bin/env python3
"""Odysseus macOS menu bar app.

Runs alongside uvicorn inside the Odysseus.app bundle (launched by
``build-macos-app.sh``). Shows an ⛵ icon in the menu bar with:

- **Open Odysseus** — bring the browser window to the front.
- **Shut Down Server** — graceful shutdown via ``/api/shutdown``.
- **Quit** — force-kill everything (fallback if shutdown endpoint is unreachable).

Requires ``rumps`` (``pip install rumps``) which wraps ``NSStatusBar`` via
pyobjc — no Xcode or Swift needed. Only runs on macOS inside the app bundle;
silently unused when running headless or on other platforms.

Phase 5.4 of the optimization plan.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import webbrowser

import rumps

logger = logging.getLogger("odysseus.menubar")

PORT = int(os.getenv("APP_PORT", os.getenv("ODYSSEUS_PORT", "7860")))
BASE_URL = f"http://127.0.0.1:{PORT}"
HEALTH_URL = f"{BASE_URL}/api/health"
SHUTDOWN_URL = f"{BASE_URL}/api/shutdown"


def _curl_json(url: str, method: str = "GET", timeout: float = 5) -> dict | None:
    """Best-effort HTTP request via ``curl`` subprocess (no ``requests`` dep).

    Returns parsed JSON on success, ``None`` on any failure.
    """
    try:
        result = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
             "-X", method, "--max-time", str(timeout), url],
            capture_output=True, text=True, timeout=timeout + 2,
        )
        return {"status_code": int(result.stdout.strip())} if result.stdout.strip() else None
    except Exception:
        return None


class OdysseusMenuBar(rumps.App):
    """macOS menu bar interface for the Odysseus server."""

    def __init__(self) -> None:
        super().__init__(
            name="Odysseus",
            title="⛵",
            icon=None,
            quit_button=None,  # We handle quit ourselves for clean shutdown.
        )

        self._shutting_down = False

        # Menu items
        self.open_item = rumps.MenuItem("Open Odysseus", callback=self._open_browser)
        self.status_item = rumps.MenuItem("Status: Starting…", callback=None)
        rumps.separator()
        self.shutdown_item = rumps.MenuItem("Shut Down Server", callback=self._shutdown_server)
        rumps.separator()
        rumps.MenuItem("Quit", callback=self._quit)

        # Status polling timer — updates the menu title with a ●/○ indicator.
        # Poll every 5 seconds; lightweight (single TCP + HTTP GET).
        self.timer(rumps.Timer(self._health_check, 5), start=True)

    # ── Callbacks ──────────────────────────────────────────────────────

    def _open_browser(self, _: rumps.MenuItem) -> None:
        """Open (or bring to front) the Odysseus UI."""
        webbrowser.open(BASE_URL)

    def _shutdown_server(self, _: rumps.MenuItem) -> None:
        """Gracefully shut down the server via the /api/shutdown endpoint."""
        if self._shutting_down:
            return
        self._shutting_down = True
        self.title = "⏳"
        self.status_item.title = "Shutting down…"

        logger.info("Menu bar: requesting server shutdown")
        resp = _curl_json(SHUTDOWN_URL, method="POST", timeout=5)
        if resp and resp.get("status_code") in (200, 201):
            # Server acknowledged — it will SIGTERM itself shortly.
            # Give it a moment then exit the menu bar app.
            rumps.Timer(
                lambda _: rumps.quit_application(),
                2,
            ).start()
        else:
            # Shutdown endpoint failed (maybe server already gone, or auth
            # blocked it). Force-kill via SIGTERM to the parent process group.
            logger.warning("Menu bar: shutdown endpoint failed, forcing quit")
            rumps.quit_application()

    def _quit(self, _: rumps.MenuItem) -> None:
        """Quit — try graceful shutdown first, then force."""
        if self._shutting_down:
            # Already shutting down; just force-quit.
            rumps.quit_application()
            return
        # Attempt graceful shutdown, then force-quit regardless after 2s.
        self._shutting_down = True
        _curl_json(SHUTDOWN_URL, method="POST", timeout=2)
        rumps.Timer(lambda _: rumps.quit_application(), 2).start()

    # ── Health check ───────────────────────────────────────────────────

    def _health_check(self, _: rumps.Timer) -> None:
        """Poll /api/health and update the menu bar title."""
        if self._shutting_down:
            return
        resp = _curl_json(HEALTH_URL, timeout=3)
        if resp and resp.get("status_code") == 200:
            self.title = "⛵"
            self.status_item.title = "Status: Running"
        else:
            self.title = "○"
            self.status_item.title = "Status: Offline"


def main() -> None:
    """Entry point for the menu bar app. Blocks until rumps quit."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    logger.info("Starting Odysseus menu bar app (port %d)", PORT)
    OdysseusMenuBar().run()


if __name__ == "__main__":
    main()
