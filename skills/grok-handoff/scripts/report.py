#!/usr/bin/env python3
"""Handoff measurement report.

Joins ``<state>/events.jsonl`` to main-conversation transcripts and to an inline
baseline of Claude's own commits. Prints per-kind savings, the exchange ratio,
and the pre-registered verdict. Derived rows are appended to ``claude_side.jsonl``
and ``baseline.jsonl`` so a later transcript purge still has them.
"""

import argparse
import fcntl
import json
import math
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

DOT = "·"
HANDOFF_TOOL = "mcp__grok__run"
FINISH_TOOL = "mcp__grok__finish"
REVIEW_TURN_CAP = 15
HANDOFF_WINDOW_S = 120
COMMIT_MATCH_S = 600
CLAUDE_CAPTURE_MAX_S = 600
MIN_CLAUDE_PAIRS = 3
MIN_GROK_ROUNDS = 3
VERDICT_MIN_N = 5
BREAK_EVEN_MIN_BASELINE = 20
BREAK_EVEN_MIN_JOBS = 10
SAME_REPO_BASELINE = 5
SIZE_BAND = 2.0
RESULT_TEXT_CAP = 8000
TOOL_RESULT_CAP = 20000

BASH_TOOLS = {"bash", "shell"}
SEPARATORS = {"&&", "||", ";", "|", "&"}
# Separators are their own tokens: `cd ~/repo; git commit` must not yield the path "~/repo;".
TOKEN_RE = re.compile(r"'[^']*'|\"[^\"]*\"|&&|\|\||[;|&]|[^\s;|&]+")
HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)([A-Za-z0-9_]+)\1")
MESSAGE_RE = re.compile(
    r"(?:^|\s)(?:-[A-Za-z]*m|--message)(?:=|\s+)('(?:[^']*)'|\"(?:[^\"]*)\"|\S+)"
)
FILE_STDIN_RE = re.compile(r"(?:^|\s)(?:-F|--file)(?:=|\s+)-(?=\s|$)")
ENV_ASSIGN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
BRACKET_SHA_RE = re.compile(r"\[[^\s\]]+\s+([0-9a-f]{7,40})\]")
ONELINE_SHA_RE = re.compile(r"(?m)^\s*([0-9a-f]{7,40})\b")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
FATAL_RE = re.compile(r"(?m)^(fatal:|error:)")

_TOPLEVEL = {}


def parse_args(argv):
    parser = argparse.ArgumentParser(prog="report.py")
    parser.add_argument("--verdict", action="store_true")
    parser.add_argument("--state", type=Path)
    parser.add_argument("--transcripts", type=Path)
    parser.add_argument("--prices", type=Path)
    return parser.parse_args(argv)


def resolve_paths(args):
    state = args.state or Path(os.environ.get("GROK_MCP_STATE") or "~/.local/state/grok-mcp")
    transcripts = args.transcripts or Path(
        os.environ.get("GROK_MCP_TRANSCRIPTS") or "~/.claude/projects"
    )
    prices = args.prices or Path(
        os.environ.get("GROK_MCP_PRICES") or Path(__file__).resolve().parent / "prices.json"
    )
    return state.expanduser(), transcripts.expanduser(), prices.expanduser()


def load_prices(path):
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if "models" not in data or "multipliers" not in data:
        raise SystemExit(f"prices missing models or multipliers: {path}")
    return data


def _num(source, key):
    if not isinstance(source, dict):
        return 0.0
    value = source.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


def message_cost(usage, model, prices):
    """Dollar cost of one assistant message.

    Cache writes come from ``cache_creation.ephemeral_5m/1h_input_tokens``.
    The flat ``cache_creation_input_tokens`` total mixes the two TTLs, which
    are priced at different multiples, so it is not a bucket of its own.
    A model id with no row (including ``<synthetic>``) costs 0.
    """
    if not isinstance(usage, dict) or not model:
        return 0.0
    price = (prices.get("models") or {}).get(model)
    if not price or "in" not in price or "out" not in price:
        return 0.0
    mult = prices.get("multipliers") or {}
    write_5m = float(mult.get("cache_write_5m", 1.25))
    write_1h = float(mult.get("cache_write_1h", 2.0))
    if "cache_read" in price:
        read_mult = float(price["cache_read"])
    else:
        read_mult = float(mult.get("cache_read", 0.1))
    created = usage.get("cache_creation") if isinstance(usage.get("cache_creation"), dict) else {}
    rate_in = float(price["in"])
    return (
        _num(usage, "input_tokens") * rate_in
        + _num(usage, "output_tokens") * float(price["out"])
        + _num(created, "ephemeral_5m_input_tokens") * rate_in * write_5m
        + _num(created, "ephemeral_1h_input_tokens") * rate_in * write_1h
        + _num(usage, "cache_read_input_tokens") * rate_in * read_mult
    )


def parse_ts(value):
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def load_jsonl(path):
    rows = []
    if not path.is_file():
        return rows
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def append_jsonl(path, rows):
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows)
    lock_path = path.parent / (path.name + ".lock")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(payload)
        os.chmod(path, 0o600)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def load_events(state):
    rows = []
    for name in ("events.1.jsonl", "events.jsonl"):
        rows.extend(load_jsonl(state / name))
    rows.sort(key=lambda row: row.get("ts") or "")
    return rows


def git_env():
    return os.environ | {"GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat", "PAGER": "cat"}


