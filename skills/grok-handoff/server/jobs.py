"""Job lifecycle: preconditions, worktree, spawn, wait, parse, finish."""

import fcntl
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import quote

from server import doctor, events, quota, rules

POLL_S = 2
PROGRESS_S = 3
PROGRESS_MAX_CHARS = 80
SESSION_CLOCK_SLACK_S = 5
GROK_SESSIONS = Path.home() / ".grok" / "sessions"
KILL_GRACE_S = 10
EDIT_BUDGET = 2500
CONSULT_BUDGET = 6000
PRUNE_DAYS = 14
MINUS = "−"
DOT = "·"
KINDS = ("implement", "fix", "refactor", "tests", "build", "research", "review", "other")
MODES = ("edit", "consult")
EFFORTS = ("low", "medium", "high", "xhigh")
REPORT_KEYS = ("STATUS", "SUMMARY", "VERIFIED", "ASSUMPTIONS", "OPEN")
USAGE_KEYS = (
    "input_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "total_tokens",
)
CREDIT_RE = re.compile(r"credit|quota|billing|usage limit", re.I)
KEY_RE = re.compile(r"^(STATUS|SUMMARY|VERIFIED|ASSUMPTIONS|OPEN):\s?(.*)$")
BILLING_MSG = "billing: fetched credits config"
ERROR_STATUSES = ("error", "grok_out", "cancelled")

_gate = threading.Lock()
_grok_version = None
_version_lock = threading.Lock()


def _git(repo, *args, input_text=None):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        input=input_text,
        text=True,
        capture_output=True,
        env=os.environ | {"GIT_TERMINAL_PROMPT": "0"},
    )


def _job_dir(jid):
    if not isinstance(jid, str) or not re.fullmatch(r"[A-Za-z0-9._-]+", jid):
        return None
    root = (events.state_root() / "jobs").resolve()
    path = (root / jid).resolve()
    if path.parent != root:
        return None
    return path


