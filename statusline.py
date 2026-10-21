#!/usr/bin/env python3
"""Claude Code status line.

Reads the session JSON on stdin (see code.claude.com/docs/en/statusline) and prints:

  Opus 4.8 hi 💡 · Ctx 8% · ⧗ 24% 16:30 · ⑦ 37% · 🖿 /project_name

  - model display name + version (from display_name, or parsed from model.id)
  - effort level (abbreviated) and thinking on/off (💡 / ✖), next to the model
  - Ctx %  = context window used
  - ⧗ %    = 5-hour rate-limit window used, with 24h-clock reset time
  - ⑦ %    = 7-day rate-limit window used. Grey and uncolored: it is context for
             the 5h number, not something to react to. No reset time -- the
             weekly reset is a fixed weekday/hour, so printing it says nothing.
  Ctx % and 5h % are colored green/yellow/red at <50 / 50-74 / >=75.
  Each rate-limit section is omitted when its block is absent (e.g. early in a
  session, or non Pro/Max). Effort is omitted when the model doesn't support it.
  - 🖿 /<dir> = the current working directory name
"""
import json
import os
import re
import sys
import time
from datetime import datetime

GREEN, YELLOW, RED, RESET = "\033[32m", "\033[33m", "\033[31m", "\033[0m"
GREY = "\033[90m"
EFFORT_ABBR = {"low": "lo", "medium": "med", "high": "hi", "xhigh": "xhi", "max": "max"}


def pct_color(pct):
    return GREEN if pct < 50 else YELLOW if pct < 75 else RED


def colored_pct(value):
    pct = int(round(value or 0))
    return f"{pct_color(pct)}{pct}%{RESET}"


def model_label(model):
    """'Opus 4.8' from display_name, or 'Opus' + version digits from the id."""
    name = model.get("display_name") or "Claude"
    if any(c.isdigit() for c in name):
        return name  # display_name already carries the version
    # short number groups only (skip date stamps like 20251001), first two
    nums = [n for n in re.findall(r"\d+", model.get("id", "")) if len(n) <= 2][:2]
    return f"{name} {'.'.join(nums)}" if nums else name


def dump_rate_limits(data):
    """claude-autoresume: persist the 5h rate-limit block for the tmux monitor.

    Must never raise or block — this runs on every status-line render, and an
    exception here would blank the status bar. Local file write only.
    Skips the write when the five_hour block is absent so a fresh session
    doesn't clobber the last known resets_at with nulls.

    Also carries the seven_day block through for burn_window.py, which needs the
    weekly bucket to tell "this quota expires unused" from "this borrows against
    Sunday". Nested rather than flattened so the 5h keys stay where
    claude-autoresume's tmux monitor expects them.
    """
    try:
        rate_limits = data.get("rate_limits") or {}
        five = rate_limits.get("five_hour")
        if not five:
            return
        week = rate_limits.get("seven_day") or {}
        path = os.environ.get("CCAR_STATE_JSON") or os.path.expanduser(
            "~/.claude/autoresume/state.json"
        )
        state_dir = os.path.dirname(path)
        os.makedirs(state_dir, mode=0o700, exist_ok=True)
        os.chmod(state_dir, 0o700)
        payload = {
            "resets_at": five.get("resets_at"),
            "used_percentage": five.get("used_percentage"),
            "captured_at": int(time.time()),
            "seven_day": {
                "resets_at": week.get("resets_at"),
                "used_percentage": week.get("used_percentage"),
            },
        }
        tmp = path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f)
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    except Exception:
        pass


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        print("")
        return

    dump_rate_limits(data)

    # --- head: model + effort + thinking -------------------------------------
    head = model_label(data.get("model", {}))

    effort = (data.get("effort") or {}).get("level")
    if effort:
        head += " " + EFFORT_ABBR.get(effort, effort)

    thinking_on = (data.get("thinking") or {}).get("enabled", False)
    head += " 💡" if thinking_on else " ✖"

    parts = [head]

    # --- context window ------------------------------------------------------
    ctx = data.get("context_window") or {}
    parts.append(f"Ctx {colored_pct(ctx.get('used_percentage'))}")

    # --- rate-limit windows (either may be absent) ---------------------------
    rate_limits = data.get("rate_limits") or {}
    five = rate_limits.get("five_hour")
    week = rate_limits.get("seven_day") or {}

    if five:
        seg = f"⧗ {colored_pct(five.get('used_percentage'))}"
        resets_at = five.get("resets_at")
        if resets_at:
            seg += " " + datetime.fromtimestamp(resets_at).strftime("%H:%M")
        parts.append(seg)

    # Weekly bucket, grey and uncolored — it frames the 5h number rather than
    # demanding action. Reset time omitted: it's the same weekday/hour forever.
    week_used = week.get("used_percentage")
    if week_used is not None:
        parts.append(f"{GREY}⑦ {int(round(week_used))}%{RESET}")

    # --- working directory ---------------------------------------------------
    cwd = (data.get("workspace") or {}).get("current_dir")
    if cwd:
        # os.path.basename extracts just the final folder name from the full path
        dir_name = os.path.basename(cwd.rstrip("/\\"))
        if dir_name:
            parts.append(f"🖿 /{dir_name}")

    # Join all the parts using the dot separator
    print(" · ".join(parts))


if __name__ == "__main__":
    main()
