#!/usr/bin/env python3
"""Scores whether changes self-improve made actually reduced friction.

Every change carries a falsifiable metric. Metrics are DECLARATIVE (a signal id +
threshold resolved against state/friction.jsonl) -- never a shell command, so an
unattended run cannot execute self-authored code.

  impact.py open                     list changes still on probation
  impact.py review [--apply]         score probations; --apply appends verdicts
  impact.py add --hypothesis H --destination D --signal S [...]   register a change

changes.jsonl is append-only: a status transition is a new record with the same
id. Readers take the last record per id.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harvest import STATE, append_jsonl, latest_snapshots, now_iso  # noqa: E402

CHANGES = STATE / "changes.jsonl"
TOLERANCE = 1e-9


def load_changes() -> dict[str, dict]:
    """Last record per change id."""
    out: dict[str, dict] = {}
    try:
        with open(CHANGES) as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "id" in rec:
                    prev = out.get(rec["id"], {})
                    merged = {**prev, **rec}
                    out[rec["id"]] = merged
    except OSError:
        pass
    return out


def next_id(changes: dict[str, dict]) -> str:
    n = 0
    for cid in changes:
        if cid.startswith("chg-"):
            try:
                n = max(n, int(cid[4:]))
            except ValueError:
                pass
    return f"chg-{n + 1:04d}"


def measure(metric: dict, since: str) -> tuple[float | None, int]:
    """Current per-session rate of the metric's signal in sessions since `since`."""
    snaps = latest_snapshots(since=since, project=metric.get("project"))
    if not snaps:
        return None, 0
    hits = sum(s.get("signals", {}).get(metric["signal"], 0) for s in snaps)
    return hits / len(snaps), len(snaps)


def verdict(metric: dict, current: float | None, n: int) -> str:
    if current is None or n < metric.get("min_sessions", 5):
        return "probation"
    target = metric.get("target", 0.0)
    baseline = metric.get("baseline")
    if metric.get("direction", "down") == "down":
        if current <= target + TOLERANCE:
            return "confirmed"
        # partial credit: >=50% reduction from baseline still counts as working
        if baseline and current <= baseline * 0.5:
            return "confirmed"
        return "no_effect"
    if current >= target - TOLERANCE:
        return "confirmed"
    return "no_effect"


def cmd_add(args) -> int:
    changes = load_changes()
    cid = next_id(changes)
    baseline, n = measure({"signal": args.signal, "project": args.project}, since=None)
    # A constitution item is a PROPOSAL, not an applied change: nothing has been
    # written yet, so it must not be scored as though it had. It leaves "proposed"
    # only when the user approves the diff in-session.
    rec = {
        "id": cid, "ts": now_iso(), "run": args.run,
        "status": "proposed" if args.constitution else "probation",
        "hypothesis": args.hypothesis, "destination": args.destination,
        "diff_ref": args.diff_ref, "constitution": bool(args.constitution),
        "metric": {"signal": args.signal, "project": args.project,
                   "direction": args.direction, "target": args.target,
                   "baseline": round(baseline, 4) if baseline is not None else None,
                   "baseline_sessions": n, "min_sessions": args.min_sessions},
        "reviews": [],
    }
    append_jsonl(CHANGES, [rec])
    print(json.dumps(rec, indent=1))
    return 0


def cmd_open(args) -> int:
    changes = load_changes()
    rows = [c for c in changes.values() if c.get("status") == "probation"]
    if not rows:
        print("no changes on probation")
        return 0
    print(f"{'id':<10} {'signal':<24} {'base':>6} {'now':>6} {'n':>3}  destination")
    for c in sorted(rows, key=lambda r: r["id"]):
        m = c["metric"]
        cur, n = measure(m, c["ts"])
        print(f"{c['id']:<10} {m['signal']:<24} "
              f"{(m.get('baseline') or 0):>6.2f} "
              f"{(cur if cur is not None else float('nan')):>6.2f} {n:>3}  {c['destination']}")

    # Proposals are never scored, but they must stay visible: an unsurfaced
    # constitution proposal is the failure mode the propose-only protocol exists
    # to prevent.
    pending = [c for c in changes.values() if c.get("status") == "proposed"]
    if pending:
        print("\nawaiting approval (constitution — not applied):")
        for c in sorted(pending, key=lambda r: r["id"]):
            print(f"{c['id']:<10} {c['destination']}")
    return 0


def cmd_review(args) -> int:
    changes = load_changes()
    probation = [c for c in changes.values() if c.get("status") == "probation"]
    if not probation:
        print("no changes on probation; nothing to score")
        return 0
    transitions, lines = [], []
    for c in sorted(probation, key=lambda r: r["id"]):
        m = c["metric"]
        cur, n = measure(m, c["ts"])
        v = verdict(m, cur, n)
        lines.append(f"  {c['id']}  {m['signal']:<24} baseline {m.get('baseline')} -> "
                     f"now {cur if cur is not None else 'n/a'} over {n} session(s): {v}")
        if v != "probation":
            transitions.append({
                "id": c["id"], "ts": now_iso(), "status": v,
                "review": {"measured": cur, "sessions": n, "run": args.run},
            })
    print("probation review:")
    print("\n".join(lines))
    if not transitions:
        print("\nall still on probation (need more sessions)")
        return 0
    if args.apply:
        append_jsonl(CHANGES, transitions)
        print(f"\napplied {len(transitions)} verdict(s) to state/changes.jsonl")
        nofx = [t["id"] for t in transitions if t["status"] == "no_effect"]
        if nofx:
            print(f"NO EFFECT -> revert or re-route: {', '.join(nofx)}")
    else:
        print(f"\n{len(transitions)} verdict(s) pending; re-run with --apply to record")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("open").set_defaults(fn=cmd_open)
    r = sub.add_parser("review"); r.add_argument("--apply", action="store_true")
    r.add_argument("--run", default="manual"); r.set_defaults(fn=cmd_review)
    a = sub.add_parser("add")
    a.add_argument("--hypothesis", required=True)
    a.add_argument("--destination", required=True)
    a.add_argument("--signal", required=True)
    a.add_argument("--run", default="manual")
    a.add_argument("--diff-ref", default=None)
    a.add_argument("--project", default=None)
    a.add_argument("--direction", default="down", choices=["down", "up"])
    a.add_argument("--target", type=float, default=0.0)
    a.add_argument("--min-sessions", type=int, default=5)
    a.add_argument("--constitution", action="store_true")
    a.set_defaults(fn=cmd_add)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
