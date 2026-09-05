#!/usr/bin/env python3
"""
memory-to-notes — Convert memory.json entries into Obsidian .md notes with wikilinks.

Incremental by default: only processes entries added since the last run.
Use --full to force a complete rebuild.
Use --digest to roll up entries since the last digest into a short
'## Recent Activity' bullet per project note, instead of appending raw facts.
Use --json-events to emit NDJSON progress events on stdout (for SSE streaming).
"""

import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── Configuration ──────────────────────────────────────────────────────────────
# Use __file__-relative paths to be immune to $HOME misconfiguration.
_SCRIPT_DIR = Path(__file__).resolve().parent
_ODYSSEUS_DIR = _SCRIPT_DIR.parent.parent          # …/Develop/odysseus
_REAL_HOME = _ODYSSEUS_DIR.parent.parent            # …/ (actual /Users/<you>)

MEMORY_PATH = _ODYSSEUS_DIR / "data" / "memory.json"
VAULT_PATH = _REAL_HOME / "Develop" / "obsidian-ws" / "Documents" / "Obsidian Vault"
PROJECTS_DIR = VAULT_PATH / "Projects"
TRACKING_FILE = _SCRIPT_DIR / ".memory_to_notes_tracking.json"
DIGEST_TRACKING_FILE = _SCRIPT_DIR / ".digest_tracking.json"
DIGEST_MAX_BULLETS = 6              # keep only the last N digest rollups per project note
DIGEST_DEFAULT_LOOKBACK_DAYS = 7    # window used on the very first digest run

# Project detection: keyword regex patterns → project slug + name
PROJECT_PATTERNS = {
    "agimus-spacelab": {
        "name": "Agimus Spacelab",
        "patterns": [r"agimus", r"spacelab", r"space\s*lab", r"cnrs", r"laas"],
    },
    "figaroh": {
        "name": "Figaroh",
        "patterns": [r"figaroh", r"figaro"],
    },
    "odysseus": {
        "name": "Odysseus",
        "patterns": [r"odysseus", r"odyssey", r"session-knowledge"],
    },
    "finance": {
        "name": "Finance",
        "patterns": [r"finance", r"trading", r"portfolio", r"stock", r"crypto", r"investment"],
    },
    "fullstack-manip": {
        "name": "Fullstack Manip",
        "patterns": [r"fullstack", r"full-stack", r"manip"],
    },
    "soarm-ws": {
        "name": "SOARM Workspace",
        "patterns": [r"soarm", r"s[o0]arm"],
    },
    "drones-sim": {
        "name": "Drones Simulation",
        "patterns": [r"drone", r"ardupilot", r"px4", r"gazebo"],
    },
    "job-radar": {
        "name": "Job Radar",
        "patterns": [r"job.radar", r"job\s*search", r"cv", r"resume", r"interview"],
    },
    "obsidian-vault": {
        "name": "Obsidian Vault",
        "patterns": [r"obsidian", r"vault", r"note.taking"],
    },
    "opencode": {
        "name": "OpenCode",
        "patterns": [r"opencode", r"open.code"],
    },
    "zcode": {
        "name": "ZCode",
        "patterns": [r"zcode", r"z.code"],
    },
    "french": {
        "name": "French Study",
        "patterns": [r"french", r"fran[çc]ais", r"language.learning"],
    },
    "robotics": {
        "name": "Robotics & RL Study",
        "patterns": [r"robotics", r"reinforcement.learning", r"\brl\b", r"robot"],
    },
}

# Directory-to-project overrides: when the directory basename doesn't match
# the project slug, map a substring of the directory path to the correct slug.
DIR_OVERRIDES = {
    "agimus-ws": "agimus-spacelab",
    "figaroh-ws": "figaroh",
    "obsidian-ws": "obsidian-vault",
    "iCloud~md~obsidian": "obsidian-vault",
}

# How many characters of the entry text to show in the note
TEXT_SNIPPET_LENGTH = 300

# ── Output mode ────────────────────────────────────────────────────────────────
_JSON_EVENTS = False  # set by --json-events flag


