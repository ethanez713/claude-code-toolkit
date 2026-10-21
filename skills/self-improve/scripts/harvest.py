#!/usr/bin/env python3
"""Deterministic friction indexer for the self-improve skill. No LLM, no network.

Reads only the bytes appended to each transcript since the last run (byte-offset
cursors), counts signals defined in state/patterns.json, and emits ranked
candidate slices so the LLM reads a few hundred lines instead of ~118MB.

  harvest.py scan [--full] [--dry-run]   incremental scan (--full: rescan all)
  harvest.py count --signal ID [--since ISO_TS] [--project SLUG]
  harvest.py report [--since ISO_TS]

Cumulative per-session snapshots are appended to state/friction.jsonl; readers
take the last record per session_id. Everything is append-only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
STATE = SKILL_DIR / "state"
CURSOR = STATE / "cursor.json"
PATTERNS = STATE / "patterns.json"
FRICTION = STATE / "friction.jsonl"
CANDIDATES = STATE / "candidates.json"
RATES = STATE / "signal_rates.json"
PROJECTS = Path.home() / ".claude" / "projects"

# Counted per session as `edit_steps`, reported as context only. NOT a proxy for
# the "generated functions" in McMillan 2026 -- that mapping was tested here and
# did not reproduce. See references/evidence.md before drawing depth conclusions.
EDIT_TOOLS = {"Edit", "Write", "NotebookEdit", "MultiEdit"}

# Self-contamination guard. A detector's own definition, quoted in the transcript
# of the run that authored it, would otherwise match itself -- inflating counts
# in exactly the sessions where self-improve runs. Skip our own footprint.
SELF_RE = re.compile(r"skills/self-improve|self-improve/state|patterns\.json")

MAX_SLICES_PER_CLUSTER = 3
SLICE_CONTEXT_LINES = 3


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_atomic(path: Path, text: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    os.replace(tmp, path)
    os.chmod(path, mode)


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return default


def append_jsonl(path: Path, records: list[dict]) -> None:
    if not records:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as fh:
        for rec in records:
            fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
    os.chmod(path, 0o600)


# --------------------------------------------------------------------------
# detectors
# --------------------------------------------------------------------------
def compile_patterns() -> list[dict]:
    """Load enabled detectors from state/patterns.json and compile them."""
    raw = load_json(PATTERNS, {}).get("patterns", [])
    out = []
    for p in raw:
        if not p.get("enabled", True):
            continue
        try:
            p = dict(p)
            p["_re"] = re.compile(p["pattern"], re.IGNORECASE if p.get("ignorecase", True) else 0)
            out.append(p)
        except re.error as exc:
            print(f"warn: bad pattern {p.get('id')}: {exc}", file=sys.stderr)
    return out


INJECT_RE = re.compile(
    r"\A(?:Base directory for this skill:|#{1,3} [^\n]*\bskill\b)", re.IGNORECASE)


def user_text(rec: dict) -> str | None:
    """Real user prompt text, or None for tool results / meta / injects."""
    if rec.get("type") != "user" or rec.get("isMeta"):
        return None
    msg = rec.get("message") or {}
    if msg.get("role") != "user":
        return None
    content = msg.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None
        text = "\n".join(parts)
    else:
        return None
    text = text.strip()
    if not text or text.startswith("<") or text.startswith("Caveat:"):
        return None
    # A skill body is injected in the user role, so prose written for the model
    # ("never stop after phase 1", "don't do X") reads as the user correcting it.
    if INJECT_RE.match(text):
        return None
    return text


def bash_commands(rec: dict) -> str:
    """Concatenated Bash `command` inputs in this record, or '' if none."""
    msg = rec.get("message") or {}
    content = msg.get("content")
    if not isinstance(content, list):
        return ""
    return "\n".join(
        b.get("input", {}).get("command", "")
        for b in content
        if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "Bash"
    )


def skill_invocations(rec: dict) -> str:
    """Concatenated Skill-tool `skill` names in this record, or '' if none."""
    msg = rec.get("message") or {}
    content = msg.get("content")
    if not isinstance(content, list):
        return ""
    return "\n".join(
        b.get("input", {}).get("skill", "")
        for b in content
        if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "Skill"
    )


def tool_results(rec: dict) -> str:
    """Concatenated tool_result payloads in this record, or '' if none.

    What a command actually printed, as opposed to prose about it. A detector whose
    pattern is an ERROR STRING has to scope here: the same string lives in CLAUDE.md
    and in the skills that warn about it, so `any` counts the documentation of a fix
    as instances of the problem it fixed.
    """
    msg = rec.get("message") or {}
    content = msg.get("content")
    if not isinstance(content, list):
        return ""
    out = []
    for b in content:
        if not isinstance(b, dict) or b.get("type") != "tool_result":
            continue
        c = b.get("content")
        if isinstance(c, str):
            out.append(c)
        elif isinstance(c, list):
            out.extend(x.get("text", "") for x in c if isinstance(x, dict))
    return "\n".join(out)


def blank_totals() -> dict:
    return {
        "prompts": 0,
        "assistant_msgs": 0,
        "edit_steps": 0,
        "tool_calls": 0,
        "retries": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "signals": {},
        "first_ts": None,
        "last_ts": None,
        "project": None,
    }


def lines_before(path: Path, offset: int) -> int:
    """Line count preceding `offset`, so a hit's line number is absolute.

    An incremental scan starts mid-file, so counting from the seek point yields a
    number that the slice's `sed` command then reads as absolute and misses by
    however many lines the previous runs already consumed.
    """
    if offset <= 0:
        return 0
    n = 0
    with open(path, "rb") as fh:
        remaining = offset
        while remaining > 0:
            chunk = fh.read(min(1 << 20, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            n += chunk.count(b"\n")
    return n


def scan_file(path: Path, offset: int, totals: dict, patterns: list[dict]) -> tuple[int, list[dict]]:
    """Scan from `offset`; mutate `totals`; return (new_offset, hits)."""
    hits: list[dict] = []
    seen_tools: dict[str, int] = defaultdict(int)
    line_no = lines_before(path, offset)
    with open(path, "rb") as fh:
        fh.seek(offset)
        while True:
            pos = fh.tell()
            raw = fh.readline()
            if not raw:
                break
            line_no += 1
            line = raw.decode("utf-8", errors="replace")
            # Pattern matching runs on the raw line: cheap, and catches text in
            # tool results and error payloads that structured access would miss.
            # Our own footprint is never a friction signal, but its token/step
            # metrics below still count, so suppress matching only.
            # Hits are buffered, not committed: a detector carrying `since` needs
            # this record's timestamp, which is only known after the parse below.
            pending: list[dict] = []
            for p in ([] if SELF_RE.search(line) else patterns):
                if p["_re"].search(line):
                    if p.get("scope") == "user_prompt":
                        try:
                            if user_text(json.loads(line)) is None:
                                continue
                        except json.JSONDecodeError:
                            continue
                    # A command-shaped detector asks "was this command run?", but the
                    # raw line also carries prose that merely mentions it — the
                    # wrapper's own source, this skill's safety rules, vendor docs.
                    # Re-match against the Bash input only, so a citation is not a hit.
                    elif p.get("scope") == "bash_command":
                        try:
                            if not p["_re"].search(bash_commands(json.loads(line))):
                                continue
                        except json.JSONDecodeError:
                            continue
                    elif p.get("scope") == "tool_result":
                        try:
                            if not p["_re"].search(tool_results(json.loads(line))):
                                continue
                        except json.JSONDecodeError:
                            continue
                    elif p.get("scope") == "skill_invocation":
                        try:
                            if not p["_re"].search(skill_invocations(json.loads(line))):
                                continue
                        except json.JSONDecodeError:
                            continue
                    pending.append({"signal": p["id"], "severity": p.get("severity", "med"),
                                    "offset": pos, "line": line_no, "_since": p.get("since")})

            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                rec = None

            # A detector's `since` is the date the fix for that friction landed.
            # Hits before it describe a problem that is already solved, so they are
            # dropped outright rather than re-adjudicated every run. An undated
            # record cannot be shown to predate the fix, so it is kept.
            rec_ts = (rec or {}).get("timestamp") or ""
            for h in pending:
                since = h.pop("_since", None)
                if since and rec_ts and rec_ts < since:
                    continue
                totals["signals"][h["signal"]] = totals["signals"].get(h["signal"], 0) + 1
                hits.append(h)

            if rec is None:
                continue

            ts = rec.get("timestamp")
            if ts:
                totals["first_ts"] = totals["first_ts"] or ts
                totals["last_ts"] = ts
            if rec.get("cwd") and not totals["project"]:
                totals["project"] = rec["cwd"]

            if user_text(rec) is not None:
                totals["prompts"] += 1

            if rec.get("type") == "assistant":
                totals["assistant_msgs"] += 1
                usage = ((rec.get("message") or {}).get("usage")) or {}
                totals["output_tokens"] += usage.get("output_tokens") or 0
                totals["cache_read_tokens"] += usage.get("cache_read_input_tokens") or 0
                for block in (rec.get("message") or {}).get("content") or []:
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    name = block.get("name") or "?"
                    totals["tool_calls"] += 1
                    if name in EDIT_TOOLS:
                        totals["edit_steps"] += 1
                    sig = name + "|" + json.dumps(block.get("input"), sort_keys=True)[:60]
                    seen_tools[sig] += 1
    totals["retries"] += sum(c - 1 for c in seen_tools.values() if c > 1)
    return os.path.getsize(path), hits  # read to EOF


def cmd_scan(args) -> int:
    patterns = compile_patterns()
    if not patterns:
        print("no enabled patterns in state/patterns.json", file=sys.stderr)
    state = load_json(CURSOR, {"version": 1, "files": {}})
    files = state.setdefault("files", {})
    scan_id = now_iso()
    snapshots, all_hits, changed = [], [], 0

    for path in sorted(PROJECTS.glob("*/*.jsonl")):
        key = str(path)
        try:
            size = path.stat().st_size
        except OSError:
            continue
        entry = files.get(key, {"offset": 0, "totals": blank_totals()})
        offset = 0 if args.full else entry.get("offset", 0)
        if offset > size:  # file rewritten -> recount from scratch
            offset, entry["totals"] = 0, blank_totals()
        if args.full:
            entry["totals"] = blank_totals()
        elif offset == size:
            files[key] = entry
            continue

        totals = entry.get("totals") or blank_totals()
        totals.setdefault("signals", {})
        new_offset, hits = scan_file(path, offset, totals, patterns)
        changed += 1
        session_id = path.stem
        for h in hits:
            h.update({"session": session_id, "file": key, "project": totals.get("project")})
        all_hits.extend(hits)
        entry.update({"offset": new_offset, "size": size,
                      "mtime": path.stat().st_mtime, "totals": totals})
        files[key] = entry
        snapshots.append({"scan": scan_id, "session": session_id, "file": key,
                          "project": totals.get("project"),
                          "first_ts": totals.get("first_ts"), "last_ts": totals.get("last_ts"),
                          "prompts": totals["prompts"], "assistant_msgs": totals["assistant_msgs"],
                          "edit_steps": totals["edit_steps"], "tool_calls": totals["tool_calls"],
                          "retries": totals["retries"],
                          "output_tokens": totals["output_tokens"],
                          "cache_read_tokens": totals["cache_read_tokens"],
                          "signals": totals["signals"]})

    state["last_scan"] = scan_id
    if args.dry_run:
        print(f"[dry-run] {changed} file(s) with new bytes, {len(all_hits)} hits; nothing written")
        return 0

    append_jsonl(FRICTION, snapshots)
    write_atomic(CURSOR, json.dumps(state, indent=1))
    write_atomic(CANDIDATES,
                 json.dumps(rank_candidates(all_hits, scan_id, len(snapshots)), indent=1))
    print(f"scanned {changed} file(s) with new bytes; {len(all_hits)} hits; "
          f"{len(snapshots)} session snapshot(s) -> state/friction.jsonl")
    return 0


STEADY_BAND = 0.25
STEADY_DAMPING = 0.4


def steady_state_damping(signal: str, rate: float) -> float:
    """Damp signals whose per-session rate has not moved since the last scan.

    A rate that holds flat run after run is the cost of how the work is done, not
    friction that appeared and can be fixed; left undamped it wins the ranking on
    volume every run and spends the adjudication budget re-reading the same
    background. Novel and spiking signals keep full weight, which is where a
    fixable regression actually shows up.
    """
    try:
        rates = json.loads(RATES.read_text())
    except (OSError, json.JSONDecodeError):
        return 1.0
    prior = rates.get(signal)
    if prior is None or prior <= 0:
        return 1.0
    return STEADY_DAMPING if abs(rate - prior) / prior <= STEADY_BAND else 1.0


def rank_candidates(hits: list[dict], scan_id: str, sessions: int = 0) -> dict:
    """Cluster hits by (signal, project) and keep the top slices to read."""
    weight = {"high": 3.0, "med": 1.0, "low": 0.3}
    clusters: dict[tuple, list[dict]] = defaultdict(list)
    for h in hits:
        clusters[(h["signal"], h.get("project") or "?")].append(h)

    totals: dict[str, int] = defaultdict(int)
    for h in hits:
        totals[h["signal"]] += 1
    rates = {s: round(n / sessions, 4) for s, n in totals.items()} if sessions else {}

    ranked = []
    for (signal, project), items in clusters.items():
        sev = items[0].get("severity", "med")
        damping = steady_state_damping(signal, rates[signal]) if signal in rates else 1.0
        ranked.append({
            "signal": signal, "project": project, "count": len(items),
            "severity": sev,
            "score": round(weight.get(sev, 1.0) * len(items) * damping, 2),
            "steady_state": damping < 1.0,
            "slices": [{"file": i["file"], "session": i["session"],
                        "line": i["line"], "offset": i["offset"],
                        "read": f"sed -n '{max(1, i['line'] - SLICE_CONTEXT_LINES)},"
                                f"{i['line'] + SLICE_CONTEXT_LINES}p' {i['file']}"}
                       for i in items[:MAX_SLICES_PER_CLUSTER]],
        })
    ranked.sort(key=lambda c: c["score"], reverse=True)
    if rates:
        write_atomic(RATES, json.dumps(rates, indent=1))
    return {"scan": scan_id, "clusters": ranked}


# --------------------------------------------------------------------------
# queries (declarative metrics -- no shell execution)
# --------------------------------------------------------------------------
def latest_snapshots(since: str | None = None, project: str | None = None) -> list[dict]:
    """Last snapshot per session, optionally limited to sessions starting >= since."""
    by_session: dict[str, dict] = {}
    try:
        with open(FRICTION) as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                by_session[rec["session"]] = rec
    except OSError:
        return []
    out = list(by_session.values())
    if since:
        out = [r for r in out if (r.get("first_ts") or "") >= since]
    if project:
        out = [r for r in out if project in (r.get("project") or "")]
    return out


def cmd_count(args) -> int:
    snaps = latest_snapshots(args.since, args.project)
    total = sum(s.get("signals", {}).get(args.signal, 0) for s in snaps)
    n = len(snaps)
    print(json.dumps({"signal": args.signal, "since": args.since, "project": args.project,
                      "sessions": n, "hits": total,
                      "per_session": round(total / n, 3) if n else None}))
    return 0


def cmd_report(args) -> int:
    snaps = latest_snapshots(args.since)
    if not snaps:
        print("no data; run: harvest.py scan --full")
        return 0
    agg: dict[str, int] = defaultdict(int)
    for s in snaps:
        for k, v in s.get("signals", {}).items():
            agg[k] += v
    tok = sum(s.get("output_tokens", 0) for s in snaps)
    steps = [s.get("edit_steps", 0) for s in snaps]
    prompts = sum(s.get("prompts", 0) for s in snaps)
    print(f"sessions {len(snaps)} | prompts {prompts} | output_tokens {tok:,} | "
          f"retries {sum(s.get('retries', 0) for s in snaps)}")
    deep = [x for x in steps if x >= 8]
    print(f"edit_steps: max {max(steps)} | mean {sum(steps)/len(steps):.1f} | "
          f"sessions>=8 steps {len(deep)}  (context only; see references/evidence.md)")
    print("\nsignal                            hits   per-session")
    for k, v in sorted(agg.items(), key=lambda kv: kv[1], reverse=True):
        print(f"  {k:<30} {v:>6}   {v/len(snaps):>8.2f}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan"); s.add_argument("--full", action="store_true")
    s.add_argument("--dry-run", action="store_true"); s.set_defaults(fn=cmd_scan)
    c = sub.add_parser("count"); c.add_argument("--signal", required=True)
    c.add_argument("--since"); c.add_argument("--project"); c.set_defaults(fn=cmd_count)
    r = sub.add_parser("report"); r.add_argument("--since"); r.set_defaults(fn=cmd_report)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
