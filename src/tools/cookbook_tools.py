"""
Cookbook tools — model download, serve, list, search, presets, and generic API bridge.

This is the largest tool group, managing model lifecycle across local and remote hosts.
"""

import asyncio
import json
import logging
import os
import re
import shlex
import time as _time
from collections import defaultdict as _dd
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

from src.tools._helpers import (
    _parse_tool_args,
    _string_arg,
    _internal_headers,
    _INTERNAL_BASE,
    _validate_cookbook_ssh_target,
    _infer_serve_port,
    _infer_serve_host,
    _cookbook_register_task,
    _cookbook_apply_retry_suggestion,
    _scan_running_model_processes,
    _cookbook_servers,
    _resolve_cookbook_host,
    _cookbook_env_for_host,
    _ensure_served_endpoint,
)

logger = logging.getLogger(__name__)


# Paths the generic `app_api` tool will refuse to call. Auth/token/user
# administration and host shell execution are too risky to route through an
# agent surface even when the agent is admin-context; accidental account or
# command mistakes have permanent blast radius.
_APP_API_BLOCKLIST_PREFIXES = (
    "/api/auth",           # login/logout/password
    "/api/users",          # user CRUD
    "/api/tokens",         # api token mgmt
    "/api/admin",          # admin one-shots (wipe etc.)
    "/api/shell",          # host shell execution
    "/api/backup/restore", # destructive restore
)

# (method, prefix) pairs to refuse specifically.
_APP_API_BLOCKLIST_METHOD_PATH = (
    ("GET",    "/api/email/accounts"),
    ("POST",   "/api/cookbook/state"),
    ("DELETE", "/api/cookbook/state"),
    ("POST",   "/api/cookbook/packages/install"),
    ("POST",   "/api/cookbook/rebuild-engine"),
    ("POST",   "/api/cookbook/kill-pid"),
    ("POST",   "/api/model/download"),
    ("POST",   "/api/model/serve"),
    ("POST",   "/api/research/start"),
    ("POST",   "/api/notes"),
    ("PUT",    "/api/notes"),
    ("DELETE", "/api/notes"),
    ("POST",   "/api/calendar/events"),
    ("PUT",    "/api/calendar/events"),
    ("DELETE", "/api/calendar/events"),
)


# ---------------------------------------------------------------------------
# Generic API bridge
# ---------------------------------------------------------------------------