def git_out(repo, *args):
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "-c", "core.quotepath=false", *args],
            capture_output=True,
            text=True,
            timeout=30,
            env=git_env(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def git_toplevel(path):
    if path in _TOPLEVEL:
        return _TOPLEVEL[path]
    try:
        proc = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=15,
            env=git_env(),
        )
    except (OSError, subprocess.TimeoutExpired):
        _TOPLEVEL[path] = None
        return None
    if proc.returncode != 0:
        _TOPLEVEL[path] = None
        return None
    top = proc.stdout.strip()
    _TOPLEVEL[path] = top or None
    return _TOPLEVEL[path]


def discover_mains(root):
    """One transcript per session id.

    Renaming a project leaves the old directory in place, so the same session
    file can exist twice. The larger copy wins and each message is priced once.
    """
    if not root.is_dir():
        return []
    best = {}
    for proj in sorted(root.iterdir()):
        if not proj.is_dir():
            continue
        for path in sorted(proj.glob("*.jsonl")):
            if not path.is_file():
                continue
            prev = best.get(path.stem)
            if prev is None or path.stat().st_size > prev.stat().st_size:
                best[path.stem] = path
    return list(best.values())


def subagent_files(main):
    folder = main.with_suffix("") / "subagents"
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.glob("*.jsonl") if p.is_file())


def unquote(token):
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "'\"":
        return token[1:-1]
    return token


def tokenize(text):
    return TOKEN_RE.findall(text)


def is_git_token(token):
    return Path(unquote(token)).name == "git"


def split_heredocs(command):
    lines = command.splitlines(keepends=True)
    kept = []
    bodies = []
    index = 0
    while index < len(lines):
        line = lines[index]
        match = HEREDOC_RE.search(line)
        if not match:
            kept.append(line)
            index += 1
            continue
        delim = match.group(2)
        indented = "<<-" in match.group(0)
        kept.append(line)
        index += 1
        body = []
        while index < len(lines):
            current = lines[index]
            check = current.rstrip("\r\n")
            if indented:
                check = check.lstrip("\t")
            if check == delim:
                index += 1
                break
            body.append(current)
            index += 1
        bodies.append("".join(body))
    return "".join(kept), bodies


def first_subject_line(text):
    if not text:
        return None
    for raw in text.splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            return line
    return None


def commit_subject(stripped, bodies):
    if FILE_STDIN_RE.search(stripped) and bodies:
        return first_subject_line(bodies[0])
    messages = MESSAGE_RE.findall(stripped)
    if messages and not (unquote(messages[0]).startswith("$(") and bodies):
        return first_subject_line(unquote(messages[0]))
    if bodies:
        return first_subject_line(bodies[0])
    return None


def command_has_oneline(stripped):
    tokens = tokenize(stripped)
    for index, token in enumerate(tokens):
        if not is_git_token(token):
            continue
        window = [unquote(tok) for tok in tokens[index + 1:index + 14]]
        if "log" in window and "--oneline" in window:
            return True
    return False


def command_starts(tokens, index):
    cursor = index
    while cursor > 0:
        prev = unquote(tokens[cursor - 1])
        if ENV_ASSIGN_RE.match(prev):
            cursor -= 1
            continue
        return prev in SEPARATORS or prev in {"(", "{"}
    return True


def iter_commits(command):
    """Yield one dict per ``git commit`` outside heredoc bodies."""
    stripped, bodies = split_heredocs(command)
    tokens = tokenize(stripped)
    subject = commit_subject(stripped, bodies)
    oneline = command_has_oneline(stripped)
    index = 0
    while index < len(tokens):
        if not is_git_token(tokens[index]) or not command_starts(tokens, index):
            index += 1
            continue
        cursor = index + 1
        c_path = None
        found = False
        while cursor < len(tokens):
            bare = unquote(tokens[cursor])
            if bare == "commit":
                args = []
                end = cursor + 1
                while end < len(tokens) and unquote(tokens[end]) not in SEPARATORS:
                    args.append(tokens[end])
                    end += 1
                amend = any(
                    unquote(arg) == "--amend" or unquote(arg).startswith("--amend=")
                    for arg in args
                )
                cd_path = last_cd(tokens, index)
                yield {
                    "amend": amend,
                    "c_path": c_path,
                    "cd_path": cd_path,
                    "subject": subject,
                    "oneline": oneline,
                }
                index = end
                found = True
                break
            if bare == "-C" and cursor + 1 < len(tokens):
                c_path = unquote(tokens[cursor + 1])
                cursor += 2
                continue
            if bare.startswith("-C") and bare != "-C":
                c_path = bare[2:]
                cursor += 1
                continue
            if bare.startswith("-") or bare.startswith("--"):
                cursor += 1
                continue
            break
        if not found:
            index += 1


def last_cd(tokens, git_index):
    path = None
    index = 0
    while index < git_index:
        if unquote(tokens[index]) == "cd" and index + 1 < git_index:
            path = unquote(tokens[index + 1])
            index += 2
            continue
        index += 1
    return path


def resolve_dir(raw, cwd):
    if not raw:
        return cwd
    expanded = os.path.expanduser(raw)
    path = Path(expanded)
    if not path.is_absolute():
        base = Path(cwd) if cwd else Path(".")
        path = base / path
    return str(path)


def strip_ansi(text):
    return ANSI_RE.sub("", text or "")


def rev_parse(repo, sha):
    out = git_out(repo, "rev-parse", "--verify", f"{sha}^{{commit}}")
    if not out:
        return None
    full = out.strip()
    if re.fullmatch(r"[0-9a-f]{40}", full):
        return full
    return None


