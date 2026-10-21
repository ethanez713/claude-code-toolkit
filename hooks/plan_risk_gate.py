#!/usr/bin/env python3
"""PreToolUse gate on ExitPlanMode: a plan must open with its risk block.

Exit 2 sends the message on stderr back to the model and blocks the call. Every
other path exits 0 — a gate that can't read the plan must not trap the session.
"""
import glob
import json
import os
import re
import sys
import time

PLANS = os.path.expanduser("~/.claude/plans")
LOG = os.path.expanduser("~/.claude/logs/plan_risk_gate.jsonl")
LOG_MAX_BYTES = 262144

ROUTINE = re.compile(r"^\**risk:?\**\s*routine\b", re.I)
CRITICAL = re.compile(r"^#{1,6}\s*\W*\s*critical decisions\b", re.I)

REJECTION = """\
The plan must open with its risk block, so a plan approved unread is still safe.
Begin the plan (after at most a '# Title' line) with EITHER:
  Risk: routine. <one clause on why>
OR a block that repeats each high-risk decision the user already answered:
  ## ⚠ Critical decisions
  - <decision> — user chose <answer> — executing it will <effect>
Any high-risk decision not yet answered goes to AskUserQuestion before ExitPlanMode.
"""


def plan_text(tool_input):
    plan = tool_input.get("plan")
    if isinstance(plan, str) and plan.strip():
        return plan, "plan"
    path = tool_input.get("planFilePath")
    if isinstance(path, str) and os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            return f.read(), "path"
    files = glob.glob(os.path.join(PLANS, "*.md"))
    if files:
        with open(max(files, key=os.path.getmtime), encoding="utf-8") as f:
            return f.read(), "newest"
    return "", "none"


def opening_line(text):
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if lines and re.match(r"^#\s+\S", lines[0]):
        lines = lines[1:]
    return lines[0] if lines else ""


def log(source, verdict, first_line):
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        if os.path.exists(LOG) and os.path.getsize(LOG) > LOG_MAX_BYTES:
            with open(LOG, "rb") as f:
                f.seek(-LOG_MAX_BYTES // 2, os.SEEK_END)
                tail = f.read().split(b"\n", 1)[-1]
            with open(LOG, "wb") as f:
                f.write(tail)
        event = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "source": source,
                 "verdict": verdict, "first_line_len": len(first_line)}
        with open(LOG, "a") as f:
            f.write(json.dumps(event) + "\n")
    except OSError:
        pass


def main():
    try:
        payload = json.load(sys.stdin)
        text, source = plan_text(payload.get("tool_input") or {})
    except (ValueError, OSError, AttributeError):
        return 0
    first = opening_line(text)
    if not text.strip():
        log(source, "no-plan", first)
        return 0
    if ROUTINE.match(first) or CRITICAL.match(first):
        log(source, "pass", first)
        return 0
    log(source, "reject", first)
    sys.stderr.write(REJECTION)
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
