#!/usr/bin/env python3
"""Cross-session error digest, fix measurement, and skill-usage counts.

Reads transcripts under SELF_IMPROVE_PROJECTS (default ~/.claude/projects) and
fix state under SELF_IMPROVE_STATE (default ~/.claude/skills/self-improve/state).
`fixes add` and `fixes dismiss` are the only writes; `fixes` never updates a
stored status.
"""
from __future__ import annotations

import argparse
import glob
import importlib.util
import json
import os
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

SKILLS_DIR = os.path.expanduser("~/.claude/skills")
SELF_RE = re.compile(r"skills/self-improve|self-improve/state|patterns\.json")
GATE_LOG = os.path.expanduser("~/.claude/logs/write_tests_gate.jsonl")
COMMAND_SELF = "<command-name>/self-improve"
MIN_AFTER_SESSIONS = 20
WORKS_RATIO = 0.5


def state_dir() -> str:
    return os.path.expanduser(os.environ.get(
        "SELF_IMPROVE_STATE", "~/.claude/skills/self-improve/state"))


def projects_dir() -> str:
    return os.path.expanduser(os.environ.get(
        "SELF_IMPROVE_PROJECTS", "~/.claude/projects"))


def fixes_path() -> str:
    return os.path.join(state_dir(), "fixes.json")


