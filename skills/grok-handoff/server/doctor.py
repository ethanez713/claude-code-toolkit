"""Prove on this box every Grok behaviour the grok MCP server relies on.

  doctor.py --dry-run   print the rendered sandbox.toml and the probe list
  doctor.py --install   write the canary and ~/.grok/sandbox.toml
  doctor.py --quota     one Grok credit read, no model call
  doctor.py             run the probes (prints PASS|FAIL lines only, never content)
"""

import argparse
import ctypes
import difflib
import json
import os
import queue
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rules  # noqa: E402

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "sandbox.toml.tmpl"
MARKER = "# managed-by: grok-handoff doctor.py"
GROK_HOME = Path(os.environ.get("GROK_HOME", "~/.grok")).expanduser()
SANDBOX_TOML = GROK_HOME / "sandbox.toml"
BILLING_LOG = GROK_HOME / "logs" / "unified.jsonl"
BILLING_MSG = "billing: fetched credits config"
STATE = Path("~/.local/state/grok-mcp").expanduser()
CANARY = Path(rules.CANARY).expanduser()
WRITE_PROBE = Path("~/grok-write-probe").expanduser()
TIMEOUT = 180
ACP_CANDIDATES = ["x.ai/billing", "x.ai/billing/config", "x.ai/billing_config",
                  "x.ai/billing/credits", "x.ai/credits", "x.ai/usage"]
SYS_LANDLOCK_CREATE_RULESET = 444

PROBES = [
    ("P1", "prereqs: bwrap runs and Landlock reports an ABI"),
    ("P2", "auth + headless JSON fields"),
    ("P3e", "claude-edit: private path masked, writes home and cwd, network"),
    ("P3c", "claude-consult: private path masked, cwd read-only, network"),
    ("P4", "--resume carries context; a fresh session does not"),
    ("P5", "--rules overrides a project CLAUDE.md that demands commits"),
    ("P6", "quota read with no model call (ACP billing, TUI startup, headless)"),
    ("P7", "a job worktree loads project rules at startup with --trust"),
    ("P8", "an `ask` rule under --always-approve does not hang headless"),
]


def grok_bin():
    return shutil.which("grok") or str(Path("~/.local/bin/grok").expanduser())


def grok_env():
    return {**os.environ, "GROK_MEMORY": "0", "GROK_CLAUDE_MCPS_ENABLED": "false"}


def render_sandbox():
    home = str(Path.home())
    text = TEMPLATE.read_text().replace('"~', '"' + home)
    deny = ", ".join(json.dumps(p) for p in rules.private_paths())
    text = text.replace("[DENY]", "[" + deny + "]")
    return re.sub(r'\[([^\]]*)\]', _drop_missing_paths, text)


def _drop_missing_paths(match):
    items = re.findall(r'"((?:[^"\\]|\\.)*)"', match.group(1))
    if not items:
        return match.group(0)
    kept = [json.dumps(p) for p in items if not p.startswith("/") or os.path.exists(p)]
    return "[" + ", ".join(kept) + "]"


def dry_run():
    print(render_sandbox(), end="")
    print()
    for pid, fact in PROBES:
        print(f"{pid}  {fact}")


def install():
    STATE.mkdir(parents=True, exist_ok=True)
    os.chmod(STATE, 0o700)
    if not CANARY.exists():
        fd = os.open(CANARY, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_hex(16) + "\n")
    os.chmod(CANARY, 0o600)
    new = render_sandbox()
    if SANDBOX_TOML.exists():
        old = SANDBOX_TOML.read_text()
        if MARKER not in old:
            sys.stdout.writelines(difflib.unified_diff(
                old.splitlines(True), new.splitlines(True),
                str(SANDBOX_TOML), "rendered"))
            print(f"\n{SANDBOX_TOML} exists without the managed-by marker; not overwriting.")
            return 1
    tmp = SANDBOX_TOML.with_suffix(".toml.tmp")
    tmp.write_text(new)
    os.replace(tmp, SANDBOX_TOML)
    print(f"installed {SANDBOX_TOML} and canary")
    return 0


def utc_second(epoch):
    """The `ts` prefix format of unified.jsonl, to second resolution."""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(epoch))


def logged_since(rec, since):
    ts = rec.get("ts")
    return not isinstance(ts, str) or ts[:19] >= since