def find_by_subject(repo, subject, when):
    if not subject or when is None:
        return None
    start = (when - timedelta(minutes=15)).isoformat()
    end = (when + timedelta(minutes=15)).isoformat()
    # HEAD may have moved off the branch that was committed.
    out = git_out(
        repo,
        "log",
        "--all",
        f"--since={start}",
        f"--until={end}",
        "--format=%H%x09%cI%x09%s",
    )
    if out is None:
        return None
    best = None
    best_gap = None
    for line in out.splitlines():
        parts = line.split("\t", 2)
        if len(parts) < 3 or parts[2] != subject:
            continue
        committed = parse_ts(parts[1])
        if committed is None:
            continue
        gap = abs((committed - when).total_seconds())
        if gap > COMMIT_MATCH_S:
            continue
        if best is None or gap < best_gap:
            best = parts[0]
            best_gap = gap
    return best


def resolve_sha(repo, invocation, result_text, result_ts):
    text = strip_ansi(result_text)
    for match in BRACKET_SHA_RE.finditer(text):
        full = rev_parse(repo, match.group(1))
        if full:
            return full
    if invocation["oneline"]:
        match = ONELINE_SHA_RE.search(text)
        if match:
            full = rev_parse(repo, match.group(1))
            if full:
                return full
    return find_by_subject(repo, invocation["subject"], result_ts)


def numstat(repo, sha):
    out = git_out(repo, "show", "--numstat", "--format=", sha)
    if out is None:
        return None
    files = added = deleted = 0
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        plus, minus = parts[0], parts[1]
        if plus == "-" or minus == "-":
            files += 1
            continue
        if plus.isdigit() and minus.isdigit():
            files += 1
            added += int(plus)
            deleted += int(minus)
    return files, added, deleted


def tool_uses(message):
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return []
    found = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        raw = block.get("input") if isinstance(block.get("input"), (dict, str)) else {}
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                raw = {}
        if not isinstance(raw, dict):
            raw = {}
        found.append({"name": block.get("name") or "", "id": block.get("id"), "input": raw})
    return found


def flatten_text(content):
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif "content" in item:
                    parts.append(flatten_text(item["content"]))
        return "\n".join(part for part in parts if part)
    if isinstance(content, dict):
        if isinstance(content.get("text"), str):
            return content["text"]
        if "content" in content:
            return flatten_text(content["content"])
    return ""


def is_bash(tool):
    return (tool.get("name") or "").lower() in BASH_TOOLS


def genuine_user(record):
    if record.get("type") != "user" or record.get("isSidechain") or record.get("isMeta"):
        return False
    origin = record.get("origin")
    if isinstance(origin, dict) and origin.get("kind") == "task-notification":
        return False
    message = record.get("message")
    if not isinstance(message, dict):
        return False
    content = message.get("content")
    if isinstance(content, list):
        if any(isinstance(block, dict) and block.get("type") == "tool_result" for block in content):
            return False
        return bool(content)
    return isinstance(content, str) and bool(content.strip())


def result_error(text, is_error):
    if is_error:
        return True
    return bool(FATAL_RE.search(text or ""))


def marker_text(raw):
    return raw.replace("\\u00b7", DOT).replace("\\u00B7", DOT)


def empty_session(session_id):
    return {
        "id": session_id,
        "turns": [],
        "markers": [],
        "users": [],
        "results": {},
    }


def consume_file(path, prices, session, points, seen, commit_ids, unpriced):
    """Fold one jsonl file into ``session`` (None for a subagent file).

    A streamed assistant message is rewritten under one id; output tokens grow
    while input and cache counts repeat, so only the last line is priced.
    """
    latest = {}
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return
    with fh:
        for seq, raw in enumerate(fh, start=1):
            try:
                record = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            if session is not None and "job " in raw:
                session["markers"].append((seq, marker_text(raw)[:RESULT_TEXT_CAP]))
            if session is not None and genuine_user(record):
                session["users"].append(seq)
            if session is not None:
                note_tool_result(session, record, seq, commit_ids)
            message = record.get("message")
            if record.get("type") != "assistant" or not isinstance(message, dict):
                continue
            usage = message.get("usage")
            if not isinstance(usage, dict) or not usage:
                continue
            key = message.get("id") or record.get("uuid")
            if not key:
                continue
            sidechain = bool(record.get("isSidechain"))
            tools = tool_uses(message)
            if session is not None and not sidechain:
                note_commit_tools(tools, commit_ids)
            latest[key] = {
                "id": key,
                "seq": seq,
                "ts": parse_ts(record.get("timestamp")),
                "cwd": record.get("cwd"),
                "model": message.get("model"),
                "usage": usage,
                "tools": tools,
                "sidechain": sidechain,
                "output_tokens": int(_num(usage, "output_tokens")),
                "cost": message_cost(usage, message.get("model"), prices),
            }
    session_id = session["id"] if session is not None else None
    for item in latest.values():
        if item["id"] in seen:
            continue
        seen.add(item["id"])
        if is_unpriced(item["model"], prices):
            unpriced[item["model"]] += 1
        if item["ts"] is not None and item["cost"]:
            points.append((item["ts"], item["cost"]))
        if session is None or item["sidechain"]:
            continue
        session["turns"].append({
            "key": (session_id, item["id"]),
            "id": item["id"],
            "seq": item["seq"],
            "ts": item["ts"],
            "cwd": item["cwd"],
            "cost": item["cost"],
            "output_tokens": item["output_tokens"],
            "tools": item["tools"],
        })


def note_commit_tools(tools, commit_ids):
    for tool in tools:
        if not is_bash(tool) or not tool.get("id"):
            continue
        command = tool["input"].get("command")
        if isinstance(command, str) and any(True for _ in iter_commits(command)):
            commit_ids.add(tool["id"])


