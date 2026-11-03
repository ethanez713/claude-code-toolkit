#!/usr/bin/env python3
"""Refuse a commit that would publish a machine-local identifier.

Added lines and staged paths are matched against a denylist of
case-insensitive regexes. The list is TOOLKIT_DENYLIST, or
~/.claude/toolkit-local/denylist, and lives outside the repo so it is
never published. A missing file, an unreadable file, or a bad regex
fails the commit: the guard is there for safety. Pass --all to audit
every tracked file instead of the index.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

CLOSING = (
    "remove the detail, or git commit --no-verify if it is a false positive\n"
)
# A staged name is not a file line. 0 keeps the path:line: hit format.
PATH_LINE = 0
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def denylist_path():
    override = os.environ.get("TOOLKIT_DENYLIST")
    if override is not None:
        return Path(override).expanduser()
    return Path.home() / ".claude" / "toolkit-local" / "denylist"


def load_patterns(path):
    if not path.exists():
        sys.stderr.write(f"missing denylist: {path}\n")
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        sys.stderr.write(f"unreadable denylist: {path}\n")
        return None
    patterns = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            patterns.append((line, re.compile(line, re.IGNORECASE)))
        except re.error:
            sys.stderr.write(f"invalid regex in {path}: {line}\n")
            return None
    return patterns


def run_git(args):
    # Pin the diff shape. A local diff.noprefix or mnemonicPrefix would
    # drop the b/ marker and the parser would miss hits.
    proc = subprocess.run(
        [
            "git",
            "-c", "diff.mnemonicPrefix=false",
            "-c", "diff.noprefix=false",
            "-c", "core.quotePath=true",
            *args,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        sys.stderr.write((err or "git failed") + "\n")
        return None
    return proc.stdout


def decode_quoted(body):
    # Git C-quotes a path as bytes. Octal escapes are those bytes, not
    # code points, so "caf\303\251" is café rather than cafÃ©.
    raw = bytearray()
    i = 0
    simple = {
        "a": 0x07, "b": 0x08, "f": 0x0C, "n": 0x0A, "r": 0x0D,
        "t": 0x09, "v": 0x0B, "\\": 0x5C, '"': 0x22,
    }
    while i < len(body):
        if body[i] != "\\":
            raw.append(ord(body[i]))
            i += 1
            continue
        i += 1
        if i >= len(body):
            break
        esc = body[i]
        if esc in simple:
            raw.append(simple[esc])
            i += 1
            continue
        if esc in "01234567":
            octal = esc
            i += 1
            for _ in range(2):
                if i < len(body) and body[i] in "01234567":
                    octal += body[i]
                    i += 1
                else:
                    break
            raw.append(int(octal, 8))
            continue
        raw.append(ord(esc))
        i += 1
    return os.fsdecode(bytes(raw))


def path_token(raw):
    if raw.startswith('"'):
        i = 1
        while i < len(raw):
            if raw[i] == "\\":
                i += 2
                continue
            if raw[i] == '"':
                return decode_quoted(raw[1:i])
            i += 1
    # Git leaves a tab after an unquoted +++ path, where a timestamp would be.
    return raw.split("\t", 1)[0]


def new_path(raw):
    path = path_token(raw)
    if path == "/dev/null":
        return None
    if path.startswith("b/"):
        return path[2:]
    return path


def added_lines(diff):
    path = None
    new_line = None
    binary = False
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            path = None
            new_line = None
            binary = False
            continue
        if line.startswith("Binary files ") or line.startswith("GIT binary patch"):
            binary = True
            new_line = None
            continue
        if binary:
            continue
        if line.startswith("rename to ") or line.startswith("copy to "):
            path = path_token(line.split(" ", 2)[2])
            continue
        if line.startswith("+++ "):
            path = new_path(line[4:])
            continue
        hunk = HUNK.match(line)
        if hunk:
            new_line = int(hunk.group(1))
            continue
        if path is None or new_line is None:
            continue
        if line.startswith("+"):
            yield path, new_line, line[1:]
            new_line += 1
        elif line.startswith("-") or line.startswith("\\"):
            continue
        else:
            new_line += 1


def nul_paths(blob):
    return [os.fsdecode(part) for part in blob.split(b"\0") if part]


def collect(patterns, path, line_no, text, hits):
    for label, cre in patterns:
        if cre.search(text):
            hits.append(f"{path}:{line_no}: matches {label}")


def scan_staged(patterns):
    names = run_git(
        ["diff", "--cached", "--name-only", "-z", "--diff-filter=ACMR"]
    )
    if names is None:
        return None
    diff = run_git(
        ["diff", "--cached", "-U0", "--no-color", "--diff-filter=ACMR"]
    )
    if diff is None:
        return None
    hits = []
    for path, line_no, line in added_lines(diff.decode("utf-8", errors="replace")):
        collect(patterns, path, line_no, line, hits)
    for path in nul_paths(names):
        collect(patterns, path, PATH_LINE, path, hits)
    return hits


def repo_root():
    out = run_git(["rev-parse", "--show-toplevel"])
    if out is None:
        return None
    return Path(os.fsdecode(out.strip()))


def scan_all(patterns):
    names = run_git(["ls-files", "-z"])
    if names is None:
        return None
    root = repo_root()
    if root is None:
        return None
    hits = []
    for rel in nul_paths(names):
        collect(patterns, rel, PATH_LINE, rel, hits)
        full = root / rel
        try:
            if full.is_symlink():
                collect(patterns, rel, 1, os.readlink(full), hits)
                continue
            data = full.read_bytes()
        except OSError:
            continue
        # Same signal git uses: a NUL means the blob is binary.
        if b"\0" in data:
            continue
        text = data.decode("utf-8", errors="replace")
        for line_no, line in enumerate(text.splitlines(), 1):
            collect(patterns, rel, line_no, line, hits)
    return hits


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    if argv not in ([], ["--all"]):
        sys.stderr.write("usage: check_identifiers.py [--all]\n")
        return 1
    patterns = load_patterns(denylist_path())
    if patterns is None:
        return 1
    hits = scan_all(patterns) if argv == ["--all"] else scan_staged(patterns)
    if hits is None:
        return 1
    if not hits:
        return 0
    sys.stderr.write("\n".join(hits) + "\n")
    sys.stderr.write(CLOSING)
    return 1


if __name__ == "__main__":
    sys.exit(main())