def _is_test_path():
    """The write-tests gate's own path matcher, so the metric and the hook cannot drift."""
    path = os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "..", "..", "hooks", "write_tests_hook.py")
    spec = importlib.util.spec_from_file_location("write_tests_hook", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.is_test_path


def transcripts():
    return glob.glob(os.path.join(projects_dir(), "*", "*.jsonl"))


def proj_of(path: str) -> str:
    return os.path.basename(os.path.dirname(path)).replace(os.path.expanduser("~").replace("/", "-"), "~")


def content_text(rc) -> str:
    if isinstance(rc, str):
        return rc
    if isinstance(rc, list):
        return "\n".join(
            b.get("text", str(b.get("content", ""))) if isinstance(b, dict) else str(b)
            for b in rc
        )
    return str(rc)


_DIGITS = re.compile(r"\d+")
_PATHS = re.compile(r"(/home/\S+|/tmp/\S+|/mnt/\S+)")
_HEX = re.compile(r"\b[0-9a-f]{8,}\b")


def normalize_sig(text: str) -> str:
    """Collapse a message to a signature stable across sessions.

    Volatile bits (paths, numbers, hashes) are placeheld so the same failure shape
    from different sessions lands in one bucket. This is what makes a pattern with
    one hit per session — invisible to per-scan ranking — countable across the corpus.
    """
    m = text.strip()
    m = m.replace("<tool_use_error>", "").replace("</tool_use_error>", "")
    m = _PATHS.sub("<path>", m)
    m = _HEX.sub("<hex>", m)
    m = _DIGITS.sub("N", m)
    m = " ".join(m.split())
    return m[:90] if m else "(empty)"


def parse_ts(ts: str) -> datetime | None:
    if not isinstance(ts, str) or not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def iter_sessions():
    """Yield (project, session id, records) for each transcript, one file at a time."""
    for path in sorted(transcripts()):
        records = []
        try:
            fh = open(path, errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict):
                    records.append(rec)
        name = os.path.basename(path)
        if name.endswith(".jsonl"):
            name = name[:-len(".jsonl")]
        yield proj_of(path), name, records


def _contains_command(value) -> bool:
    if isinstance(value, str):
        return COMMAND_SELF in value
    if isinstance(value, dict):
        return any(_contains_command(v) for v in value.values())
    if isinstance(value, list):
        return any(_contains_command(v) for v in value)
    return False


def is_self_improve(records: list) -> bool:
    """A run of this skill contaminates the corpus: it quotes its own detectors."""
    for rec in records:
        if _contains_command(rec):
            return True
        message = rec.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            inp = block.get("input")
            if (block.get("name") == "Skill" and isinstance(inp, dict)
                    and inp.get("skill") == "self-improve"):
                return True
            if inp is not None and SELF_RE.search(json.dumps(inp, default=str)):
                return True
    return False


def tool_blob(block: dict) -> str:
    name = block.get("name")
    if not isinstance(name, str):
        name = ""
    try:
        dumped = json.dumps(block.get("input"))
    except (TypeError, ValueError):
        dumped = str(block.get("input"))
    return name + " " + dumped


@dataclass
class SessionView:
    project: str
    session_id: str
    started: str
    self_improve: bool
    errors: list  # (timestamp, text) from user records only
    tool_blobs: list


def session_view(project: str, session_id: str, records: list) -> SessionView:
    started = ""
    started_dt = None
    errors = []
    blobs = []
    for rec in records:
        ts = rec.get("timestamp") if isinstance(rec.get("timestamp"), str) else ""
        dt = parse_ts(ts)
        if dt is not None and (started_dt is None or dt < started_dt):
            started_dt = dt
            started = ts
        message = rec.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        # Replay records (assistant, api-request blobs) repeat the same tool result.
        user_record = rec.get("type") == "user"
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                blobs.append(tool_blob(block))
            elif user_record and block.get("type") == "tool_result" and block.get("is_error") is True:
                errors.append((ts, content_text(block.get("content"))))
    return SessionView(project, session_id, started, is_self_improve(records), errors, blobs)


def empty_state() -> dict:
    return {"fixes": [], "dismissed": []}


def read_state() -> dict:
    path = fixes_path()
    try:
        with open(path) as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return empty_state()
    except (OSError, json.JSONDecodeError) as exc:
        print(f"warn: {path}: {exc}", file=sys.stderr)
        return empty_state()
    if not isinstance(data, dict):
        print(f"warn: {path}: expected an object", file=sys.stderr)
        return empty_state()
    fixes = data.get("fixes")
    dismissed = data.get("dismissed")
    return {
        "fixes": fixes if isinstance(fixes, list) else [],
        "dismissed": dismissed if isinstance(dismissed, list) else [],
    }


def load_state_strict() -> dict | None:
    path = fixes_path()
    try:
        with open(path) as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return empty_state()
    except (OSError, json.JSONDecodeError) as exc:
        print(f"cannot read {path}: {exc}", file=sys.stderr)
        return None
    if not isinstance(data, dict):
        print(f"cannot read {path}: expected an object", file=sys.stderr)
        return None
    fixes = data.get("fixes")
    dismissed = data.get("dismissed")
    if "fixes" in data and not isinstance(fixes, list):
        print(f"cannot read {path}: fixes is not a list", file=sys.stderr)
        return None
    if "dismissed" in data and not isinstance(dismissed, list):
        print(f"cannot read {path}: dismissed is not a list", file=sys.stderr)
        return None
    return {
        "fixes": fixes if isinstance(fixes, list) else [],
        "dismissed": dismissed if isinstance(dismissed, list) else [],
    }


def save_state(data: dict) -> None:
    directory = state_dir()
    os.makedirs(directory, exist_ok=True)
    path = fixes_path()
    text = json.dumps(data, indent=2) + "\n"
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def compile_regex(pattern: str, label: str):
    try:
        return re.compile(pattern)
    except re.error as exc:
        print(f"warn: bad {label} regex {pattern!r}: {exc}", file=sys.stderr)
        return None


def check_regex(pattern: str | None, label: str) -> bool:
    if pattern is None:
        return True
    try:
        re.compile(pattern)
    except re.error as exc:
        print(f"invalid {label} regex: {exc}", file=sys.stderr)
        return False
    return True


@dataclass
class Bucket:
    sig: str
    count: int = 0
    sessions: set = field(default_factory=set)
    projects: set = field(default_factory=set)
    labels: list = field(default_factory=list)
    label_ids: set = field(default_factory=set)
    dismissed: bool = False
    first_ts: str = ""
    first_dt: datetime | None = None
    last_ts: str = ""
    last_dt: datetime | None = None
    last_project: str = ""
    last_session: str = ""


def _fix_rules(data: dict):
    rules = []
    for fix in data["fixes"]:
        if not isinstance(fix, dict):
            continue
        pattern = fix.get("signature")
        if not isinstance(pattern, str) or not pattern:
            continue
        cre = compile_regex(pattern, "fix")
        if cre is None:
            continue
        rules.append((cre, str(fix.get("id") or ""), str(fix.get("status") or "")))
    return rules


def _dismissed_rules(data: dict):
    rules = []
    for item in data["dismissed"]:
        if not isinstance(item, dict):
            continue
        pattern = item.get("signature")
        if not isinstance(pattern, str) or not pattern:
            continue
        cre = compile_regex(pattern, "dismissed")
        if cre is not None:
            rules.append(cre)
    return rules


def iter_views():
    for project, session_id, records in iter_sessions():
        yield session_view(project, session_id, records)


def cmd_digest(args) -> int:
    data = read_state()
    fix_rules = _fix_rules(data)
    dismissed_rules = _dismissed_rules(data)
    buckets: dict[str, Bucket] = {}
    n_err = 0
    n_read = 0
    n_skip = 0
    for view in iter_views():
        n_read += 1
        # The whole session is one self-improve run, including errors logged
        # before the skill invocation that marks it.
        if view.self_improve:
            n_skip += 1
            continue
        for ts, text in view.errors:
            if args.since and ts < args.since:
                continue
            sig = normalize_sig(text)
            bucket = buckets.get(sig)
            if bucket is None:
                bucket = Bucket(sig)
                buckets[sig] = bucket
            bucket.count += 1
            bucket.sessions.add(view.session_id)
            bucket.projects.add(view.project)
            n_err += 1
            dt = parse_ts(ts)
            if dt is not None:
                if bucket.first_dt is None or dt < bucket.first_dt:
                    bucket.first_dt = dt
                    bucket.first_ts = ts
                if bucket.last_dt is None or dt >= bucket.last_dt:
                    bucket.last_dt = dt
                    bucket.last_ts = ts
                    bucket.last_project = view.project
                    bucket.last_session = view.session_id
            if not bucket.dismissed:
                for cre in dismissed_rules:
                    if cre.search(text):
                        bucket.dismissed = True
                        break
            for cre, fid, status in fix_rules:
                if fid not in bucket.label_ids and cre.search(text):
                    bucket.labels.append((fid, status))
                    bucket.label_ids.add(fid)
    shown = []
    hidden = 0
    for bucket in buckets.values():
        if bucket.dismissed and not args.all:
            hidden += 1
            continue
        shown.append(bucket)
    shown.sort(key=lambda b: (-b.count, b.sig))
    noun = "error result" if n_err == 1 else "error results"
    sess = "session" if n_read == 1 else "sessions"
    span = f" since {args.since}" if args.since else ""
    print(f"# digest: {n_err} {noun} across {n_read} {sess} ({n_skip} self-improve skipped){span}")
    print(f"# {len(buckets)} distinct signatures; top {args.top}")
    if hidden:
        print(f"# dismissed signatures hidden: {hidden}")
    print()
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.recent_days)
    for bucket in shown[: args.top]:
        projs = ",".join(sorted(bucket.projects)[:6])
        date = bucket.first_ts[:10] if bucket.first_ts else "-"
        new = " NEW" if bucket.first_dt is not None and bucket.first_dt >= cutoff else ""
        labels = "".join(f" [fix {fid} {status}]" for fid, status in bucket.labels)
        print(f"{bucket.count:5}  [{len(bucket.sessions)} sessions, {len(bucket.projects)} proj: {projs}]"
              f"  first {date}{new}{labels}")
        if bucket.last_ts:
            print(f"       loc: {bucket.last_project} {bucket.last_session[:8]} {bucket.last_ts}")
        print(f"       sig: {bucket.sig}")
    return 0


