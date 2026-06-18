"""Lazy distillation trigger — finds un-distilled CSF sessions.

Plan §6.2: distillation runs on-demand when context is queried, not eagerly.
This module identifies which sessions need distilling, optionally filtered by project.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .csf_reader import read_session_header, count_messages
from .memory_store import get_distilled_session_ids


def find_csf_files(traces_dir: str) -> list[str]:
    """Find all .csf.jsonl files recursively in traces directory."""
    results: list[str] = []
    for root, _dirs, files in os.walk(traces_dir):
        for f in files:
            if f.endswith(".csf.jsonl"):
                results.append(os.path.join(root, f))
    return sorted(results)


def find_undistilled(
    traces_dir: str,
    log_path: str,
    project_dir: str | None = None,
    min_messages: int = 5,
) -> list[dict]:
    """Find un-distilled CSF sessions.

    Args:
        traces_dir: Root directory containing CSF files
        log_path: Path to distillation_log.json
        project_dir: If set, only return sessions matching this directory
        min_messages: Skip sessions with fewer messages (plan §6.2)

    Returns list of dicts: {session_id, file_path, directory, title, message_count}
    """
    all_files = find_csf_files(traces_dir)
    distilled_ids = get_distilled_session_ids(log_path)

    results: list[dict] = []

    for file_path in all_files:
        # Read only the session header (first line)
        header = read_session_header(file_path)
        if header is None:
            continue

        session_id = header["id"]
        if session_id in distilled_ids:
            continue

        directory = header.get("directory", "unknown")
        if project_dir and directory != project_dir:
            continue

        # Count messages (quick scan, no full parse)
        msg_count = count_messages(file_path)
        if msg_count < min_messages:
            continue

        results.append(
            {
                "session_id": session_id,
                "file_path": file_path,
                "directory": directory,
                "title": header.get("title"),
                "source": header.get("source"),
                "message_count": msg_count,
            }
        )

    # Sort by message count descending (distill richest sessions first)
    results.sort(key=lambda x: x["message_count"], reverse=True)
    return results


def get_distillation_stats(traces_dir: str, log_path: str) -> dict:
    """Get summary statistics about distillation state."""
    all_files = find_csf_files(traces_dir)
    log = json.loads(Path(log_path).read_text()) if Path(log_path).exists() else {}

    total = len(all_files)
    distilled = sum(1 for info in log.values() if not info.get("skipped"))
    skipped = sum(1 for info in log.values() if info.get("skipped"))
    pending = total - distilled - skipped

    return {
        "total_csf_files": total,
        "distilled": distilled,
        "skipped": skipped,
        "pending": pending,
    }
