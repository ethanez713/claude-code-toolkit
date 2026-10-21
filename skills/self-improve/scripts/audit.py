#!/usr/bin/env python3
"""Cross-session audits the per-scan friction ranking cannot see.

harvest.py ranks only a single scan's new hits, clustered per (signal, project),
so a pattern spread thin across many sessions never scores high enough to be read,
and steady-state damping buries high-volume flat signals. These passes look at the
whole corpus instead:

  audit.py digest [--since ISO] [--top N]   detector-independent error-content buckets
                                            + files re-read many times in one session
  audit.py efficiency [--since ISO]         per-session token/tool ratios, outliers
  audit.py skills                           skill invocation counts, last use, dead/dormant

Read-only. Prints; writes nothing.
"""
from __future__ import annotations
import argparse, glob, importlib.util, json, os, re, sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "..", "state")
FRICTION = os.path.join(STATE, "friction.jsonl")
PROJECTS = os.path.expanduser("~/.claude/projects")
SKILLS_DIR = os.path.expanduser("~/.claude/skills")
SELF_RE = re.compile(r"skills/self-improve|self-improve/state|patterns\.json")
GATE_LOG = os.path.join(STATE, "write-tests-gate.jsonl")


def _is_test_path():
    """The write-tests gate's own path matcher, so the metric and the hook cannot drift."""
    path = os.path.join(HERE, "write_tests_hook.py")
    spec = importlib.util.spec_from_file_location("write_tests_hook", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.is_test_path


def transcripts():
    return glob.glob(os.path.join(PROJECTS, "*", "*.jsonl"))


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


def read_snapshots(since: str | None):
    """Latest snapshot per session from friction.jsonl, optionally since a start ts."""
    latest: dict[str, dict] = {}
    try:
        with open(FRICTION) as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                sid = r.get("session")
                if not sid:
                    continue
                latest[sid] = r
    except OSError:
        return []
    rows = list(latest.values())
    if since:
        rows = [r for r in rows if (r.get("first_ts") or "") >= since]
    return rows


def cmd_digest(args) -> int:
    sigs: Counter = Counter()
    sig_projects: dict[str, set] = defaultdict(set)
    sig_example: dict[str, str] = {}
    reread: Counter = Counter()  # (project, session, path) -> read count
    n_err = 0
    for f in transcripts():
        proj = proj_of(f)
        session = os.path.basename(f)[:8]
        try:
            fh = open(f, errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                if ('"is_error"' not in line and "tool_use_error" not in line
                        and '"name":"Read"' not in line and '"name": "Read"' not in line):
                    continue
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if args.since and (o.get("timestamp", "") or "") < args.since:
                    continue
                c = o.get("message", {}).get("content")
                if not isinstance(c, list):
                    continue
                for b in c:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "tool_result" and b.get("is_error"):
                        txt = content_text(b.get("content"))
                        if SELF_RE.search(txt):
                            continue
                        s = normalize_sig(txt)
                        sigs[s] += 1
                        sig_projects[s].add(proj)
                        n_err += 1
                        sig_example.setdefault(s, txt.strip().splitlines()[0][:120] if txt.strip() else "")
                    elif b.get("type") == "tool_use" and b.get("name") == "Read":
                        inp = b.get("input") or {}
                        p = inp.get("file_path", "")
                        # Only a full re-read is waste. A windowed re-read (offset/limit)
                        # before an edit is correct: it refreshes the exact old_string after
                        # earlier edits shifted the file, which is how stale-string failures
                        # are avoided. Counting those flags disciplined editing as friction.
                        if p and inp.get("offset") is None and inp.get("limit") is None:
                            reread[(proj, session, p)] += 1
    print(f"# digest: {n_err} error results across {len(transcripts())} transcripts"
          + (f" since {args.since}" if args.since else " (all-time)"))
    print(f"# {len(sigs)} distinct error signatures; top {args.top} by count:\n")
    for s, n in sigs.most_common(args.top):
        projs = ",".join(sorted(sig_projects[s])[:6])
        print(f"{n:5}  [{len(sig_projects[s])} proj: {projs}]")
        print(f"       sig: {s}")
        if sig_example.get(s) and sig_example[s] != s:
            print(f"       e.g. {sig_example[s]}")
    heavy = [(k, v) for k, v in reread.items() if v >= 3]
    heavy.sort(key=lambda kv: kv[1], reverse=True)
    if heavy:
        print(f"\n# same file FULL-read (no offset/limit) >=3x in one session ({len(heavy)} cases, top 12):")
        for (proj, sess, path), n in heavy[:12]:
            print(f"{n:5}  [{proj} {sess}] {path}")
    return 0


def cmd_efficiency(args) -> int:
    rows = read_snapshots(args.since)
    if not rows:
        print("no snapshots")
        return 0

    def ratio(r, num, den):
        d = r.get(den, 0) or 0
        return (r.get(num, 0) or 0) / d if d else 0.0

    tot = defaultdict(int)
    for r in rows:
        for k in ("prompts", "tool_calls", "edit_steps", "retries",
                  "output_tokens", "cache_read_tokens", "assistant_msgs"):
            tot[k] += r.get(k, 0) or 0
    n = len(rows)
    print(f"# efficiency over {n} sessions"
          + (f" since {args.since}" if args.since else " (all-time)"))
    P = tot["prompts"] or 1
    print(f"  per user prompt: tool_calls {tot['tool_calls']/P:.1f} | "
          f"edit_steps {tot['edit_steps']/P:.1f} | retries {tot['retries']/P:.2f} | "
          f"output_tok {tot['output_tokens']//P}")
    print(f"  totals: prompts {tot['prompts']} | tool_calls {tot['tool_calls']} | "
          f"retries {tot['retries']} | out_tok {tot['output_tokens']:,} | "
          f"cache_read_tok {tot['cache_read_tokens']:,}")

    scored = []
    for r in rows:
        scored.append((
            ratio(r, "retries", "prompts"),
            ratio(r, "tool_calls", "prompts"),
            r.get("edit_steps", 0) or 0,
            r,
        ))
    print("\n# high retries/prompt (>=3, min 3 prompts), top 12:")
    for rp, tc, es, r in sorted([s for s in scored if s[0] >= 3 and (r_ := s[3]).get("prompts", 0) >= 3],
                                key=lambda s: s[0], reverse=True)[:12]:
        print(f"  retries/prompt {rp:5.1f}  tool/prompt {tc:5.1f}  edits {es:4}  "
              f"[{proj_of(r['file'])}] {r['session'][:8]}  {(r.get('first_ts') or '')[:10]}")
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


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("digest"); d.add_argument("--since"); d.add_argument("--top", type=int, default=20)
    d.set_defaults(fn=cmd_digest)
    e = sub.add_parser("efficiency"); e.add_argument("--since"); e.set_defaults(fn=cmd_efficiency)
    s = sub.add_parser("skills"); s.add_argument("--since"); s.set_defaults(fn=cmd_skills)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