def log(event_type, payload=None):
    """Emit either human-readable text or a JSON event depending on mode."""
    if _JSON_EVENTS:
        event = {"event": event_type}
        if payload:
            event.update(payload)
        print(json.dumps(event), flush=True)
    elif event_type == "phase":
        print(f"\n📂 {payload.get('message', '')}")
    elif event_type == "progress":
        pct = payload.get("percent", 0)
        bar_len = 20
        filled = int(bar_len * pct / 100)
        bar = "█" * filled + "░" * (bar_len - filled)
        print(f"\r   [{bar}] {payload.get('current', '?')}/{payload.get('total', '?')} ({pct}%)", end="", flush=True)
    elif event_type == "entry_matched":
        pass  # too verbose for console
    elif event_type == "entry_skipped":
        print(f"   ⏭  Skipped {payload.get('entry_id', '?')}: {payload.get('reason', '')}")
    elif event_type == "project_written":
        print(f"   ✓  {payload.get('project', '?')}: {'+'+str(payload.get('entries_added', 0)) if payload.get('entries_added') else payload.get('action', '')}")
    elif event_type == "summary":
        print(f"\n✅ Done. {payload.get('new_entries', '?')} new entries across {payload.get('projects_updated', '?')} projects.")
    elif event_type == "error":
        print(f"   ❌ {payload.get('message', '')}", file=sys.stderr)
    elif event_type == "done":
        pass
    else:
        # fallback for unknown events
        print(f"[{event_type}] {json.dumps(payload) if payload else ''}")


# ── Helpers ────────────────────────────────────────────────────────────────────