def note_tool_result(session, record, seq, commit_ids):
    message = record.get("message")
    if not isinstance(message, dict):
        return
    content = message.get("content")
    blocks = content if isinstance(content, list) else []
    results = [block for block in blocks if isinstance(block, dict) and block.get("type") == "tool_result"]
    if not results:
        return
    parent = record.get("toolUseResult")
    parent_dict = parent if isinstance(parent, dict) else {}
    for block in results:
        tool_id = block.get("tool_use_id")
        if not tool_id or tool_id not in commit_ids:
            continue
        text = flatten_text(block.get("content"))
        if not text and isinstance(parent, str):
            text = parent
        if not text and parent_dict:
            text = (parent_dict.get("stdout") or "") + (parent_dict.get("stderr") or "")
        is_error = bool(block.get("is_error")) or bool(parent_dict.get("interrupted"))
        code = parent_dict.get("returnCode", parent_dict.get("exitCode"))
        if isinstance(code, int) and not isinstance(code, bool) and code != 0:
            is_error = True
        session["results"][tool_id] = {
            "text": text[:TOOL_RESULT_CAP],
            "is_error": is_error,
            "seq": seq,
            "ts": parse_ts(record.get("timestamp")),
        }


def is_unpriced(model, prices):
    return bool(model) and model != "<synthetic>" and model not in (prices.get("models") or {})


def load_corpus(root, prices):
    sessions = []
    points = []
    unpriced = Counter()
    seen = set()
    for main in discover_mains(root):
        session = empty_session(main.stem)
        commit_ids = set()
        consume_file(main, prices, session, points, seen, commit_ids, unpriced)
        session["turns"].sort(key=lambda turn: turn["seq"])
        sessions.append(session)
        for sub in subagent_files(main):
            consume_file(sub, prices, None, points, seen, set(), unpriced)
    return sessions, points, unpriced


def cwd_in_repo(cwd, repo_name):
    if not cwd or not repo_name:
        return False
    expanded = os.path.expanduser(str(cwd))
    # Edit events name the git toplevel; consult events name the cwd itself.
    if repo_name in Path(expanded).parts:
        return True
    top = git_toplevel(expanded)
    return bool(top) and Path(top).name == repo_name


def round_start(event):
    end = parse_ts(event.get("ts"))
    duration = event.get("duration_s")
    if end is None or isinstance(duration, bool) or not isinstance(duration, (int, float)):
        return None
    return end - timedelta(seconds=float(duration))


def handoff_matches(turn, event, start):
    if start is None or turn["ts"] is None:
        return False
    delta = (start - turn["ts"]).total_seconds()
    if delta < 0 or delta > HANDOFF_WINDOW_S:
        return False
    repo = Path(str(event.get("repo") or "")).name
    brief = event.get("brief_chars")
    if isinstance(brief, bool) or not isinstance(brief, int) or not repo:
        return False
    for tool in turn["tools"]:
        if tool["name"] != HANDOFF_TOOL:
            continue
        task = tool["input"].get("task")
        if not isinstance(task, str) or len(task) != brief:
            continue
        given = tool["input"].get("job")
        if given and given != event.get("job"):
            continue
        cwd = tool["input"].get("cwd")
        if cwd:
            if not cwd_in_repo(cwd, repo):
                continue
        elif given != event.get("job"):
            continue
        return True
    return False


def find_handoff(sessions, event, used):
    start = round_start(event)
    best = None
    best_delta = None
    for session in sessions:
        for turn in session["turns"]:
            if turn["key"] in used or not handoff_matches(turn, event, start):
                continue
            delta = (start - turn["ts"]).total_seconds()
            if best is None or delta < best_delta:
                best = (session, turn)
                best_delta = delta
    return best


def find_marker(session, after_seq, job, rnd):
    needle = f"job {job} r{rnd} {DOT}"
    for seq, text in session["markers"]:
        if seq > after_seq and needle in text:
            return seq
    return None


def is_finish_for(turn, job_id):
    return any(
        tool["name"] == FINISH_TOOL and tool["input"].get("job") == job_id
        for tool in turn["tools"]
    )


def other_grok_call(turns, job_id):
    for turn in turns:
        for tool in turn["tools"]:
            if tool["name"] not in (HANDOFF_TOOL, FINISH_TOOL):
                continue
            if tool["input"].get("job") != job_id:
                return True
    return False


def review_window(session, result_seq, job_id, next_seq):
    chosen = []
    for turn in session["turns"]:
        if turn["seq"] <= result_seq:
            continue
        if next_seq is not None and turn["seq"] >= next_seq:
            break
        chosen.append(turn)
        if is_finish_for(turn, job_id):
            break
    return chosen


def first_turn_after(session, seq):
    for turn in session["turns"]:
        if turn["seq"] > seq:
            return turn
    return None


def side_key(row):
    return (str(row["job"]), int(row["round"]))


