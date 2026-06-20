"""
Research tools — list, read, delete deep-research results; trigger new research.
"""

import json as _json
import logging
import re
from pathlib import Path as _Path
from typing import Any, Dict, List, Optional

from src.constants import DEEP_RESEARCH_DIR
from src.tools._helpers import _parse_tool_args, _internal_headers, _INTERNAL_BASE

logger = logging.getLogger(__name__)


async def do_manage_research(content: str, owner: Optional[str] = None) -> Dict:
    """List, read/open, or delete saved deep-research results from the Library.
    Args (JSON): {"action": "list|read|delete", "id": "<id>", "search": "..."}.
    Research is stored as data/deep_research/<id>.json (query, summary, sources)."""
    try:
        args = _parse_tool_args(content) if content.strip().startswith("{") else {}
    except ValueError:
        args = {}
    if not isinstance(args, dict):
        args = {}
    action = (args.get("action") or "list").lower()
    rid = (args.get("id") or args.get("session_id") or args.get("research_id") or "").strip()
    data_dir = _Path(DEEP_RESEARCH_DIR)

    if rid and not re.fullmatch(r"[A-Za-z0-9_-]+", rid):
        return {"error": "Invalid research id."}

    def _load(p):
        try:
            return _json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None

    if action in ("read", "open", "view", "get"):
        if not rid:
            return {"error": "Provide the research id (from action='list')."}
        p = data_dir / f"{rid}.json"
        if not p.exists():
            return {"error": f"Research '{rid}' not found."}
        d = _load(p) or {}
        summary = d.get("result") or d.get("raw_report") or d.get("summary") or d.get("report") or "(no report body)"
        srcs = d.get("sources", []) or []
        out = f"# {d.get('query', '(untitled)')}\n\n{summary}"
        if srcs:
            out += "\n\nSources:\n" + "\n".join(
                f"- {s.get('title') or s.get('url', '')}: {s.get('url', '')}" for s in srcs[:30]
            )
        return {"output": out[:16000], "exit_code": 0}

    if action == "delete":
        if not rid:
            return {"error": "Provide the research id to delete (from action='list')."}
        p = data_dir / f"{rid}.json"
        if p.exists():
            try:
                p.unlink()
            except Exception as e:
                return {"error": f"Failed to delete: {e}"}
            return {"output": f"Deleted research '{rid}'.", "exit_code": 0}
        return {"error": f"Research '{rid}' not found."}

    search = (args.get("search") or "").lower()
    items = []
    if data_dir.exists():
        for p in data_dir.glob("*.json"):
            d = _load(p)
            if not d:
                continue
            q = d.get("query", "")
            if search and search not in q.lower():
                continue
            items.append((d.get("completed_at", 0) or 0, p.stem, q, len(d.get("sources", []) or [])))
    items.sort(reverse=True)
    if not items:
        return {"output": "No research found in the library." + (f" (search: {search})" if search else ""), "exit_code": 0}
    rows = "\n".join(f"- [{q or '(untitled)'}](#research-{sid}) — {n} sources" for _, sid, q, n in items[:50])
    return {"output": f"Research library ({len(items)} item{'s' if len(items) != 1 else ''}):\n{rows}", "exit_code": 0}


async def do_trigger_research(content: str, owner: Optional[str] = None) -> Dict:
    """Start a live deep-research job that appears in the Deep Research
    sidebar. Hits /api/research/start (the same path the sidebar's
    'Research' button uses) so the session is discoverable + streamable
    there, rather than creating a scheduled task that never surfaces."""
    import httpx
    try:
        args = _parse_tool_args(content)
    except ValueError:
        return {"error": "Invalid JSON arguments", "exit_code": 1}
    topic = args.get("topic", "") or args.get("query", "")
    if not topic:
        return {"error": "topic (or query) is required", "exit_code": 1}
    payload: Dict[str, Any] = {"query": topic}
    if args.get("max_rounds") is not None:
        try: payload["max_rounds"] = int(args["max_rounds"])
        except (ValueError, TypeError): pass
    if args.get("max_time") is not None:
        try: payload["max_time"] = int(args["max_time"])
        except (ValueError, TypeError): pass
    if args.get("category"):
        payload["category"] = args["category"]
    if args.get("search_provider"):
        payload["search_provider"] = args["search_provider"]
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(f"{_INTERNAL_BASE}/api/research/start",
                                     json=payload, headers=_internal_headers(owner))
        if resp.status_code >= 400:
            return {"error": f"research/start returned HTTP {resp.status_code}: {resp.text[:200]}", "exit_code": 1}
        data = resp.json()
        sid = data.get("session_id", "?")
        return {
            "output": (
                f"Deep research started: [{topic}](#research-{sid}). "
                "Click to open the Deep Research sidebar and watch progress / read the report."
            ),
            "session_id": sid,
            "anchor": f"[{topic}](#research-{sid})",
            "ui_event": "research_started",
            "research_session_id": sid,
            "exit_code": 0,
        }
    except Exception as e:
        return {"error": str(e), "exit_code": 1}