class BillingTail:
    """New `billing: fetched credits config` lines since construction.

    Grok trims unified.jsonl in place, so the byte offset only narrows the read; the `ts`
    window decides which lines are new.
    """

    def __init__(self):
        self.offset = BILLING_LOG.stat().st_size if BILLING_LOG.exists() else 0
        self.since = utc_second(time.time())

    def fresh(self):
        if not BILLING_LOG.exists():
            return []
        size = BILLING_LOG.stat().st_size
        start = self.offset if size >= self.offset else 0
        with open(BILLING_LOG, "rb") as f:
            f.seek(start)
            chunk = f.read()
        lines = []
        for raw in chunk.splitlines():
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            if rec.get("msg") != BILLING_MSG or not logged_since(rec, self.since):
                continue
            cfg = (rec.get("ctx") or {}).get("config") or {}
            lines.append({
                "pid": rec.get("pid"),
                "pct": cfg.get("creditUsagePercent"),
                "end": (cfg.get("currentPeriod") or {}).get("end"),
            })
        return lines


def run_grok(args, cwd, timeout=TIMEOUT, model_call=True):
    """Returns (rc, stdout, pid); rc is None on timeout."""
    cmd = [grok_bin(), *args] + (["--effort", "low"] if model_call else [])
    proc = subprocess.Popen(cmd, cwd=cwd, env=grok_env(), stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True, start_new_session=True)
    try:
        out, _ = proc.communicate(timeout=timeout)
        return proc.returncode, out, proc.pid
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        out, _ = proc.communicate()
        return None, out, proc.pid


def last_json(out):
    """Headless --output-format json pretty-prints one object across many lines."""
    decoder = json.JSONDecoder()
    for m in reversed(list(re.finditer(r"(?m)^\{", out))):
        try:
            return decoder.raw_decode(out, m.start())[0]
        except ValueError:
            continue
    return None


def stream_events(out):
    events = []
    for line in out.splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    return events


class Stream:
    def __init__(self, out):
        events = stream_events(out)
        self.calls = {e.get("toolCallId"): e for e in events if e.get("type") == "tool_call"}
        self.updates = {}
        for e in events:
            if e.get("type") == "tool_call_update":
                self.updates.setdefault(e.get("toolCallId"), []).append(e)
        self.text = "".join(e.get("data", "") for e in events if e.get("type") == "text")

    def call_for(self, needle):
        for cid, call in self.calls.items():
            if needle in json.dumps(call.get("rawInput")):
                return cid
        return None

    def output_of(self, cid):
        return "\n".join(json.dumps(u.get("rawOutput")) + json.dumps(u.get("content"))
                         for u in self.updates.get(cid, []))

    def status_of(self, cid):
        statuses = [u.get("status") for u in self.updates.get(cid, []) if u.get("status")]
        return statuses[-1] if statuses else None

    def all_output(self):
        return "\n".join(self.output_of(cid) for cid in self.updates)


def result_line(ok, pid, fact):
    return f"{'PASS' if ok else 'FAIL'} {pid} {fact}"


def make_scratch_repo(pong):
    repo = Path(tempfile.mkdtemp(prefix="grok-doctor-"))
    (repo / "CLAUDE.md").write_text(
        "# Scratch repo\n\nCommit after every change.\n\n"
        f"When asked ping-rules, reply exactly {pong}\n")
    git = ["git", "-C", str(repo), "-c", "user.name=doctor", "-c", "user.email=doctor@localhost"]
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(git + ["add", "CLAUDE.md"], check=True)
    subprocess.run(git + ["commit", "-qm", "init"], check=True)
    return repo


def commit_count(repo):
    out = subprocess.run(["git", "-C", str(repo), "rev-list", "--count", "HEAD"],
                         capture_output=True, text=True)
    return out.stdout.strip()


def probe_prereqs():
    try:
        bwrap = subprocess.run(["bwrap", "--version"], capture_output=True, text=True).returncode == 0
    except FileNotFoundError:
        bwrap = False
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    abi = libc.syscall(SYS_LANDLOCK_CREATE_RULESET, None, ctypes.c_size_t(0), ctypes.c_uint32(1))
    return result_line(bwrap and abi >= 1, "P1", f"bwrap={'ok' if bwrap else 'missing'} landlock_abi={abi}")