def join_rounds(events, sessions):
    ends = [row for row in events if row.get("event") == "job_end" and row.get("job") is not None and row.get("round") is not None]
    ends.sort(key=lambda row: parse_ts(row.get("ts")) or datetime.min.replace(tzinfo=timezone.utc))
    claimed = []
    used = set()
    for event in ends:
        found = find_handoff(sessions, event, used)
        if not found:
            continue
        session, turn = found
        used.add(turn["key"])
        claimed.append({"event": event, "session": session, "turn": turn})
    by_job = defaultdict(list)
    for item in claimed:
        by_job[item["event"]["job"]].append(item)
    for items in by_job.values():
        items.sort(key=lambda item: int(item["event"]["round"]))
    rows = []
    for item in claimed:
        event = item["event"]
        session = item["session"]
        job = event["job"]
        rnd = int(event["round"])
        result_seq = find_marker(session, item["turn"]["seq"], job, rnd)
        if result_seq is None:
            continue
        mode = event.get("mode") or "edit"
        later = [
            other for other in by_job[job]
            if int(other["event"]["round"]) > rnd and other["session"]["id"] == session["id"]
        ]
        next_seq = later[0]["turn"]["seq"] if later else None
        if mode == "consult":
            # A consult has no review window: overhead is the handoff plus the
            # single turn that reads the result.
            reader = first_turn_after(session, result_seq)
            if reader is None:
                continue
            rows.append({
                "job": job,
                "round": rnd,
                "mode": mode,
                "kind": event.get("kind"),
                "sessionId": session["id"],
                "handoff_cost": item["turn"]["cost"],
                "handoff_output_tokens": item["turn"]["output_tokens"],
                "review_turns": 0,
                "review_cost": 0.0,
                "result_cost": reader["cost"],
                "contaminated": False,
                "closed": True,
            })
            continue
        window = review_window(session, result_seq, job, next_seq)
        finished = any(is_finish_for(turn, job) for turn in window)
        rows.append({
            "job": job,
            "round": rnd,
            "mode": mode,
            "kind": event.get("kind"),
            "sessionId": session["id"],
            "handoff_cost": item["turn"]["cost"],
            "handoff_output_tokens": item["turn"]["output_tokens"],
            "review_turns": len(window),
            "review_cost": sum(turn["cost"] for turn in window),
            "result_cost": 0.0,
            "contaminated": len(window) > REVIEW_TURN_CAP or other_grok_call(window, job),
            "closed": finished or next_seq is not None,
        })
    return rows


def window_has_grok(session, start_seq, end_seq):
    for turn in session["turns"]:
        if start_seq < turn["seq"] <= end_seq and any(
            (tool.get("name") or "").startswith("mcp__grok__") for tool in turn["tools"]
        ):
            return True
    return False


def scan_baseline(sessions):
    rows = []
    unresolved = 0
    resolved = 0
    gone = 0
    for session in sessions:
        calls = []
        for turn in session["turns"]:
            for tool in turn["tools"]:
                if not is_bash(tool):
                    continue
                command = tool["input"].get("command")
                if not isinstance(command, str):
                    continue
                result = session["results"].get(tool.get("id"))
                for invocation in iter_commits(command):
                    calls.append({
                        "turn": turn,
                        "command": command,
                        "invocation": invocation,
                        "result": result,
                    })
        calls.sort(key=lambda call: call["turn"]["seq"])
        previous_seq = 0
        for call in calls:
            invocation = call["invocation"]
            if invocation["amend"]:
                continue
            result = call["result"]
            if result is None or result_error(result["text"], result["is_error"]):
                continue
            turn = call["turn"]
            user_seq = 0
            for seq in session["users"]:
                if seq < turn["seq"]:
                    user_seq = seq
                else:
                    break
            start_seq = max(user_seq, previous_seq)
            previous_seq = turn["seq"]
            if window_has_grok(session, start_seq, turn["seq"]):
                continue
            raw_dir = invocation["c_path"] or invocation["cd_path"]
            folder = resolve_dir(raw_dir, turn["cwd"])
            # A deleted repo can never be sized; it says nothing about parse health.
            if folder and not os.path.isdir(folder):
                gone += 1
                continue
            top = git_toplevel(folder) if folder else None
            when = result["ts"] or turn["ts"]
            sha = resolve_sha(top, invocation, result["text"], when) if top else None
            if not top or not sha:
                unresolved += 1
                continue
            size = numstat(top, sha)
            if size is None:
                unresolved += 1
                continue
            files, added, deleted = size
            window = [item for item in session["turns"] if start_seq < item["seq"] <= turn["seq"]]
            resolved += 1
            rows.append({
                "sessionId": session["id"],
                "sha": sha,
                "repo": Path(top).name,
                "turns": len(window),
                "cost": sum(item["cost"] for item in window),
                "files": files,
                "added": added,
                "deleted": deleted,
            })
    return rows, unresolved, resolved, gone


def base_key(row):
    return (str(row.get("sessionId")), str(row.get("sha")))


def merge_new(existing, computed, key_fn, persist_if=None):
    have = {key_fn(row) for row in existing}
    fresh = []
    for row in computed:
        key = key_fn(row)
        if key in have:
            continue
        if persist_if is not None and not persist_if(row):
            continue
        have.add(key)
        fresh.append(row)
    merged = list(existing)
    seen = {key_fn(row) for row in existing}
    for row in computed:
        key = key_fn(row)
        if key in seen:
            continue
        seen.add(key)
        merged.append(row)
    return merged, fresh


def total_tokens(event):
    usage = event.get("usage")
    if isinstance(usage, dict) and not isinstance(usage.get("total_tokens"), bool):
        value = usage.get("total_tokens")
        if isinstance(value, (int, float)):
            return float(value)
    value = event.get("total_tokens")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return 0.0


def age_fresh(event):
    age = event.get("claude_captured_age_s")
    return isinstance(age, (int, float)) and not isinstance(age, bool) and age < CLAUDE_CAPTURE_MAX_S


