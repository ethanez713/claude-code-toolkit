#!/usr/bin/env python3
"""PreToolUse gate on Write|Edit: a worklog is named after its project directory.

`~/.claude/worklog/-home-<user>-<project>.md` is the transcript-folder slug, and a session
that writes it splits the project's state across two files; a resumed session then reads
the stale one. Exit 2 sends the message on stderr back to the model and blocks the call.
Every other path exits 0.
"""
import json
import os
import sys

WORKLOG_DIR = os.path.expanduser("~/.claude/worklog")


def suggested_name(cwd):
    if not cwd or os.path.realpath(cwd) == os.path.expanduser("~"):
        return "<project-dir-basename>.md"
    return os.path.basename(os.path.realpath(cwd).rstrip("/")) + ".md"


def rejection(event):
    inp = event.get("tool_input")
    path = inp.get("file_path") if isinstance(inp, dict) else None
    if not isinstance(path, str):
        return None
    full = os.path.realpath(os.path.expanduser(path))
    if os.path.dirname(full) != os.path.realpath(WORKLOG_DIR):
        return None
    if not os.path.basename(full).startswith("-"):
        return None
    name = suggested_name(event.get("cwd"))
    return (
        f"Worklogs are named after the project directory's basename, not the '-home-…' "
        f"transcript slug: use ~/.claude/worklog/{name} (it may already exist; read it first)."
    )


def main():
    try:
        event = json.load(sys.stdin)
        message = rejection(event)
    except Exception:
        return 0
    if message:
        print(message, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
