"""Grok and Claude quota reads, and the routing decision."""

import importlib.util
import os
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from server import events

# self-improve proposes re-tuning these from the report
GROK_OUT_PCT = 98
BURN_WITHIN_H = 24
BURN_MAX_PCT = 80
CONSERVE_PCT = 85
CONSERVE_MIN_H = 36
# Credit running out mid-flight strands every running job half-done, so the cap on
# concurrent jobs tightens as the weekly credit is used: (used % below, jobs allowed).
CONCURRENCY_TIERS = ((70, 3), (85, 2))
MIN_CONCURRENT = 1

BILLING_MSG = "billing: fetched credits config"
DEFAULT_STATUSLINE = "~/.grok/statusline.py"
DEFAULT_BURN = "~/.claude/skills/self-improve/scripts/burn_window.py"
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

_load_seq = 0
_load_lock = threading.Lock()


def _load(path):
    global _load_seq
    with _load_lock:
        _load_seq += 1
        name = f"_grok_mcp_dyn_{_load_seq}"
    path = os.path.expanduser(path)
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _statusline_path():
    return os.environ.get("GROK_MCP_GROK_STATUSLINE", DEFAULT_STATUSLINE)


def _burn_path():
    return os.environ.get("GROK_MCP_BURN_WINDOW", DEFAULT_BURN)


def _grok_home():
    return Path(os.environ.get("GROK_HOME") or "~/.grok").expanduser()


def _billing_log():
    return _grok_home() / "logs" / "unified.jsonl"