def claude_calibration(ends, points):
    ordered = sorted(ends, key=lambda event: parse_ts(event.get("ts")) or datetime.min.replace(tzinfo=timezone.utc))
    rates = []
    for earlier, later in zip(ordered, ordered[1:]):
        if not age_fresh(earlier) or not age_fresh(later):
            continue
        before = earlier.get("claude_7d_pct")
        after = later.get("claude_7d_pct")
        if before is None or after is None or isinstance(before, bool) or isinstance(after, bool):
            continue
        if not isinstance(before, (int, float)) or not isinstance(after, (int, float)):
            continue
        if after - before < 1:
            continue
        start = parse_ts(earlier.get("ts"))
        end = parse_ts(later.get("ts"))
        if start is None or end is None or end <= start:
            continue
        dollars = sum(cost for ts, cost in points if start < ts <= end)
        # No priced tokens between the two reads: the pair has no rate.
        if dollars <= 0:
            continue
        rates.append((after - before) / dollars)
    if len(rates) < MIN_CLAUDE_PAIRS:
        return None, len(rates)
    return median(rates), len(rates)


def grok_qualifying(event):
    if not event.get("grok_fresh_before") or not event.get("grok_fresh_after"):
        return False
    if event.get("concurrent_jobs") != 1:
        return False
    if event.get("grok_foreign_billing") != 0:
        return False
    before = event.get("grok_pct_before")
    after = event.get("grok_pct_after")
    if isinstance(before, bool) or isinstance(after, bool):
        return False
    return isinstance(before, (int, float)) and isinstance(after, (int, float))


def grok_calibration(ends):
    """Pooled % per token.

    ``creditUsagePercent`` is a whole number, so one round's own delta is too
    coarse to price that round. Qualifying rounds only estimate the rate;
    every round is then priced from its tokens.
    """
    good = [event for event in ends if grok_qualifying(event)]
    count = len(good)
    if count < MIN_GROK_ROUNDS:
        return None, None, count
    tokens = sum(total_tokens(event) for event in good)
    delta = sum(event["grok_pct_after"] - event["grok_pct_before"] for event in good)
    duration = sum(float(event.get("duration_s") or 0) for event in good)
    per_token = delta / tokens if tokens > 0 else None
    per_hour = delta / duration * 3600 if duration > 0 else None
    return per_token, per_hour, count


def median(values):
    vals = [value for value in values if value is not None]
    if not vals:
        return None
    vals.sort(key=lambda value: (math.isinf(value), value))
    mid = len(vals) // 2
    if len(vals) % 2:
        return vals[mid]
    left, right = vals[mid - 1], vals[mid]
    if math.isinf(left) or math.isinf(right):
        return math.inf
    return (left + right) / 2


def predicted_dollars(repo, changed, baselines):
    if changed is None:
        return None

    """Median cost of Claude's own commits of about this size.

    Cost per line is not a rate: it falls roughly 35x between a 10-line commit
    and a 1,000-line one, so a per-line median times the diff overstates large
    diffs several times over.
    """
    if not changed:
        return None

    def size(row):
        return (row.get("added") or 0) + (row.get("deleted") or 0)

    def in_band(rows):
        return [row for row in rows if changed / SIZE_BAND <= size(row) <= changed * SIZE_BAND]

    pool = [row for row in baselines if size(row) > 0 and (row.get("cost") or 0) > 0]
    if not pool:
        return None
    matched = in_band([row for row in pool if row.get("repo") == repo])
    if len(matched) < SAME_REPO_BASELINE:
        matched = in_band(pool)
    if len(matched) < SAME_REPO_BASELINE:
        nearest = sorted(pool, key=lambda row: abs(math.log(size(row) / changed)))
        matched = nearest[:SAME_REPO_BASELINE]
    return median([row["cost"] for row in matched])


def round_overhead(row):
    dollars = (row.get("handoff_cost") or 0) + (row.get("review_cost") or 0) + (row.get("result_cost") or 0)
    if row.get("mode") == "consult":
        turns = 2
    else:
        turns = 1 + int(row.get("review_turns") or 0)
    return dollars, turns


def job_exchange(saved, claude_pct, grok_pct, calibrations_ready):
    if not calibrations_ready or saved is None or grok_pct is None:
        return None
    if saved <= 0 or not claude_pct:
        return math.inf
    return grok_pct / claude_pct


def decide_verdict(count, merged, median_r):
    if count < VERDICT_MIN_N:
        return None
    enough = merged * 100 >= 70 * count
    low = merged * 100 < 50 * count
    # job_exchange is infinite for a job that saved Claude nothing.
    no_saving = median_r is not None and math.isinf(median_r)
    if low or no_saving:
        return "inline"
    if enough and median_r is not None:
        return "delegate"
    return "unchanged"


def break_even_text(overheads, per_turns, n_jobs, n_baseline):
    if n_baseline < BREAK_EVEN_MIN_BASELINE or n_jobs < BREAK_EVEN_MIN_JOBS:
        return "break-even: insufficient"
    if not overheads or not per_turns:
        return "break-even: insufficient"
    med_overhead = median(overheads)
    med_per_turn = median(per_turns)
    if med_overhead is None or not med_per_turn:
        return "break-even: insufficient"
    return f"break-even: {med_overhead / med_per_turn:.2f} tool calls"


def drop_line(unjoined, rounds, contaminated, joined_edit, baseline_bad, baseline_n, null_pct, foreign,
              repo_gone=0):
    return (
        f"unjoined {unjoined}/{rounds} {DOT} contaminated {contaminated}/{joined_edit} {DOT} "
        f"baseline unresolved {baseline_bad}/{baseline_n} {DOT} "
        f"null grok pct {null_pct}/{rounds} {DOT} foreign billing {foreign}/{rounds} {DOT} "
        f"repo gone {repo_gone}"
    )


def pct_null(event):
    return event.get("grok_pct_before") is None or event.get("grok_pct_after") is None


def foreign_billing(event):
    value = event.get("grok_foreign_billing")
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def fmt_money(value):
    if value is None:
        return "n/a"
    return f"${value:.4f}"


