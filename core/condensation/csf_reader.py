"""CSF JSONL reader — parses Canonical Session Format files.

CSF files are JSONL: first line is a session record, subsequent lines are
message records. Each line: {"version": 1, "type": "session"|"message", "data": {...}}
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class CSFSession:
    id: str
    source: str
    directory: str
    title: str | None
    created_at: str
    agent: str | None
    model: str | None
    has_redactions: bool


@dataclass
class CSFMessage:
    id: str
    role: str  # user, assistant, system, tool
    content: list[dict]  # ContentItem dicts
    timestamp: str


@dataclass
class CSFData:
    session: CSFSession
    messages: list[CSFMessage] = field(default_factory=list)


def read_csf(file_path: str) -> CSFData:
    """Read a CSF .jsonl file and return structured data.

    Raises ValueError if no session record is found.
    """
    session: CSFSession | None = None
    messages: list[CSFMessage] = []

    with open(file_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            rec_type = record.get("type")
            data = record.get("data", {})

            if rec_type == "session":
                session = CSFSession(
                    id=data["id"],
                    source=data["source"],
                    directory=data.get("directory", "unknown"),
                    title=data.get("title"),
                    created_at=data.get("createdAt", ""),
                    agent=data.get("agent"),
                    model=data.get("model"),
                    has_redactions=data.get("hasRedactions", False),
                )
            elif rec_type == "message":
                messages.append(
                    CSFMessage(
                        id=data["id"],
                        role=data["role"],
                        content=data.get("content", []),
                        timestamp=data.get("timestamp", ""),
                    )
                )

    if session is None:
        raise ValueError(f"No session record found in {file_path}")

    return CSFData(session=session, messages=messages)


def read_session_header(file_path: str) -> dict | None:
    """Read only the first line (session record) of a CSF file.

    Faster than read_csf when you only need session metadata.
    Returns the session data dict, or None if file is empty/invalid.
    """
    with open(file_path, encoding="utf-8") as f:
        first_line = f.readline().strip()
        if not first_line:
            return None
        record = json.loads(first_line)
        if record.get("type") != "session":
            return None
        return record.get("data")


def count_messages(file_path: str) -> int:
    """Count message records in a CSF file without full parsing."""
    count = 0
    with open(file_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                if record.get("type") == "message":
                    count += 1
            except json.JSONDecodeError:
                continue
    return count
