# routes/memory_to_notes_routes.py
"""SSE endpoint for the Memory → Obsidian Notes sync pipeline.

Wraps scripts/ingest/memory_to_notes.py --json-events and streams its
NDJSON lines as Server-Sent Events. Also exposes a lightweight /status
endpoint for the frontend status bar.
"""

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

logger = logging.getLogger(__name__)

# Path to the ingest script
_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "ingest" / "memory_to_notes.py"


def _python() -> str:
    """Best-effort python3 executable."""
    return sys.executable or "python3"


def setup_memory_to_notes_routes():
    router = APIRouter(prefix="/api/memory-to-notes", tags=["memory-to-notes"])

    @router.get("/status")
    async def get_status(request: Request):
        """Return current sync status: last run, project breakdown, entry counts."""
        try:
            proc = await asyncio.create_subprocess_exec(
                _python(), str(_SCRIPT_PATH), "--status",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
            if proc.returncode != 0:
                logger.warning(f"memory_to_notes --status failed: {stderr.decode()}")
                raise HTTPException(500, "Status check failed")
            return json.loads(stdout.decode())
        except asyncio.TimeoutError:
            raise HTTPException(504, "Status check timed out")
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON from memory_to_notes --status: {e}")
            raise HTTPException(500, "Invalid status response")

    @router.post("/start")
    async def start_sync(
        request: Request,
        mode: str = Query("incremental", description="'incremental', 'full', or 'dry-run'"),
    ):
        """Run the memory-to-notes sync and stream progress as SSE.

        mode:
          - incremental (default): process only new entries since last run
          - full: rebuild all project notes from scratch
          - dry-run: preview what would be processed without writing
        """
        if mode not in ("incremental", "full", "dry-run"):
            raise HTTPException(400, "mode must be incremental, full, or dry-run")

        args = [_python(), str(_SCRIPT_PATH), "--json-events"]
        if mode == "full":
            args.append("--full")
        elif mode == "dry-run":
            args.append("--dry-run")

        async def event_stream():
            try:
                proc = await asyncio.create_subprocess_exec(
                    *args,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )

                # Read stdout line by line (NDJSON) and forward as SSE
                try:
                    while True:
                        line = await asyncio.wait_for(proc.stdout.readline(), timeout=120)
                        if not line:
                            break
                        line_str = line.decode("utf-8").strip()
                        if not line_str:
                            continue
                        try:
                            event = json.loads(line_str)
                            event_type = event.get("event", "message")
                            # SSE format: event: <type>\ndata: <json>\n\n
                            yield f"event: {event_type}\ndata: {json.dumps(event)}\n\n"
                        except json.JSONDecodeError:
                            logger.warning(f"Non-JSON line from memory_to_notes: {line_str[:200]}")
                            continue
                except asyncio.TimeoutError:
                    logger.error("memory_to_notes timed out (120s without output)")
                    yield 'event: error\ndata: {"event":"error","message":"Sync timed out after 120s of silence"}\n\n'

                # Collect stderr
                stderr_data = await proc.stderr.read()
                if stderr_data:
                    stderr_text = stderr_data.decode("utf-8", errors="replace").strip()
                    if stderr_text:
                        logger.warning(f"memory_to_notes stderr: {stderr_text[:500]}")

                await proc.wait()

                if proc.returncode != 0:
                    yield f'event: error\ndata: {{"event":"error","message":"Process exited with code {proc.returncode}"}}\n\n'

            except Exception as e:
                logger.exception("Error in memory-to-notes SSE stream")
                yield f'event: error\ndata: {{"event":"error","message":"{str(e)}"}}\n\n'

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",  # disable nginx buffering
            },
        )

    return router
