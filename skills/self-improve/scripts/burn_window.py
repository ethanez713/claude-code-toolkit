#!/usr/bin/env python3
"""Is the rate-limit 'burn window' open? -- quota about to expire unused.

Reads ~/.claude/autoresume/state.json, which statusline.py already refreshes on
every render ({resets_at, used_percentage, captured_at, seven_day}). No network,
no tokens.

Open when BOTH buckets say the spend is free:

  5h     resets soon AND quota left  -- that slice expires unused
  weekly resets soon AND quota left  -- otherwise 5h spend just borrows
                                        against Sunday's cap

The 5h test alone is not sufficient: the 5h window rolls ~5x a day all week, so
gating on it fired on a Monday with 83% of the week still to run. Weekly quota
only becomes use-it-or-lose-it near its own reset.

  burn_window.py             human line; exit 0 open, 1 closed, 2 unknown
  burn_window.py --json      machine readable
  burn_window.py --statusline  short token for the status line, else nothing
  burn_window.py --hook      one-line context inject for SessionStart, else nothing
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

STATE_JSON = Path(os.environ.get("CCAR_STATE_JSON",
                                Path.home() / ".claude" / "autoresume" / "state.json"))
LEAD_MIN = int(os.environ.get("SELF_IMPROVE_LEAD_MIN", "75"))
MAX_USED = int(os.environ.get("SELF_IMPROVE_MAX_USED", "75"))
# 43h before a Sunday-07:00 reset lands on Friday noon -- the point where
# leftover weekly quota is realistically unspendable and so worth burning.
WEEK_LEAD_H = int(os.environ.get("SELF_IMPROVE_WEEK_LEAD_H", "43"))
WEEK_MAX_USED = int(os.environ.get("SELF_IMPROVE_WEEK_MAX_USED", "80"))
STALE_S = 3600  # a captured_at older than this makes used_percentage untrustworthy


def evaluate() -> dict:
    try:
        data = json.loads(STATE_JSON.read_text())
    except (OSError, json.JSONDecodeError):
        return {"state": "unknown", "reason": "no rate-limit state yet"}

    resets_at, used = data.get("resets_at"), data.get("used_percentage")
    captured = data.get("captured_at") or 0
    if not resets_at or used is None:
        return {"state": "unknown", "reason": "incomplete state"}

    now = time.time()
    mins_left = (resets_at - now) / 60.0
    week = data.get("seven_day") or {}
    week_resets, week_used = week.get("resets_at"), week.get("used_percentage")
    week_hours_left = (week_resets - now) / 3600.0 if week_resets else None

    out = {"minutes_left": round(mins_left, 1), "used_percentage": used,
           "lead_min": LEAD_MIN, "max_used": MAX_USED,
           "week_hours_left": round(week_hours_left, 1) if week_hours_left else None,
           "week_used_percentage": week_used,
           "week_lead_h": WEEK_LEAD_H, "week_max_used": WEEK_MAX_USED,
           "captured_age_s": int(now - captured)}

    if mins_left <= 0:
        return {**out, "state": "unknown", "reason": "window already reset; state stale"}
    if now - captured > STALE_S:
        return {**out, "state": "unknown", "reason": "usage figure is stale"}
    if mins_left > LEAD_MIN:
        return {**out, "state": "closed", "reason": f"{mins_left:.0f}m left in window"}
    if used > MAX_USED:
        return {**out, "state": "closed", "reason": f"only {100 - used}% quota left"}

    # Weekly gate. Absent block => the harness reports no weekly bucket; fall
    # back to the 5h verdict rather than going silent forever.
    if week_hours_left is not None and week_used is not None:
        if week_hours_left <= 0:
            return {**out, "state": "unknown", "reason": "weekly window already reset; state stale"}
        if week_hours_left > WEEK_LEAD_H:
            return {**out, "state": "closed",
                    "reason": f"weekly reset is {week_hours_left:.0f}h away "
                              f"({week_used}% of the week already used)"}
        if week_used > WEEK_MAX_USED:
            return {**out, "state": "closed",
                    "reason": f"only {100 - week_used}% weekly quota left"}
        return {**out, "state": "open",
                "reason": f"{mins_left:.0f}m left, {100 - used}% quota unused; "
                          f"weekly resets in {week_hours_left:.0f}h with "
                          f"{100 - week_used}% unused"}

    return {**out, "state": "open",
            "reason": f"{mins_left:.0f}m left, {100 - used}% quota unused"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--json", action="store_true")
    g.add_argument("--statusline", action="store_true")
    g.add_argument("--hook", action="store_true")
    args = ap.parse_args()
    r = evaluate()
    open_ = r["state"] == "open"

    if args.json:
        print(json.dumps(r))
    elif args.statusline:
        if open_:
            print("♻ improve", end="")
    elif args.hook:
        # SessionStart: emit the documented envelope so the text reaches the model.
        # Silent when closed, and ALWAYS exit 0 -- a closed window is the normal
        # case, and a non-zero exit would surface as a failing hook every session.
        if open_:
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": (
                    f"Rate-limit burn window is open ({r['reason']}). That quota expires "
                    f"unused at reset. If the user has no other priority, offer to run "
                    f"the self-improve skill now."),
            }}))
        return 0
    else:
        print(f"{r['state']}: {r['reason']}")
    return 0 if open_ else (1 if r["state"] == "closed" else 2)


if __name__ == "__main__":
    sys.exit(main())