def _opportunity(view: SessionView, pattern: re.Pattern | None) -> bool:
    if pattern is None:
        return True
    return any(pattern.search(blob) for blob in view.tool_blobs)


def _has_error(view: SessionView, pattern: re.Pattern) -> bool:
    return any(pattern.search(text) for _, text in view.errors)


def _rate(views, signature: re.Pattern, opportunity: re.Pattern | None,
          start: datetime | None, end: datetime | None):
    """Opportunity-session rate for session starts in [start, end). None is an open bound."""
    sessions = 0
    hits = 0
    for view in views:
        dt = parse_ts(view.started)
        if dt is None or view.self_improve:
            continue
        if start is not None and dt < start:
            continue
        if end is not None and dt >= end:
            continue
        if not _opportunity(view, opportunity):
            continue
        sessions += 1
        if _has_error(view, signature):
            hits += 1
    rate = hits / sessions if sessions else 0.0
    return rate, sessions


def _hook_firings(views, pattern: re.Pattern, shipped: datetime) -> int:
    n = 0
    for view in views:
        for ts, text in view.errors:
            dt = parse_ts(ts) or parse_ts(view.started)
            if dt is None or dt < shipped:
                continue
            if pattern.search(text):
                n += 1
    return n


def _views() -> list[SessionView]:
    return list(iter_views())


