#!/usr/bin/env python3
"""Distillation CLI — distill CSF sessions into odysseus memories.

Usage:
  # Distill a single session
  python scripts/distill.py path/to/session.csf.jsonl

  # Distill all un-distilled sessions for a project
  python scripts/distill.py --project /Users/me/Develop/myproject

  # Distill all un-distilled sessions (batch of 5)
  python scripts/distill.py --all

  # Show distillation stats
  python scripts/distill.py --stats

  # Dry run (show what would be distilled, no LLM call)
  python scripts/distill.py --all --dry-run

  # Mock mode (no LLM API call, generates placeholder output)
  python scripts/distill.py --all --mock

  # Review pending memories
  python scripts/distill.py --review

  # Auto-promote old pending memories (>7 days)
  python scripts/distill.py --promote

Options:
  --limit N       Max sessions to distill in one run (default 5)
  --traces-dir    CSF traces directory (default ~/copilot-trace-data/traces)
  --memory-path   Path to memory.json (default odysseus/data/memory.json)
  --log-path      Path to distillation log (default odysseus/data/distillation_log.json)
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

# Add project root to path for imports
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.condensation.distill_session import distill_session
from core.condensation.lazy_trigger import find_undistilled, get_distillation_stats
from core.condensation.memory_store import (
    get_pending_review,
    auto_promote_pending,
    load_memories,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Distill CSF sessions into odysseus memories",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("csf_file", nargs="?", help="Single CSF file to distill")
    parser.add_argument("--project", help="Distill all un-distilled sessions for a project directory")
    parser.add_argument("--all", action="store_true", help="Distill all un-distilled sessions")
    parser.add_argument("--stats", action="store_true", help="Show distillation statistics")
    parser.add_argument("--mock", action="store_true", help="Use mock LLM (no API call)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be distilled without calling LLM")
    parser.add_argument("--limit", type=int, default=5, help="Max sessions to distill (default 5)")
    parser.add_argument("--traces-dir", default=os.path.expanduser("~/copilot-trace-data/traces"))
    parser.add_argument("--memory-path", default=None)
    parser.add_argument("--log-path", default=None)
    parser.add_argument("--review", action="store_true", help="Show memories pending review")
    parser.add_argument("--promote", action="store_true", help="Auto-promote pending memories older than 7 days")
    parser.add_argument("--owner", default=None, help="Owner username for memories")

    args = parser.parse_args()

    # Set default paths
    data_dir = PROJECT_ROOT / "data"
    memory_path = args.memory_path or str(data_dir / "memory.json")
    log_path = args.log_path or str(data_dir / "distillation_log.json")

    # ─── Stats mode ──────────────────────────────────────────────
    if args.stats:
        stats = get_distillation_stats(args.traces_dir, log_path)
        print("=== Distillation Stats ===")
        print(f"Total CSF files:  {stats['total_csf_files']}")
        print(f"Distilled:        {stats['distilled']}")
        print(f"Skipped:          {stats['skipped']}")
        print(f"Pending:          {stats['pending']}")
        mem_count = len(load_memories(memory_path))
        print(f"Total memories:   {mem_count}")
        pending = get_pending_review(memory_path)
        print(f"Pending review:   {len(pending)}")
        return 0

    # ─── Review mode ─────────────────────────────────────────────
    if args.review:
        pending = get_pending_review(memory_path)
        if not pending:
            print("No memories pending review.")
            return 0
        print(f"=== {len(pending)} Memories Pending Review ===\n")
        for m in pending:
            conf = f" (confidence: {m.get('confidence', '?')})" if m.get("confidence") else ""
            print(f"  [{m.get('category', '?')}] {m['text'][:100]}{conf}")
            print(f"    session: {m.get('session_id', '?')}")
            print()
        return 0

    # ─── Promote mode ────────────────────────────────────────────
    if args.promote:
        count = auto_promote_pending(memory_path)
        print(f"Auto-promoted {count} memories from pending to active.")
        return 0

    # ─── Single file mode ────────────────────────────────────────
    if args.csf_file:
        if not os.path.exists(args.csf_file):
            print(f"Error: file not found: {args.csf_file}", file=sys.stderr)
            return 1

        if args.dry_run:
            from core.condensation.csf_reader import read_csf
            data = read_csf(args.csf_file)
            print(f"Would distill: {data.session.title or 'untitled'}")
            print(f"  Session ID: {data.session.id}")
            print(f"  Messages: {len(data.messages)}")
            print(f"  Directory: {data.session.directory}")
            return 0

        print(f"Distilling: {args.csf_file}")
        result = distill_session(
            args.csf_file, memory_path, log_path,
            mock=args.mock, owner=args.owner,
        )
        _print_result(result)
        return 0

    # ─── Batch mode (--all or --project) ─────────────────────────
    if args.all or args.project:
        sessions = find_undistilled(
            args.traces_dir, log_path,
            project_dir=args.project,
        )

        if not sessions:
            print("No un-distilled sessions found.")
            return 0

        print(f"Found {len(sessions)} un-distilled sessions")
        if args.project:
            print(f"  Project: {args.project}")

        if args.dry_run:
            print("\nWould distill (dry run):")
            for s in sessions[:args.limit]:
                print(f"  {s['session_id'][:20]}... | msgs: {s['message_count']} | {s['title'] or 'untitled'}")
            if len(sessions) > args.limit:
                print(f"  ... and {len(sessions) - args.limit} more (limited to {args.limit})")
            return 0

        batch = sessions[:args.limit]
        print(f"Distilling {len(batch)} sessions{' (mock)' if args.mock else ''}...\n")

        total_memories = 0
        errors = 0
        for i, s in enumerate(batch, 1):
            print(f"[{i}/{len(batch)}] {s['title'] or 'untitled'}")
            print(f"  File: {s['file_path']}")
            result = distill_session(
                s["file_path"], memory_path, log_path,
                mock=args.mock, owner=args.owner,
            )
            _print_result(result)
            total_memories += result.get("memories_extracted", 0)
            if result.get("skipped") and result.get("reason") == "llm_error":
                errors += 1
            print()

        print(f"=== Batch Complete ===")
        print(f"Sessions processed: {len(batch)}")
        print(f"Total memories extracted: {total_memories}")
        if errors > 0:
            print(f"LLM errors (skipped): {errors}")
        remaining = len(sessions) - len(batch)
        if remaining > 0:
            print(f"Remaining: {remaining} sessions (run again to continue)")
        return 0

    # No mode specified
    parser.print_help()
    return 1


def _print_result(result: dict) -> None:
    """Print a distillation result summary."""
    if result.get("skipped"):
        print(f"  Skipped: {result['reason']} ({result.get('message_count', 0)} messages)")
        return

    print(f"  Memories: {result['memories_extracted']}")
    print(f"  Confidence: {result['confidence']:.2f}")
    print(f"  Summary: {result['summary'][:100]}")
    cats = result.get("categories", {})
    if cats:
        parts = [f"{k}: {v}" for k, v in cats.items() if v > 0]
        if parts:
            print(f"  Categories: {', '.join(parts)}")


if __name__ == "__main__":
    raise SystemExit(main())
