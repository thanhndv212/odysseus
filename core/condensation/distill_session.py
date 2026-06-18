"""Distillation pipeline — CSF session → LLM → structured memories.

This is the heart of the condensation pipeline (plan §6).
Uses Instructor for structured LLM output, stores results as odysseus memories.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

import instructor
from openai import OpenAI

from .models import DistilledKnowledge, MemoryEntry
from .csf_reader import read_csf, CSFData
from .memory_store import add_memories, update_distillation_log


# ─── Session Formatting ──────────────────────────────────────────


def _extract_text(content: list[dict]) -> str:
    """Extract text content from message content items."""
    texts = []
    for item in content:
        if item.get("type") == "text":
            texts.append(item.get("text", ""))
    return "\n".join(texts)


def _extract_tool_summary(content: list[dict]) -> str:
    """Extract a brief summary of tool calls from content items."""
    tools = []
    for item in content:
        if item.get("type") == "tool_use":
            name = item.get("name", "unknown")
            input_data = item.get("input", {})
            if isinstance(input_data, dict):
                # Show first 2 keys as brief context
                keys = list(input_data.keys())[:2]
                brief = ", ".join(f"{k}={str(input_data[k])[:60]}" for k in keys)
            else:
                brief = str(input_data)[:80]
            tools.append(f"{name}({brief})")
    return "; ".join(tools)


def _extract_tool_results(content: list[dict]) -> str:
    """Extract brief tool result summaries."""
    results = []
    for item in content:
        if item.get("type") == "tool_result":
            output = item.get("output", "")
            is_error = item.get("isError", False)
            prefix = "ERROR: " if is_error else ""
            results.append(f"{prefix}{output[:200]}")
    return "\n".join(results)


def format_session_for_prompt(data: CSFData, max_chars: int = 80000) -> str:
    """Format CSF session into a readable prompt for the LLM.

    Includes user messages, assistant responses, and tool call summaries.
    Skips reasoning blocks (internal to the model, not useful for extraction).
    Truncates to max_chars to stay within LLM context limits.
    """
    lines = [
        f"Session: {data.session.title or 'untitled'}",
        f"Source: {data.session.source}",
        f"Directory: {data.session.directory}",
        f"Agent: {data.session.agent or 'unknown'}",
        f"Model: {data.session.model or 'unknown'}",
        f"Messages: {len(data.messages)}",
        "",
        "--- Conversation ---",
    ]

    for msg in data.messages:
        if msg.role == "user":
            text = _extract_text(msg.content)
            if text:
                lines.append(f"\nUser: {text}")

        elif msg.role == "assistant":
            text = _extract_text(msg.content)
            tools = _extract_tool_summary(msg.content)
            if text:
                lines.append(f"\nAssistant: {text}")
            if tools:
                lines.append(f"  [Tools: {tools}]")

        elif msg.role == "tool":
            results = _extract_tool_results(msg.content)
            if results:
                lines.append(f"  [Result: {results[:300]}]")

        elif msg.role == "system":
            text = _extract_text(msg.content)
            if text:
                lines.append(f"\nSystem: {text[:500]}")

    result = "\n".join(lines)
    if len(result) > max_chars:
        result = result[:max_chars] + "\n\n... [truncated for length]"
    return result


def build_distillation_prompt(data: CSFData, existing_context: str = "") -> str:
    """Build the full distillation prompt.

    Follows plan §6.3: extract decisions, learnings, patterns, open questions, summary.
    Explicitly excludes preferences and instructions (only user-stated).
    """
    session_text = format_session_for_prompt(data)

    prompt = """You are distilling knowledge from a coding session. Extract:
