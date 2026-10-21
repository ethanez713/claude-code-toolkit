#!/usr/bin/env python3
"""Stand-in for the grok binary. Behaviour is selected by FAKE_GROK_MODE."""

import fcntl
import json
import os
import sys
import time
from pathlib import Path


def log_invocation():
    path = os.environ.get("FAKE_GROK_LOG")
    if not path:
        return
    rec = {
        "argv": sys.argv,
        "GROK_CLAUDE_MCPS_ENABLED": os.environ.get("GROK_CLAUDE_MCPS_ENABLED"),
    }
    data = (json.dumps(rec) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.write(fd, data)
    finally:
        os.close(fd)


def cwd_of(argv):
    if "--cwd" not in argv:
        return None
    return argv[argv.index("--cwd") + 1]


def emit(text):
    cwd = cwd_of(sys.argv)
    if cwd:
        Path(cwd, "made_by_grok.txt").write_text("ok\n", encoding="utf-8")
    payload = {
        "text": text,
        "sessionId": "s-fake",
        "num_turns": 2,
        "usage": {
            "input_tokens": 1000,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "output_tokens": 50,
            "reasoning_tokens": 0,
            "total_tokens": 1050,
        },
        "modelUsage": {"grok-4.7": {"modelCalls": 1}},
    }
    json.dump(payload, sys.stdout, indent=2)
    sys.stdout.write("\n")
    sys.stdout.flush()


def ok_text():
    return (
        "Did the task.\n"
        "\n"
        "STATUS: done\n"
        "SUMMARY: added the file\n"
        "VERIFIED: none\n"
        "ASSUMPTIONS: none\n"
        "OPEN: none\n"
    )


def huge_text():
    body = "x" * (50 * 1024)
    summary = "y" * 4000
    return (
        f"{body}\n"
        "STATUS: done\n"
        f"SUMMARY: {summary}\n"
        "VERIFIED: checked\n"
        "ASSUMPTIONS: none\n"
        "OPEN: none\n"
    )


def main():
    log_invocation()
    args = sys.argv[1:]
    if "--version" in args and "--prompt-file" not in args:
        print("grok fake 0.0.0")
        return 0
    mode = os.environ.get("FAKE_GROK_MODE", "ok")
    if mode == "sleep":
        time.sleep(float(os.environ.get("FAKE_GROK_SLEEP", "1")))
        mode = "ok"
    if mode == "error":
        sys.stdout.write(json.dumps({"type": "error", "message": "credits exhausted"}) + "\n")
        return 1
    if mode == "garbage":
        sys.stdout.write("this is not json\n")
        return 0
    if mode == "huge":
        emit(huge_text())
        return 0
    if mode == "ok":
        emit(ok_text())
        return 0
    sys.stderr.write(f"unknown FAKE_GROK_MODE {mode}\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
