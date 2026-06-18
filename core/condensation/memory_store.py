"""Memory storage — reads/writes odysseus memory.json + distillation log.

Memories are stored as JSON in odysseus/data/memory.json.
The distillation log tracks which sessions have been processed.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .models import MemoryEntry


def load_memories(memory_path: str) -> list[dict]:
    """Load existing memories from JSON file."""
    p = Path(memory_path)
    if not p.exists():
        return []
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def save_memories(memory_path: str, memories: list[dict]) -> None:
    """Save memories to JSON file (atomic write)."""
    p = Path(memory_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(memories, f, indent=2, ensure_ascii=False)
    os.replace(tmp, p)


def add_memories(memory_path: str, new_entries: list[MemoryEntry]) -> int:
    """Add new memory entries to the store. Returns count added."""
    existing = load_memories(memory_path)
    for entry in new_entries:
        existing.append(entry.model_dump())
    save_memories(memory_path, existing)
    return len(new_entries)


# ─── Distillation Log ────────────────────────────────────────────


def load_distillation_log(log_path: str) -> dict:
    """Load the distillation log (session_id → metadata)."""
    p = Path(log_path)
    if not p.exists():
        return {}
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def save_distillation_log(log_path: str, log: dict) -> None:
    """Save distillation log (atomic write)."""
    p = Path(log_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(log, f, indent=2, ensure_ascii=False)
    os.replace(tmp, p)


def update_distillation_log(
    log_path: str, session_id: str, csf_file: str, result: dict
) -> None:
    """Record a session as distilled in the log."""
    log = load_distillation_log(log_path)
    log[session_id] = {
        "distilled_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "csf_file": csf_file,
        "memories_extracted": result.get("memories_extracted", 0),
        "confidence": result.get("confidence", 0.0),
        "summary": result.get("summary", ""),
        "skipped": result.get("skipped", False),
        "skip_reason": result.get("reason"),
    }
    save_distillation_log(log_path, log)


def get_distilled_session_ids(log_path: str) -> set[str]:
    """Get set of session IDs that have been distilled."""
    log = load_distillation_log(log_path)
    return {sid for sid, info in log.items() if not info.get("skipped")}


# ─── Quality Control ─────────────────────────────────────────────


def auto_promote_pending(memory_path: str, max_age_days: int = 7) -> int:
    """Auto-promote memories from pending_review to active after max_age_days.

    Returns count of promoted memories.
    """
    memories = load_memories(memory_path)
    now = int(time.time())
    max_age_seconds = max_age_days * 86400
    promoted = 0

    for m in memories:
        if m.get("pending_review") is True:
            age = now - m.get("timestamp", 0)
            if age > max_age_seconds:
                m["pending_review"] = False
                promoted += 1

    if promoted > 0:
        save_memories(memory_path, memories)

    return promoted


def get_pending_review(memory_path: str) -> list[dict]:
    """Get all memories with pending_review=True."""
    memories = load_memories(memory_path)
    return [m for m in memories if m.get("pending_review") is True]