def probe_auth(ctx):
    tail = BillingTail()
    rc, out, pid = run_grok(["-p", "Reply with exactly: OK", "--output-format", "json",
                             "--max-turns", "2"], ctx["repo"])
    ctx["p2_billing"] = (pid, tail.fresh())
    data = last_json(out) or {}
    if rc is None:
        return result_line(False, "P2", "timed out")
    if data.get("type") == "error":
        return result_line(False, "P2", f"error: {str(data.get('message'))[:160]}")
    missing = [k for k in ("text", "sessionId", "usage", "num_turns") if k not in data]
    ok = rc == 0 and not missing
    ctx["auth_ok"] = rc == 0
    detail = f"rc={rc}" + (f" missing={','.join(missing)}" if missing else " fields ok")
    if missing:
        detail += (f" keys={','.join(sorted(data))[:120]}" if data
                   else f" unparseable stdout ({len(out)} bytes, {len(out.splitlines())} lines)")
    return result_line(ok, "P2", detail)


def network_result(stream, cid):
    return "on" if cid and "HTTP/" in stream.output_of(cid) else "off"


def probe_sandbox_edit(ctx):
    repo, canary = ctx["repo"], ctx["canary"]
    WRITE_PROBE.unlink(missing_ok=True)
    prompt = (f"Use your shell tool to run each of these commands separately and show what they print: "
              f"`cat {CANARY}` then `touch ~/grok-write-probe` then `curl -sI https://example.com`. "
              "Then create the file inside.txt in the current directory containing ok.")
    rc, out, _ = run_grok(["-p", prompt, "--sandbox", "claude-edit", "--always-approve",
                           "--output-format", "streaming-json", "--max-turns", "8"], repo)
    s = Stream(out)
    attempted = {k: s.call_for(k) for k in (str(CANARY.name), "grok-write-probe", "example.com")}
    not_tried = [k for k, v in attempted.items() if not v]
    leaked = canary in s.all_output() or canary in s.text
    wrote_home = WRITE_PROBE.exists()
    wrote_cwd = (repo / "inside.txt").exists()
    WRITE_PROBE.unlink(missing_ok=True)
    ok = rc is not None and not not_tried and not leaked and wrote_home and wrote_cwd
    detail = (f"masked={not leaked} home_write={wrote_home} cwd_write={wrote_cwd} "
              f"edit network: {network_result(s, attempted['example.com'])}")
    if not_tried:
        detail += f" not_attempted={','.join(not_tried)}"
    if rc is None:
        detail += " timed out"
    return result_line(ok, "P3e", detail)


def probe_sandbox_consult(ctx):
    canary = ctx["canary"]
    probe_dir = STATE / "probe"
    probe_dir.mkdir(parents=True, exist_ok=True)
    try:
        prompt = (f"Use your shell tool to run each of these commands separately and show what they print: "
                  f"`cat {CANARY}` then `touch inside2.txt` then `curl -sI https://example.com`.")
        rc, out, _ = run_grok(["-p", prompt, "--sandbox", "claude-consult", "--always-approve",
                               "--output-format", "streaming-json", "--max-turns", "8"], probe_dir)
        s = Stream(out)
        attempted = {k: s.call_for(k) for k in (str(CANARY.name), "inside2.txt", "example.com")}
        not_tried = [k for k, v in attempted.items() if not v]
        leaked = canary in s.all_output() or canary in s.text
        wrote = (probe_dir / "inside2.txt").exists()
    finally:
        shutil.rmtree(probe_dir, ignore_errors=True)
    ok = rc is not None and not not_tried and not leaked and not wrote
    detail = (f"masked={not leaked} cwd_write={wrote} "
              f"consult network: {network_result(s, attempted['example.com'])}")
    if not_tried:
        detail += f" not_attempted={','.join(not_tried)}"
    if rc is None:
        detail += " timed out"
    return result_line(ok, "P3c", detail)