async def do_app_api(content: str, owner: Optional[str] = None) -> Dict:
    """Generic loopback to allowed internal Odysseus API endpoints. Lets the
    agent reach the full UI-button surface (cookbook, email, notes,
    calendar, skills, sessions, gallery, research, etc.) without us
    landing a named tool wrapper for every one.

    Args (JSON):
      action: "call" (default) | "endpoints"
      path:   "/api/cookbook/gpus"     # required for call
      method: "GET" | "POST" | "PUT" | "PATCH" | "DELETE" (default GET)
      body:   <object>                 # JSON body for POST/PUT/PATCH
      query:  <object>                 # querystring params

    The `endpoints` action returns the OpenAPI surface (method + path +
    summary) so the agent can discover what's reachable. A blocklist
    refuses sensitive auth/user/admin/shell paths and method-specific
    host-control routes to keep blast radius bounded.
    """
    import httpx
    try:
        args = _parse_tool_args(content) if content.strip() else {}
    except ValueError:
        return {"error": "Invalid JSON arguments", "exit_code": 1}

    action = (args.get("action") or "call").lower()
    base = _INTERNAL_BASE

    if action == "endpoints":
        kw = (args.get("filter") or "").lower()
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(f"{base}/openapi.json",
                                        headers=_internal_headers())
                data = resp.json()
        except Exception as e:
            return {"error": f"OpenAPI fetch failed: {e}", "exit_code": 1}
        rows: List[Dict[str, Any]] = []
        for path, methods in (data.get("paths") or {}).items():
            if not isinstance(methods, dict):
                continue
            if any(path.startswith(p) for p in _APP_API_BLOCKLIST_PREFIXES):
                continue
            for method, op in methods.items():
                if method.lower() not in ("get", "post", "put", "patch", "delete"):
                    continue
                if any(method.upper() == m and path.startswith(p) for m, p in _APP_API_BLOCKLIST_METHOD_PATH):
                    continue
                summary = (op or {}).get("summary") or (op or {}).get("description") or ""
                if isinstance(summary, str):
                    summary = summary.strip().split("\n")[0][:140]
                if kw and kw not in path.lower() and kw not in (summary or "").lower():
                    continue
                rows.append({"method": method.upper(), "path": path, "summary": summary})
        rows.sort(key=lambda r: (r["path"], r["method"]))
        if not rows:
            return {"output": f"No endpoints match filter {kw!r}." if kw else "No endpoints found.", "exit_code": 0}
        lines = [f"{len(rows)} endpoint(s)" + (f" matching {kw!r}" if kw else "") + ":"]
        for r in rows[:200]:
            line = f"  {r['method']:6s} {r['path']}"
            if r["summary"]:
                line += f"  — {r['summary']}"
            lines.append(line)
        if len(rows) > 200:
            lines.append(f"  ...({len(rows) - 200} more — filter to narrow)")
        return {"output": "\n".join(lines), "endpoints": rows, "exit_code": 0}

    path = args.get("path") or ""
    if not path:
        return {"error": "path is required (e.g. '/api/cookbook/gpus')", "exit_code": 1}
    if not path.startswith("/"):
        path = "/" + path
    if any(path.startswith(p) for p in _APP_API_BLOCKLIST_PREFIXES):
        return {"error": f"Path blocked for safety: {path}. Sensitive endpoints are off-limits via app_api.", "exit_code": 1}

    method = (args.get("method") or "GET").upper()
    if method not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
        return {"error": f"Unsupported method: {method}", "exit_code": 1}
    if any(method == m and path.startswith(p) for m, p in _APP_API_BLOCKLIST_METHOD_PATH):
        if "/api/email/accounts" in path:
            return {"error": "Don't use /api/email/accounts via app_api — it is owner-filtered in tool context and may return empty. Use the `list_email_accounts` email tool, then pass `account` to list_emails/read_email.", "exit_code": 1}
        if "/api/cookbook/packages/install" in path:
            return {"error": "Don't POST /api/cookbook/packages/install via app_api — package installation is host code execution. Use the dedicated Cookbook dependency UI/flow instead.", "exit_code": 1}
        if "/api/cookbook/rebuild-engine" in path:
            return {"error": "Don't POST /api/cookbook/rebuild-engine via app_api — engine rebuild mutates local or remote host state. Use the dedicated Cookbook UI/flow instead.", "exit_code": 1}
        if "/api/cookbook/kill-pid" in path:
            return {"error": "Don't POST /api/cookbook/kill-pid via app_api — process signalling is host control. Use the dedicated Cookbook stop/diagnostic flow instead.", "exit_code": 1}
        if "/api/model/download" in path:
            return {"error": "Don't POST /api/model/download directly — use the `download_model` tool (it resolves the server name, sets the venv env_prefix, and registers the task so it shows in the UI).", "exit_code": 1}
        if "/api/model/serve" in path:
            return {"error": "Don't POST /api/model/serve directly — use the `serve_model` or `serve_preset` tool (handles host resolution, env_prefix, and cookbook tracking).", "exit_code": 1}
        if "/api/research/start" in path:
            return {"error": "Don't POST /api/research/start directly — use the `trigger_research` tool (it surfaces the session in the Deep Research sidebar).", "exit_code": 1}
        if "/api/notes" in path:
            return {"error": "Don't hit /api/notes via app_api — use the `manage_notes` tool. It accepts natural-language due_date ('11pm today', 'tomorrow at 9am'), fires reminders from the due_date itself (no separate calendar event), and uses the caller's timezone. The raw endpoint requires ISO-UTC + a separate calendar event, both of which the agent tends to get wrong.", "exit_code": 1}
        if "/api/calendar/events" in path:
            return {"error": "Don't hit /api/calendar/events via app_api — use the `manage_calendar` tool. It handles tz-aware natural-language datetimes and reminder_minutes correctly. If the user wants a note + reminder, prefer `manage_notes` with due_date — it bundles both.", "exit_code": 1}
        return {"error": f"{method} {path} is blocked — it overwrites the whole cookbook state file. Use list_serve_presets / serve_preset / serve_model instead.", "exit_code": 1}

    body = args.get("body")
    query = args.get("query") or None
    headers = {**_internal_headers(owner=owner), "Content-Type": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.request(
                method, f"{base}{path}",
                json=body if body is not None and method in ("POST", "PUT", "PATCH") else None,
                params=query,
                headers=headers,
            )
        try:
            payload = resp.json()
            preview = json.dumps(payload, indent=2, default=str)
            if len(preview) > 4000:
                preview = preview[:4000] + "\n... (truncated)"
        except Exception:
            payload = None
            preview = (resp.text or "")[:4000]
        if resp.status_code >= 400:
            return {
                "error": f"{method} {path} -> HTTP {resp.status_code}",
                "status_code": resp.status_code,
                "body": preview,
                "exit_code": 1,
            }
        return {
            "output": f"{method} {path} -> {resp.status_code}\n{preview}",
            "status_code": resp.status_code,
            "json": payload,
            "exit_code": 0,
        }
    except Exception as e:
        return {"error": f"{method} {path} failed: {e}", "exit_code": 1}


# ---------------------------------------------------------------------------
# Download / Serve / List / Stop
# ---------------------------------------------------------------------------

async def do_download_model(content: str, owner: Optional[str] = None) -> Dict:
    """Download a HuggingFace model via the cookbook API."""
    import httpx
    try:
        args = _parse_tool_args(content)
    except ValueError:
        return {"error": "Invalid JSON arguments", "exit_code": 1}
    repo_id = args.get("repo_id", "")
    if not repo_id:
        return {"error": "repo_id is required", "exit_code": 1}
    host = (args.get("host") or "").strip()
    if host:
        host = await _resolve_cookbook_host(host)
    _host_defaulted = False
    if not host and not args.get("local"):
        _servers = await _cookbook_servers()
        if _servers.get("default_host"):
            host = _servers["default_host"]
            _host_defaulted = True
    backend = (args.get("backend") or "").strip().lower()
    if not backend and "/" not in repo_id and ":" in repo_id:
        backend = "ollama"
    payload = {"repo_id": repo_id}
    if backend:
        payload["backend"] = backend
    if host:
        payload["remote_host"] = host
    if args.get("include"):
        payload["include"] = args["include"]
    env_cfg = await _cookbook_env_for_host(host)
    if env_cfg.get("env_prefix"): payload["env_prefix"] = env_cfg["env_prefix"]
    if env_cfg.get("hf_token"):   payload["hf_token"]   = env_cfg["hf_token"]
    if env_cfg.get("platform"):   payload["platform"]   = env_cfg["platform"]
    if env_cfg.get("ssh_port"):   payload["ssh_port"]   = env_cfg["ssh_port"]
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(f"{_INTERNAL_BASE}/api/model/download",
                                     json=payload, headers=_internal_headers())
            data = resp.json()
        if data.get("ok"):
            sid = data.get("session_id", "?")
            registered = await _cookbook_register_task(
                session_id=sid, model=repo_id, host=host,
                cmd=(f"ollama pull {repo_id}" if backend == "ollama" else f"hf download {repo_id}"),
                task_type="download",
            )
            note = "" if registered else " (state-write failed — download may not show in UI)"
            where = host or "local"
            default_note = " (defaulted to the cookbook's selected server — pass host= or local=true to override)" if _host_defaulted else ""
            return {
                "output": f"Download started: {repo_id} on {where} (session: {sid}){note}{default_note}",
                "session_id": sid,
                "host": host,
                "task_type": "download",
                "phase": "running",
                "exit_code": 0,
            }
        return {"error": data.get("error", "Download failed"), "exit_code": 1}
    except Exception as e:
        return {"error": str(e), "exit_code": 1}


async def do_serve_model(content: str, owner: Optional[str] = None) -> Dict:
    """Start serving a model via the cookbook API."""
    import httpx
    try:
        args = _parse_tool_args(content)
    except ValueError:
        return {"error": "Invalid JSON arguments", "exit_code": 1}
    repo_id = args.get("repo_id", "")
    cmd = args.get("cmd", "")
    if not repo_id or not cmd:
        return {"error": "repo_id and cmd are required", "exit_code": 1}
    host = (args.get("host") or "").strip()
    if host:
        host = await _resolve_cookbook_host(host)
    if not host and not args.get("local"):
        _servers = await _cookbook_servers()
        if _servers.get("default_host"):
            host = _servers["default_host"]
    payload = {"repo_id": repo_id, "cmd": cmd}
    if host:
        payload["remote_host"] = host
    env_cfg = await _cookbook_env_for_host(host)
    env_path = (env_cfg.get("env_path") or "").rstrip("/")
    env_type = (env_cfg.get("env_type") or env_cfg.get("env") or "").lower()
    if env_type == "venv" and env_path:
        venv_bin = f"{env_path}/bin"
        import re as _re3
        tokens = cmd.split()
        idx = 0
        env_re = _re3.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
        while idx < len(tokens) and env_re.match(tokens[idx]):
            idx += 1
        if idx < len(tokens):
            head = tokens[idx]
            if head in ("vllm", "python3", "python"):
                tokens[idx] = f"{venv_bin}/{head}"
                cmd = " ".join(tokens)
                payload["cmd"] = cmd
    if env_cfg.get("env_prefix"): payload["env_prefix"] = env_cfg["env_prefix"]
    if env_cfg.get("gpus"):       payload["gpus"]       = env_cfg["gpus"]
    if env_cfg.get("hf_token"):   payload["hf_token"]   = env_cfg["hf_token"]
    if env_cfg.get("platform"):   payload["platform"]   = env_cfg["platform"]
    if env_cfg.get("ssh_port"):   payload["ssh_port"]   = env_cfg["ssh_port"]
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(f"{_INTERNAL_BASE}/api/model/serve",
                                     json=payload, headers=_internal_headers())
            data = resp.json()
        if data.get("ok"):
            sid = data.get("session_id", "?")
            endpoint_id = data.get("endpoint_id") or ""
            if endpoint_id:
                endpoint_added = True
            else:
                endpoint_meta = await _ensure_served_endpoint(model=repo_id, cmd=cmd, host=host)
                endpoint_added = bool(endpoint_meta.get("added"))
                endpoint_id = endpoint_meta.get("endpoint_id", "") or endpoint_id
            registered = await _cookbook_register_task(
                session_id=sid, model=repo_id,
                host=host, cmd=cmd, task_type="serve",
                endpoint_added=endpoint_added, endpoint_id=endpoint_id or "",
            )
            note = "" if registered else " (state-write failed — task may not show in UI)"
            return {
                "output": f"Serving {repo_id} (session: {sid}){note}",
                "session_id": sid,
                "task_type": "serve",
                "phase": "running",
                "host": host,
                "endpoint_id": endpoint_id,
                "exit_code": 0,
            }
        err_msg = data.get("error") or data.get("detail") or "Serve failed"
        hint = ""
        if isinstance(err_msg, str) and "cmd" in err_msg.lower():
            hint = (" — the cmd must START with an allowlisted binary "
                    "(vllm, python3, llama-server, ollama, sglang, lmdeploy, node, npx). "
                    "Do NOT prefix with `cd …`, `source …`, or chain with `&&`. "
                    "env_prefix (e.g. `source ~/qwen35-env/bin/activate`) is added "
                    "automatically from the host's saved venv settings.")
        return {"error": f"{err_msg}{hint}", "exit_code": 1}
    except Exception as e:
        return {"error": str(e), "exit_code": 1}


async def do_list_served_models(content: str, owner: Optional[str] = None) -> Dict:
    """List running model servers — merges cookbook-tracked tasks with
    a /proc scan for externally-launched LLM/diffusion processes."""
    import httpx

    cookbook_tasks: List[Dict[str, Any]] = []
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"{_INTERNAL_BASE}/api/cookbook/tasks/status",
                                    headers=_internal_headers())
            cookbook_tasks = (resp.json() or {}).get("tasks") or []
    except Exception as e:
        logger.debug(f"cookbook tasks/status fetch failed: {e}")

    external = await asyncio.to_thread(_scan_running_model_processes)

    merged: List[Dict[str, Any]] = []
    merged.extend(cookbook_tasks)
    cookbook_pids = set()
    for t in cookbook_tasks:
        if isinstance(t, dict) and t.get("pid"):
            cookbook_pids.add(t["pid"])
    for p in external:
        if p.get("pid") not in cookbook_pids:
            merged.append(p)

    if not merged:
        return {
            "output": "No model servers currently running (cookbook task tracker empty; /proc scan found no vLLM / sglang / llama.cpp / Ollama / ComfyUI / A1111 / Fooocus / InvokeAI / TGI / Aphrodite / Triton / Diffusers processes).",
            "exit_code": 0,
        }

    _ORDER = {
        "ready": 0, "running": 1, "loading": 1, "warming": 1,
        "queued": 2, "starting": 2,
        "error": 5, "crashed": 5, "failed": 5,
        "stopped": 6, "killed": 6, "cancelled": 6, "canceled": 6,
        "done": 7, "completed": 7, "finished": 7,
    }
    def _rank(t: Dict[str, Any]) -> int:
        phase = (t.get("phase") or t.get("status") or "unknown").lower()
        return _ORDER.get(phase, 3)
    merged.sort(key=_rank)

    cb_n = len(cookbook_tasks)
    ext_n = len(external)
    live_n = sum(1 for t in merged if _rank(t) <= 2)
    header = []
    if cb_n:
        header.append(f"{cb_n} cookbook-tracked")
    if ext_n:
        header.append(f"{ext_n} external")
    if live_n:
        header.insert(0, f"{live_n} LIVE")
    lines = [f"Running: {', '.join(header)}."]
    for t in merged:
        phase = t.get("phase") or t.get("status", "unknown")
        model = t.get("model", "?")
        remote = t.get("remote", "local")
        sid = t.get("session_id", "?")
        tag = " [external]" if t.get("external") else ""
        lines.append(f"- {model}: {phase} ({remote}, session: {sid}){tag}")
        diag = t.get("diagnosis") if isinstance(t.get("diagnosis"), dict) else None
        if diag:
            lines.append(f"    diagnosis: {diag.get('message')}")
            cmd = t.get("cmd") or ""
            suggestions = diag.get("suggestions") or []
            actionable = []
            for s in suggestions[:3]:
                label = s.get("label") or "retry"
                retry_cmd = _cookbook_apply_retry_suggestion(cmd, s)
                if retry_cmd and retry_cmd != cmd and s.get("op") in {"append", "replace", "remove"}:
                    actionable.append(f"{label}: `{retry_cmd}`")
                else:
                    actionable.append(label)
            if actionable:
                lines.append("    suggestions: " + " | ".join(actionable))
        if t.get("status") == "error" and t.get("output_tail"):
            tail = str(t.get("output_tail") or "").strip()
            if tail:
                _tail_lines = tail.splitlines()
                _shown = _tail_lines[-30:]
                for _i, _ln in enumerate(_tail_lines):
                    if "Traceback (most recent call last)" in _ln or "ERROR" in _ln or "Error:" in _ln:
                        _shown = _tail_lines[_i:_i + 40]
                        break
                lines.append("    recent log:")
                for line in _shown:
                    lines.append(f"      {line[:220]}")
        if t.get("external") and t.get("cmdline_preview"):
            lines.append(f"    cmd: {t['cmdline_preview']}")
    return {"output": "\n".join(lines), "tasks": merged, "exit_code": 0}


