#!/usr/bin/env python3
"""Grok status line.

  Grok 4.6 xhi · Ctx 8% · ⑦ 58% · $0.12 · 🖿 /project_name

Stdin has no rate-limit block. ⑦ is the SuperGrok weekly credit window
(`creditUsagePercent` from grok's own billing fetch, tailed out of
~/.grok/logs/unified.jsonl). Colored like Ctx: Grok has no 5h bucket, so
this is the quota to watch. Omitted until a billing line exists.
"""
import json
import os
import re
import sys

GREEN, YELLOW, RED, RESET = "\033[32m", "\033[33m", "\033[31m", "\033[0m"
EFFORT_ABBR = {"low": "lo", "medium": "med", "high": "hi", "xhigh": "xhi", "max": "max"}

GROK_HOME = os.environ.get("GROK_HOME") or os.path.expanduser("~/.grok")
BILLING_LOG = os.path.join(GROK_HOME, "logs", "unified.jsonl")
BILLING_CACHE = os.path.join(GROK_HOME, "statusline-quota.json")
BILLING_MARKER = "billing: fetched credits config"
TAIL_BYTES = 262144


def pct_color(pct):
    return GREEN if pct < 50 else YELLOW if pct < 75 else RED


def colored_pct(value):
    pct = int(round(value or 0))
    return f"{pct_color(pct)}{pct}%{RESET}"


def model_label(model):
    name = model.get("display_name") or "Grok"
    if any(c.isdigit() for c in name):
        return name
    nums = [n for n in re.findall(r"\d+", model.get("id", "")) if len(n) <= 2][:2]
    return f"{name} {'.'.join(nums)}" if nums else name


def format_cost(usd):
    if usd is None or usd < 0.005:
        return None
    if usd < 10:
        return f"${usd:.2f}"
    return f"${usd:.0f}"


def _read_cache():
    try:
        with open(BILLING_CACHE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_cache(payload):
    try:
        tmp = BILLING_CACHE + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f)
        os.replace(tmp, BILLING_CACHE)
        os.chmod(BILLING_CACHE, 0o600)
    except Exception:
        pass


def _pct_from_line(line):
    try:
        obj = json.loads(line)
    except Exception:
        return None
    val = ((obj.get("ctx") or {}).get("config") or {}).get("creditUsagePercent")
    return val if isinstance(val, (int, float)) else None


def weekly_used_pct():
    """Latest weekly creditUsagePercent, or None.

    Must not raise or block: this runs on every status-line paint.
    """
    cache = _read_cache()
    try:
        st = os.stat(BILLING_LOG)
    except OSError:
        pct = cache.get("pct")
        return pct if isinstance(pct, (int, float)) else None

    cached_size = cache.get("size")
    if cached_size == st.st_size and cache.get("mtime_ns") == st.st_mtime_ns:
        pct = cache.get("pct")
        return pct if isinstance(pct, (int, float)) else None

    pct = cache.get("pct") if isinstance(cache.get("pct"), (int, float)) else None
    if isinstance(cached_size, int) and 0 <= cached_size <= st.st_size:
        start = cached_size
    else:
        start = max(0, st.st_size - TAIL_BYTES)

    try:
        with open(BILLING_LOG, encoding="utf-8", errors="replace") as f:
            if start:
                f.seek(start)
                if start > 0:
                    f.readline()
            for line in f:
                if BILLING_MARKER not in line:
                    continue
                found = _pct_from_line(line)
                if found is not None:
                    pct = found
    except Exception:
        return pct if isinstance(pct, (int, float)) else None

    _write_cache({"size": st.st_size, "mtime_ns": st.st_mtime_ns, "pct": pct})
    return pct if isinstance(pct, (int, float)) else None


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        print("")
        return

    model = data.get("model") or {}
    head = model_label(model) if model else None
    effort = (data.get("effort") or {}).get("level")
    if head and effort:
        head += " " + EFFORT_ABBR.get(effort, effort)

    parts = [head] if head else []

    used = (data.get("context_window") or {}).get("used_percentage")
    if used is not None:
        parts.append(f"Ctx {colored_pct(used)}")

    week = weekly_used_pct()
    if week is not None:
        parts.append(f"⑦ {colored_pct(week)}")

    cost = format_cost((data.get("cost") or {}).get("total_cost_usd"))
    if cost:
        parts.append(cost)

    cwd = (data.get("workspace") or {}).get("current_dir") or data.get("cwd")
    if cwd:
        dir_name = os.path.basename(str(cwd).rstrip("/\\"))
        if dir_name:
            parts.append(f"🖿 /{dir_name}")

    print(" · ".join(parts) if parts else "")


if __name__ == "__main__":
    main()