def fmt_plain(value):
    if value is None:
        return "n/a"
    if isinstance(value, float) and math.isinf(value):
        return "∞"
    if isinstance(value, float):
        return f"{value:.4g}"
    return str(value)


def fmt_ratio(part, whole):
    if not whole:
        return "n/a"
    value = 100.0 * part / whole
    if abs(value - round(value)) < 1e-9:
        return f"{round(value):.0f}%"
    return f"{value:.1f}%"


def fmt_cal(value, count):
    shown = "n/a" if value is None else f"{value:.4g}"
    return f"{shown} (n={count})"


def aggregate(events, side_rows, baselines, points, baseline_unresolved, baseline_resolved,
              baseline_gone=0):
    ends = [row for row in events if row.get("event") == "job_end"]
    finishes = {}
    for row in events:
        if row.get("event") == "job_finish" and row.get("job") is not None:
            finishes[row["job"]] = row
    by_job = defaultdict(list)
    for row in ends:
        if row.get("job") is not None:
            by_job[row["job"]].append(row)
    side = {side_key(row): row for row in side_rows}
    claude_rate, claude_n = claude_calibration(ends, points)
    grok_per_token, grok_per_hour, grok_n = grok_calibration(ends)
    ready = claude_rate is not None and grok_per_token is not None

    round_info = {}
    for event in ends:
        if event.get("job") is None or event.get("round") is None:
            continue
        key = (str(event["job"]), int(event["round"]))
        grok_pct = None if grok_per_token is None else total_tokens(event) * grok_per_token
        info = {"grok_pct": grok_pct, "tokens": total_tokens(event)}
        row = side.get(key)
        if row:
            info.update(row)
        round_info[key] = info

    edit_groups = defaultdict(list)
    consult_groups = defaultdict(list)
    for job, rounds in by_job.items():
        finish = finishes.get(job)
        if finish is None:
            continue
        kind = next((row.get("kind") for row in rounds if row.get("kind")), None) or "?"
        mode = next((row.get("mode") for row in rounds if row.get("mode")), None)
        action = finish.get("action")
        entry = {"job": job, "rounds": rounds, "finish": finish, "kind": kind, "mode": mode}
        if action == "consult" or mode == "consult":
            consult_groups[kind].append(entry)
        elif action in ("merge", "discard"):
            edit_groups[kind].append(entry)

    def priced(entry):
        action = entry["finish"].get("action")
        if action != "merge":
            return False
        for event in entry["rounds"]:
            row = side.get((str(event["job"]), int(event["round"])))
            # A contaminated or still-open window is not a measurement of the review.
            if row is None or row.get("contaminated") or row.get("closed") is False:
                return False
        return True

    edit_rows = []
    overheads = []
    for kind in sorted(edit_groups):
        jobs = edit_groups[kind]
        merged_r1 = sum(
            1 for job in jobs
            if job["finish"].get("action") == "merge" and job["finish"].get("rounds") == 1
        )
        merged_later = sum(
            1 for job in jobs
            if job["finish"].get("action") == "merge" and job["finish"].get("rounds") != 1
        )
        discarded = sum(1 for job in jobs if job["finish"].get("action") == "discard")
        sample = [job for job in jobs if priced(job)]
        oh_dollars = []
        oh_turns = []
        predicted = []
        saved = []
        saved_pct = []
        grok_pcts = []
        ratios = []
        for job in sample:
            dollars = 0.0
            turns = 0
            tokens = 0.0
            for event in job["rounds"]:
                row = side[(str(event["job"]), int(event["round"]))]
                part, part_turns = round_overhead(row)
                dollars += part
                turns += part_turns
                tokens += total_tokens(event)
            changed = (job["finish"].get("diff_added") or 0) + (job["finish"].get("diff_deleted") or 0)
            repo = Path(str(job["rounds"][0].get("repo") or "")).name
            pred = predicted_dollars(repo, changed, baselines)
            save = None if pred is None else pred - dollars
            claude_pct = None if save is None or claude_rate is None else save * claude_rate
            grok_pct = None if grok_per_token is None else tokens * grok_per_token
            oh_dollars.append(dollars)
            oh_turns.append(turns)
            overheads.append(dollars)
            if pred is not None:
                predicted.append(pred)
            if save is not None:
                saved.append(save)
            if claude_pct is not None:
                saved_pct.append(claude_pct)
            if grok_pct is not None:
                grok_pcts.append(grok_pct)
            ratio = job_exchange(save, claude_pct, grok_pct, ready)
            if ratio is not None:
                ratios.append(ratio)
        count = len(jobs)
        merged = merged_r1 + merged_later
        edit_rows.append({
            "kind": kind,
            "n": count,
            "merged_r1": merged_r1,
            "merged_later": merged_later,
            "discarded": discarded,
            "overhead_turns": median(oh_turns),
            "overhead": median(oh_dollars),
            "predicted": median(predicted),
            "saved": median(saved),
            "saved_pct": median(saved_pct),
            "grok_pct": median(grok_pcts),
            "r": median(ratios),
            "verdict": decide_verdict(count, merged, median(ratios)),
        })

    consult_rows = []
    for kind in sorted(consult_groups):
        jobs = consult_groups[kind]
        claude_dollars = []
        grok_pcts = []
        for job in jobs:
            tokens = sum(total_tokens(event) for event in job["rounds"])
            if grok_per_token is not None:
                grok_pcts.append(tokens * grok_per_token)
            complete = True
            dollars = 0.0
            for event in job["rounds"]:
                row = side.get((str(event["job"]), int(event["round"])))
                if row is None:
                    complete = False
                    break
                part, _turns = round_overhead(row)
                dollars += part
            if complete:
                claude_dollars.append(dollars)
        consult_rows.append({
            "kind": kind,
            "n": len(jobs),
            "claude": median(claude_dollars),
            "grok_pct": median(grok_pcts),
        })

    well = [
        event for event in ends
        if event.get("job") is not None and event.get("round") is not None
    ]
    joined_edit = 0
    contaminated = 0
    for event in well:
        if (event.get("mode") or "edit") == "consult":
            continue
        row = side.get((str(event["job"]), int(event["round"])))
        if row is None:
            continue
        joined_edit += 1
        if row.get("contaminated"):
            contaminated += 1
    unjoined = sum(1 for event in well if (str(event["job"]), int(event["round"])) not in side)
    nulls = sum(1 for event in well if pct_null(event))
    foreign = sum(1 for event in well if foreign_billing(event))
    baseline_n = baseline_resolved + baseline_unresolved
    per_turns = [
        row["cost"] / row["turns"]
        for row in baselines
        if row.get("turns")
    ]
    finished_edit = sum(len(jobs) for jobs in edit_groups.values())
    text = drop_line(
        unjoined, len(well), contaminated, joined_edit,
        baseline_unresolved, baseline_n, nulls, foreign, baseline_gone,
    )
    return {
        "edit": edit_rows,
        "consult": consult_rows,
        "calibrations": {
            "claude_per_dollar": {"value": claude_rate, "n": claude_n},
            "grok_per_mtok": {
                "value": None if grok_per_token is None else grok_per_token * 1_000_000,
                "n": grok_n,
            },
            "grok_per_hour": {"value": grok_per_hour, "n": grok_n},
        },
        "break_even": break_even_text(overheads, per_turns, finished_edit, len(baselines)),
        "drop": text,
        "rounds": round_info,
        "baseline": baselines,
    }


