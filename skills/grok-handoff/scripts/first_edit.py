#!/usr/bin/env python3
"""Model calls a Grok edit job spends before its first file edit, by Grok version.

`Start at` in a brief exists to cut this phase, so this is the number it can move. Reads
round 1 of each edit job from Grok's own session transcripts.
"""

import argparse
import json
import re
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

JOB_DIR_RE = re.compile(r"\.grok-jobs%2F(g[0-9a-f]+)(?:%2F|$)")
# Parallel tool calls from one model call share the uuid and differ only in the suffix.
MODEL_CALL_RE = re.compile(r"call-([0-9a-f-]{36})-\d+$")
START_AT_RE = re.compile(r"(?m)^\s*Start at\b")
EDIT_KINDS = {"edit", "write"}
SEARCH_KINDS = {"search", "list"}
MIN_REPO_ROWS = 3


@dataclass
class Job:
    job: str
    version: str
    repo: str
    lines: int
    start_at: bool
    before: int
    searches: int
    total: int


def load_jsonl(path):
    rows = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return rows


def first_round_edits(state):
    rows = {}
    for name in ("events.1.jsonl", "events.jsonl"):
        for row in load_jsonl(state / name):
            if (row.get("event"), row.get("mode"), row.get("round")) == ("job_end", "edit", 1):
                rows[row.get("job")] = row
    return rows


def transcripts_by_job(sessions):
    found = defaultdict(list)
    if not sessions.is_dir():
        return found
    for workdir in sessions.iterdir():
        match = JOB_DIR_RE.search(workdir.name)
        if match:
            found[match.group(1)].extend(workdir.glob("*/updates.jsonl"))
    return found


def earliest_session(paths):
    loaded = [(rows[0].get("timestamp", ""), path, rows)
              for path in paths if (rows := load_jsonl(path))]
    return min(loaded, key=lambda item: int(item[0] or 0))[1:] if loaded else None


def brief_has_start_at(updates_path):
    session_id = updates_path.parent.name
    for row in load_jsonl(updates_path.parent.parent / "prompt_history.jsonl"):
        if row.get("session_id") == session_id:
            return bool(START_AT_RE.search(row.get("prompt") or ""))
    return False


def measure_round(rows):
    """Returns (calls before the first edit, searches before it, total calls), or None
    when the round never used an edit tool. The closing answer is a call with no tools."""
    calls = []
    searches = 0
    before = None
    for row in rows:
        update = (row.get("params") or {}).get("update") or {}
        kind = update.get("sessionUpdate")
        if kind == "turn_completed":
            break
        if kind != "tool_call":
            continue
        match = MODEL_CALL_RE.match(update.get("toolCallId") or "")
        if not match:
            continue
        if match.group(1) not in calls:
            calls.append(match.group(1))
        tool = ((update.get("_meta") or {}).get("x.ai/tool") or {}).get("kind")
        if before is None:
            if tool in EDIT_KINDS:
                before = len(calls) - 1
            elif tool in SEARCH_KINDS:
                searches += 1
    return None if before is None else (before, searches, len(calls) + 1)


def version_of(event):
    parts = (event.get("grok_version") or "").split()
    return parts[1] if len(parts) > 1 else "unknown"


def version_key(version):
    return [int(part) if part.isdigit() else 0 for part in version.split(".")]


def collect(state, sessions):
    jobs, unread, no_edit = [], 0, 0
    transcripts = transcripts_by_job(sessions)
    for job, event in first_round_edits(state).items():
        session = earliest_session(transcripts.get(job, []))
        if session is None:
            unread += 1
            continue
        path, rows = session
        measured = measure_round(rows)
        if measured is None:
            no_edit += 1
            continue
        lines = (event.get("diff_added") or 0) + (event.get("diff_deleted") or 0)
        jobs.append(Job(job, version_of(event), event.get("repo") or "?", lines,
                        brief_has_start_at(path), *measured))
    return jobs, unread, no_edit


def row(label, jobs):
    median = statistics.median
    before = median(job.before for job in jobs)
    total = median(job.total for job in jobs)
    return (f"{label:<26}{len(jobs):>4}{before:>8g}{median(job.searches for job in jobs):>10g}"
            f"{median(job.total - job.before for job in jobs):>7g}{total:>7g}"
            f"{median(job.lines for job in jobs):>7g}")


def render(jobs, unread, no_edit):
    header = f"{'':<26}{'n':>4}{'before':>8}{'searches':>10}{'after':>7}{'total':>7}{'lines':>7}"
    lines = ["Model calls before the first edit, round 1 of edit jobs (medians)", header]
    versions = sorted({job.version for job in jobs}, key=version_key)
    for version in versions:
        lines.append(row(version, [job for job in jobs if job.version == version]))
    lines.append("")
    for flag, label in ((True, "with Start at"), (False, "without Start at")):
        group = [job for job in jobs if job.start_at == flag]
        if group:
            lines.append(row(label, group))
    lines.append("")
    pairs = defaultdict(list)
    for job in jobs:
        pairs[(job.repo, job.version)].append(job)
    for (repo, version), group in sorted(
            pairs.items(), key=lambda item: (item[0][0], version_key(item[0][1]))):
        if len(group) >= MIN_REPO_ROWS:
            lines.append(row(f"{repo} {version}", group))
    lines.append("")
    lines.append(f"excluded: {unread} without a transcript, {no_edit} with no edit tool")
    return lines


def main(argv=None):
    parser = argparse.ArgumentParser(prog="first_edit.py")
    parser.add_argument("--state", type=Path, default=Path.home() / ".local/state/grok-mcp")
    parser.add_argument("--sessions", type=Path, default=Path.home() / ".grok/sessions")
    args = parser.parse_args(argv)
    for line in render(*collect(args.state, args.sessions)):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
