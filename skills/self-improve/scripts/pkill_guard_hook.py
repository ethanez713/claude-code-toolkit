#!/usr/bin/env python3
"""PreToolUse gate: refuse a pattern kill that would match its own shell.

Wired from ~/.claude/settings.json on Bash. `pkill -f` matches full command lines, and
the shell running the command carries the pattern in its own, so it dies with exit 144
(143 in `adb shell`) before anything after the kill runs, and its output is lost. Always
exits 0: a hook failure would surface on every Bash call.
"""
from __future__ import annotations

import json
import re
import sys

KILLER = r"(?:^|[;&|(\n]|\$\()\s*(?:sudo\s+|timeout\s+\S+\s+)?"
# Text that mentions a kill is not a kill. Refusing it costs more than a missed self-kill.
PROSE = re.compile(
    r"""(?:-m|--message)\s+(?:"(?:[^"\\]|\\.)*"|'[^']*')"""
    r"""|'[^']*\n[^']*'|"(?:[^"\\]|\\.)*\n(?:[^"\\]|\\.)*\"""")
PATTERN = r"""('[^']*'|"[^"]*"|[^\s;|&)'"]+)"""
FLAGS = r"((?:\s+--?[A-Za-z0-9]+(?:\s+\d+)?)*)"
FULL_MATCH = re.compile(r"(?:^|\s)(?:-[A-Za-z0-9]*f|--full)")
PKILL = re.compile(KILLER + r"pkill" + FLAGS + r"\s+" + PATTERN)
PGREP = re.compile(KILLER + r"pgrep" + FLAGS + r"\s+" + PATTERN + r"[^;&|\n]*\|\s*xargs[^;&|\n]*\bkill\b")
KILL_PGREP = re.compile(r"\bkill\b[^;&|\n]*\$\(\s*pgrep" + FLAGS + r"\s+" + PATTERN)
HEREDOC = re.compile(r"<<-?\s*'?\"?(\w+)'?\"?[^\n]*\n.*?\n\s*\1\b", re.S)
REASON = (
    "This pattern also matches this shell's own command line, so the kill would end the "
    "command with exit 144 and lose its output. Bracket one character so the pattern no "
    "longer matches its own text: pkill -f '[g]make' instead of pkill -f gmake. If the "
    "match is only text in an argument, move that text into a file."
)


def self_matching(command: str) -> bool:
    code = PROSE.sub("", HEREDOC.sub("", command))
    for regex in (PKILL, PGREP, KILL_PGREP):
        for flags, pattern in (m.groups() for m in regex.finditer(code)):
            if not FULL_MATCH.search(flags):
                continue
            try:
                if re.search(pattern.strip("'\""), command):
                    return True
            except re.error:
                continue
    return False


def main() -> int:
    try:
        event = json.loads(sys.stdin.read())
        command = event["tool_input"]["command"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return 0
    if isinstance(command, str) and self_matching(command):
        json.dump(
            {"hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": REASON,
            }},
            sys.stdout,
            separators=(",", ":"),
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