def cmd_fixes(args) -> int:
    data = read_state()
    views = _views()
    fixes = [f for f in data["fixes"] if isinstance(f, dict)]
    print(f"# fixes: {len(fixes)} registered")
    for fix in fixes:
        print(f"\n{fix.get('id')}")
        if fix.get("problem"):
            print(f"  problem: {fix['problem']}")
        signature = fix.get("signature")
        if not isinstance(signature, str) or not signature:
            print(f"  measured by: {fix.get('evidence') or ''}")
            continue
        cre = compile_regex(signature, "fix")
        if cre is None:
            print("  skipped: invalid signature regex")
            continue
        opportunity = fix.get("opportunity")
        opp = None
        if isinstance(opportunity, str) and opportunity:
            opp = compile_regex(opportunity, "opportunity")
            if opp is None:
                print("  skipped: invalid opportunity regex")
                continue
        hook = fix.get("hook_signature")
        hook_re = None
        if isinstance(hook, str) and hook:
            hook_re = compile_regex(hook, "hook")
            if hook_re is None:
                print("  skipped: invalid hook regex")
                continue
        shipped = parse_ts(fix.get("shipped") or "")
        base = fix.get("baseline") if isinstance(fix.get("baseline"), dict) else {}
        base_rate = float(base.get("rate") or 0.0)
        base_n = int(base.get("sessions") or 0)
        base_w = base.get("window_days")
        if shipped is None:
            rate, n = 0.0, 0
            firings = None
        else:
            rate, n = _rate(views, cre, opp, shipped, None)
            firings = _hook_firings(views, hook_re, shipped) if hook_re is not None else None
        wpart = f" ({base_w}d)" if base_w is not None else ""
        print(f"  baseline: {base_rate:.3f} over {base_n} sessions{wpart}")
        print(f"  after: {rate:.3f} over {n} sessions since {fix.get('shipped')}")
        if firings is None:
            print("  hook firings since shipped: n/a")
        else:
            print(f"  hook firings since shipped: {firings}")
        if n < MIN_AFTER_SESSIONS:
            verdict = "pending"
        elif rate <= WORKS_RATIO * base_rate:
            verdict = "works"
        else:
            verdict = "failing"
        print(f"  verdict: {verdict}")
    return 0


def compute_baseline(signature: str, opportunity: str | None, shipped: str, window_days: int) -> dict:
    cre = re.compile(signature)
    opp = re.compile(opportunity) if opportunity else None
    end = parse_ts(shipped)
    start = end - timedelta(days=window_days)
    views = _views()
    rate, sessions = _rate(views, cre, opp, start, end)
    return {"rate": rate, "sessions": sessions, "window_days": window_days}


