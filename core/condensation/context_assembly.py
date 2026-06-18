"""Context assembly — builds a 2K-token context brief from odysseus memories.

Plan §8.1: When a new session starts, assemble a context brief from:
- Project knowledge (facts, decisions, patterns for this project)
- Recent learnings
- Open tasks/questions
- User preferences (global, not project-specific)
- Standing instructions (global)
- Recent session summaries

Auto-extracted knowledge is labeled "(auto)" vs user-stated.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .memory_store import load_memories
from .lazy_trigger import get_distillation_stats


# ─── Scoring ─────────────────────────────────────────────────────


def _score_memory(m: dict, project_dir: str | None, query: str | None = None) -> float:
    """Score a memory for relevance to a project/query.

    Factors:
    - Directory match (project-specific memories score higher)
    - Category priority (preferences/instructions always included)
    - Recency (newer = better)
    - Confidence (higher = better)
    - Pinned (always high priority)
    - Keyword overlap (if query provided)
    """
    score = 0.0

    # Pinned memories always high priority
    if m.get("pinned"):
        score += 100

    # Directory match
    mem_dir = m.get("directory", "")
    if project_dir and mem_dir:
        if mem_dir == project_dir:
            score += 50  # Exact project match
        elif project_dir.startswith(mem_dir) or mem_dir.startswith(project_dir):
            score += 30  # Parent/child directory match

    # Category priority
    category = m.get("category", "fact")
    category_priority = {
        "preference": 40,  # Always include preferences
        "instruction": 40,  # Always include instructions
        "identity": 35,
        "goal": 25,
        "project": 25,
        "task": 20,
        "learning": 15,
        "fact": 10,
        "summary": 5,
        "contact": 5,
    }
    score += category_priority.get(category, 10)

    # Recency (memories from last 7 days get bonus)
    ts = m.get("timestamp", 0)
    age_days = (int(time.time()) - ts) / 86400
    if age_days < 7:
        score += 20
    elif age_days < 30:
        score += 10
    elif age_days < 90:
        score += 5

    # Confidence
    conf = m.get("confidence")
    if conf is not None:
        score += conf * 10

    # Keyword overlap
    if query:
        text_lower = m.get("text", "").lower()
        query_lower = query.lower()
        query_words = set(query_lower.split())
        text_words = set(text_lower.split())
        overlap = len(query_words & text_words)
        score += overlap * 5

    return score


# ─── Brief Assembly ──────────────────────────────────────────────


def assemble_context(
    memory_path: str,
    project_dir: str | None = None,
    log_path: str | None = None,
    traces_dir: str | None = None,
    max_chars: int = 8000,
    query: str | None = None,
) -> str:
    """Assemble a context brief from odysseus memories.

    Args:
        memory_path: Path to memory.json
        project_dir: Current project directory (for filtering)
        log_path: Path to distillation_log.json (for stats)
        traces_dir: CSF traces directory (for stats)
        max_chars: Maximum brief size in chars (~2K tokens)
        query: Optional search query for keyword filtering

    Returns a markdown-formatted context brief string.
    """
    memories = load_memories(memory_path)

    if not memories:
        return _empty_brief(project_dir)

    # Score and sort all memories
    scored = [(m, _score_memory(m, project_dir, query)) for m in memories]
    scored.sort(key=lambda x: x[1], reverse=True)

    # Global memories (preferences, instructions, identity) — always include
    global_categories = {"preference", "instruction", "identity"}
    global_mems = [m for m, s in scored if m.get("category") in global_categories]

    # Project-specific memories
    project_mems = []
    if project_dir:
        project_mems = [
            (m, s) for m, s in scored
            if m.get("directory") == project_dir
            and m.get("category") not in global_categories
        ]
    else:
        # No project filter — take top-scored non-global
        project_mems = [
            (m, s) for m, s in scored
            if m.get("category") not in global_categories
        ]

    # Build the brief
    lines: list[str] = []

    # Header
    if project_dir:
        project_name = os.path.basename(project_dir.rstrip("/")) or project_dir
        lines.append(f"## Brain Context: {project_name}")
        lines.append(f"*Project: {project_dir}*")
    else:
        lines.append("## Brain Context")
    lines.append("")

    # Preferences (global)
    prefs = [m for m in global_mems if m.get("category") == "preference"]
    if prefs:
        lines.append("### Preferences")
        for m in prefs[:5]:
            lines.append(f"- {_format_memory(m)}")
        lines.append("")

    # Instructions (global)
    instrs = [m for m in global_mems if m.get("category") == "instruction"]
    if instrs:
        lines.append("### Standing Instructions")
        for m in instrs[:5]:
            lines.append(f"- {_format_memory(m)}")
        lines.append("")

    # Identity (global)
    identities = [m for m in global_mems if m.get("category") == "identity"]
    if identities:
        lines.append("### Identity")
        for m in identities[:3]:
            lines.append(f"- {_format_memory(m)}")
        lines.append("")

    # Project facts
    facts = [m for m, s in project_mems if m.get("category") == "fact"]
    if facts:
        lines.append("### Key Facts")
        for m in facts[:10]:
            lines.append(f"- {_format_memory(m)}")
        lines.append("")

    # Learnings
    learnings = [m for m, s in project_mems if m.get("category") == "learning"]
    if learnings:
        lines.append("### Learnings")
        for m in learnings[:8]:
            lines.append(f"- {_format_memory(m)}")
        lines.append("")

    # Open tasks
    tasks = [m for m, s in project_mems if m.get("category") == "task"]
    if tasks:
        lines.append("### Open Tasks / Questions")
        for m in tasks[:5]:
            lines.append(f"- {_format_memory(m)}")
        lines.append("")

    # Session summaries
    summaries = [m for m, s in project_mems if m.get("category") == "summary"]
    if summaries:
        lines.append("### Recent Sessions")
        for m in summaries[:5]:
            lines.append(f"- {_format_memory(m)}")
        lines.append("")

    # Goals
    goals = [m for m, s in project_mems if m.get("category") == "goal"]
    if goals:
        lines.append("### Goals")
        for m in goals[:3]:
            lines.append(f"- {_format_memory(m)}")
        lines.append("")

    # Stats footer
    if log_path and traces_dir:
        try:
            stats = get_distillation_stats(traces_dir, log_path)
            lines.append("---")
            lines.append(
                f"*{len(memories)} total memories | "
                f"{stats['distilled']} sessions distilled | "
                f"{stats['pending']} pending distillation*"
            )
        except Exception:
            pass

    brief = "\n".join(lines)

    # Truncate if too long
    if len(brief) > max_chars:
        brief = brief[:max_chars] + "\n\n... [truncated for length]"

    return brief


def _format_memory(m: dict) -> str:
    """Format a memory entry for the context brief."""
    text = m.get("text", "")
    source = m.get("source", "user")
    conf = m.get("confidence")

    # Truncate long memories
    if len(text) > 200:
        text = text[:200] + "..."

    # Label auto-extracted memories
    if source == "distillation":
        label = "(auto"
        if conf is not None:
            label += f", conf: {conf:.1f}"
        label += ")"
        return f"{text} {label}"

    return text


def _empty_brief(project_dir: str | None) -> str:
    """Return an empty context brief with degradation message."""
    if project_dir:
        project_name = os.path.basename(project_dir.rstrip("/")) or project_dir
        return f"## Brain Context: {project_name}\n\n*No memories found for this project.*"
    return "## Brain Context\n\n*No memories available.*"
