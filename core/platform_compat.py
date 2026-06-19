"""macOS + SSH-remote compatibility helpers.

Odysseus is a macOS-native app with optional SSH-remote support for Linux GPU
servers. This module centralizes the small set of platform-specific helpers so
the rest of the codebase stays clean. Import from here instead of sprinkling
``os.name == "nt"`` checks across modules.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
import sys
from typing import List, Optional
import platform

# Allows APFEL support and ARM-native binary recommendations on Apple Silicon Macs.
IS_APPLE_SILICON = (
    platform.system() == "Darwin"
    and platform.machine().lower()
    in {
        "arm64",
        "aarch64",
    }
)


# ── File permissions ────────────────────────────────────────────────────────
def safe_chmod(path, mode: int) -> bool:
    """``os.chmod`` that returns True when the mode was applied, False otherwise.

    Used to lock secret/key files down to 0o600. macOS (like all POSIX systems)
    supports permission bits; any failure is caught and reported as False.
    """
    try:
        os.chmod(path, mode)
        return True
    except OSError:
        return False


# ── Process detach / liveness / teardown ────────────────────────────────────
def detached_popen_kwargs() -> dict:
    """Keyword args for :class:`subprocess.Popen` that fully detach a child so
    it outlives the request/stream that launched it.

    Uses ``start_new_session=True`` (setsid) — new session + process group.
    """
    return {"start_new_session": True}


def pid_alive(pid: Optional[int]) -> bool:
    """True if a process with ``pid`` is currently running.

    Uses the classic ``os.kill(pid, 0)`` probe (safe on POSIX/macOS).
    """
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def kill_process_tree(pid: Optional[int]) -> None:
    """Terminate ``pid`` and all of its descendants.

    Signals the whole process group (``killpg``), falling back to a plain
    ``kill`` if the pid isn't a group leader.
    """
    if not pid:
        return
    import signal

    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except Exception:
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass


# ── Shell / executable resolution ───────────────────────────────────────────
_BASH_CACHE: Optional[str] = None
_BASH_PROBED = False

# Paths to add to the remote SSH probe command to find tools like nvidia-smi that may not be on PATH.
_SSH_PATH_MEMBERS = (
    "/usr/bin",
    "/usr/local/bin",
    "/usr/local/cuda/bin",
    "/usr/lib/wsl/lib"
)
# Fallback locations for nvidia-smi on WSL and other Linux distros where it may not be on PATH.
NVIDIA_PATH_CANDIDATES = (
    "/usr/bin/nvidia-smi",
    "/usr/local/bin/nvidia-smi",
    "/usr/local/cuda/bin/nvidia-smi",
    "/usr/lib/wsl/lib/nvidia-smi",
)


def _ssh_path_override() -> str:
    """Build the PATH export snippet used for remote SSH shell probes."""
    return f"export PATH=\"$PATH:{':'.join(_SSH_PATH_MEMBERS)}\"; "


SSH_PATH_OVERRIDE = _ssh_path_override()


def find_bash() -> Optional[str]:
    """Locate a real ``bash`` interpreter, or None.

    Result is cached.
    """
    global _BASH_CACHE, _BASH_PROBED
    if _BASH_PROBED:
        return _BASH_CACHE
    _BASH_PROBED = True
    _BASH_CACHE = which_tool("bash")
    return _BASH_CACHE


def has_bash() -> bool:
    return find_bash() is not None


def which_tool(name: str) -> Optional[str]:
    """``shutil.which`` wrapper. Returns the full path to the tool or None."""
    return shutil.which(name)


def run_script_argv(script_path) -> List[str]:
    """argv to execute a shell *script file*.

    Prefers bash, falls back to ``sh``.
    """
    bash = find_bash()
    if bash:
        return [bash, str(script_path)]
    return ["sh", str(script_path)]


def _ssh_exec_argv(
    remote: str,
    ssh_port: str | None,
    *,
    remote_cmd: str | None = None,
    connect_timeout: int | None = None,
    strict_host_key_checking: bool | None = None,
) -> list[str]:
    """Build a consistent ssh argv for remote command execution."""
    remote_value = str(remote or "").strip()
    remote_host = remote_value.rsplit("@", 1)[-1]
    if not remote_value or remote_value.startswith("-") or not remote_host or remote_host.startswith("-"):
        raise ValueError("Invalid SSH remote host")
    argv = ["ssh"]
    if connect_timeout is not None:
        argv.extend(["-o", f"ConnectTimeout={int(connect_timeout)}"])
    if strict_host_key_checking is not None:
        argv.extend(
            [
                "-o",
                "StrictHostKeyChecking=yes"
                if strict_host_key_checking
                else "StrictHostKeyChecking=no",
            ]
        )
    if ssh_port and ssh_port != "22":
        argv.extend(["-p", str(ssh_port)])
    argv.append(remote)
    if remote_cmd is not None:
        argv.append(remote_cmd)
    return argv


def run_ssh_command(
    remote: str,
    ssh_port: str | None,
    remote_cmd: str,
    *,
    timeout: float,
    connect_timeout: int | None = None,
    strict_host_key_checking: bool | None = None,
    text: bool = True,
) -> subprocess.CompletedProcess:
    """Run an ssh command with centralized timeout and stderr/stdout capture."""
    return subprocess.run(
        _ssh_exec_argv(
            remote,
            ssh_port,
            remote_cmd=remote_cmd,
            connect_timeout=connect_timeout,
            strict_host_key_checking=strict_host_key_checking,
        ),
        timeout=timeout,
        capture_output=True,
        text=text,
    )



