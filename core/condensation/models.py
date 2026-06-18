"""Pydantic models for the condensation pipeline."""

from __future__ import annotations

from pydantic import BaseModel, Field


class DistilledKnowledge(BaseModel):
    """Structured output from LLM distillation.

    Used with Instructor for validated JSON extraction from any LLM.
    See plan §6.3.
    """

    decisions: list[str] = Field(
        default_factory=list,
        description="Key decisions made during the session, with rationale",
    )
    learnings: list[str] = Field(
        default_factory=list,
        description="Lessons learned — what worked, what didn't, surprising findings",
    )
    patterns: list[str] = Field(
        default_factory=list,
        description="Recurring patterns identified in the codebase or workflow",
    )
    open_questions: list[str] = Field(
        default_factory=list,
        description="Unresolved issues or questions left open at session end",
    )
    summary: str = Field(
        description="1-2 sentence session summary",
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Overall confidence in the extraction quality, 0.0-1.0",
    )


class MemoryEntry(BaseModel):
    """A single memory entry stored in odysseus memory.json.

    Extends the existing odysseus memory schema with:
    - pending_review: quality control flag (plan §6.4)
    - confidence: distillation confidence score (plan §6.4)
    """

    id: str
    text: str
    category: str  # fact, learning, summary, task, project, goal, etc.
    source: str  # "distillation" for auto-extracted
    timestamp: int  # Unix timestamp
    session_id: str | None = None
    directory: str | None = None  # Project directory for context filtering
    owner: str | None = None
    pinned: bool = False
    uses: int = 0
    pending_review: bool = True
    confidence: float | None = None