def probe_resume(ctx):
    word = secrets.choice(["heron", "quartz", "ember", "lantern", "saffron"]) + str(secrets.randbelow(900) + 100)
    base = ["--output-format", "json", "--max-turns", "2"]
    rc1, out1, _ = run_grok(["-p", f"The codeword is {word}. Reply OK."] + base, ctx["repo"])
    session = (last_json(out1) or {}).get("sessionId")
    if not session:
        return result_line(False, "P4", f"turn 1 gave no sessionId (rc={rc1})")
    question = "What was the codeword? Reply with just the word, or 'unknown'."
    _, out2, _ = run_grok(["-p", question, "--resume", session] + base, ctx["repo"])
    _, out3, _ = run_grok(["-p", question] + base, ctx["repo"])
    resumed = word in str((last_json(out2) or {}).get("text", ""))
    control = word in str((last_json(out3) or {}).get("text", ""))
    return result_line(resumed and not control, "P4", f"resumed_recall={resumed} control_recall={control}")


def probe_rules_override(ctx):
    repo = ctx["repo"]
    before = commit_count(repo)
    prompt = "Create the file inside3.txt in the current directory containing ok. Then: ping-rules"
    rc, out, _ = run_grok(["-p", prompt, "--sandbox", "claude-edit", "--trust", "--always-approve",
                           "--rules", rules.text("edit"), "--output-format", "json",
                           "--max-turns", "8"], repo)
    text = str((last_json(out) or {}).get("text", ""))
    committed = commit_count(repo) != before
    exists = (repo / "inside3.txt").exists()
    pong = ctx["pong"] in text
    ok = rc is not None and not committed and exists and pong
    return result_line(ok, "P5", f"committed={committed} file={exists} pong={pong}"
                       + ("" if rc is not None else " timed out"))


class AcpClient:
    def __init__(self):
        self.proc = subprocess.Popen([grok_bin(), "agent", "stdio"], env=grok_env(),
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True, start_new_session=True)
        self.lines = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()
        self.next_id = 0

    def _pump(self):
        for line in self.proc.stdout:
            self.lines.put(line)

    def request(self, method, params, timeout=20):
        self.next_id += 1
        rid = self.next_id
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method,
                                          "params": params}) + "\n")
        self.proc.stdin.flush()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                msg = json.loads(self.lines.get(timeout=max(0.1, deadline - time.monotonic())))
            except (queue.Empty, ValueError):
                continue
            if msg.get("id") == rid and "method" not in msg:
                return msg
        return None

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=10)
        except (subprocess.TimeoutExpired, OSError):
            os.killpg(self.proc.pid, signal.SIGKILL)


def acp_initialize(client):
    return client.request("initialize", {
        "protocolVersion": 1,
        "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False},
                               "terminal": False},
    })


def find_key(obj, key):
    if isinstance(obj, dict):
        if key in obj:
            return True, obj[key]
        for v in obj.values():
            found, val = find_key(v, key)
            if found:
                return found, val
    elif isinstance(obj, list):
        for v in obj:
            found, val = find_key(v, key)
            if found:
                return found, val
    return False, None


def advertised_methods(obj):
    names = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.startswith("x.ai/"):
                names.add(k)
            names |= advertised_methods(v)
    elif isinstance(obj, list):
        for v in obj:
            names |= advertised_methods(v)
    elif isinstance(obj, str) and re.fullmatch(r"x\.ai/[\w/.-]+", obj):
        names.add(obj)
    return names


def acp_billing_read(methods):
    """Returns (winning method or None, advertised names, pct, end)."""
    client = AcpClient()
    try:
        init = acp_initialize(client)
        advertised = sorted(advertised_methods(init)) if init else []
        caps = client.request("x.ai/capabilities", {}) if "x.ai/capabilities" in advertised else None
        if caps and "result" in caps:
            advertised = sorted(set(advertised) | advertised_methods(caps["result"]))
        wanted = methods + [m for m in advertised
                            if re.search("billing|credit", m) and m not in methods]
        for method in wanted:
            resp = client.request(method, {})
            if not resp or "result" not in resp:
                continue
            found, pct = find_key(resp["result"], "creditUsagePercent")
            if found:
                _, period = find_key(resp["result"], "currentPeriod")
                end = period.get("end") if isinstance(period, dict) else None
                return method, advertised, pct, end
        return None, advertised, None, None
    finally:
        client.close()