def unpriced_line(unpriced):
    """Turns of a model with no price row cost $0, which silently shrinks both overhead and baseline."""
    if not unpriced:
        return None
    models = ", ".join(f"{model} x{count}" for model, count in sorted(unpriced.items()))
    return f"UNPRICED {models}: add to prices.json and drop the $0 rows from claude_side/baseline before reading verdicts"


def render_lines(report, verdict_only):
    lines = render_body(report, verdict_only)
    if report.get("unpriced"):
        lines.append(report["unpriced"])
    return lines


def render_body(report, verdict_only):
    if verdict_only:
        lines = [
            f"{row['kind']} {row['verdict']}"
            for row in report["edit"]
            if row["n"] >= VERDICT_MIN_N and row["verdict"]
        ]
        lines.append(report["break_even"])
        lines.append(report["drop"])
        return lines
    lines = [
        "kind  n  merged_r1  merged_later  discarded  overhead_turns  "
        "overhead$  predicted$  saved$  saved%  grok%  R  verdict"
    ]
    if not report["edit"] and not report["consult"]:
        lines.append("(no finished jobs)")
    for row in report["edit"]:
        verdict = row["verdict"] or "—"
        lines.append(
            f"{row['kind']}  {row['n']}  {fmt_ratio(row['merged_r1'], row['n'])}  "
            f"{fmt_ratio(row['merged_later'], row['n'])}  {fmt_ratio(row['discarded'], row['n'])}  "
            f"{fmt_plain(row['overhead_turns'])}  {fmt_money(row['overhead'])}  "
            f"{fmt_money(row['predicted'])}  {fmt_money(row['saved'])}  {fmt_plain(row['saved_pct'])}  "
            f"{fmt_plain(row['grok_pct'])}  {fmt_plain(row['r'])}  {verdict}"
        )
    for row in report["consult"]:
        lines.append(
            f"consult  {row['kind']}  n={row['n']}  claude$ {fmt_money(row['claude'])}  "
            f"grok% {fmt_plain(row['grok_pct'])}"
        )
    cal = report["calibrations"]
    lines.append(f"claude % per $: {fmt_cal(cal['claude_per_dollar']['value'], cal['claude_per_dollar']['n'])}")
    lines.append(f"grok % per Mtok: {fmt_cal(cal['grok_per_mtok']['value'], cal['grok_per_mtok']['n'])}")
    lines.append(f"grok % per job-hour: {fmt_cal(cal['grok_per_hour']['value'], cal['grok_per_hour']['n'])}")
    lines.append(report["break_even"])
    lines.append(report["drop"])
    return lines


def build(state, transcripts, prices_path):
    prices = load_prices(prices_path)
    events = load_events(state)
    sessions, points, unpriced = load_corpus(transcripts, prices)
    computed_side = join_rounds(events, sessions)
    computed_base, unresolved, resolved, gone = scan_baseline(sessions)
    existing_side = load_jsonl(state / "claude_side.jsonl")
    existing_base = load_jsonl(state / "baseline.jsonl")
    side_rows, new_side = merge_new(
        existing_side,
        computed_side,
        side_key,
        persist_if=lambda row: row.get("closed") is not False,
    )
    base_rows, new_base = merge_new(existing_base, computed_base, base_key)
    # An open review is still growing; writing it would freeze a partial cost.
    append_jsonl(state / "claude_side.jsonl", new_side)
    append_jsonl(state / "baseline.jsonl", new_base)
    report = aggregate(events, side_rows, base_rows, points, unresolved, resolved, gone)
    report["unpriced"] = unpriced_line(unpriced)
    report["lines"] = render_lines(report, verdict_only=False)
    return report


def main(argv=None):
    args = parse_args(argv)
    state, transcripts, prices = resolve_paths(args)
    report = build(state, transcripts, prices)
    for line in render_lines(report, verdict_only=args.verdict):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