def cmd_fixes_add(args) -> int:
    if not check_regex(args.signature, "signature"):
        return 1
    if not check_regex(args.opportunity, "opportunity"):
        return 1
    if not check_regex(args.hook_signature, "hook-signature"):
        return 1
    shipped = now_iso() if args.shipped == "now" else args.shipped
    if parse_ts(shipped) is None:
        print(f"invalid --shipped: {args.shipped}", file=sys.stderr)
        return 1
    data = load_state_strict()
    if data is None:
        return 1
    if any(isinstance(fix, dict) and fix.get("id") == args.id for fix in data["fixes"]):
        print(f"duplicate id: {args.id}", file=sys.stderr)
        return 1
    baseline = None
    if args.signature:
        baseline = compute_baseline(args.signature, args.opportunity, shipped, args.window_days)
    data["fixes"].append({
        "id": args.id,
        "shipped": shipped,
        "mechanism": args.mechanism,
        "commit": args.commit,
        "problem": args.problem,
        "signature": args.signature,
        "opportunity": args.opportunity,
        "hook_signature": args.hook_signature,
        "baseline": baseline,
        "status": "pending",
        "evidence": args.evidence,
    })
    save_state(data)
    if baseline is None:
        print(f"added {args.id} (no signature; baseline not computed)")
    else:
        print(f"added {args.id} baseline {baseline['rate']:.3f} over {baseline['sessions']} sessions"
              f" ({baseline['window_days']}d)")
    return 0


def cmd_fixes_dismiss(args) -> int:
    if not check_regex(args.signature, "signature"):
        return 1
    data = load_state_strict()
    if data is None:
        return 1
    data["dismissed"].append({"signature": args.signature, "reason": args.reason})
    save_state(data)
    print(f"dismissed {args.signature}")
    return 0


def _skill_names() -> list[str]:
    out = []
    for d in sorted(glob.glob(os.path.join(SKILLS_DIR, "*"))):
        if os.path.isdir(d) and os.path.exists(os.path.join(d, "SKILL.md")):
            out.append(os.path.basename(d))
    return out


# Skills followed by behaviour, not the Skill tool: their trigger is a task shape,
# so a low invocation count is expected and is NOT evidence they are dead.
BEHAVIOUR_TRIGGERED = {"review", "write-tests", "organize", "refine-spec",
                       "orchestrator-bootstrap", "create-skill"}