async def _cookbook_kill_session(session_id: str, *, remote_host: str = "",
                                 ssh_port: str = "", verb: str = "Stopped") -> Dict:
    """Kill a cookbook tmux session — remote-aware — AND mark the task
    stopped in cookbook_state.json."""
    import httpx
    headers = _internal_headers()
    remote = remote_host or ""
    sport = ssh_port or ""

    state: Dict[str, Any] = {}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{_INTERNAL_BASE}/api/cookbook/state", headers=headers)
            state = resp.json() or {}
    except Exception as e:
        logger.debug(f"cookbook state lookup failed for {session_id}: {e}")
    if not isinstance(state, dict):
        state = {}
    matched = None
    for t in (state.get("tasks") or []):
        if isinstance(t, dict) and (t.get("sessionId") == session_id or t.get("id") == session_id):
            matched = t
            if not remote:
                remote = t.get("remoteHost") or ""
            if not sport:
                sport = t.get("sshPort") or ""
            break

    if remote:
        try:
            remote, sport = _validate_cookbook_ssh_target(remote, sport)
        except HTTPException as e:
            return {"error": str(getattr(e, "detail", e)), "exit_code": 1}
        _pf = f"-p {shlex.quote(str(sport))} " if sport and str(sport) != "22" else ""
        cmd = (
            f"ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no "
            f"{_pf}{shlex.quote(remote)} 'tmux kill-session -t {shlex.quote(session_id)}'"
        )
        target_label = f"{session_id} on {remote}"
    else:
        cmd = f"tmux kill-session -t {shlex.quote(session_id)}"
        target_label = session_id

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(f"{_INTERNAL_BASE}/api/shell/exec",
                                     json={"command": cmd}, headers=headers)
        if resp.status_code >= 400:
            return {"error": f"shell/exec returned HTTP {resp.status_code}: {resp.text[:200]}", "exit_code": 1}
        try:
            data = resp.json()
        except Exception:
            data = {}
        kill_failed = isinstance(data, dict) and data.get("exit_code") not in (None, 0)
        kill_err = ((data.get("stderr") or data.get("error") or "").strip() if isinstance(data, dict) else "")
        already_gone = any(s in kill_err.lower() for s in ("no server running", "can't find session", "session not found"))
        if kill_failed and not already_gone:
            return {"error": f"Failed to {verb.lower()} {target_label}: {kill_err or 'kill-session returned non-zero'}", "exit_code": 1}

        if matched is not None:
            try:
                matched["status"] = "stopped"
                async with httpx.AsyncClient(timeout=10) as client:
                    await client.post(f"{_INTERNAL_BASE}/api/cookbook/state",
                                      json=state, headers=headers)
            except Exception as e:
                logger.debug(f"failed to mark {session_id} stopped in state: {e}")

        suffix = " (was already gone)" if already_gone else ""
        return {"output": f"{verb} {target_label}{suffix}", "exit_code": 0}
    except Exception as e:
        return {"error": str(e), "exit_code": 1}


