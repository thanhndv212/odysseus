"""
Shared internal helpers extracted from tool_implementations.py.

These are used by multiple domain tool modules and should not import
from other src.tools.* modules to avoid circular dependencies.
"""

import asyncio
import json
import logging
import os
import re
import time as _time
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException
from src.constants import VAULT_FILE
from src.tool_utils import get_mcp_manager
from core.constants import internal_api_base
from routes._validators import validate_remote_host, validate_ssh_port

logger = logging.getLogger(__name__)

# In-process loopback base for agent tools that call Odysseus's own API
# (cookbook state, model serve, gallery, email, calendar). We ride the
# per-process internal token so require_admin lets us through. See
# core/middleware.py. Resolution (override / APP_PORT / 7000) lives in
# core.constants.internal_api_base().
_INTERNAL_BASE = internal_api_base()


# ---------------------------------------------------------------------------
# General argument/string helpers
# ---------------------------------------------------------------------------

def _string_arg(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _parse_tool_args(content):
    """Parse a tool-call argument blob.

    Accepts either a JSON string or an already-decoded dict. Unwraps the
    common `{"body": {...}}` envelope that smaller models emit when they
    read tool descriptions like "Body is JSON: {...}" literally — they
    pass `body` as a field name rather than treating it as a noun.

    Returns a dict on success, raises ValueError on bad JSON.
    """
    if isinstance(content, str):
        try:
            args = json.loads(content) if content.strip() else {}
        except (json.JSONDecodeError, TypeError) as e:
            raise ValueError(str(e))
    elif isinstance(content, dict):
        args = content
    else:
        args = {}
    # Unwrap {"body": {...}} envelope — but only if `body` is the sole key
    # and points at a dict. We don't want to clobber a legitimate `body`
    # field on tools where it's a real arg (e.g. send_email body text).
    if (
        isinstance(args, dict)
        and len(args) == 1
        and "body" in args
        and isinstance(args["body"], dict)
        and "action" in args["body"]  # extra safety: only unwrap if the inner dict looks like a tool call
    ):
        args = args["body"]
    return args


def _skill_dump(sk) -> Dict:
    """Translate a parsed Skill back into the kwargs `update_skill` expects."""
    return {
        "name": sk.name,
        "description": sk.description,
        "version": sk.version,
        "category": sk.category,
        "tags": sk.tags,
        "platforms": sk.platforms,
        "requires_toolsets": sk.requires_toolsets,
        "fallback_for_toolsets": sk.fallback_for_toolsets,
        "status": sk.status,
        "confidence": sk.confidence,
        "source": sk.source,
        "teacher_model": sk.teacher_model,
        "owner": sk.owner,
        "when_to_use": sk.when_to_use,
        "procedure": sk.procedure,
        "pitfalls": sk.pitfalls,
        "verification": sk.verification,
        "body_extra": sk.body_extra,
    }


def _validate_cookbook_ssh_target(remote_host: Any, ssh_port: Any = "") -> tuple[str, str]:
    remote = validate_remote_host(_string_arg(remote_host) or None) or ""
    sport = validate_ssh_port(_string_arg(ssh_port) or None) or ""
    return remote, sport


# ---------------------------------------------------------------------------
# MCP command validation
# ---------------------------------------------------------------------------

# Parallel to routes/cookbook_helpers._validate_serve_cmd but deliberately the
# opposite policy: that gate guards an admin-only serve command and allows
# interpreters (python3/etc) because model-serving needs them, whereas this is
# the model/prompt-injection-reachable manage_mcp path, so interpreters and
# runners are denied here.
#
# Commands that can execute arbitrary code regardless of their arguments. These
# are NEVER accepted on the manage_mcp agent path, even if an operator lists one
# in ODYSSEUS_MCP_ALLOWED_COMMANDS -- a stdio server that genuinely needs an
# interpreter or package runner must be registered via the trusted admin route.
_MCP_DENIED_COMMANDS = frozenset({
    "sh", "bash", "zsh", "fish", "dash", "ksh", "csh", "tcsh", "ash", "busybox",
    "cmd", "command.com", "powershell", "pwsh",
    "python", "pypy", "node", "nodejs", "deno", "bun", "ruby", "jruby",
    "perl", "raku", "php", "lua", "luajit", "tclsh", "wish", "expect", "rscript",
    "groovy", "scala", "elixir", "erl", "iex", "java", "javac", "jshell", "jbang",
    "kotlin", "kotlinc", "dotnet", "mono", "swift", "osascript", "tsx", "ts-node",
    "npx", "bunx", "uvx", "pipx", "npm", "pnpm", "yarn", "pip", "uv",
    "gem", "cargo", "go", "bundle", "poetry", "conda", "mamba", "brew",
    "apt", "apt-get", "yum", "dnf", "pacman", "apk",
    "env", "xargs", "nohup", "setsid", "nice", "ionice", "time", "timeout",
    "watch", "stdbuf", "unbuffer", "script", "ssh", "scp", "sshpass", "sudo",
    "doas", "su", "make", "cmake", "docker", "podman", "kubectl", "find",
    "awk", "gawk", "sed", "vi", "vim", "nvim", "emacs", "ed", "tee", "eval",
})

# Argv flags that make even an allowlisted binary execute inline code. Matched
# by prefix so glued forms (-cimport os, --eval=...) are caught, not just the
# exact-token form.
_MCP_CODE_EXEC_SHORT_FLAGS = ("-c", "-e", "-m")
_MCP_CODE_EXEC_LONG_FLAGS = ("--eval", "--exec", "--print", "--module", "--command", "--require")

_MCP_URL_SCHEMES = ("http://", "https://", "ftp://", "ftps://", "file://", "data:", "jar:", "blob:")

# Shell metacharacters refused in command/args. Args are passed as an argv list
# (no shell), but refusing these keeps the surface narrow and obvious.
_MCP_SHELL_METACHARS = set(";|&$`><\n\r")

# Env vars that let a child process load attacker-supplied code before main().
_MCP_DANGEROUS_ENV = frozenset({
    "LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT", "DYLD_INSERT_LIBRARIES",
    "DYLD_LIBRARY_PATH", "DYLD_FRAMEWORK_PATH", "PYTHONPATH", "PYTHONSTARTUP",
    "PYTHONHOME", "PYTHONEXECUTABLE", "NODE_OPTIONS", "NODE_PATH", "BASH_ENV",
    "ENV", "SHELLOPTS", "PERL5LIB", "PERL5OPT", "RUBYOPT", "RUBYLIB", "GEM_PATH",
    "R_PROFILE", "R_HOME", "PATH", "IFS", "PROMPT_COMMAND",
})


def _mcp_allowed_commands() -> set:
    """Operator-configured allowlist of safe MCP launcher basenames for the agent
    path. Empty by default; set ODYSSEUS_MCP_ALLOWED_COMMANDS (comma-separated)
    to opt specific trusted binaries in. Denied commands are rejected even if
    listed here."""
    raw = os.environ.get("ODYSSEUS_MCP_ALLOWED_COMMANDS", "")
    return {c.strip().lower() for c in raw.split(",") if c.strip()}


def _validate_mcp_command(command, args, env) -> Optional[str]:
    """Validate a model-supplied stdio MCP registration. Returns an error string
    if it must be rejected, else None.

    Closes the RCE where manage_mcp 'add' passed prompt-injection-controlled
    command/args/env straight to a subprocess spawn (issue #438): a payload
    smuggled into a skill description, memory entry, fetched page, or email body
    could register a stdio server running arbitrary code as the app UID.
    """
    if not isinstance(command, str) or not command.strip():
        return "command must be a non-empty string"
    command = command.strip()
    if "/" in command or "\\" in command:
        return "command must be a bare executable name, not a path"
    if any(ch in _MCP_SHELL_METACHARS for ch in command):
        return "command contains shell metacharacters"
    base = command.lower()
    if base.endswith(".exe") or base.endswith(".cmd") or base.endswith(".bat"):
        base = base.rsplit(".", 1)[0]
    # Canonicalize a trailing version suffix so versioned aliases collapse to the
    # family name (python3.11 -> python, node18 -> node, pip3 -> pip); both the
    # raw basename and the canonical form are denied, so an operator cannot
    # accidentally allowlist a runtime alias back into the path.
    canon = re.sub(r"[-_.]?\d+(?:\.\d+)*$", "", base)
    if base in _MCP_DENIED_COMMANDS or canon in _MCP_DENIED_COMMANDS:
        return (
            f"command '{command}' is not allowed on the agent MCP path: "
            "interpreters, runtimes, package runners, and shells can execute "
            "arbitrary code. Register such a server via the admin route instead."
        )
    if base not in _mcp_allowed_commands():
        return (
            f"command '{command}' is not in the MCP allowlist. Add it to "
            "the ODYSSEUS_MCP_ALLOWED_COMMANDS if you trust it, or register the "
            "server via the admin route."
        )

    if args is not None:
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                return "args must be a JSON list"
        if not isinstance(args, list):
            return "args must be a list"
        for a in args:
            if not isinstance(a, str):
                return "args must all be strings"
            s = a.strip()
            low = s.lower()
            if any(s == f or s.startswith(f) for f in _MCP_CODE_EXEC_SHORT_FLAGS):
                return f"arg '{a}' is a code-execution flag and is not allowed"
            if any(low == f or low.startswith(f + "=") for f in _MCP_CODE_EXEC_LONG_FLAGS):
                return f"arg '{a}' is a code-execution flag and is not allowed"
            if any(low.startswith(u) for u in _MCP_URL_SCHEMES):
                return f"arg '{a}' is a remote URL and is not allowed"
            if any(ch in _MCP_SHELL_METACHARS for ch in a):
                return f"arg '{a}' contains shell metacharacters"

    if env:
        if isinstance(env, str):
            try:
                env = json.loads(env)
            except Exception:
                return "env must be a JSON object"
        if not isinstance(env, dict):
            return "env must be an object"
        for k in env:
            if str(k).strip().upper() in _MCP_DANGEROUS_ENV:
                return f"env var '{k}' can inject code into the child process and is not allowed"

    return None


# ---------------------------------------------------------------------------
# Internal HTTP helpers (loopback to Odysseus's own API)
# ---------------------------------------------------------------------------

def _internal_headers(owner: Optional[str] = None) -> Dict[str, str]:
    from core.middleware import INTERNAL_TOOL_HEADER, INTERNAL_TOOL_TOKEN
    headers = {INTERNAL_TOOL_HEADER: INTERNAL_TOOL_TOKEN}
    if owner:
        headers["X-Odysseus-Owner"] = owner
    return headers


# ---------------------------------------------------------------------------
# Cookbook helpers (shared across cookbook tools)
# ---------------------------------------------------------------------------

async def _cookbook_servers() -> Dict[str, Any]:
    """Return the cookbook's configured servers + the currently-selected
    default host. Shape: {default_host, hosts: [{host, platform, env, envPath}]}.
    The agent uses this to route downloads/serves to the right machine
    instead of silently defaulting to localhost."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{_INTERNAL_BASE}/api/cookbook/state", headers=_internal_headers())
            state = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    except Exception:
        return {"default_host": "", "hosts": []}
    env = (state or {}).get("env") or {}
    if not isinstance(env, dict):
        return {"default_host": "", "hosts": []}
    hosts = []
    for s in (env.get("servers") or []):
        if isinstance(s, dict):
            hosts.append({
                "name": s.get("name") or "",
                "host": s.get("host") or "",   # "" = Local
                "platform": s.get("platform") or "",
                "env": s.get("env") or "",
                "envPath": s.get("envPath") or "",
                "port": s.get("port") or "",
            })
    return {"default_host": env.get("remoteHost") or "", "hosts": hosts}


async def _resolve_cookbook_host(name_or_host: str) -> str:
    """Map a friendly server NAME ('gpu-box', 'workstation') to its ssh host
    string ('user@192.0.2.10'). If the input already looks like an
    ssh host (contains '@' or matches a known host), or matches nothing,
    it's returned unchanged. 'local'/'localhost' → '' (this machine)."""
    if not name_or_host:
        return ""
    val = name_or_host.strip()
    low = val.lower()
    if low in ("local", "localhost", "this machine", "here"):
        return ""
    servers = await _cookbook_servers()
    # Exact host match → already an ssh host
    for h in servers.get("hosts") or []:
        if h.get("host") and h["host"] == val:
            return val
    # Name match (case-insensitive)
    for h in servers.get("hosts") or []:
        if (h.get("name") or "").lower() == low:
            return h.get("host") or ""   # "" for the Local entry
    # Substring name match as a fallback
    for h in servers.get("hosts") or []:
        if low and low in (h.get("name") or "").lower():
            return h.get("host") or ""
    # No match — assume the caller passed a raw host/alias; return as-is
    # (ssh can resolve aliases from ~/.ssh/config).
    return val


async def _cookbook_env_for_host(host: str) -> Dict[str, Any]:
    """Resolve env_prefix / gpus / platform / hf_token / ssh_port for a
    given host by looking it up in cookbook_state.env. The user
    configures these per-host in the Cookbook UI; without them, raw
    `vllm serve …` fails with 'command not found' because vLLM lives
    inside a venv that has to be sourced first.

    Returns a dict with keys ready to drop into the /api/model/serve
    payload: env_prefix, gpus, platform, hf_token, ssh_port.
    Falls back to the top-level env settings if no per-host entry exists.
    """
    import httpx
    headers = _internal_headers()
    state: Dict[str, Any] = {}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{_INTERNAL_BASE}/api/cookbook/state", headers=headers)
            state = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    except Exception as e:
        logger.debug(f"cookbook env lookup failed for host={host!r}: {e}")
        return {}
    if not isinstance(state, dict):
        return {}
    env_root = state.get("env") or {}
    if not isinstance(env_root, dict):
        return {}

    # Per-host entry takes precedence over top-level.
    per_host: Dict[str, Any] = {}
    for s in (env_root.get("servers") or []):
        if isinstance(s, dict) and (s.get("host") or "") == (host or ""):
            per_host = s
            break

    env_kind = per_host.get("env") or env_root.get("env") or "none"
    env_path = per_host.get("envPath") or env_root.get("envPath") or ""
    platform = per_host.get("platform") or env_root.get("platform") or "linux"
    ssh_port = per_host.get("sshPort") or env_root.get("sshPort") or ""

    env_prefix = ""
    if env_kind == "venv" and env_path:
        if platform == "windows":
            activate = env_path if env_path.endswith("\\Scripts\\Activate.ps1") else env_path.rstrip("\\") + "\\Scripts\\Activate.ps1"
            env_prefix = f"& {activate}"
        else:
            activate = env_path if env_path.endswith("/bin/activate") else env_path.rstrip("/") + "/bin/activate"
            env_prefix = f"source {activate}"
    elif env_kind == "conda" and env_path:
        if platform == "windows":
            env_prefix = f"conda activate {env_path}"
        else:
            env_prefix = f'eval "$(conda shell.bash hook)" && conda activate {env_path}'

    from routes.cookbook_helpers import load_stored_hf_token
    return {
        "env_prefix": env_prefix,
        "env_type": env_kind,
        "env_path": env_path,
        "gpus": env_root.get("gpus") or "",
        "platform": platform,
        "hf_token": load_stored_hf_token(),
        "ssh_port": ssh_port,
    }


def _infer_serve_port(cmd: str) -> int:
    """Infer likely listen port from a serve command."""
    if not cmd:
        return 8080
    m = re.search(r"--port\\s+(\\d+)", cmd)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            pass
    m = re.search(r"OLLAMA_HOST=[^\\s]*?:(\\d+)", cmd)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            pass
    if "ollama" in cmd:
        return 11434
    return 8080


def _infer_serve_host(host: str | None) -> tuple[str, bool]:
    """Return (host, container_local) for registering a served endpoint."""
    if not (host or "").strip():
        return "localhost", True
    base_host = host.split("@", 1)[-1] if "@" in host else host
    return base_host, False


async def _ensure_served_endpoint(
    *,
    model: str,
    cmd: str,
    host: str | None,
) -> Dict[str, Any]:
    """Register/fetch a model endpoint for a running serve session."""
    import httpx
    endpoint_host, container_local = _infer_serve_host(host)
    port = _infer_serve_port(cmd)
    base_url = f"http://{endpoint_host}:{port}/v1"
    short_name = model.split("/")[-1] if "/" in model else model
    is_image = "diffusion_server.py" in (cmd or "")
    payload = {
        "name": short_name if not is_image else f"{short_name} (image)",
        "base_url": base_url,
        "skip_probe": "true",
        "model_type": "image" if is_image else "llm",
        "container_local": "true" if container_local else "false",
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{_INTERNAL_BASE}/api/model-endpoints",
                data=payload,
                headers=_internal_headers(),
            )
            data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        if resp.status_code >= 400:
            logger.debug(
                f"ensure endpoint failed for {model!r}: status={resp.status_code} data={data}"
            )
            return {"added": False, "endpoint_id": "", "base_url": base_url, "error": data}
        ep_id = data.get("id") if isinstance(data, dict) else None
        return {
            "added": bool(ep_id),
            "endpoint_id": ep_id or "",
            "base_url": base_url,
            "data": data,
        }
    except Exception as e:
        logger.debug(f"ensure endpoint exception for {model!r}: {e}")
        return {"added": False, "endpoint_id": "", "base_url": base_url, "error": str(e)}


async def _cookbook_register_task(
    session_id: str,
    model: str,
    host: str,
    cmd: str,
    task_type: str = "serve",
    *,
    endpoint_added: bool = False,
    endpoint_id: str = "",
) -> bool:
    """Append a task entry to cookbook_state.json after the agent
    launches via /api/model/serve or /api/model/download. The route
    spawns tmux but leaves state-writing to the UI; the agent needs to
    do that here so the task shows up in the Cookbook tab.
    Returns True on success, False if the write failed (best-effort)."""
    import httpx
    headers = _internal_headers()
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{_INTERNAL_BASE}/api/cookbook/state", headers=headers)
            state = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    except Exception as e:
        logger.debug(f"cookbook state read failed: {e}")
        return False
    if not isinstance(state, dict):
        state = {}
    tasks = state.get("tasks") if isinstance(state.get("tasks"), list) else []
    # Skip duplicate (same session_id) entries
    if any(isinstance(t, dict) and t.get("sessionId") == session_id for t in tasks):
        return True
    display_name = model.split("/")[-1] if "/" in model else model
    target = f"{host}:" if host else "local:"
    placeholder = (
        f"Launched via agent — waiting for tmux output…\n"
        f"  session: {session_id}\n"
        f"  target:  {target}{(cmd.split() or [''])[0] if cmd else ''}\n"
        f"  cmd:     {cmd[:200]}{'…' if len(cmd) > 200 else ''}"
    )
    tasks.append({
        "id": session_id,
        "sessionId": session_id,
        "name": display_name,
        "modelId": model,
        "type": task_type,
        "status": "running",
        "output": placeholder,
        "ts": int(_time.time() * 1000),
        "payload": {"repo_id": model, "remote_host": host or "", "_cmd": cmd},
        "remoteHost": host or "",
        "sshPort": "",
        "platform": "linux",
        "_serveReady": False,
        "_endpointAdded": bool(endpoint_added),
        "_endpointId": endpoint_id or "",
    })
    state["tasks"] = tasks
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(f"{_INTERNAL_BASE}/api/cookbook/state",
                                  json=state, headers=headers)
        return r.status_code < 400
    except Exception as e:
        logger.debug(f"cookbook state write failed: {e}")
        return False


# ---------------------------------------------------------------------------
# Model process scanning
# ---------------------------------------------------------------------------

# Patterns for detecting running LLM/diffusion model servers outside
# the cookbook's task tracker. Each entry: (label, substring-list).
# Match is case-insensitive against the FULL cmdline. First-match wins.
_MODEL_PROCESS_PATTERNS = [
    ("vLLM",            ["vllm.entrypoints", "vllm serve", "/vllm/", "vllm-openai"]),
    ("SGLang",          ["sglang.launch_server", "sglang/launch_server"]),
    ("llama.cpp",       ["llama-server", "llama_cpp_server", "llamacppserver"]),
    ("Ollama",          ["ollama serve", "ollama runner", "/ollama "]),
    ("ComfyUI",         ["comfyui/main.py", "/ComfyUI/main.py", "ComfyUI"]),
    ("A1111 WebUI",     ["stable-diffusion-webui/webui", "stable-diffusion-webui/launch", "webui.sh"]),
    ("Fooocus",         ["Fooocus/entry_with_update", "Fooocus/launch"]),
    ("InvokeAI",        ["invokeai-web", "invokeai.app", "invokeai/api_app"]),
    ("Forge WebUI",     ["stable-diffusion-webui-forge", "forge/webui"]),
    ("SD.Next",         ["automatic/webui", "sd.next"]),
    ("TGI",             ["text-generation-launcher", "text_generation_launcher"]),
    ("Aphrodite",       ["aphrodite.endpoints", "aphrodite-engine"]),
    ("Triton",          ["tritonserver", "triton/main"]),
    ("Diffusers",       ["diffusers.pipelines", "StableDiffusionInpaintPipeline", "DiffusionPipeline"]),
]


def _scan_running_model_processes() -> List[Dict[str, Any]]:
    """Scan /proc for running model server processes. Linux-only; returns
    [] on other platforms or if /proc isn't accessible. Each match returns
    a dict shaped like a cookbook task so the caller can merge cleanly.
    """
    if not os.path.isdir("/proc"):
        return []
    out: List[Dict[str, Any]] = []
    seen_keys = set()
    try:
        for pid_dir in os.listdir("/proc"):
            if not pid_dir.isdigit():
                continue
            try:
                with open(f"/proc/{pid_dir}/cmdline", "rb") as f:
                    raw = f.read()
            except (OSError, PermissionError):
                continue
            if not raw:
                continue
            cmdline = raw.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()
            if not cmdline:
                continue
            lower = cmdline.lower()
            for label, needles in _MODEL_PROCESS_PATTERNS:
                if any(n.lower() in lower for n in needles):
                    key = (label, cmdline.split(" ")[0])
                    if key in seen_keys:
                        break
                    seen_keys.add(key)
                    model = ""
                    for tok in cmdline.split():
                        if "/" in tok and any(s in tok.lower() for s in (
                            "model", "checkpoint", ".safetensors", ".gguf", ".bin", "huggingface"
                        )):
                            model = tok
                            break
                    out.append({
                        "session_id": f"pid-{pid_dir}",
                        "model": model or label,
                        "phase": "running (external)",
                        "type": "serve",
                        "remote": "local",
                        "pid": int(pid_dir),
                        "label": label,
                        "cmdline_preview": cmdline[:140] + ("…" if len(cmdline) > 140 else ""),
                        "external": True,
                    })
                    break
    except Exception as e:
        logger.debug(f"_scan_running_model_processes failed: {e}")
    return out


def _cookbook_apply_retry_suggestion(cmd: str, suggestion: Dict[str, Any]) -> str:
    """Apply a structured Cookbook diagnosis suggestion to a serve command."""
    if not cmd or not suggestion:
        return cmd
    op = suggestion.get("op")
    if op == "append":
        arg = (suggestion.get("arg") or "").strip()
        if not arg or arg in cmd:
            return cmd
        return f"{cmd.rstrip()} {arg}"
    if op == "remove":
        flag = (suggestion.get("flag") or "").strip()
        if not flag:
            return cmd
        return re.sub(rf"\s*{re.escape(flag)}(?:\s+\S+)?", "", cmd).strip()
    if op == "replace":
        flag = (suggestion.get("flag") or "").strip()
        value = str(suggestion.get("value") or "").strip()
        if not flag or not value:
            return cmd
        repl = f"{flag} {value}"
        if re.search(rf"(^|\s){re.escape(flag)}(\s+\S+)?", cmd):
            return re.sub(rf"(^|\s){re.escape(flag)}(?:\s+\S+)?", lambda m: (m.group(1) or " ") + repl, cmd).strip()
        return f"{cmd.rstrip()} {repl}"
    return cmd


# ---------------------------------------------------------------------------
# Vault helpers
# ---------------------------------------------------------------------------

def _load_vault_config() -> Dict:
    """Load Vaultwarden config from data/vault.json."""
    from pathlib import Path
    p = Path(VAULT_FILE)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}