def slugify(text):
    """Turn a project name into a kebab-case slug."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def extract_date(entry):
    """Pull the best date from a memory entry (created_at, timestamp, or now)."""
    for field in ("created_at", "timestamp", "date"):
        val = entry.get(field)
        if val is None:
            continue
        # Unix timestamp (int or float)
        if isinstance(val, (int, float)):
            try:
                return datetime.fromtimestamp(val, tz=timezone.utc)
            except (ValueError, OSError):
                pass
        # ISO-8601 string
        if isinstance(val, str):
            try:
                return datetime.fromisoformat(val.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                pass
    return datetime.now(timezone.utc)


def detect_projects(text):
    """Return set of project slugs whose patterns match the text."""
    text_lower = text.lower()
    found = set()
    for slug, proj in PROJECT_PATTERNS.items():
        for pattern in proj["patterns"]:
            if re.search(pattern, text_lower):
                found.add(slug)
                break
    return found


def detect_project_from_dir(directory):
    """Derive a project slug from the entry's directory field.

    Returns a project slug string, or None if directory is missing/generic.
    Checks DIR_OVERRIDES first, then auto-derives from the basename.
    """
    if not directory or not directory.strip():
        return None
    dir_lower = directory.lower()
    # Check overrides first (longest match first for specificity)
    for pattern, slug in sorted(DIR_OVERRIDES.items(), key=lambda x: -len(x[0])):
        if pattern.lower() in dir_lower:
            return slug
    # Auto-derive from basename
    basename = directory.rstrip("/").split("/")[-1]
    if basename.lower() in ("develop", "", "unknown"):
        return None  # Too generic — fall back to keyword matching
    return slugify(basename)


def detect_entry_projects(entry):
    """Detect projects for a memory entry using directory first, keywords as fallback.

    Priority:
    1. entry['directory'] → project slug (ground truth from distillation)
    2. keyword matching on entry text (fallback for entries without directory)
    3. 'uncategorized' if neither yields a match
    """
    # Primary: directory-based detection
    dir_slug = detect_project_from_dir(entry.get("directory"))
    if dir_slug:
        return {dir_slug}
    # Fallback: keyword-based detection for entries without a usable directory
    search_text = json.dumps(entry).lower()
    kw_projects = detect_projects(search_text)
    if kw_projects:
        return kw_projects
    # Last resort
    return {"uncategorized"}


def format_entry(entry):
    """Render a single memory entry as a markdown list item."""
    cat = entry.get("category", "other")
    text = entry.get("text", entry.get("content", str(entry)))
    snippet = text[:TEXT_SNIPPET_LENGTH]
    if len(text) > TEXT_SNIPPET_LENGTH:
        snippet += "…"
    date = extract_date(entry).strftime("%Y-%m-%d")
    entry_id = entry.get("id", "")
    return f"- [{cat}] ({date}) {snippet}  `{entry_id}`"


def load_tracking():
    """Load the tracking state {last_entry_id: ..., last_timestamp: ...}."""
    if os.path.exists(TRACKING_FILE):
        with open(TRACKING_FILE, "r") as f:
            return json.load(f)
    return {}


def save_tracking(state):
    """Persist the tracking state."""
    os.makedirs(os.path.dirname(TRACKING_FILE), exist_ok=True)
    with open(TRACKING_FILE, "w") as f:
        json.dump(state, f, indent=2)


def load_digest_tracking():
    """Load the digest tracking state {last_digest_ts: ...}."""
    if os.path.exists(DIGEST_TRACKING_FILE):
        with open(DIGEST_TRACKING_FILE, "r") as f:
            return json.load(f)
    return {}


def save_digest_tracking(state):
    with open(DIGEST_TRACKING_FILE, "w") as f:
        json.dump(state, f, indent=2)


def load_memory():
    """Load the full memory.json array."""
    with open(MEMORY_PATH, "r") as f:
        return json.load(f)


def ensure_dir(path):
    os.makedirs(path, exist_ok=True)


# ── Core logic ─────────────────────────────────────────────────────────────────

def process_entries(entries, full_rebuild=False):
    """
    Group entries by project and category, then write/update project notes.

    Returns the tracking state that should be saved after success.
    """
    if not entries:
        log("phase", {"message": "No entries to process."})
        return None

    start_time = time.time()

    # Phase: grouping
    log("phase", {"message": f"Grouping {len(entries)} entries by project..."})

    grouped = defaultdict(lambda: defaultdict(list))
    matched_count = 0
    skipped_count = 0
    seen_ids = set()
    latest_id = None
    latest_ts = None
    total = len(entries)

    for i, entry in enumerate(entries):
        entry_id = entry.get("id", "")
        seen_ids.add(entry_id)
        if latest_id is None:
            latest_id = entry_id
            latest_ts = extract_date(entry).isoformat()

        # Detect project: directory first (ground truth), keywords as fallback
        projects = detect_entry_projects(entry)

        for slug in projects:
            cat = entry.get("category", "other")
            grouped[slug][cat].append(entry)

        matched_count += 1

        # Progress every 500 entries
        if (i + 1) % 500 == 0 or (i + 1) == total:
            pct = int((i + 1) / total * 100)
            log("progress", {"current": i + 1, "total": total, "percent": pct})

    log("phase", {"message": f"Matched {matched_count} entries to {len(grouped)} projects."})

    # Phase: writing notes
    log("phase", {"message": "Writing project notes..."})
    ensure_dir(PROJECTS_DIR)
    project_slugs_updated = set()
    total_new_entries = 0

    for slug, cat_map in sorted(grouped.items()):
        proj_name = PROJECT_PATTERNS.get(slug, {}).get("name", slug.replace("-", " ").title())
        note_path = os.path.join(PROJECTS_DIR, slug, f"{slug}.md")
        project_slugs_updated.add(slug)

        try:
            if full_rebuild or not os.path.exists(note_path):
                count = sum(len(v) for v in cat_map.values())
                write_new_note(note_path, slug, proj_name, cat_map)
                log("project_written", {
                    "project": slug,
                    "name": proj_name,
                    "action": "created",
                    "entries_added": count,
                    "path": note_path,
                })
                total_new_entries += count
            else:
                added = append_to_note(note_path, slug, proj_name, cat_map)
                log("project_written", {
                    "project": slug,
                    "name": proj_name,
                    "action": "updated" if added > 0 else "unchanged",
                    "entries_added": added,
                    "path": note_path,
                })
                total_new_entries += added
        except Exception as exc:
            log("error", {"message": str(exc), "project": slug})

    # Regenerate the projects index
    log("phase", {"message": "Updating projects index..."})
    try:
        write_projects_index(project_slugs_updated)
    except Exception as exc:
        log("error", {"message": f"Failed to update index: {exc}"})

    duration = round(time.time() - start_time, 1)

    result = {
        "last_entry_id": latest_id,
        "last_timestamp": latest_ts,
        "total_entries_processed": len(entries),
        "entry_ids_seen": sorted(seen_ids),
        "summary": {
            "total_entries": len(entries),
            "new_entries": total_new_entries,
            "projects_updated": len(project_slugs_updated),
            "duration_sec": duration,
            "mode": "full" if full_rebuild else "incremental",
        },
    }

    log("summary", result["summary"])
    return result


def write_new_note(note_path, slug, proj_name, cat_map):
    """Create a fresh project note."""
    ensure_dir(os.path.dirname(note_path))

    lines = [
        "---",
        f"project: {slug}",
        f"name: {proj_name}",
        f"updated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "type: project-note",
        "---",
        "",
        f"# {proj_name}",
        "",
        "## Related",
        f"- [[projects-index|Projects Index]]",
        "",
    ]

    related = find_related_projects(slug, cat_map)
    for r in sorted(related):
        lines.append(f"- [[{r}/{r}|{PROJECT_PATTERNS.get(r, {}).get('name', r)}]]")
    lines.append("")

    for cat, entries in sorted(cat_map.items()):
        lines.append(f"## {cat.title()}")
        lines.append("")
        for e in sorted(entries, key=lambda x: extract_date(x), reverse=True):
            lines.append(format_entry(e))
        lines.append("")

    with open(note_path, "w") as f:
        f.write("\n".join(lines))


def append_to_note(note_path, slug, proj_name, cat_map):
    """Append new entries to an existing project note under the right categories.
    Returns the number of new entries added."""
    with open(note_path, "r") as f:
        content = f.read()

    existing_ids = set(re.findall(r"`([a-f0-9-]{20,})`", content))

    appended = 0
    for cat, entries in sorted(cat_map.items()):
        new_entries = [e for e in entries if e.get("id", "") not in existing_ids]
        if not new_entries:
            continue

        heading = f"## {cat.title()}"
        heading_pos = content.find(heading)

        new_lines = []
        for e in sorted(new_entries, key=lambda x: extract_date(x), reverse=True):
            new_lines.append(format_entry(e))
        new_lines.append("")
        new_block = "\n".join(new_lines)

        if heading_pos != -1:
            after_heading = content.index("\n", heading_pos + len(heading)) + 1
            next_blank = content.index("\n", after_heading)
            insert_pos = next_blank + 1
            content = content[:insert_pos] + new_block + content[insert_pos:]
        else:
            related_pos = content.find("## Related")
            if related_pos != -1:
                content = content[:related_pos] + f"{heading}\n\n{new_block}" + content[related_pos:]
            else:
                content += f"\n{heading}\n\n{new_block}"

        appended += len(new_entries)

    content = re.sub(
        r"updated: .*",
        f"updated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        content,
    )

    if appended > 0:
        with open(note_path, "w") as f:
            f.write(content)

    return appended


def find_related_projects(slug, cat_map, max_related=5):
    """Find projects that share entry keywords with this one."""
    related = set()
    all_text = " ".join(e.get("text", "") for entries in cat_map.values() for e in entries)
    for other_slug, proj in PROJECT_PATTERNS.items():
        if other_slug == slug:
            continue
        for pattern in proj["patterns"]:
            if re.search(pattern, all_text.lower()):
                related.add(other_slug)
                break
    return related


def summarize_digest(cat_map):
    """Rule-based one-line rollup: counts by category + which dirs were touched.

    No LLM call — just a readable roll-up of what the raw fact log already
    contains. Good enough to answer "what happened here recently" at a glance.
    """
    parts = [
        f"{len(entries)} {cat}{'s' if len(entries) != 1 else ''}"
        for cat, entries in sorted(cat_map.items())
    ]
    dirs = sorted({
        entry["directory"].rstrip("/").split("/")[-1]
        for entries in cat_map.values()
        for entry in entries
        if entry.get("directory")
    })
    line = ", ".join(parts)
    if dirs:
        shown = ", ".join(dirs[:6])
        line += f" — touched: {shown}"
        if len(dirs) > 6:
            line += f", +{len(dirs) - 6} more"
    return line


def write_digest(note_path, since_dt, cat_map):
    """Insert/refresh the '## Recent Activity' rollup at the top of a project note.

    Keeps only the last DIGEST_MAX_BULLETS dated bullets so the section stays
    a quick skim, while the full per-entry fact log (written by
    write_new_note/append_to_note) stays untouched further down the file.
    Returns False if the project has no note yet (nothing to prepend to).
    """
    if not os.path.exists(note_path):
        return False

    with open(note_path, "r") as f:
        content = f.read()

    today = datetime.now().strftime("%Y-%m-%d")
    since_str = since_dt.strftime("%Y-%m-%d")
    new_bullet = f"- **{today}** (since {since_str}): {summarize_digest(cat_map)}"

    heading = "## Recent Activity"
    start = content.find(heading)

    if start == -1:
        h1 = re.search(r"^# .+$", content, re.MULTILINE)
        insert_at = h1.end() + 1 if h1 else len(content)
        section = f"\n{heading}\n{new_bullet}\n"
        content = content[:insert_at] + section + content[insert_at:]
    else:
        line_end = content.index("\n", start) + 1
        rest = content[line_end:]
        next_heading = re.search(r"^## ", rest, re.MULTILINE)
        block_end = line_end + (next_heading.start() if next_heading else len(rest))
        existing_bullets = [l for l in content[line_end:block_end].splitlines() if l.strip()]
        bullets = [new_bullet] + existing_bullets[:DIGEST_MAX_BULLETS - 1]
        content = content[:line_end] + "\n".join(bullets) + "\n\n" + content[block_end:]

    with open(note_path, "w") as f:
        f.write(content)
    return True


def run_digest(all_entries):
    """Roll up entries since the last digest run into a '## Recent Activity'
    bullet per affected project note. Independent of the incremental
    fact-append tracking — safe to run on any cadence (manual, weekly cron)."""
    tracking = load_digest_tracking()
    last_ts = tracking.get("last_digest_ts")
    since_dt = (
        datetime.fromisoformat(last_ts) if last_ts
        else datetime.now(timezone.utc) - timedelta(days=DIGEST_DEFAULT_LOOKBACK_DAYS)
    )

    entries_since = [e for e in all_entries if extract_date(e) > since_dt]
    if not entries_since:
        log("phase", {"message": f"No new entries since last digest ({since_dt.date()})."})
        log("done", {})
        return

    grouped = defaultdict(lambda: defaultdict(list))
    for entry in entries_since:
        for slug in detect_entry_projects(entry):
            grouped[slug][entry.get("category", "other")].append(entry)

    updated = 0
    for slug, cat_map in sorted(grouped.items()):
        note_path = os.path.join(PROJECTS_DIR, slug, f"{slug}.md")
        if write_digest(note_path, since_dt, cat_map):
            updated += 1
            log("project_written", {"project": slug, "action": "digest"})
        else:
            log("entry_skipped", {"entry_id": slug, "reason": "no note file yet — run without --digest first"})

    save_digest_tracking({"last_digest_ts": datetime.now(timezone.utc).isoformat()})
    log("summary", {"new_entries": len(entries_since), "projects_updated": updated})
    log("done", {})


def write_projects_index(updated_slugs=None):
    """Write or update the projects-index.md file in the vault root."""
    index_path = os.path.join(VAULT_PATH, "projects-index.md")

    existing_slugs = set()
    if os.path.exists(PROJECTS_DIR):
        for d in os.listdir(PROJECTS_DIR):
            note = os.path.join(PROJECTS_DIR, d, f"{d}.md")
            if os.path.exists(note):
                existing_slugs.add(d)

    lines = [
        "---",
        "type: index",
        f"updated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "---",
        "",
        "# Projects Index",
        "",
    ]

    for slug in sorted(existing_slugs | (updated_slugs or set())):
        name = PROJECT_PATTERNS.get(slug, {}).get("name", slug.replace("-", " ").title())
        note_path = os.path.join(PROJECTS_DIR, slug, f"{slug}.md")
        if os.path.exists(note_path):
            lines.append(f"- [[{slug}/{slug}|{name}]]")
        else:
            lines.append(f"- {name} *(no note yet)*")

    with open(index_path, "w") as f:
        f.write("\n".join(lines))


# ── Status helper (for API) ────────────────────────────────────────────────────

def get_status():
    """Return status info suitable for the API /status endpoint."""
    tracking = load_tracking()
    all_entries = load_memory()

    # Count entries per project (fast scan — no file writes)
    project_counts = defaultdict(int)
    for entry in all_entries:
        projects = detect_entry_projects(entry)
        for slug in projects:
            project_counts[slug] += 1

    # Check which project notes exist on disk
    existing_projects = []
    if os.path.exists(PROJECTS_DIR):
        for d in sorted(os.listdir(PROJECTS_DIR)):
            note = os.path.join(PROJECTS_DIR, d, f"{d}.md")
            if os.path.exists(note):
                existing_projects.append(d)

    return {
        "last_run": tracking.get("last_timestamp"),
        "last_entry_id": tracking.get("last_entry_id"),
        "total_entries": len(all_entries),
        "total_entries_processed": tracking.get("total_entries_processed", 0),
        "projects": {
            slug: {
                "name": PROJECT_PATTERNS.get(slug, {}).get("name", slug.replace("-", " ").title()),
                "entries": count,
                "has_note": slug in existing_projects,
            }
            for slug, count in sorted(project_counts.items(), key=lambda x: -x[1])
        },
    }


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    global _JSON_EVENTS
    _JSON_EVENTS = "--json-events" in sys.argv
    full_rebuild = "--full" in sys.argv
    dry_run = "--dry-run" in sys.argv
    status_only = "--status" in sys.argv
    digest_mode = "--digest" in sys.argv

    if status_only:
        print(json.dumps(get_status(), indent=2))
        return

    log("phase", {"message": f"Loading memory.json..."})
    all_entries = load_memory()
    log("phase", {"message": f"Total entries in memory: {len(all_entries)}"})

    if digest_mode:
        run_digest(all_entries)
        return

    tracking = load_tracking()
    last_id = tracking.get("last_entry_id")
    last_ts = tracking.get("last_timestamp")

    if full_rebuild:
        log("phase", {"message": "Full rebuild mode: processing all entries"})
        entries_to_process = all_entries
    elif last_id:
        try:
            last_index = next(
                i for i, e in enumerate(all_entries) if e.get("id") == last_id
            )
            entries_to_process = all_entries[last_index + 1:]
            log("phase", {"message": f"Incremental: {len(entries_to_process)} new entries since last run"})
        except StopIteration:
            log("phase", {"message": "Last ID not found, falling back to timestamp comparison"})
            if last_ts:
                last_dt = datetime.fromisoformat(last_ts)
                entries_to_process = [
                    e for e in all_entries
                    if extract_date(e) > last_dt
                ]
            else:
                entries_to_process = all_entries
    else:
        log("phase", {"message": "First run: processing all entries"})
        entries_to_process = all_entries

    if not entries_to_process:
        log("phase", {"message": "No new entries to process."})
        log("done", {})
        return

    log("phase", {"message": f"Entries to process: {len(entries_to_process)}"})

    if dry_run:
        log("phase", {"message": "DRY RUN — previewing entries that would be processed"})
        for e in entries_to_process[:10]:
            log("entry_matched", {
                "entry_id": e.get("id", "?"),
                "category": e.get("category", "?"),
                "text": e.get("text", "")[:80],
            })
        if len(entries_to_process) > 10:
            log("phase", {"message": f"... and {len(entries_to_process) - 10} more"})
        log("done", {"dry_run": True})
        return

    new_state = process_entries(entries_to_process, full_rebuild=full_rebuild)

    if new_state:
        save_tracking(new_state)
        log("phase", {"message": f"Tracking saved: last_id={new_state['last_entry_id']}"})

    log("done", {})

    if not _JSON_EVENTS:
        print(f"\n💡 Next run: `python3 {__file__}`  (incremental)")
        print(f"   Full rebuild: `python3 {__file__} --full`")
        print(f"   Dry run:      `python3 {__file__} --dry-run`")
        print(f"   Digest:       `python3 {__file__} --digest`")


if __name__ == "__main__":
    main()