async def do_stop_served_model(content: str, owner: Optional[str] = None) -> Dict:
    """Stop a running model server by killing its tmux session (remote-aware)."""
    try:
        args = _parse_tool_args(content)
    except ValueError:
        return {"error": "Invalid JSON arguments", "exit_code": 1}
    session_id = args.get("session_id", "")
    if not session_id:
        return {"error": "session_id is required", "exit_code": 1}
    return await _cookbook_kill_session(
        session_id,
        remote_host=args.get("remote_host") or args.get("host") or "",
        ssh_port=args.get("ssh_port") or "",
        verb="Stopped server",
    )


async def do_tail_serve_output(content: str, owner: Optional[str] = None) -> Dict:
    """Capture the last N lines of a cookbook task's tmux pane — remote-aware."""
    import httpx
    try:
        args = _parse_tool_args(content)
    except ValueError:
        return {"error": "Invalid JSON arguments", "exit_code": 1}
    session_id = (args.get("session_id") or "").strip()
    if not session_id:
        return {"error": "session_id is required (from list_served_models)", "exit_code": 1}
    import re as _re
    if not _re.fullmatch(r"[a-zA-Z0-9_-]+", session_id):
        return {"error": "Invalid session_id format", "exit_code": 1}
    try:
        tail = int(args.get("tail") or 400)
    except (TypeError, ValueError):
        tail = 400
    tail = max(20, min(tail, 4000))
    headers = _internal_headers()
    remote = _string_arg(args.get("remote_host") or args.get("host"))
    sport = _string_arg(args.get("ssh_port"))
    if not remote:
        state: Dict[str, Any] = {}
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{_INTERNAL_BASE}/api/cookbook/state", headers=headers)
                state = resp.json() or {}
        except Exception as e:
            logger.debug(f"cookbook state lookup failed for {session_id}: {e}")
        if isinstance(state, dict):
            for t in (state.get("tasks") or []):
                if isinstance(t, dict) and (t.get("sessionId") == session_id or t.get("id") == session_id):
                    remote = t.get("remoteHost") or ""
                    if not sport:
                        sport = t.get("sshPort") or ""
                    break
    if remote:
        try:
            remote, sport = _validate_cookbook_ssh_target(remote, sport)
        except HTTPException as e:
            return {"error": str(getattr(e, "detail", e)), "exit_code": 1}

    log_path = f"/tmp/odysseus-tmux/{session_id}.log"
    pane_inner = f"tmux capture-pane -t {shlex.quote(session_id)} -p -S -{tail} 2>/dev/null"
    file_inner = f"tail -n {tail} {shlex.quote(log_path)} 2>/dev/null"
    inner = (
        f"if [ -s {shlex.quote(log_path)} ]; then {file_inner}; "
        f"else {pane_inner}; fi"
    )
    if remote:
        _pf = f"-p {shlex.quote(str(sport))} " if sport and str(sport) != "22" else ""
        cmd = (
            f"ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no "
            f"{_pf}{shlex.quote(remote)} {shlex.quote(inner)}"
        )
        host_label = remote
    else:
        cmd = inner
        host_label = "local"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(f"{_INTERNAL_BASE}/api/shell/exec",
                                     json={"command": cmd}, headers=headers)
        if resp.status_code >= 400:
            return {"error": f"shell/exec returned HTTP {resp.status_code}: {resp.text[:200]}", "exit_code": 1}
        data = resp.json() if resp.content else {}
        output_text = (data.get("stdout") or "").strip()
        stderr_text = (data.get("stderr") or "").strip()
        rc = data.get("exit_code")
        if rc not in (None, 0) and not output_text:
            already_gone = any(s in (stderr_text or "").lower() for s in ("no server running", "can't find session", "session not found"))
            if already_gone:
                return {"output": f"Tmux session {session_id} on {host_label} is gone (task already exited).", "exit_code": 0, "session_id": session_id, "host": host_label}
            return {"error": f"capture-pane failed on {host_label}: {stderr_text or f'exit {rc}'}", "exit_code": 1}
        import re as _re2
        lines = output_text.splitlines()
        dedup_lines = []
        seen_progress = set()
        progress_re = _re2.compile(r"^([\w./\-]+):\s+(\d+)%")
        for ln in lines:
            m = progress_re.match(ln.strip())
            if m:
                key = (m.group(1), int(m.group(2)) // 10)
                if key in seen_progress:
                    continue
                seen_progress.add(key)
            dedup_lines.append(ln)
        output_text = "\n".join(dedup_lines)
        MAX_CHARS = 8000
        if len(output_text) > MAX_CHARS:
            output_text = "…(earlier output truncated)…\n" + output_text[-MAX_CHARS:]
        return {
            "output": output_text or "(empty pane)",
            "session_id": session_id,
            "host": host_label,
            "tail_lines": tail,
            "exit_code": 0,
        }
    except Exception as e:
        return {"error": str(e), "exit_code": 1}


async def do_list_downloads(content: str, owner: Optional[str] = None) -> Dict:
    """List in-flight model downloads."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"{_INTERNAL_BASE}/api/cookbook/tasks/status",
                                    headers=_internal_headers())
            data = resp.json()
        tasks = [t for t in data.get("tasks", []) if (t.get("type") or "").lower() == "download"]
        if not tasks:
            return {"output": "No downloads in progress.", "exit_code": 0}
        lines = [f"{len(tasks)} download(s) in progress:"]
        for t in tasks:
            phase = t.get("phase") or t.get("status", "unknown")
            model = t.get("model", "?")
            pct = t.get("progress_percent") or t.get("percent")
            pct_str = f" {pct}%" if pct is not None else ""
            lines.append(f"- {model}: {phase}{pct_str} ({t.get('remote', 'local')}, session: {t.get('session_id', '?')})")
        return {"output": "\n".join(lines), "downloads": tasks, "exit_code": 0}
    except Exception as e:
        return {"error": str(e), "exit_code": 1}


async def do_cancel_download(content: str, owner: Optional[str] = None) -> Dict:
    """Cancel a model download by killing its tmux session (remote-aware)."""
    try:
        args = _parse_tool_args(content)
    except ValueError:
        return {"error": "Invalid JSON arguments", "exit_code": 1}
    session_id = args.get("session_id", "")
    if not session_id:
        return {"error": "session_id is required (from list_downloads)", "exit_code": 1}
    return await _cookbook_kill_session(
        session_id,
        remote_host=args.get("remote_host") or args.get("host") or "",
        ssh_port=args.get("ssh_port") or "",
        verb="Cancelled download",
    )


async def do_search_hf_models(content: str, owner: Optional[str] = None) -> Dict:
    """Search HuggingFace via the cookbook /api/cookbook/hf-latest endpoint."""
    import httpx
    try:
        args = _parse_tool_args(content)
    except ValueError:
        return {"error": "Invalid JSON arguments", "exit_code": 1}
    query = args.get("query", "") or args.get("search", "")
    limit = args.get("limit", 10)
    params: Dict[str, str] = {}
    if query:
        params["search"] = query
    if limit:
        params["limit"] = str(limit)
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(f"{_INTERNAL_BASE}/api/cookbook/hf-latest",
                                    params=params, headers=_internal_headers())
            data = resp.json()
        models = data.get("models") if isinstance(data, dict) else data
        if not models:
            return {"output": f"No models found for query: {query!r}", "exit_code": 0}
        lines = [f"Found {len(models)} model(s) for {query!r}:" if query else f"{len(models)} model(s):"]
        for m in models[:limit if isinstance(limit, int) else 10]:
            if isinstance(m, dict):
                name = m.get("repo_id") or m.get("modelId") or m.get("id") or "?"
                dl = m.get("downloads")
                size = m.get("size_gb") or m.get("needed_vram_gb")
                bits = []
                if size:
                    bits.append(f"~{size}GB")
                if dl:
                    bits.append(f"{dl} downloads")
                tail = f" ({', '.join(bits)})" if bits else ""
                lines.append(f"- {name}{tail}")
            else:
                lines.append(f"- {m}")
        return {"output": "\n".join(lines), "models": models, "exit_code": 0}
    except Exception as e:
        return {"error": str(e), "exit_code": 1}


async def do_adopt_served_model(content: str, owner: Optional[str] = None) -> Dict:
    """Register an externally-launched model server into the Cookbook."""
    import httpx
    try:
        args = _parse_tool_args(content)
    except ValueError:
        return {"error": "Invalid JSON arguments", "exit_code": 1}

    host = _string_arg(args.get("host") or args.get("remote_host"))
    sess = (args.get("tmux_session") or args.get("session_id") or "").strip()
    model = (args.get("model") or args.get("repo_id") or "").strip()
    port = args.get("port") or 8000
    display_name = (args.get("name") or "").strip() or (model.split("/")[-1] if "/" in model else model)
    add_endpoint = args.get("add_endpoint", True)

    if not sess or not model:
        return {"error": "tmux_session and model are required", "exit_code": 1}

    if host:
        try:
            host, _ = _validate_cookbook_ssh_target(host)
        except HTTPException as e:
            return {"error": str(getattr(e, "detail", e)), "exit_code": 1}

    headers = _internal_headers()
    if host:
        check = f"ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no {shlex.quote(host)} 'tmux has-session -t {shlex.quote(sess)} 2>&1'"
    else:
        check = f"tmux has-session -t {shlex.quote(sess)} 2>&1"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(f"{_INTERNAL_BASE}/api/shell/exec",
                                  json={"command": check}, headers=headers)
            data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code >= 400 or (data.get("exit_code") not in (None, 0)):
            err = (data.get("stderr") or data.get("error") or r.text[:200]).strip()
            return {"error": f"tmux session {sess!r} not found on {host or 'local'}: {err}", "exit_code": 1}
    except Exception as e:
        return {"error": f"verify failed: {e}", "exit_code": 1}

    if host:
        health_cmd = f"ssh -o ConnectTimeout=5 {shlex.quote(host)} 'curl -s -m 3 http://localhost:{int(port)}/v1/models'"
    else:
        health_cmd = f"curl -s -m 3 http://localhost:{int(port)}/v1/models"
    server_up = False
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(f"{_INTERNAL_BASE}/api/shell/exec",
                                  json={"command": health_cmd}, headers=headers)
            body = (r.json() or {}).get("stdout", "") if r.headers.get("content-type", "").startswith("application/json") else ""
            server_up = '"data"' in body or '"object"' in body
    except Exception:
        pass

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{_INTERNAL_BASE}/api/cookbook/state", headers=headers)
            state = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    except Exception as e:
        return {"error": f"could not read cookbook state: {e}", "exit_code": 1}
    if not isinstance(state, dict):
        state = {}
    tasks = state.get("tasks") if isinstance(state.get("tasks"), list) else []
    if any(isinstance(t, dict) and t.get("sessionId") == sess for t in tasks):
        adopted_already = True
    else:
        adopted_already = False
        import time as _time
        new_task = {
            "id": sess,
            "sessionId": sess,
            "name": display_name,
            "type": "serve",
            "status": "running",
            "output": (
                f"Adopted externally-launched session {sess!r} on {host or 'local'}.\n"
                "Reconnect polling will start streaming tmux output shortly."
            ),
            "ts": int(_time.time() * 1000),
            "payload": {"repo_id": model, "remote_host": host or "", "_cmd": "(adopted — launched outside cookbook)"},
            "remoteHost": host or "",
            "sshPort": "",
            "platform": "linux",
            "_serveReady": bool(server_up),
            "_endpointAdded": False,
            "_adoptedExternally": True,
        }
        tasks.append(new_task)
        state["tasks"] = tasks
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(f"{_INTERNAL_BASE}/api/cookbook/state",
                                  json=state, headers=headers)
        except Exception as e:
            return {"error": f"could not save cookbook state: {e}", "exit_code": 1}

    endpoint_msg = ""
    if add_endpoint:
        host_only = host.split("@", 1)[-1] if host else "localhost"
        endpoint_url = f"http://{host_only}:{int(port)}/v1"
        from src.tools.admin_tools import do_manage_endpoints
        try:
            ep_result = await do_manage_endpoints(json.dumps({
                "action": "add",
                "name": display_name,
                "endpoint_url": endpoint_url,
                "is_local": False,
            }), owner=owner)
            if isinstance(ep_result, dict) and not ep_result.get("error"):
                endpoint_msg = f" Endpoint {endpoint_url} added as {display_name!r}."
            else:
                endpoint_msg = f" Endpoint registration skipped: {(ep_result or {}).get('error', 'unknown')}"
        except Exception as e:
            endpoint_msg = f" Endpoint registration failed: {e}"

    return {
        "output": (
            f"Adopted session {sess!r} ({model}) on {host or 'local'}:{port}. "
            + ("Already tracked — skipped state write. " if adopted_already else "Added to cookbook state. ")
            + ("Server responding. " if server_up else "Server not responding yet (still loading?). ")
            + endpoint_msg
        ).strip(),
        "session_id": sess,
        "host": host,
        "port": int(port),
        "server_up": server_up,
        "exit_code": 0,
    }


async def do_list_cookbook_servers(content: str, owner: Optional[str] = None) -> Dict:
    """List the cookbook's configured servers and which one is the current default."""
    servers = await _cookbook_servers()
    hosts = servers.get("hosts") or []
    default = servers.get("default_host") or ""
    if not hosts:
        return {"output": "No cookbook servers configured. Downloads/serves default to localhost.", "servers": [], "default_host": "", "exit_code": 0}
    default_name = next((h.get("name") for h in hosts if h.get("host") == default and h.get("name")), default or "local")
    lines = [f"{len(hosts)} configured server(s) (default: {default_name}):"]
    for h in hosts:
        name = h.get("name") or "(unnamed)"
        host = h.get("host") or "local"
        mark = " ← default" if h.get("host") == default else ""
        env_bit = f" [{h.get('env')}: {h.get('envPath')}]" if h.get("env") and h.get("env") != "none" else ""
        plat = f" ({h.get('platform')})" if h.get("platform") else ""
        lines.append(f"- {name} → {host}{plat}{env_bit}{mark}")
    lines.append("\nRefer to servers by their name (e.g. download_model with host=\"gpu-box\").")
    return {"output": "\n".join(lines), "servers": hosts, "default_host": default, "exit_code": 0}


async def do_list_serve_presets(content: str, owner: Optional[str] = None) -> Dict:
    """List saved serve presets from cookbook_state.json."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{_INTERNAL_BASE}/api/cookbook/state",
                                    headers=_internal_headers())
            state = resp.json() or {}
    except Exception as e:
        return {"error": f"Failed to fetch cookbook state: {e}", "exit_code": 1}

    presets = state.get("presets") or []
    if not presets:
        return {
            "output": "No serve presets saved. Tell the user to save one from the Cookbook UI first, or use serve_model with explicit repo_id + cmd + host.",
            "presets": [],
            "exit_code": 0,
        }
    lines = [f"{len(presets)} saved serve preset(s):"]
    for p in presets:
        if not isinstance(p, dict):
            continue
        name = p.get("name", "?")
        model = p.get("model") or p.get("modelId") or "?"
        host = p.get("host") or p.get("remoteHost") or "local"
        port = p.get("port", "")
        cmd = (p.get("cmd") or "").strip()
        bits = [f"- {name}: {model}", f"host={host}"]
        if port:
            bits.append(f"port={port}")
        lines.append("  ".join(bits))
        if cmd:
            cmd_preview = cmd if len(cmd) < 140 else cmd[:140] + "…"
            lines.append(f"    cmd: {cmd_preview}")
    return {"output": "\n".join(lines), "presets": presets, "exit_code": 0}


async def do_serve_preset(content: str, owner: Optional[str] = None) -> Dict:
    """Launch a saved serve preset by name."""
    import httpx
    try:
        args = _parse_tool_args(content)
    except ValueError:
        return {"error": "Invalid JSON arguments", "exit_code": 1}
    name = (args.get("name") or args.get("preset") or "").strip()
    if not name:
        return {"error": "name (preset name) is required. Call list_serve_presets to see what's available.", "exit_code": 1}

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{_INTERNAL_BASE}/api/cookbook/state",
                                    headers=_internal_headers())
            state = resp.json() or {}
    except Exception as e:
        return {"error": f"Failed to fetch cookbook state: {e}", "exit_code": 1}

    presets = state.get("presets") or []
    chosen = None
    lname = name.lower()
    for p in presets:
        if isinstance(p, dict) and (p.get("name") or "").lower() == lname:
            chosen = p
            break
    if chosen is None:
        for p in presets:
            if isinstance(p, dict) and lname in (p.get("name") or "").lower():
                chosen = p
                break
    if chosen is None:
        sample = ", ".join((p.get("name") or "?") for p in presets[:8] if isinstance(p, dict))
        return {"error": f"No preset matching {name!r}. Available: {sample or '(none)'}", "exit_code": 1}

    repo_id = chosen.get("model") or chosen.get("modelId") or ""
    cmd = (chosen.get("cmd") or "").strip()
    host = chosen.get("host") or chosen.get("remoteHost") or ""
    if not repo_id or not cmd:
        return {"error": f"Preset {chosen.get('name')!r} is missing model or cmd — can't launch.", "exit_code": 1}

    payload: Dict[str, Any] = {"repo_id": repo_id, "cmd": cmd}
    if host:
        payload["remote_host"] = host
    env_cfg = await _cookbook_env_for_host(host)
    if env_cfg.get("env_prefix"): payload["env_prefix"] = env_cfg["env_prefix"]
    if env_cfg.get("gpus"):       payload["gpus"]       = env_cfg["gpus"]
    if env_cfg.get("hf_token"):   payload["hf_token"]   = env_cfg["hf_token"]
    if env_cfg.get("platform"):   payload["platform"]   = env_cfg["platform"]
    if env_cfg.get("ssh_port"):
        payload["ssh_port"] = env_cfg["ssh_port"]

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(f"{_INTERNAL_BASE}/api/model/serve",
                                     json=payload, headers=_internal_headers())
            data = resp.json()
        if data.get("ok"):
            sid = data.get("session_id", "?")
            endpoint_id = data.get("endpoint_id") or ""
            if endpoint_id:
                endpoint_added = True
            else:
                endpoint_meta = await _ensure_served_endpoint(model=repo_id, cmd=cmd, host=host)
                endpoint_added = bool(endpoint_meta.get("added"))
                endpoint_id = endpoint_meta.get("endpoint_id", "") or endpoint_id
            registered = await _cookbook_register_task(
                session_id=sid, model=repo_id, host=host,
                cmd=cmd, task_type="serve",
                endpoint_added=endpoint_added, endpoint_id=endpoint_id or "",
            )
            note = "" if registered else " (state-write failed — task may not show in UI)"
            return {"output": f"Launched preset {chosen.get('name')!r}: {repo_id} on {host or 'local'} (session: {sid}){note}", "session_id": sid, "host": host, "endpoint_id": endpoint_id, "exit_code": 0}
        return {"error": data.get("error", "Serve failed"), "exit_code": 1}
    except Exception as e:
        return {"error": str(e), "exit_code": 1}


async def do_list_cached_models(content: str, owner: Optional[str] = None) -> Dict:
    """List models already cached locally and/or on remote hosts."""
    import httpx
    try:
        args = _parse_tool_args(content) if content.strip() else {}
    except ValueError:
        return {"error": "Invalid JSON arguments", "exit_code": 1}
    raw_host = (args.get("host") or "").strip()
    headers = _internal_headers()

    async def _scan_one(host_label: str, host_val: str, ssh_port: str = "",
                        platform: str = "", model_dir: str = "") -> list:
        p: Dict[str, str] = {}
        if host_val:
            p["host"] = host_val
        if args.get("model_dir"):
            p["model_dir"] = args["model_dir"]
        elif model_dir:
            p["model_dir"] = model_dir
        if ssh_port:
            p["ssh_port"] = ssh_port
        elif args.get("ssh_port"):
            p["ssh_port"] = str(args["ssh_port"])
        if platform:
            p["platform"] = platform
        elif args.get("platform"):
            p["platform"] = args["platform"]
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.get(f"{_INTERNAL_BASE}/api/model/cached",
                                        params=p, headers=headers)
                data = resp.json()
            ms = data.get("models", []) if isinstance(data, dict) else (data or [])
            for m in ms:
                m["host"] = host_label or "local"
            return ms or []
        except Exception as e:
            logger.debug(f"list_cached_models scan({host_label}) failed: {e}")
            return []

    try:
        servers: list = []
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                st = await client.get(f"{_INTERNAL_BASE}/api/cookbook/state", headers=headers)
                st_data = st.json() if st.headers.get("content-type", "").startswith("application/json") else {}
            servers = (st_data.get("env", {}) or {}).get("servers") or []
        except Exception as e:
            logger.debug(f"server list fetch failed: {e}")
            st_data = {}

        def _dirs_for(server_record: Dict[str, Any]) -> str:
            mds = server_record.get("modelDirs") if isinstance(server_record, dict) else None
            HF_DEFAULTS = {"~/.cache/huggingface/hub", "~/.cache/huggingface"}
            if isinstance(mds, list):
                extras = [d for d in mds if isinstance(d, str) and d.strip() and d.strip() not in HF_DEFAULTS]
                return ",".join(extras)
            if isinstance(mds, str) and mds.strip() not in HF_DEFAULTS:
                return mds
            return ""

        if raw_host:
            host = await _resolve_cookbook_host(raw_host)
            srv = next(
                (s for s in servers if isinstance(s, dict)
                 and (s.get("name") == raw_host or s.get("host") == host or s.get("host") == raw_host)),
                {},
            )
            models = await _scan_one(raw_host, host, model_dir=_dirs_for(srv))
        else:
            local_srv = next((s for s in servers if isinstance(s, dict) and not (s.get("host") or "").strip()), {})
            scans: list = [_scan_one("local", "", model_dir=_dirs_for(local_srv))]
            for s in servers:
                if not isinstance(s, dict):
                    continue
                name = s.get("name") or s.get("host")
                host_val = s.get("host") or ""
                if not host_val:
                    continue
                scans.append(_scan_one(
                    name,
                    host_val,
                    ssh_port=str(s.get("port") or ""),
                    platform=s.get("platform") or "",
                    model_dir=_dirs_for(s),
                ))
            results = await asyncio.gather(*scans, return_exceptions=False)
            seen = set()
            models: list = []
            for batch in results:
                for m in batch:
                    key = (m.get("host", ""), m.get("repo_id", ""))
                    if key in seen:
                        continue
                    seen.add(key)
                    models.append(m)
        if not models:
            downloaded = []
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    st = await client.get(f"{_INTERNAL_BASE}/api/cookbook/state", headers=headers)
                    state = st.json() if st.headers.get("content-type", "").startswith("application/json") else {}
                for t in (state.get("tasks") or []):
                    if not isinstance(t, dict) or t.get("type") != "download":
                        continue
                    if (t.get("status") or "").lower() not in {"done", "completed"}:
                        continue
                    task_host = t.get("remoteHost") or (t.get("payload") or {}).get("remote_host") or ""
                    if raw_host and task_host != raw_host:
                        continue
                    repo = t.get("modelId") or t.get("repoId") or (t.get("payload") or {}).get("repo_id") or t.get("name")
                    if repo and repo not in downloaded:
                        downloaded.append(repo)
            except Exception:
                downloaded = []
            host_str = f" on {raw_host}" if raw_host else ""
            if downloaded:
                lines = [f"No cache paths were detected{host_str}, but Cookbook has completed download task(s):"]
                lines.extend(f"- {repo} — downloaded via Cookbook task" for repo in downloaded)
                return {"output": "\n".join(lines), "models": [{"repo_id": repo, "source": "cookbook_task"} for repo in downloaded], "exit_code": 0}
            return {"output": f"No cached models found{host_str}.", "exit_code": 0}
        if raw_host:
            lines = [f"{len(models)} cached model(s) on {raw_host}:"]
            for m in models:
                name = m.get("repo_id", "?")
                sz = m.get("size") or (f"{m.get('size_bytes', 0) / (1024**3):.1f}GB" if m.get("size_bytes") else "")
                inc = " (incomplete)" if m.get("has_incomplete") else ""
                kind = " [diffusion]" if m.get("is_diffusion") else ""
                lines.append(f"- {name}{kind} — {sz}{inc}")
        else:
            by_host = _dd(list)
            for m in models:
                by_host[m.get("host", "local")].append(m)
            lines = [f"{len(models)} cached model(s) across {len(by_host)} server(s):"]
            for host_name in sorted(by_host.keys()):
                lines.append(f"\n[{host_name}]")
                for m in by_host[host_name]:
                    name = m.get("repo_id", "?")
                    sz = m.get("size") or (f"{m.get('size_bytes', 0) / (1024**3):.1f}GB" if m.get("size_bytes") else "")
                    inc = " (incomplete)" if m.get("has_incomplete") else ""
                    kind = " [diffusion]" if m.get("is_diffusion") else ""
                    backend = f" ({m.get('backend')})" if m.get("backend") else ""
                    lines.append(f"- {name}{kind}{backend} — {sz}{inc}")
        return {"output": "\n".join(lines), "models": models, "exit_code": 0}
    except Exception as e:
        return {"error": str(e), "exit_code": 1}