def descendants(root):
    parents = {}
    for stat in Path("/proc").glob("[0-9]*/stat"):
        try:
            text = stat.read_text()
        except OSError:
            continue
        fields = text[text.rfind(")") + 2:].split()
        parents.setdefault(int(fields[1]), []).append(int(stat.parent.name))
    found, stack = set(), [root]
    while stack:
        pid = stack.pop()
        found.add(pid)
        stack.extend(p for p in parents.get(pid, ()) if p not in found)
    return found


def tui_startup(extra=(), tail=None, limit=20):
    """Start the TUI under a pty; stop once `tail` sees a billing line from it.

    Returns the pids of the read's process tree, so its billing lines can be told apart
    from the user's own Grok use.
    """
    cmd = " ".join([grok_bin(), *extra])
    proc = subprocess.Popen(["script", "-qfec", cmd, "/dev/null"], env=grok_env(),
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)
    pids = {proc.pid}
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline and proc.poll() is None:
        pids |= descendants(proc.pid)
        if tail and any(line["pid"] in pids for line in tail.fresh()):
            break
        time.sleep(0.25)
    if proc.poll() is None:
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
    return pids


def fmt_pct(pct):
    return "null" if pct is None else str(pct)


def probe_quota(ctx):
    lines = []
    tail, t0 = BillingTail(), time.monotonic()
    method, advertised, _, _ = acp_billing_read(ACP_CANDIDATES)
    fresh_a, dt_a = tail.fresh(), time.monotonic() - t0
    a_ok = bool(method) and bool(fresh_a)
    lines.append(f"  P6a acp: advertised={','.join(advertised) or 'none'}")
    lines.append(f"  P6a acp: method={method or 'none'} fresh_lines={len(fresh_a)} "
                 f"{dt_a:.1f}s pct={fmt_pct(fresh_a[-1]['pct']) if fresh_a else '-'}")

    tail, t0 = BillingTail(), time.monotonic()
    tui_startup(tail=tail)
    fresh_b, dt_b = tail.fresh(), time.monotonic() - t0
    b_ok = bool(fresh_b)
    lines.append(f"  P6b tui: fresh_lines={len(fresh_b)} {dt_b:.1f}s "
                 f"pct={fmt_pct(fresh_b[-1]['pct']) if fresh_b else '-'}")

    p2_pid, p2_lines = ctx.get("p2_billing", (None, []))
    pids = sorted({str(l["pid"]) for l in p2_lines})
    lines.append(f"  P6c headless: fresh_lines={len(p2_lines)} pids={','.join(pids) or '-'} "
                 f"child_pid={p2_pid}")

    if not a_ok and not b_ok:
        dbg = Path(tempfile.mkstemp(prefix="grok-debug-", dir=STATE if STATE.exists() else None)[1])
        try:
            tui_startup(["--debug-file", str(dbg)])
            hits = set()
            for line in dbg.read_text(errors="replace").splitlines():
                if re.search("billing|credit", line, re.I):
                    for host, path in re.findall(r"https?://([^/\s\"']+)(/[^\s\"'?]*)", line):
                        hits.add(host + path)
            lines.append(f"  P6 debug: {', '.join(sorted(hits)[:5]) or 'no billing requests'}")
        finally:
            dbg.unlink(missing_ok=True)
    winner = "a" if a_ok else "b" if b_ok else "c" if p2_lines else "none"
    head = result_line(a_ok or b_ok, "P6", f"winner={winner}")
    return "\n".join([head] + lines)


def probe_worktree_rules(ctx):
    repo = ctx["repo"]
    wt = repo / ".grok-jobs" / "t"
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", str(wt)],
                   check=True, capture_output=True)
    rc, out, _ = run_grok(["-p", "ping-rules", "--sandbox", "claude-edit", "--trust",
                           "--output-format", "json", "--max-turns", "1"], wt)
    pong = ctx["pong"] in str((last_json(out) or {}).get("text", ""))
    line = result_line(pong, "P7", f"pong={pong}" + ("" if rc is not None else " timed out"))
    if pong:
        return line
    rc, out, _ = run_grok(["inspect", "--json"], wt, timeout=60, model_call=False)
    extra = []
    try:
        extra = _inspect_fields(json.loads(out))
    except ValueError:
        extra = [f"inspect rc={rc}, unparseable"]
    return "\n".join([line] + [f"  P7 inspect: {e}" for e in extra[:6]])


