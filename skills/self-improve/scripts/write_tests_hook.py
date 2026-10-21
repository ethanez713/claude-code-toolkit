#!/usr/bin/env python3
"""PreToolUse gate: make the write-tests skill load before a session's first test write.

Wired from ~/.claude/settings.json on Write|Edit. The first write to a test file in a
session is denied with an instruction to invoke write-tests and retry; every later
write in that session passes. A plain reminder was tried first and ignored in every
test-writing session (0 of 6), so this is deliberately a gate. Silent unless the path
is a test file; always exits 0 — a hook failure would surface on every tool call.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time

WRITE_TOOLS = {"Write", "Edit"}
TEST_DIRS = {"test", "tests", "androidTest"}
MARKERS = "/tmp/claude-write-tests-gate"
LOG = os.path.expanduser("~/.claude/skills/self-improve/state/write-tests-gate.jsonl")
INVOKED = re.compile(r'"skill"\s*:\s*"write-tests"')
GATE_REASON = (
    "This session's first test-file write is gated on the write-tests skill. Invoke it "
    "with the Skill tool, apply it, then retry this exact edit — it will go through. "
    "The gate fires once per session."
)


def is_test_path(path: str) -> bool:
    if not path:
        return False
    posix = path.replace("\\", "/")
    name = posix.rsplit("/", 1)[-1]
    if name.endswith("Test.kt"):
        return True
    if name.startswith("test_") and name.endswith(".py"):
        return True
    stem, dot, ext = name.rpartition(".")
    if dot and ext and stem.endswith("_test"):
        return True
    return any(part in TEST_DIRS for part in posix.split("/") if part)


def file_path_from(event: dict) -> str:
    inp = event.get("tool_input")
    if not isinstance(inp, dict):
        return ""
    for key in ("file_path", "path"):
        value = inp.get(key)
        if isinstance(value, str):
            return value
    return ""


def is_test_write(event: dict) -> bool:
    return event.get("tool_name") in WRITE_TOOLS and is_test_path(file_path_from(event))


def claim_session(session: str) -> bool:
    """True the first time a session is seen; the marker makes the gate fire once."""
    os.makedirs(MARKERS, exist_ok=True)
    try:
        os.close(os.open(os.path.join(MARKERS, session), os.O_CREAT | os.O_EXCL))
        return True
    except FileExistsError:
        return False


def already_invoked(transcript_path: str) -> bool:
    try:
        with open(transcript_path, errors="replace") as fh:
            return any(INVOKED.search(line) for line in fh if '"Skill"' in line)
    except OSError:
        return False


def main() -> int:
    raw = sys.stdin.read()
    if not raw.strip():
        return 0
    try:
        event = json.loads(raw)
    except json.JSONDecodeError:
        return 0
    if not isinstance(event, dict) or not is_test_write(event):
        return 0
    session = event.get("session_id")
    if not isinstance(session, str) or not session or not claim_session(session):
        return 0
    invoked = already_invoked(event.get("transcript_path") or "")
    with open(LOG, "a") as fh:
        fh.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "session": session,
                             "file": os.path.basename(file_path_from(event)),
                             "already_invoked": invoked}) + "\n")
    if invoked:
        return 0
    json.dump(
        {"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": GATE_REASON,
        }},
        sys.stdout,
        separators=(",", ":"),
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