def cmd_skills(args) -> int:
    is_test_path = _is_test_path()
    inv: Counter = Counter()
    last: dict[str, str] = defaultdict(str)
    cmd_re = re.compile(r"<command-name>/?([a-z0-9-]+)")
    # write-tests coverage: first test-file write per session, and whether the skill fired.
    test_write_ts: dict[str, str] = {}
    wt_sessions: set[str] = set()
    for f in transcripts():
        session = os.path.basename(f)[:-len(".jsonl")]
        try:
            fh = open(f, errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                if ("skill" not in line.lower() and "command-name" not in line
                        and '"Write"' not in line and '"Edit"' not in line
                        and '"MultiEdit"' not in line):
                    continue
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = o.get("timestamp", "")
                c = o.get("message", {}).get("content")
                if isinstance(c, list):
                    for b in c:
                        if not (isinstance(b, dict) and b.get("type") == "tool_use"):
                            continue
                        if b.get("name") in ("Write", "Edit", "MultiEdit"):
                            p = (b.get("input") or {}).get("file_path") or ""
                            if is_test_path(p) and session not in test_write_ts:
                                test_write_ts[session] = ts
                            continue
                        if b.get("name") != "Skill":
                            continue
                        s = (b.get("input") or {}).get("skill", "?")
                        if s == "write-tests":
                            wt_sessions.add(session)
                        inv[s] += 1
                        if ts > last[s]:
                            last[s] = ts
                elif isinstance(c, str):
                    m = cmd_re.search(c)
                    if m:
                        s = m.group(1)
                        inv[s] += 1
                        if ts > last[s]:
                            last[s] = ts

    print("# skill usage (Skill tool + /slash invocations). Behaviour-triggered skills")
    print("# are marked [B]: a low count is expected, judge them by whether the work happened.\n")
    print(f"  {'skill':26} {'invocations':>11}  {'last used':>10}")
    for name in _skill_names():
        n = inv.get(name, 0)
        tag = " [B]" if name in BEHAVIOUR_TRIGGERED else ""
        flag = ""
        if n == 0:
            flag = "  <- DEAD?" if name not in BEHAVIOUR_TRIGGERED else "  <- 0 explicit; verify by behaviour"
        print(f"  {name+tag:26} {n:>11}  {last.get(name,'-')[:10]:>10}{flag}")
    # write-tests is gated on a session's first test-file write (PreToolUse); this is its metric.
    since = args.since or ""
    wrote = {s for s, ts in test_write_ts.items() if ts >= since}
    fired = wrote & wt_sessions
    pct = f"{100 * len(fired) / len(wrote):.0f}%" if wrote else "n/a"
    print(f"\n# write-tests coverage{' since ' + since if since else ' (all-time)'}: "
          f"{len(wrote)} sessions wrote test files, {len(fired)} invoked write-tests ({pct})")
    try:
        with open(GATE_LOG) as fh:
            gated = [json.loads(l) for l in fh if l.strip()]
    except (OSError, json.JSONDecodeError):
        gated = []
    denied = [g for g in gated if not g.get("already_invoked") and g.get("ts", "") >= since[:19]]
    if gated:
        followed = sum(1 for g in denied if g.get("session") in wt_sessions)
        print(f"  gate denied {len(denied)} session(s); {followed} then invoked the skill")

    # Invoked names with no skill dir = deleted skills still referenced in history.
    ghosts = {s for s in inv if s not in set(_skill_names()) and not s.startswith(("clear", "model", "effort"))}
    if ghosts:
        print("\n# invoked but no SKILL.md (deleted or built-in), for reference:")
        for s in sorted(ghosts):
            print(f"  {s:26} {inv[s]:>11}  {last.get(s,'-')[:10]:>10}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="audit.py")
    sub = ap.add_subparsers(dest="cmd", required=True)

    digest = sub.add_parser("digest", help="bucket error tool results by signature")
    digest.add_argument("--since", help="ISO timestamp; ignore earlier error results")
    digest.add_argument("--top", type=int, default=20)
    digest.add_argument("--recent-days", type=int, default=14)
    digest.add_argument("--all", action="store_true", help="include dismissed signatures")
    digest.set_defaults(fn=cmd_digest)

    skills = sub.add_parser("skills", help="skill invocation counts")
    skills.add_argument("--since")
    skills.set_defaults(fn=cmd_skills)

    fixes = sub.add_parser("fixes", help="measure registered fixes, or change the registry")
    fixes.set_defaults(fn=cmd_fixes)
    fixes_sub = fixes.add_subparsers(dest="fixes_cmd")

    add = fixes_sub.add_parser("add", help="register a fix and store its baseline")
    add.add_argument("--id", required=True)
    add.add_argument("--problem", required=True)
    add.add_argument("--mechanism", required=True)
    add.add_argument("--commit", required=True)
    add.add_argument("--signature")
    add.add_argument("--opportunity")
    add.add_argument("--hook-signature")
    add.add_argument("--evidence")
    add.add_argument("--shipped", default="now", help="ISO timestamp, or 'now'")
    add.add_argument("--window-days", type=int, default=60)
    add.set_defaults(fn=cmd_fixes_add)

    dismiss = fixes_sub.add_parser("dismiss", help="hide a signature from digest")
    dismiss.add_argument("--signature", required=True)
    dismiss.add_argument("--reason", required=True)
    dismiss.set_defaults(fn=cmd_fixes_dismiss)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