def _inspect_fields(obj, prefix=""):
    found = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = f"{prefix}.{k}" if prefix else k
            if re.search("trust|instruction|rules|claude_md|agents_md", k, re.I) and not isinstance(v, (dict, list)):
                found.append(f"{key}={v}")
            elif re.search("instruction|rules", k, re.I) and isinstance(v, list):
                found.append(f"{key}={[x if isinstance(x, str) else x.get('path') for x in v][:4]}")
            else:
                found += _inspect_fields(v, key)
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:10]):
            found += _inspect_fields(v, f"{prefix}[{i}]")
    return found


def probe_ask_rules(ctx):
    prompt = "Use your shell tool to run exactly this command and report what happened: python3 -m pip install --dry-run six"
    rc, out, _ = run_grok(["-p", prompt, "--sandbox", "claude-edit", "--always-approve",
                           "--output-format", "streaming-json", "--max-turns", "4"], ctx["repo"])
    if rc is None:
        return result_line(False, "P8", "hung (180s timeout)")
    s = Stream(out)
    cid = s.call_for("pip install")
    if not cid:
        return result_line(True, "P8", "not attempted")
    status = s.status_of(cid)
    if status == "completed":
        return result_line(True, "P8", "ran")
    first = next((l for l in s.output_of(cid).splitlines() if l.strip() not in ("null", "[]")), "")
    return result_line(True, "P8", f"refused status={status}: {first[:120]}")


def run_probes():
    if MARKER not in (SANDBOX_TOML.read_text() if SANDBOX_TOML.exists() else "") or not CANARY.exists():
        print("FAIL setup run `doctor.py --install` first")
        return 1
    pong = f"pong-{secrets.token_hex(4)}"
    ctx = {"pong": pong, "canary": CANARY.read_text().strip(), "auth_ok": False}
    ctx["repo"] = make_scratch_repo(pong)
    failed = False
    try:
        steps = [probe_prereqs, probe_auth, probe_sandbox_edit, probe_sandbox_consult,
                 probe_resume, probe_rules_override, probe_quota, probe_worktree_rules,
                 probe_ask_rules]
        needs_model = {probe_sandbox_edit, probe_sandbox_consult, probe_resume,
                       probe_rules_override, probe_worktree_rules, probe_ask_rules}
        for step in steps:
            if step in needs_model and not ctx["auth_ok"]:
                line = result_line(False, STEP_IDS[step.__name__], "skipped: P2 failed")
            else:
                try:
                    line = step(ctx) if step is not probe_prereqs else step()
                except Exception as e:  # a probe crashing is a FAIL, not a doctor crash
                    line = result_line(False, STEP_IDS[step.__name__], f"crashed: {type(e).__name__}: {str(e)[:120]}")
            failed |= line.startswith("FAIL")
            print(line, flush=True)
    finally:
        WRITE_PROBE.unlink(missing_ok=True)
        subprocess.run(["git", "-C", str(ctx["repo"]), "worktree", "prune"], capture_output=True)
        shutil.rmtree(ctx["repo"], ignore_errors=True)
    return 1 if failed else 0


STEP_IDS = {
    "probe_prereqs": "P1", "probe_auth": "P2", "probe_sandbox_edit": "P3e",
    "probe_sandbox_consult": "P3c", "probe_resume": "P4", "probe_rules_override": "P5",
    "probe_quota": "P6", "probe_worktree_rules": "P7", "probe_ask_rules": "P8",
}


def quota():
    """TUI startup fetches credits with no model call; ACP exposes no billing method."""
    tail, t0 = BillingTail(), time.monotonic()
    tui_startup(tail=tail)
    fresh = tail.fresh()
    if not fresh:
        print("grok quota: TUI startup logged no billing line")
        return 1
    pct, end = fresh[-1]["pct"], fresh[-1]["end"]
    print(f"grok {fmt_pct(pct)}% · period ends {end} · {time.monotonic() - t0:.1f}s")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--install", action="store_true")
    mode.add_argument("--quota", action="store_true")
    args = ap.parse_args()
    if args.dry_run:
        dry_run()
        return 0
    if args.install:
        return install()
    if args.quota:
        return quota()
    return run_probes()


if __name__ == "__main__":
    sys.exit(main())