def _parse_dt(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _in_period(start, end):
    now = datetime.now(timezone.utc)
    end_dt = _parse_dt(end)
    if end_dt is None or now >= end_dt:
        return False
    start_dt = _parse_dt(start)
    if start_dt is not None and now < start_dt:
        return False
    return True


def _period_ended(end):
    end_dt = _parse_dt(end)
    return end_dt is not None and datetime.now(timezone.utc) >= end_dt


def hours_until(end):
    end_dt = _parse_dt(end)
    if end_dt is None:
        return None
    return (end_dt - datetime.now(timezone.utc)).total_seconds() / 3600.0


def _coerce_pct(pct, start, end):
    """A null credit inside the current period is 0: the window has started and nothing is used."""
    if isinstance(pct, bool):
        return None
    if isinstance(pct, (int, float)):
        return pct
    if pct is None and _in_period(start, end):
        return 0
    return None


def _newest_billing():
    path = _billing_log()
    if not path.exists():
        return None
    newest = None
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in lines:
        if BILLING_MSG not in line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("msg") != BILLING_MSG:
            continue
        cfg = (obj.get("ctx") or {}).get("config") or {}
        period = cfg.get("currentPeriod") or {}
        newest = {
            "pid": obj.get("pid"),
            "pct": cfg.get("creditUsagePercent"),
            "end": period.get("end"),
            "start": period.get("start"),
        }
    return newest


def _grok_out_path():
    return events.state_root() / "grok_out"


def note_grok_out(period_end=None):
    """Lock Grok out until the credit period ends: every call before then fails."""
    if _period_ended(period_end) or _parse_dt(period_end) is None:
        newest = _newest_billing()
        period_end = newest.get("end") if newest else None
    until = period_end if _parse_dt(period_end) and not _period_ended(period_end) else None
    events.write_private(_grok_out_path(), json.dumps({"ts": time.time(), "until": until}))
    events.log("grok_lockout", until=until)


def _grok_out_until():
    """Reset time of a recorded lockout, or None when there is none or its reset is unknown."""
    try:
        data = json.loads(_grok_out_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    until = data.get("until") if isinstance(data, dict) else None
    return until if _parse_dt(until) else None


def _grok_out_active():
    return _grok_out_path().exists()


def _locked_out_reading():
    """A 100% reading that costs no billing read, while a recorded lockout's reset is still ahead."""
    until = _grok_out_until()
    if until is None:
        return None
    if _period_ended(until):
        _clear_grok_out()
        return None
    return {"pct": 100, "period_end": until, "fresh": False, "age_s": 0}


def _clear_grok_out():
    try:
        _grok_out_path().unlink()
    except FileNotFoundError:
        pass


def _cache_age(mod):
    path = getattr(mod, "BILLING_CACHE", None)
    if not path:
        return 0
    try:
        return max(0, int(time.time() - os.stat(path).st_mtime))
    except OSError:
        return 0


def _write_fresh_cache(pct):
    try:
        mod = _load(_statusline_path())
    except Exception:
        return
    path = _billing_log()
    try:
        st = path.stat()
    except OSError:
        return
    mod._write_cache({"size": st.st_size, "mtime_ns": st.st_mtime_ns, "pct": pct})


OWN_BILLING_KEEP = 500
_own_billing_lock = threading.Lock()


def _own_billing_path():
    return events.state_root() / "own-billing-pids"


def own_billing_pids():
    """Pids of the server's own quota reads, whose billing lines are not user Grok use."""
    try:
        return {int(p) for p in _own_billing_path().read_text(encoding="utf-8").split()}
    except (OSError, ValueError):
        return set()


def _record_own_billing(pids):
    pids = [p for p in pids if isinstance(p, int)]
    if not pids:
        return
    with _own_billing_lock:
        try:
            kept = _own_billing_path().read_text(encoding="utf-8").split()
        except OSError:
            kept = []
        kept = (kept + [str(p) for p in pids])[-OWN_BILLING_KEEP:]
        events.write_private(_own_billing_path(), " ".join(kept))


def _read_fresh():
    from server import doctor
    home = _grok_home()
    doctor.GROK_HOME = home
    doctor.BILLING_LOG = home / "logs" / "unified.jsonl"
    tail = doctor.BillingTail()
    t0 = time.monotonic()
    pids = doctor.tui_startup(tail=tail)
    age = max(0, int(time.monotonic() - t0))
    lines = tail.fresh()
    if pids:
        lines = [line for line in lines if line.get("pid") in pids]
        _record_own_billing({line.get("pid") for line in lines})
    if not lines:
        return None
    rec = lines[-1]
    pct = _coerce_pct(rec.get("pct"), None, rec.get("end"))
    if pct is None:
        return None
    _write_fresh_cache(pct)
    return {"pct": pct, "period_end": rec.get("end"), "fresh": True, "age_s": age}


def _read_cached(reason):
    try:
        mod = _load(_statusline_path())
        pct = mod.weekly_used_pct()
        age = _cache_age(mod)
    except Exception:
        events.log("quota_fallback", reason="statusline load failed")
        if _grok_out_active():
            return {"pct": 100, "period_end": None, "fresh": False, "age_s": 0}
        return "unknown"
    newest = _newest_billing()
    end = newest.get("end") if newest else None
    start = newest.get("start") if newest else None
    if newest is not None and newest.get("pct") is None and _in_period(start, end):
        pct = 0
    elif _period_ended(end):
        pct = None
    if _grok_out_active():
        pct = 100
        reason = "grok_out"
    if pct is None or isinstance(pct, bool):
        events.log("quota_fallback", reason="period ended" if _period_ended(end) else reason)
        return "unknown"
    events.log("quota_fallback", reason=reason)
    return {"pct": pct, "period_end": end, "fresh": False, "age_s": age}


def grok_read(refresh):
    """Return {pct, period_end, fresh, age_s}, or 'unknown'. Never raises."""
    locked = _locked_out_reading()
    if locked is not None:
        return locked
    fresh_off = os.environ.get("GROK_MCP_FRESH") == "off"
    if refresh and not fresh_off:
        try:
            got = _read_fresh()
        except Exception:
            got = None
        if got is not None:
            if got["pct"] >= 100:
                note_grok_out(got.get("period_end"))
            else:
                _clear_grok_out()
            return got
        reason = "no fresh billing line"
    else:
        reason = "fresh disabled"
    try:
        return _read_cached(reason)
    except Exception:
        events.log("quota_fallback", reason="cached read failed")
        return "unknown"


def claude_read():
    empty = {
        "five_pct": None,
        "five_min_left": None,
        "week_pct": None,
        "week_h_left": None,
        "burn_state": "unknown",
        "captured_age_s": None,
    }
    try:
        mod = _load(_burn_path())
        ev = mod.evaluate()
    except Exception:
        return empty
    if not isinstance(ev, dict):
        return empty
    out = dict(empty)
    out["five_pct"] = ev.get("used_percentage")
    out["five_min_left"] = ev.get("minutes_left")
    out["week_pct"] = ev.get("week_used_percentage")
    out["week_h_left"] = ev.get("week_hours_left")
    out["burn_state"] = ev.get("state", "unknown")
    out["captured_age_s"] = ev.get("captured_age_s")
    return out


def route(g, c):
    """Return (mode, reason). Order is load-bearing."""
    pct = hours = None
    if isinstance(g, dict):
        raw = g.get("pct")
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            pct = raw
            hours = hours_until(g.get("period_end"))
    if pct is None:
        return "claude-only", "grok quota unknown"
    if pct >= GROK_OUT_PCT:
        return "claude-only", (f"grok {_num(pct)}% >= {GROK_OUT_PCT}, "
                               f"out until reset {_fmt_reset(g.get('period_end'))}")
    burn = c.get("burn_state") if isinstance(c, dict) else None
    if burn == "open":
        return "claude-only", "claude burn window open"
    if hours is not None and hours <= BURN_WITHIN_H and pct <= BURN_MAX_PCT:
        return "burn", f"resets in {hours:.0f}h at {_num(pct)}%"
    if hours is not None and pct >= CONSERVE_PCT and hours > CONSERVE_MIN_H:
        return "conserve", f"{_num(pct)}% used with {hours:.0f}h until reset"
    return "normal", "under the conserve line"


def max_concurrent(g):
    pct = g.get("pct") if isinstance(g, dict) else None
    if not isinstance(pct, (int, float)) or isinstance(pct, bool):
        return MIN_CONCURRENT
    for below, allowed in CONCURRENCY_TIERS:
        if pct < below:
            return allowed
    return MIN_CONCURRENT


def _num(pct):
    if isinstance(pct, float) and pct.is_integer():
        return str(int(pct))
    if isinstance(pct, int):
        return str(pct)
    return str(pct)


def _fmt_reset(end):
    dt = _parse_dt(end)
    if dt is None:
        return "unknown"
    return f"{_MONTHS[dt.month - 1]} {dt.day:02d} {dt.hour:02d}:{dt.minute:02d}Z"


def _fmt_minutes(minutes):
    total = max(0, int(round(minutes)))
    hours, mins = divmod(total, 60)
    if hours:
        return f"{hours}h{mins:02d}m"
    return f"{mins}m"


def _fmt_five(c):
    pct, mins = c.get("five_pct"), c.get("five_min_left")
    if pct is None:
        return "?"
    shown = f"{_num(pct)}%"
    if isinstance(mins, (int, float)) and not isinstance(mins, bool):
        shown += f" ({_fmt_minutes(mins)} left)"
    return shown


def _fmt_week(c):
    pct, hours = c.get("week_pct"), c.get("week_h_left")
    if pct is None:
        return "?"
    shown = f"{_num(pct)}%"
    if isinstance(hours, (int, float)) and not isinstance(hours, bool):
        shown += f" ({hours / 24:.1f}d left)"
    return shown


def _fmt_grok(g):
    if not isinstance(g, dict) or g.get("pct") is None:
        return "grok unknown"
    kind = "fresh" if g.get("fresh") else "cached"
    age = g.get("age_s")
    age_s = f"{int(round(age))}s" if isinstance(age, (int, float)) and not isinstance(age, bool) else "?"
    return (f"grok {_num(g['pct'])}% ({kind} {age_s} · resets {_fmt_reset(g.get('period_end'))})")


def tool_text(refresh=True):
    g = grok_read(refresh)
    c = claude_read()
    mode, reason = route(g, c)
    return (f"{_fmt_grok(g)} · claude 5h {_fmt_five(c)} · 7d {_fmt_week(c)} · route {mode} ({reason})"
            f" · max {max_concurrent(g)} concurrent")