def _read_meta(job_dir):
    try:
        return json.loads((Path(job_dir) / "meta.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_meta(job_dir, meta):
    events.write_json_atomic(Path(job_dir) / "meta.json", meta)


def _running_count():
    root = events.state_root() / "jobs"
    count = 0
    if not root.exists():
        return 0
    for entry in root.iterdir():
        meta = _read_meta(entry)
        if meta and meta.get("status") == "running":
            count += 1
    return count


def _under_private(path):
    resolved = Path(path).resolve()
    for raw in rules.PRIVATE_PATHS:
        priv = Path(os.path.expanduser(raw)).resolve()
        if resolved == priv:
            return True
        try:
            resolved.relative_to(priv)
        except ValueError:
            continue
        else:
            return True
    return False


def _resolve_cwd(cwd):
    if not isinstance(cwd, str) or not cwd:
        return None, "cwd is required"
    path = Path(os.path.expanduser(cwd))
    if not path.is_absolute():
        return None, "cwd must be absolute"
    if not path.is_dir():
        return None, "cwd does not exist"
    return path, None


def _alloc_id():
    root = events.state_root() / "jobs"
    for _ in range(32):
        jid = "g" + secrets.token_hex(3)
        if not (root / jid).exists():
            return jid
    raise RuntimeError("could not allocate a job id")


def grok_version(bin_path):
    global _grok_version
    with _version_lock:
        if _grok_version is None:
            proc = subprocess.run([bin_path, "--version"], capture_output=True, text=True)
            _grok_version = (proc.stdout or proc.stderr).strip() or "unknown"
        return _grok_version


def _ensure_exclude(repo):
    proc = _git(repo, "rev-parse", "--git-common-dir")
    if proc.returncode != 0:
        return
    common = Path(proc.stdout.strip())
    if not common.is_absolute():
        common = (Path(repo) / common).resolve()
    info = common / "info"
    info.mkdir(parents=True, exist_ok=True)
    exclude = info / "exclude"
    marker = "/.grok-jobs/"
    text = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    if marker in text.splitlines():
        return
    with exclude.open("a", encoding="utf-8") as fh:
        if text and not text.endswith("\n"):
            fh.write("\n")
        fh.write(marker + "\n")


def _billing_offset():
    path = Path(os.environ.get("GROK_HOME") or "~/.grok").expanduser() / "logs" / "unified.jsonl"
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _prepare(arguments, progress_token):
    """Preconditions, in order. Returns (meta, None) or (None, (text, True))."""
    task = arguments.get("task")
    if not isinstance(task, str) or task == "":
        return None, ("task is required", True)
    job_arg = arguments.get("job")
    mode = arguments.get("mode", "edit")
    kind = arguments.get("kind")
    effort = arguments.get("effort", "high")
    model = arguments.get("model")
    max_turns = arguments.get("max_turns") if "max_turns" in arguments else None
    cwd_arg = arguments.get("cwd")
    existing = None
    if job_arg:
        job_dir = _job_dir(job_arg)
        existing = _read_meta(job_dir) if job_dir else None
        if existing is None:
            return None, ("no such job", True)
        mode = existing.get("mode")
        kind = existing.get("kind")
        cwd_arg = existing.get("cwd") or existing.get("workdir")
        if "effort" not in arguments:
            effort = existing.get("effort") or "high"
        if "model" not in arguments:
            model = existing.get("model")
    if mode not in MODES:
        return None, ("mode must be edit or consult", True)
    if kind not in KINDS:
        return None, ("kind is required" if not job_arg else "job has no kind", True)
    if effort not in EFFORTS:
        return None, ("effort must be low, medium, high, or xhigh", True)
    if model is not None and not isinstance(model, str):
        return None, ("model must be a string", True)
    if model == "":
        model = None
    if max_turns is not None and (isinstance(max_turns, bool) or not isinstance(max_turns, int)):
        return None, ("max_turns must be an integer", True)

    cwd, err = _resolve_cwd(cwd_arg)
    if err and not job_arg:
        return None, (err, True)
    if err:
        return None, (err, True)
    if mode == "edit":
        top = _git(cwd, "rev-parse", "--show-toplevel")
        if top.returncode != 0:
            return None, ("cwd is not inside a git work tree", True)
        checked = Path(top.stdout.strip())
    else:
        checked = cwd
    if _under_private(checked):
        return None, ("cwd is under a private path", True)

    reading = quota.grok_read(True)
    claude = quota.claude_read()
    route, reason = quota.route(reading, claude)
    if route == "claude-only":
        return None, (f"grok unavailable: {reason}", True)
    allowed = quota.max_concurrent(reading)
    if _running_count() >= allowed:
        return None, (f"already {allowed} jobs running, the cap at grok "
                      f"{quota._num(reading['pct'])}% used", True)
    grok = shutil.which("grok")
    if not grok:
        return None, ("grok is not on PATH", True)

    if job_arg:
        if not existing.get("session"):
            return None, ("job has no session", True)
        if existing.get("status") == "running":
            return None, ("job is running", True)
        if existing.get("status") == "finished":
            return None, ("job is finished", True)
        jid = existing["id"]
        job_dir = _job_dir(jid)
        rnd = int(existing.get("round") or 1) + 1
        repo_root = existing.get("repo_root")
        workdir = existing.get("workdir")
        branch = existing.get("branch")
        base_sha = existing.get("base_sha")
        base_dirty = existing.get("base_dirty") or 0
        session = existing.get("session")
    else:
        jid = _alloc_id()
        job_dir = events.state_root() / "jobs" / jid
        job_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(job_dir, 0o700)
        rnd = 1
        session = None
        branch = None
        base_sha = None
        base_dirty = 0
        repo_root = str(checked)
        workdir = str(cwd)
        if mode == "edit":
            repo = Path(repo_root)
            _ensure_exclude(repo)
            wt = repo / ".grok-jobs" / jid
            wt.parent.mkdir(parents=True, exist_ok=True)
            branch = f"grok/{jid}"
            added = _git(repo, "worktree", "add", "-b", branch, str(wt), "HEAD")
            if added.returncode != 0:
                shutil.rmtree(job_dir, ignore_errors=True)
                return None, (f"worktree add failed: {(added.stderr or added.stdout).strip()}", True)
            base_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
            porcelain = _git(repo, "status", "--porcelain")
            base_dirty = len([ln for ln in porcelain.stdout.splitlines() if ln.strip()])
            rel = cwd.resolve().relative_to(repo.resolve())
            workdir = str((wt / rel).resolve())
            if not Path(workdir).is_dir():
                _remove_worktree({"repo_root": str(repo), "id": jid, "branch": branch})
                shutil.rmtree(job_dir, ignore_errors=True)
                return None, (f"cwd {rel} is not in HEAD, so the worktree has no copy of it", True)
            repo_root = str(repo.resolve())

    brief = job_dir / f"brief-r{rnd}.md"
    events.write_private(brief, task)
    (job_dir / "cancel").unlink(missing_ok=True)

    sandbox = "claude-edit" if mode == "edit" else "claude-consult"
    argv = [
        grok, "--prompt-file", str(brief), "--output-format", "json", "--cwd", str(workdir),
        "--sandbox", sandbox, "--always-approve", "--trust", "--effort", effort,
        "--rules", rules.text(mode), "--deny", "Bash(git push*)",
    ]
    if model:
        argv += ["--model", model]
    if max_turns is not None:
        argv += ["--max-turns", str(max_turns)]
    if session:
        argv += ["--resume", session]

    out_path = job_dir / f"out-r{rnd}.json"
    err_path = job_dir / f"err-r{rnd}.log"
    out_fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    err_fd = os.open(err_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(workdir),
            stdin=subprocess.DEVNULL,
            stdout=out_fd,
            stderr=err_fd,
            start_new_session=True,
            env=os.environ | {"GROK_CLAUDE_MCPS_ENABLED": "false", "GROK_CLAUDE_AGENTS_ENABLED": "false"},
        )
        # Hold the Popen until waitpid. Dropping it puts the child in
        # subprocess._active, and the next Popen reaps it out from under us.
        _retain_child(proc)
    finally:
        os.close(out_fd)
        os.close(err_fd)
        os.chmod(out_path, 0o600)
        os.chmod(err_path, 0o600)
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        pgid = proc.pid
    version = grok_version(grok)
    meta = {
        "id": jid,
        "round": rnd,
        "pid": proc.pid,
        "pgid": pgid,
        "started_at": time.time(),
        "mode": mode,
        "kind": kind,
        "effort": effort,
        "model": model,
        "cwd": str(cwd),
        "repo_root": repo_root,
        "workdir": str(workdir),
        "branch": branch,
        "base_sha": base_sha,
        "base_dirty": base_dirty,
        "session": session,
        "status": "running",
        "grok_version": version,
        "brief_chars": len(task),
        "progress_token": bool(progress_token),
        "grok_pct_before": _pct_of(reading),
        "grok_period_end": reading.get("period_end") if isinstance(reading, dict) else None,
        "grok_fresh_before": _fresh_of(reading),
        "billing_offset": _billing_offset(),
        "concurrent_jobs": _running_count() + 1,
        "max_turns": max_turns,
    }
    _write_meta(job_dir, meta)
    return meta, None


def _pct_of(reading):
    if not isinstance(reading, dict):
        return None
    pct = reading.get("pct")
    if isinstance(pct, bool) or not isinstance(pct, (int, float)):
        return None
    return pct


def _fresh_of(reading):
    return bool(isinstance(reading, dict) and reading.get("fresh"))


def _exit_code(status):
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    if os.WIFSIGNALED(status):
        return 128 + os.WTERMSIG(status)
    return None


_children = {}


def _retain_child(proc):
    _children[proc.pid] = proc


def _release_child(pid, status):
    proc = _children.pop(pid, None)
    if proc is None:
        return
    if isinstance(status, int) and os.WIFSIGNALED(status):
        proc.returncode = -os.WTERMSIG(status)
    elif isinstance(status, int) and os.WIFEXITED(status):
        proc.returncode = os.WEXITSTATUS(status)
    elif proc.returncode is None:
        proc.returncode = 0


def _own_poll(pid):
    try:
        wpid, status = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        _release_child(pid, None)
        return "foreign", None
    if wpid == 0:
        return "running", None
    _release_child(pid, status)
    return "exited", _exit_code(status)


def _cmdline_has_grok(pid):
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False
    return b"grok" in raw


def _proc_tree(root_pid):
    children = {}
    proc = Path("/proc")
    try:
        entries = list(proc.iterdir())
    except OSError:
        return {root_pid}
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            text = (entry / "stat").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rpar = text.rfind(")")
        if rpar < 0:
            continue
        try:
            pid = int(text.split(" ", 1)[0])
            ppid = int(text[rpar + 2:].split()[1])
        except (ValueError, IndexError):
            continue
        children.setdefault(ppid, []).append(pid)
    seen = set()
    stack = [root_pid]
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        stack.extend(children.get(pid, ()))
    seen.add(root_pid)
    return seen


def _record_pids(job_dir, pids):
    path = Path(job_dir) / "pids"
    existing = set()
    try:
        existing = {int(part) for part in path.read_text(encoding="utf-8").split() if part}
    except (OSError, ValueError):
        existing = set()
    existing |= set(pids)
    events.write_private(path, " ".join(str(pid) for pid in sorted(existing)))


def _load_pids(job_dir, pid):
    pids = {pid} if pid else set()
    try:
        for part in (Path(job_dir) / "pids").read_text(encoding="utf-8").split():
            pids.add(int(part))
    except (OSError, ValueError):
        pass
    return pids


def _foreign_billing(offset, tree, started_at=None):
    path = Path(os.environ.get("GROK_HOME") or "~/.grok").expanduser() / "logs" / "unified.jsonl"
    try:
        size = path.stat().st_size
    except OSError:
        return 0
    start = offset if isinstance(offset, int) and 0 <= offset <= size else 0
    try:
        with path.open("rb") as fh:
            fh.seek(start)
            chunk = fh.read()
    except OSError:
        return 0
    tree = set(tree) | quota.own_billing_pids()
    since = doctor.utc_second(started_at) if started_at else ""
    count = 0
    for raw in chunk.splitlines():
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        if rec.get("msg") != BILLING_MSG or not doctor.logged_since(rec, since):
            continue
        try:
            pid = int(rec.get("pid"))
        except (TypeError, ValueError):
            pid = None
        if pid not in tree:
            count += 1
    return count


def _poll_until_dead(pid, timeout):
    """Reap our own child as soon as it dies. A zombie still has a /proc entry."""
    deadline = time.monotonic() + timeout
    foreign = False
    while True:
        if not foreign:
            state, code = _own_poll(pid)
            if state == "exited":
                return code
            if state == "foreign":
                foreign = True
                continue
        elif not _cmdline_has_grok(pid):
            return None
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.2)


def _kill_group(pgid, pid):
    if not isinstance(pgid, int) or pgid <= 0:
        return None
    # A re-attached job's pid may have been reused since it ran; never signal a stranger.
    if pid not in _children and not _cmdline_has_grok(pid):
        return None
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return _reap(pid)
    code = _poll_until_dead(pid, KILL_GRACE_S)
    if code is not None or not _cmdline_has_grok(pid):
        return code
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    return _poll_until_dead(pid, 2)


def _reap(pid):
    try:
        wpid, status = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        _release_child(pid, None)
        return None
    if wpid == 0:
        return None
    _release_child(pid, status)
    return _exit_code(status)


def _parse_report(text):
    lines = text.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.startswith("STATUS:"):
            start = index
    if start is None:
        return {}
    fields = {}
    current = None
    buf = []

    def flush():
        nonlocal current, buf
        if current is not None:
            fields[current] = "\n".join(buf).strip()
        current = None
        buf = []

    for line in lines[start:]:
        match = KEY_RE.match(line)
        if match:
            flush()
            current = match.group(1)
            buf = [match.group(2)]
        elif current is not None:
            buf.append(line)
    flush()
    return fields


def _status_from_text(text):
    found = None
    for line in text.splitlines():
        match = re.match(r"^STATUS:\s*(\S+)", line)
        if match and match.group(1) in ("done", "partial", "blocked"):
            found = match.group(1)
    return found or "unreported"


def _parse_output(path):
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError:
        raw = ""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = None
    if not isinstance(data, dict):
        events.log("contract_error", what="not JSON")
        return {"status": "error", "reason": "grok CLI contract: not JSON",
                "report": {}, "data": {}, "text": "", "session": None}
    if data.get("type") == "error":
        message = str(data.get("message") or "")
        status = "grok_out" if CREDIT_RE.search(message) else "error"
        return {"status": status, "reason": message, "report": {}, "data": data,
                "text": "", "session": None}
    missing = "text" if "text" not in data else "sessionId" if "sessionId" not in data else None
    if missing:
        events.log("contract_error", what=f"missing {missing}")
        return {"status": "error", "reason": f"grok CLI contract: missing {missing}",
                "report": {}, "data": data, "text": str(data.get("text") or ""), "session": None}
    text = data.get("text") or ""
    if not isinstance(text, str):
        text = str(text)
    return {
        "status": _status_from_text(text),
        "reason": "",
        "report": _parse_report(text),
        "data": data,
        "text": text,
        "session": data.get("sessionId"),
    }


def _usage(data):
    raw = data.get("usage") if isinstance(data, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    return {key: raw.get(key) for key in USAGE_KEYS}


def _model_calls(data):
    usage = data.get("modelUsage") if isinstance(data, dict) else None
    if not isinstance(usage, dict):
        return 0
    total = 0
    for info in usage.values():
        if isinstance(info, dict) and isinstance(info.get("modelCalls"), int):
            total += info["modelCalls"]
    return total


def _model_name(meta, data):
    if meta.get("model"):
        return meta["model"]
    usage = data.get("modelUsage") if isinstance(data, dict) else None
    if isinstance(usage, dict) and usage:
        return next(iter(usage))
    return "grok"


def _fmt_dur(seconds):
    total = max(0, int(round(seconds)))
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def _fmt_tok(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "?"
    number = int(value)
    if abs(number) >= 1000:
        return f"{round(number / 1000)}k"
    return str(number)


def _fmt_pct(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "?"
    if float(value).is_integer():
        return str(int(value))
    return str(value)


def _header(meta, status, data, before, after):
    usage = _usage(data)
    dur = time.time() - (meta.get("started_at") or time.time())
    return (
        f"job {meta['id']} r{meta['round']} {DOT} {status} {DOT} {_fmt_dur(dur)} {DOT} "
        f"{_model_name(meta, data)}/{meta.get('effort') or 'high'} {DOT} "
        f"{_fmt_tok(usage.get('total_tokens'))} tok ({_fmt_tok(usage.get('output_tokens'))} out) {DOT} "
        f"grok {_fmt_pct(before)}→{_fmt_pct(after)}%"
    )


def _numstat(text):
    files = []
    added = deleted = 0
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        add, delete, path = parts[0], parts[1], "\t".join(parts[2:])
        add_n = int(add) if add.isdigit() else 0
        del_n = int(delete) if delete.isdigit() else 0
        added += add_n
        deleted += del_n
        files.append((path, add_n, del_n))
    return files, added, deleted


def _commit_and_diff(meta, report):
    repo = Path(meta["repo_root"])
    workdir = Path(meta["workdir"])
    jid = meta["id"]
    rnd = meta["round"]
    branch = meta.get("branch") or f"grok/{jid}"
    add = _git(workdir, "add", "-A")
    if add.returncode != 0:
        return None, (add.stderr or add.stdout or "git add failed").strip()
    quiet = _git(workdir, "diff", "--cached", "--quiet")
    if quiet.returncode != 0:
        summary = (report.get("SUMMARY") or "update").splitlines()
        line = (summary[0] if summary else "update")[:72]
        commit = _git(
            workdir, "commit", "-q",
            "-m", f"grok {jid} r{rnd}: {line}",
            "-m", f"Grok-Job: {jid}",
        )
        if commit.returncode != 0:
            return None, (commit.stderr or commit.stdout or "git commit failed").strip()
    diff = _git(repo, "diff", "--numstat", meta.get("base_sha") or "HEAD", branch)
    full = _git(repo, "diff", meta.get("base_sha") or "HEAD", branch)
    patch = Path(_job_dir(jid)) / "diff.patch"
    events.write_private(patch, full.stdout)
    files, added, deleted = _numstat(diff.stdout)
    return (files, added, deleted, patch), None


def _compose_edit(header, branch_line, diff_line, files, extra, report, summary):
    lines = [header, branch_line, diff_line]
    for path, add, delete in files:
        lines.append(f"  {path} +{add} {MINUS}{delete}")
    if extra > 0:
        lines.append(f"  +{extra} more")
    if "VERIFIED" in report:
        lines.append(f"VERIFIED: {report.get('VERIFIED') or ''}")
    if summary is not None:
        lines.append(f"SUMMARY: {summary}")
    assumptions = report.get("ASSUMPTIONS")
    if "ASSUMPTIONS" in report and (assumptions or "").strip() != "none":
        lines.append(f"ASSUMPTIONS: {assumptions}")
    opened = report.get("OPEN")
    if "OPEN" in report and (opened or "").strip() != "none":
        lines.append(f"OPEN: {opened}")
    return "\n".join(lines)


def _render_edit(meta, report, data, before, after, diff_info):
    files, added, deleted, patch = diff_info
    header = _header(meta, report.get("_status") or "unreported", data, before, after)
    repo = Path(meta["repo_root"])
    try:
        loc = Path(meta["workdir"]).resolve().relative_to(repo.resolve()).as_posix()
    except ValueError:
        loc = meta.get("workdir") or ""
    short = (meta.get("base_sha") or "")[:7] or "?"
    branch = meta.get("branch") or f"grok/{meta['id']}"
    branch_line = f"branch {branch} at {loc} {DOT} base {short}"
    dirty = meta.get("base_dirty") or 0
    if dirty:
        noun = "file" if dirty == 1 else "files"
        branch_line += f" {DOT} {dirty} uncommitted base {noun} not included"
    noun = "file" if len(files) == 1 else "files"
    diff_line = f"diff {len(files)} {noun} +{added} {MINUS}{deleted} {DOT} {patch}"
    summary = report.get("SUMMARY") if "SUMMARY" in report else None
    summary_text = summary or ""
    limit = min(12, len(files))
    for _ in range(10000):
        shown = files[:limit]
        extra = len(files) - limit
        text = _compose_edit(header, branch_line, diff_line, shown, extra, report,
                             summary_text if summary is not None else None)
        if len(text) <= EDIT_BUDGET:
            return text
        if summary is not None and summary_text:
            overflow = len(text) - EDIT_BUDGET
            new_len = len(summary_text) - overflow - 1
            if new_len <= 0:
                summary_text = ""
            else:
                summary_text = summary_text[:new_len].rstrip() + "…"
            continue
        if limit > 0:
            limit -= 1
            continue
        return text[:EDIT_BUDGET]
    return text[:EDIT_BUDGET]


def _render_consult(meta, text, data, before, after, answer_path):
    header = _header(meta, meta.get("_round_status") or "unreported", data, before, after)
    full = header + "\n" + text
    if len(full) <= CONSULT_BUDGET:
        return full
    suffix = "\n" + str(answer_path)
    budget = CONSULT_BUDGET - len(suffix)
    if budget < 1:
        return str(answer_path)[:CONSULT_BUDGET]
    return full[:budget].rstrip() + suffix


def _render_error(meta, status, reason, err_path, data, before, after):
    header = _header(meta, status, data, before, after)
    return f"{header}\n{reason}\n{err_path}"


def _remove_worktree(meta):
    repo = meta.get("repo_root")
    jid = meta.get("id")
    if not repo or not jid or not Path(repo).exists():
        return
    wt = Path(repo) / ".grok-jobs" / jid
    _git(repo, "worktree", "remove", "--force", str(wt))
    branch = meta.get("branch") or f"grok/{jid}"
    _git(repo, "branch", "-D", branch)


def _finalize(meta, exit_code, reattached):
    jid = meta["id"]
    job_dir = _job_dir(jid)
    rnd = meta["round"]
    out_path = job_dir / f"out-r{rnd}.json"
    err_path = job_dir / f"err-r{rnd}.log"
    cancelled = (job_dir / "cancel").exists()
    if cancelled:
        parsed = {"status": "cancelled", "reason": "cancelled", "report": {},
                  "data": {}, "text": "", "session": None}
        try:
            loaded = json.loads(out_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                parsed["data"] = loaded
        except (OSError, json.JSONDecodeError):
            pass
    else:
        parsed = _parse_output(out_path)
    round_status = parsed["status"]
    if round_status == "grok_out":
        quota.note_grok_out(meta.get("grok_period_end"))
    after = quota.grok_read(True)
    claude = quota.claude_read()
    before = meta.get("grok_pct_before")
    after_pct = _pct_of(after)
    data = parsed["data"]
    diff_files = diff_added = diff_deleted = 0
    commit_error = None
    diff_info = ([], 0, 0, job_dir / "diff.patch")
    if meta.get("mode") == "edit" and round_status not in ERROR_STATUSES:
        diff_info, commit_error = _commit_and_diff(meta, parsed["report"])
        if commit_error:
            round_status = "error"
            parsed["reason"] = commit_error
            diff_info = ([], 0, 0, job_dir / "diff.patch")
        else:
            diff_files = len(diff_info[0])
            diff_added = diff_info[1]
            diff_deleted = diff_info[2]
    report = dict(parsed["report"])
    report["_status"] = round_status
    if round_status in ERROR_STATUSES or commit_error:
        text = _render_error(meta, round_status, parsed["reason"] or round_status, err_path,
                             data, before, after_pct)
        # A consult has nothing to merge; leaving it unfinished would list it forever.
        meta["status"] = "finished" if meta.get("mode") == "consult" else round_status
        is_error = True
    elif meta.get("mode") == "consult":
        answer = job_dir / "answer.md"
        events.write_private(answer, parsed["text"])
        meta["_round_status"] = round_status
        text = _render_consult(meta, parsed["text"], data, before, after_pct, answer)
        meta["status"] = "finished"
        is_error = False
    else:
        text = _render_edit(meta, report, data, before, after_pct, diff_info)
        meta["status"] = round_status
        is_error = False
    if len(text) > EDIT_BUDGET and meta.get("mode") == "edit" and not is_error:
        text = text[:EDIT_BUDGET]
    summary = (parsed["report"].get("SUMMARY") or "").splitlines()
    meta["summary"] = summary[0] if summary else ""
    meta["result"] = text
    meta["is_error"] = is_error
    if parsed.get("session"):
        meta["session"] = parsed["session"]
    meta["diff_files"] = diff_files
    meta["diff_added"] = diff_added
    meta["diff_deleted"] = diff_deleted
    meta.pop("_round_status", None)
    _write_meta(job_dir, meta)
    tree = _load_pids(job_dir, meta.get("pid"))
    cost = data.get("total_cost_usd") if isinstance(data, dict) else None
    if isinstance(data, dict) and "total_cost_usd" not in data:
        cost = None
    events.log(
        "job_end",
        job=jid,
        round=rnd,
        mode=meta.get("mode"),
        kind=meta.get("kind"),
        effort=meta.get("effort"),
        model=meta.get("model"),
        repo=Path(meta.get("repo_root") or "").name,
        status=round_status,
        exit_code=None if reattached else exit_code,
        duration_s=round(time.time() - (meta.get("started_at") or time.time()), 3),
        brief_chars=meta.get("brief_chars"),
        result_chars=len(text),
        grok_version=meta.get("grok_version"),
        progress_token=bool(meta.get("progress_token")),
        usage=_usage(data),
        num_turns=data.get("num_turns") if isinstance(data, dict) else None,
        model_calls=_model_calls(data),
        cost_usd=cost,
        grok_pct_before=before,
        grok_pct_after=after_pct,
        grok_fresh_before=bool(meta.get("grok_fresh_before")),
        grok_fresh_after=_fresh_of(after),
        grok_foreign_billing=_foreign_billing(meta.get("billing_offset") or 0, tree,
                                              meta.get("started_at")),
        concurrent_jobs=meta.get("concurrent_jobs"),
        claude_5h_pct=claude.get("five_pct"),
        claude_7d_pct=claude.get("week_pct"),
        claude_captured_age_s=claude.get("captured_age_s"),
        diff_files=diff_files,
        diff_added=diff_added,
        diff_deleted=diff_deleted,
        base_dirty=meta.get("base_dirty") or 0,
        report_fields=[key for key in REPORT_KEYS if key in parsed["report"]],
    )
    if meta.get("mode") == "consult" and meta.get("status") == "finished":
        _log_finish(meta, "consult", None, False)
    return text, is_error


def _log_finish(meta, action, merged_sha, conflict):
    events.log(
        "job_finish",
        job=meta["id"],
        action=action,
        rounds=meta.get("round"),
        merged_sha=merged_sha,
        conflict=conflict,
        diff_files=meta.get("diff_files") or 0,
        diff_added=meta.get("diff_added") or 0,
        diff_deleted=meta.get("diff_deleted") or 0,
    )


def _activity_of(update):
    kind = update.get("kind")
    title = (update.get("title") or "").replace("`", "")
    locations = update.get("locations") or []
    if locations and isinstance(locations[0], dict) and locations[0].get("path"):
        return f"{kind} {Path(locations[0]['path']).name}"
    if kind == "execute":
        return f"run {title.removeprefix('Execute ')}"
    return f"{kind} {title}"


class ActivityTracker:
    """Reads grok's live ACP transcript and reduces it to a one-line summary."""

    def __init__(self, workdir, started_at):
        self.sessions = GROK_SESSIONS / quote(str(workdir), safe="")
        self.started_at = started_at
        self.path = None
        self.offset = 0
        self.tools = 0
        self.activity = None

    def _find_transcript(self):
        floor = self.started_at - SESSION_CLOCK_SLACK_S
        newest = None
        try:
            for entry in self.sessions.iterdir():
                updates = entry / "updates.jsonl"
                try:
                    mtime = updates.stat().st_mtime
                except OSError:
                    continue
                if mtime >= floor and (newest is None or mtime > newest[0]):
                    newest = (mtime, updates)
        except OSError:
            return None
        return newest[1] if newest else None

    def _consume(self, line):
        try:
            update = json.loads(line)["params"]["update"]
            kind = update["sessionUpdate"]
        except (ValueError, KeyError, TypeError):
            return
        if kind == "tool_call":
            self.tools += 1
        elif kind == "tool_call_update" and update.get("kind"):
            self.activity = _activity_of(update)
        elif kind == "agent_thought_chunk":
            self.activity = "thinking"
        elif kind == "agent_message_chunk":
            self.activity = "writing"

    def poll(self):
        """Returns "<n> tools · <activity>" or None while no transcript is readable."""
        if self.path is None:
            self.path = self._find_transcript()
            if self.path is None:
                return None
        try:
            with open(self.path, "rb") as handle:
                handle.seek(self.offset)
                chunk = handle.read()
        except OSError:
            return None
        end = chunk.rfind(b"\n") + 1
        for line in chunk[:end].splitlines():
            self._consume(line)
        self.offset += end
        parts = [f"{self.tools} tools"] if self.tools else []
        if self.activity:
            parts.append(self.activity)
        return f" {DOT} ".join(parts) or None


def _progress_message(job_id, elapsed, summary):
    minutes, secs = divmod(elapsed, 60)
    head = f"grok {job_id} {minutes:02d}:{secs:02d}"
    message = f"{head} {DOT} {summary}" if summary else head
    return message[:PROGRESS_MAX_CHARS]


def _progress_loop(stop, job_id, started, notify, workdir=None):
    tracker = ActivityTracker(workdir, started) if workdir else None
    while not stop.wait(PROGRESS_S):
        elapsed = max(0, int(time.time() - started))
        try:
            notify({
                "progress": elapsed,
                "message": _progress_message(job_id, elapsed, tracker.poll() if tracker else None),
            })
        except Exception:
            return


def _wait(jid, stop_event, notify_progress):
    job_dir = _job_dir(jid)
    if job_dir is None:
        return ("no such job", True)
    meta = _read_meta(job_dir)
    if meta is None:
        return ("no such job", True)
    if meta.get("status") != "running":
        return (meta.get("result") or "", bool(meta.get("is_error")))
    lock_path = job_dir / "reaper.lock"
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    progress_stop = threading.Event()
    progress_thread = None
    try:
        while True:
            meta = _read_meta(job_dir)
            if meta is None:
                return ("no such job", True)
            if meta.get("status") != "running":
                return (meta.get("result") or "", bool(meta.get("is_error")))
            # A cancelled tool call stops waiting and sends nothing. It does not kill the job.
            if stop_event is not None and stop_event.is_set():
                return None
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                if stop_event is not None and stop_event.wait(POLL_S):
                    return None
                elif stop_event is None:
                    time.sleep(POLL_S)
                continue
            try:
                if notify_progress:
                    progress_thread = threading.Thread(
                        target=_progress_loop,
                        args=(progress_stop, jid, meta.get("started_at") or time.time(), notify_progress,
                              meta.get("workdir")),
                        name=f"progress-{jid}",
                        daemon=True,
                    )
                    progress_thread.start()
                return _reap_until_done(job_dir, meta, stop_event)
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        progress_stop.set()
        os.close(fd)
        if progress_thread is not None:
            progress_thread.join(timeout=1)


def _reap_until_done(job_dir, meta, stop_event):
    pid = meta.get("pid")
    pgid = meta.get("pgid")
    state, code = _own_poll(pid)
    reattached = state == "foreign"
    if state == "exited":
        _record_pids(job_dir, _proc_tree(pid))
        return _finalize(meta, code, False)
    while True:
        if (job_dir / "cancel").exists():
            code = _kill_group(pgid, pid)
            _record_pids(job_dir, _load_pids(job_dir, pid))
            return _finalize(meta, code, reattached)
        if reattached:
            if not _cmdline_has_grok(pid):
                _record_pids(job_dir, _load_pids(job_dir, pid))
                return _finalize(meta, None, True)
        else:
            state, code = _own_poll(pid)
            if state == "exited":
                _record_pids(job_dir, _proc_tree(pid) | _load_pids(job_dir, pid))
                return _finalize(meta, code, False)
            if state == "foreign":
                reattached = True
                continue
        _record_pids(job_dir, _proc_tree(pid))
        if stop_event is not None and stop_event.is_set():
            return None
        if stop_event is not None:
            stop_event.wait(POLL_S)
        else:
            time.sleep(POLL_S)


def run(arguments, stop_event=None, progress_token=False, notify_progress=None):
    if not isinstance(arguments, dict):
        return ("arguments must be an object", True)
    with _gate:
        meta, err = _prepare(arguments, progress_token)
        if err:
            return err
        jid = meta["id"]
    return _wait(jid, stop_event, notify_progress if progress_token else None)


def result(arguments, stop_event=None, notify_progress=None):
    if not isinstance(arguments, dict):
        arguments = {}
    jid = arguments.get("job")
    cancel = bool(arguments.get("cancel", False))
    if not jid:
        if cancel:
            return ("cancel requires a job", True)
        return (_list_unfinished(), False)
    job_dir = _job_dir(jid)
    meta = _read_meta(job_dir) if job_dir else None
    if meta is None:
        return ("no such job", True)
    if cancel:
        if meta.get("status") != "running":
            return (meta.get("result") or "job is not running", bool(meta.get("is_error")))
        events.write_private(job_dir / "cancel", "1")
    return _wait(jid, stop_event, notify_progress)


def _list_unfinished():
    root = events.state_root() / "jobs"
    if not root.exists():
        return "no unfinished jobs"
    lines = []
    now = time.time()
    for entry in sorted(root.iterdir()):
        meta = _read_meta(entry)
        if not meta or meta.get("status") == "finished":
            continue
        age = _fmt_dur(now - (meta.get("started_at") or now))
        repo = Path(meta.get("repo_root") or "").name
        line = (f"{meta.get('id')} {DOT} {meta.get('status')} {DOT} {age} {DOT} "
                f"{meta.get('mode')} {DOT} {meta.get('kind')} {DOT} {repo}")
        summary = (meta.get("summary") or "").splitlines()
        if summary and summary[0]:
            line += f" {DOT} {summary[0]}"
        lines.append(line)
    return "\n".join(lines) if lines else "no unfinished jobs"


def _paths_from_merge(stderr, repo):
    names = _git(repo, "diff", "--name-only", "--diff-filter=U").stdout.strip()
    if names:
        return names
    found = []
    for line in (stderr or "").splitlines():
        if "would be overwritten" in line or line.strip().startswith("CONFLICT"):
            found.append(line.strip())
    return "\n".join(found)


def finish(arguments):
    if not isinstance(arguments, dict):
        return ("arguments must be an object", True)
    jid = arguments.get("job")
    action = arguments.get("action")
    if not jid:
        return ("job is required", True)
    if action not in ("merge", "discard"):
        return ("action must be merge or discard", True)
    job_dir = _job_dir(jid)
    meta = _read_meta(job_dir) if job_dir else None
    if meta is None:
        return ("no such job", True)
    if meta.get("status") == "running":
        return ("job is running", True)
    if meta.get("status") == "finished":
        return ("job is finished", True)
    if action == "discard":
        _remove_worktree(meta)
        meta["status"] = "finished"
        _write_meta(job_dir, meta)
        _log_finish(meta, "discard", None, False)
        return (f"discarded {jid}", False)
    return _merge(job_dir, meta, arguments.get("message"))


def _merge(job_dir, meta, message):
    if meta.get("mode") != "edit":
        return ("not an edit job", True)
    if meta.get("status") == "error":
        return ("job status is error", True)
    if not isinstance(message, str) or not message.strip():
        return ("message is required for merge", True)
    repo = Path(meta["repo_root"])
    branch = meta.get("branch") or f"grok/{meta['id']}"
    if _git(repo, "rev-parse", "--verify", "--quiet", branch).returncode != 0:
        return ("branch does not exist", True)
    porcelain = _git(repo, "status", "--porcelain", "--untracked-files=no")
    if porcelain.stdout.strip():
        return ("commit your own changes first; nothing was changed", True)
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    tracked = porcelain.stdout
    merged = _git(repo, "merge", "--squash", branch)
    if merged.returncode != 0:
        err = (merged.stderr or merged.stdout or "").strip()
        paths = _paths_from_merge(err, repo)
        _git(repo, "reset", "--hard", "HEAD")
        head2 = _git(repo, "rev-parse", "HEAD").stdout.strip()
        tracked2 = _git(repo, "status", "--porcelain", "--untracked-files=no").stdout
        note = ""
        if head2 != head or tracked2 != tracked:
            note = "\nrestore check failed"
        detail = err
        if paths:
            detail = f"{detail}\n{paths}" if detail else paths
        return (f"{detail}\nbranch kept; resolve by hand or discard{note}".strip(), True)
    if _git(repo, "diff", "--cached", "--quiet").returncode == 0:
        return ("nothing to merge; discard it", True)
    body = message
    trailer = f"Grok-Job: {meta['id']}"
    if trailer not in body:
        body = body.rstrip("\n") + f"\n\n{trailer}\n"
    commit = _git(repo, "commit", "-q", "-F", "-", input_text=body)
    if commit.returncode != 0:
        _git(repo, "reset", "--hard", "HEAD")
        return ((commit.stderr or commit.stdout or "commit failed").strip(), True)
    sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    short = _git(repo, "rev-parse", "--short=7", "HEAD").stdout.strip()
    files, added, deleted = _numstat(_git(repo, "diff", "--numstat", "HEAD~1", "HEAD").stdout)
    _remove_worktree(meta)
    meta["status"] = "finished"
    meta["merged_sha"] = sha
    meta["diff_files"] = len(files)
    meta["diff_added"] = added
    meta["diff_deleted"] = deleted
    _write_meta(job_dir, meta)
    _log_finish(meta, "merge", sha, False)
    noun = "file" if len(files) == 1 else "files"
    return (f"merged {meta['id']} as {short}: {len(files)} {noun} +{added} {MINUS}{deleted}", False)


def _prune_one(entry, cutoff):
    meta_path = entry / "meta.json"
    if not meta_path.is_file():
        return
    if meta_path.stat().st_mtime >= cutoff:
        return
    meta = _read_meta(entry)
    if not meta:
        return
    status = meta.get("status")
    if status == "finished":
        shutil.rmtree(entry, ignore_errors=True)
    elif status in ("cancelled", "error"):
        _remove_worktree(meta)
        shutil.rmtree(entry, ignore_errors=True)


def prune():
    root = events.state_root() / "jobs"
    if not root.exists():
        return
    cutoff = time.time() - PRUNE_DAYS * 86400
    for entry in list(root.iterdir()):
        try:
            _prune_one(entry, cutoff)
        except Exception:
            continue