1. Key decisions made (with rationale)
2. Learnings (what worked, what didn't, surprising findings)
3. Patterns identified (recurring codebase or workflow patterns)
4. Open questions / unresolved issues
5. Project summary (1-2 sentences)

Do NOT extract preferences or instructions — these are only ever user-stated.
Focus on technical knowledge: architecture decisions, tool discoveries, debugging insights, code patterns.
Each item should be a concise, self-contained statement that would be useful in future sessions.
"""
    if existing_context:
        prompt += f"\nExisting context from prior condensation:\n{existing_context}\n"

    prompt += f"\nSession messages:\n{session_text}"
    return prompt


# ─── LLM Client ──────────────────────────────────────────────────


def get_llm_client() -> tuple[instructor.Instructor, str]:
    """Get LLM client configured from env vars or opencode auth.

    Priority:
    1. DISTILL_LLM_API_KEY + DISTILL_LLM_BASE_URL env vars
    2. DeepSeek key from opencode auth.json

    Returns (instructor_client, model_name).
    """
    api_key = os.environ.get("DISTILL_LLM_API_KEY")
    base_url = os.environ.get("DISTILL_LLM_BASE_URL", "https://api.deepseek.com/v1")
    model = os.environ.get("DISTILL_LLM_MODEL", "deepseek-chat")

    if not api_key:
        # Read from opencode auth
        auth_path = os.path.expanduser("~/.local/share/opencode/auth.json")
        try:
            with open(auth_path, encoding="utf-8") as f:
                auth = json.load(f)
            if "deepseek" in auth:
                api_key = auth["deepseek"]["key"]
            elif "opencode-go" in auth:
                api_key = auth["opencode-go"]["key"]
                base_url = "https://api.opencode.ai/v1"
                model = "auto"
            else:
                raise ValueError(
                    "No LLM API key found. Set DISTILL_LLM_API_KEY or configure opencode auth."
                )
        except (FileNotFoundError, json.JSONDecodeError) as e:
            raise ValueError(f"Cannot read opencode auth: {e}") from e

    client = instructor.from_openai(OpenAI(api_key=api_key, base_url=base_url))
    return client, model


# ─── Knowledge → Memories ────────────────────────────────────────


def distill_to_memories(
    knowledge: DistilledKnowledge,
    session_id: str,
    owner: str | None = None,
) -> list[MemoryEntry]:
    """Convert DistilledKnowledge to MemoryEntry list.

    Mapping (plan §6.4 + §7.3):
    - decisions → category="fact"
    - learnings → category="learning"
    - patterns → category="fact"
    - open_questions → category="task"
    - summary → category="summary"

    All with source="distillation", pending_review=True, confidence score.
    """
    memories: list[MemoryEntry] = []
    ts = int(time.time())
    conf = knowledge.confidence

    for decision in knowledge.decisions:
        memories.append(
            MemoryEntry(
                id=str(uuid.uuid4()),
                text=decision,
                category="fact",
                source="distillation",
                timestamp=ts,
                session_id=session_id,
                owner=owner,
                pending_review=True,
                confidence=conf,
            )
        )

    for learning in knowledge.learnings:
        memories.append(
            MemoryEntry(
                id=str(uuid.uuid4()),
                text=learning,
                category="learning",
                source="distillation",
                timestamp=ts,
                session_id=session_id,
                owner=owner,
                pending_review=True,
                confidence=conf,
            )
        )

    for pattern in knowledge.patterns:
        memories.append(
            MemoryEntry(
                id=str(uuid.uuid4()),
                text=pattern,
                category="fact",
                source="distillation",
                timestamp=ts,
                session_id=session_id,
                owner=owner,
                pending_review=True,
                confidence=conf,
            )
        )

    for question in knowledge.open_questions:
        memories.append(
            MemoryEntry(
                id=str(uuid.uuid4()),
                text=question,
                category="task",
                source="distillation",
                timestamp=ts,
                session_id=session_id,
                owner=owner,
                pending_review=True,
                confidence=conf,
            )
        )

    if knowledge.summary:
        memories.append(
            MemoryEntry(
                id=str(uuid.uuid4()),
                text=knowledge.summary,
                category="summary",
                source="distillation",
                timestamp=ts,
                session_id=session_id,
                owner=owner,
                pending_review=True,
                confidence=conf,
            )
        )

    return memories


# ─── Main Distillation Function ──────────────────────────────────


def distill_session(
    csf_path: str,
    memory_path: str,
    log_path: str,
    mock: bool = False,
    owner: str | None = None,
) -> dict:
    """Distill a single CSF session into odysseus memories.

    Args:
        csf_path: Path to the .csf.jsonl file
        memory_path: Path to odysseus memory.json
        log_path: Path to distillation_log.json
        mock: If True, use mock LLM output (no API call)
        owner: Owner username for memories

    Returns dict with: session_id, memories_extracted, confidence, summary,
    or {skipped: True, reason: ...} if session was skipped.
    """
    data = read_csf(csf_path)

    # Skip sessions with <5 messages (plan §6.2)
    if len(data.messages) < 5:
        result = {
            "skipped": True,
            "reason": "too_few_messages",
            "message_count": len(data.messages),
        }
        update_distillation_log(log_path, data.session.id, csf_path, result)
        return result

    prompt = build_distillation_prompt(data)

    if mock:
        # Generate mock result for testing
        knowledge = DistilledKnowledge(
            decisions=["[Mock] Decided to use sqlite-vec for vector storage"],
            learnings=["[Mock] Learned that ChromaDB adds unnecessary complexity"],
            patterns=["[Mock] Pattern: prefer single-file solutions over multi-component ones"],
            open_questions=["[Mock] How to handle cross-machine vector sync?"],
            summary=f"[Mock] Session about {data.session.title or 'unknown topic'}",
            confidence=0.5,
        )
    else:
        client, model = get_llm_client()
        knowledge = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            response_model=DistilledKnowledge,
            max_tokens=2000,
        )

    # Convert to memory entries
    memories = distill_to_memories(knowledge, data.session.id, owner=owner)

    # Store
    count = add_memories(memory_path, memories)

    result = {
        "session_id": data.session.id,
        "title": data.session.title,
        "source": data.session.source,
        "directory": data.session.directory,
        "messages": len(data.messages),
        "memories_extracted": count,
        "confidence": knowledge.confidence,
        "summary": knowledge.summary,
        "categories": {
            "decisions": len(knowledge.decisions),
            "learnings": len(knowledge.learnings),
            "patterns": len(knowledge.patterns),
            "open_questions": len(knowledge.open_questions),
        },
    }

    update_distillation_log(log_path, data.session.id, csf_path, result)
    return result
