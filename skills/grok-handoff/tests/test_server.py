"""MCP server tests against tests/fake_grok.py. Never launches the real grok."""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL))

from server import doctor, jobs, quota, rules  # noqa: E402
from server.grok_mcp import INSTRUCTIONS, TOOLS  # noqa: E402

JOB_END_FIELDS = (
    "job", "round", "mode", "kind", "effort", "model", "repo", "status",
    "exit_code", "duration_s", "brief_chars", "result_chars", "grok_version",
    "progress_token", "usage", "num_turns", "model_calls", "cost_usd",
    "grok_pct_before", "grok_pct_after", "grok_fresh_before", "grok_fresh_after",
    "grok_foreign_billing", "concurrent_jobs", "claude_5h_pct", "claude_7d_pct",
    "claude_captured_age_s", "diff_files", "diff_added", "diff_deleted",
    "base_dirty", "report_fields",
)
USAGE_KEYS = (
    "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens",
    "output_tokens", "reasoning_tokens", "total_tokens",
)
INSTRUCTIONS_TEXT = (
    "Hands implementation, review or research to Grok, whose quota is separate from Claude's. "
    "Load the grok-handoff skill before the first run call in a session."
)


def billing(pct, end, start="2020-01-01T00:00:00Z"):
    return {
        "msg": "billing: fetched credits config",
        "pid": 424242,
        "ctx": {"config": {
            "creditUsagePercent": pct,
            "currentPeriod": {"start": start, "end": end},
        }},
    }


def load_statusline():
    import importlib.util
    path = SKILL / "grok_statusline.py"
    spec = importlib.util.spec_from_file_location(f"statusline_parity_{time.time_ns()}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class ServerTest(unittest.TestCase):
    def setUp(self):
        self._old = os.environ.copy()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.make_repo(self.repo)
        self.state = self.root / "state"
        self.log = self.root / "fake.log"
        self.home = self.root / "ghome"
        self.write_billing(self.home, 10, days=20)
        self.ccar = self.root / "ccar.json"
        now = time.time()
        self.ccar.write_text(json.dumps({
            "resets_at": now + 10 * 3600,
            "used_percentage": 10,
            "captured_at": now,
            "seven_day": {"resets_at": now + 100 * 3600, "used_percentage": 10},
        }), encoding="utf-8")
        bindir = self.root / "bin"
        bindir.mkdir()
        os.symlink(SKILL / "tests" / "fake_grok.py", bindir / "grok")
        os.environ["PATH"] = str(bindir) + os.pathsep + self._old.get("PATH", "")
        os.environ["GROK_MCP_STATE"] = str(self.state)
        os.environ["GROK_MCP_FRESH"] = "off"
        os.environ["FAKE_GROK_LOG"] = str(self.log)
        os.environ["FAKE_GROK_MODE"] = "ok"
        os.environ.pop("FAKE_GROK_SLEEP", None)
        os.environ["GROK_HOME"] = str(self.home)
        os.environ["GROK_MCP_GROK_STATUSLINE"] = str(SKILL / "grok_statusline.py")
        os.environ["GROK_MCP_BURN_WINDOW"] = str(SKILL.parent / "self-improve" / "scripts" / "burn_window.py")
        os.environ["CCAR_STATE_JSON"] = str(self.ccar)
        self.head = self.git("rev-parse", "HEAD").stdout.strip()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._old)
        self.tmp.cleanup()

    def make_repo(self, path):
        path.mkdir(parents=True, exist_ok=True)
        subprocess.check_call(["git", "init", "-q", "-b", "main", str(path)])
        subprocess.check_call(["git", "-C", str(path), "config", "user.email", "t@localhost"])
        subprocess.check_call(["git", "-C", str(path), "config", "user.name", "t"])
        subprocess.check_call(["git", "-C", str(path), "config", "commit.gpgsign", "false"])
        (path / "README").write_text("x\n", encoding="utf-8")
        subprocess.check_call(["git", "-C", str(path), "add", "README"])
        subprocess.check_call(["git", "-C", str(path), "commit", "-qm", "init"])

    def git(self, *args, repo=None):
        return subprocess.run(
            ["git", "-C", str(repo or self.repo), *args],
            capture_output=True, text=True,
        )

    def write_billing(self, home, pct, days=20, end=None):
        logs = home / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        if end is None:
            end = (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        (logs / "unified.jsonl").write_text(json.dumps(billing(pct, end)) + "\n", encoding="utf-8")
        return end

    def call_run(self, task="do the task", cwd=None, kind="implement", **extra):
        args = {"task": task, "cwd": str(cwd or self.repo), "kind": kind}
        args.update(extra)
        return jobs.run(args)

    def jid(self, text):
        self.assertTrue(text.startswith("job g"), text)
        return text.split()[1]

    def argv_log(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines() if line.strip()]

    def prompts(self):
        return [rec for rec in self.argv_log() if "--prompt-file" in rec.get("argv", [])]

    def events(self):
        path = self.state / "events.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def server(self):
        return subprocess.Popen(
            [sys.executable, str(SKILL / "server" / "grok_mcp.py")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=os.environ.copy(), cwd=SKILL, text=True, encoding="utf-8",
        )

    def fingerprint(self, repo):
        files = {}
        for path in sorted(repo.rglob("*")):
            if ".git" in path.parts or not path.is_file():
                continue
            files[str(path.relative_to(repo))] = path.read_bytes()
        head = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"])
        status = subprocess.check_output(["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=no"])
        return head, status, files

    def test_protocol(self):
        self.assertEqual(INSTRUCTIONS, INSTRUCTIONS_TEXT)
        for tool in TOOLS:
            self.assertLessEqual(len(tool["description"]), 160, tool["name"])
        proc = self.server()
        proc.stdin.write(json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "t", "version": "0"}},
        }) + "\n")
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}) + "\n")
        proc.stdin.write(json.dumps({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "quota", "arguments": {}},
        }) + "\n")
        proc.stdin.close()
        proc.stdin = None
        out, err = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 0, err)
        lines = [json.loads(line) for line in out.splitlines() if line.strip()]
        self.assertEqual(len(lines), 3, out)
        self.assertEqual(lines[0]["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(lines[0]["result"]["instructions"], INSTRUCTIONS_TEXT)
        self.assertEqual(lines[0]["result"]["serverInfo"], {"name": "grok", "version": "0.1.0"})
        tools = lines[1]["result"]["tools"]
        self.assertEqual([tool["name"] for tool in tools], ["run", "result", "finish", "quota"])
        run = next(tool for tool in tools if tool["name"] == "run")
        self.assertEqual(run["inputSchema"]["required"], ["task"])
        self.assertFalse(lines[2]["result"]["isError"])
        self.assertEqual(lines[2]["result"]["content"][0]["type"], "text")

    def test_edit_happy(self):
        text, err = self.call_run()
        self.assertFalse(err, text)
        self.assertTrue(text.startswith("job g"), text)
        self.assertIn(" · done · ", text.splitlines()[0])
        jid = self.jid(text)
        wt = self.repo / ".grok-jobs" / jid
        self.assertTrue((wt / "made_by_grok.txt").is_file())
        self.assertFalse((self.repo / "made_by_grok.txt").exists())
        self.assertEqual(self.git("rev-parse", "--verify", f"grok/{jid}").returncode, 0)
        self.assertEqual(self.git("rev-parse", "HEAD").stdout.strip(), self.head)
        self.assertEqual(self.git("status", "--porcelain").stdout.strip(), "")
        argv = self.prompts()[0]["argv"]
        self.assertEqual(argv[argv.index("--sandbox") + 1], "claude-edit")
        self.assertIn("--always-approve", argv)
        self.assertIn("--trust", argv)
        self.assertEqual(self.prompts()[0]["GROK_CLAUDE_MCPS_ENABLED"], "false")

    def test_error_json(self):
        os.environ["FAKE_GROK_MODE"] = "error"
        text, err = self.call_run()
        self.assertTrue(err, text)
        self.assertIn(" · grok_out · ", text.splitlines()[0])
        jid = self.jid(text)
        self.assertEqual(self.git("rev-parse", f"grok/{jid}").stdout.strip(), self.head)

    def test_garbage(self):
        os.environ["FAKE_GROK_MODE"] = "garbage"
        text, err = self.call_run()
        self.assertTrue(err, text)
        self.assertIn(" · error · ", text.splitlines()[0])
        self.assertIn("not JSON", text)
        self.assertTrue(any(rec["event"] == "contract_error" for rec in self.events()))

    def test_huge(self):
        os.environ["FAKE_GROK_MODE"] = "huge"
        text, err = self.call_run()
        self.assertFalse(err, text)
        self.assertLessEqual(len(text), 2500)
        jid = self.jid(text)
        patch = self.state / "jobs" / jid / "diff.patch"
        self.assertIn(str(patch), text)
        self.assertTrue(patch.is_file())

        consult = self.root / "consult"
        consult.mkdir()
        text, err = jobs.run({
            "task": "explain", "cwd": str(consult), "kind": "research", "mode": "consult",
        })
        self.assertFalse(err, text)
        self.assertLessEqual(len(text), 6000)
        answers = list((self.state / "jobs").glob("*/answer.md"))
        self.assertEqual(len(answers), 1)
        answer = answers[0]
        self.assertTrue(text.endswith(str(answer)), text[-200:])
        out = json.loads((answer.parent / "out-r1.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(out["text"]), 50 * 1024)
        self.assertEqual(answer.read_text(encoding="utf-8"), out["text"])

    def test_concurrent_limit(self):
        os.environ["FAKE_GROK_MODE"] = "sleep"
        os.environ["FAKE_GROK_SLEEP"] = "10"
        results = []
        gate = threading.Lock()

        def go(i):
            out = jobs.run({"task": f"t{i}", "cwd": str(self.repo), "kind": "implement"})
            with gate:
                results.append(out)

        threads = [threading.Thread(target=go, args=(i,)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(40)
        self.assertEqual(len(results), 4)
        refused = [item for item in results if item[1]]
        self.assertEqual(len(refused), 1, results)
        self.assertIn("already 3", refused[0][0])
        self.assertEqual(len(self.prompts()), 3)
        self.assertEqual(len(list((self.state / "jobs").iterdir())), 3)

    def test_given_credit_mostly_used_when_second_job_starts_then_refused(self):
        self.write_billing(self.home, 90)
        os.environ["FAKE_GROK_MODE"] = "sleep"
        os.environ["FAKE_GROK_SLEEP"] = "10"
        results = []
        gate = threading.Lock()

        def go(i):
            out = jobs.run({"task": f"t{i}", "cwd": str(self.repo), "kind": "implement"})
            with gate:
                results.append(out)

        threads = [threading.Thread(target=go, args=(i,)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(40)
        refused = [item for item in results if item[1]]
        self.assertEqual(len(refused), 1, results)
        self.assertIn("already 1", refused[0][0])

    def test_concurrency_cap_tightens_as_credit_is_used(self):
        caps = {0: 3, 69.9: 3, 70: 2, 84.9: 2, 85: 1, 100: 1}
        for pct, allowed in caps.items():
            self.assertEqual(quota.max_concurrent({"pct": pct}), allowed, pct)
        self.assertEqual(quota.max_concurrent("unknown"), 1)

    def test_private_path(self):
        priv = self.root / "private"
        repo = priv / "repo"
        self.make_repo(repo)
        old = rules.PRIVATE_PATHS
        rules.PRIVATE_PATHS = (str(priv),)
        try:
            text, err = jobs.run({"task": "nope", "cwd": str(repo), "kind": "implement"})
        finally:
            rules.PRIVATE_PATHS = old
        self.assertTrue(err, text)
        self.assertIn("private", text)
        self.assertEqual(self.prompts(), [])

    def test_continuation(self):
        text, err = self.call_run()
        self.assertFalse(err, text)
        jid = self.jid(text)
        wt = self.repo / ".grok-jobs" / jid
        text2, err2 = jobs.run({"task": "second round", "job": jid})
        self.assertFalse(err2, text2)
        self.assertIn(" r2 ", text2.splitlines()[0])
        self.assertEqual([p.name for p in (self.repo / ".grok-jobs").iterdir()], [jid])
        self.assertTrue(wt.is_dir())
        argv = self.prompts()[-1]["argv"]
        self.assertIn("--resume", argv)
        self.assertEqual(argv[argv.index("--resume") + 1], "s-fake")

    def test_reattach(self):
        os.environ["FAKE_GROK_MODE"] = "sleep"
        os.environ["FAKE_GROK_SLEEP"] = "12"
        proc = self.server()
        proc.stdin.write(json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "run", "arguments": {
                "task": "sleep", "cwd": str(self.repo), "kind": "implement",
            }},
        }) + "\n")
        proc.stdin.flush()
        meta = None
        for _ in range(100):
            found = list((self.state / "jobs").glob("*/meta.json"))
            if found:
                meta = json.loads(found[0].read_text(encoding="utf-8"))
                if meta.get("status") == "running" and meta.get("pid"):
                    break
            time.sleep(0.1)
        else:
            proc.kill()
            self.fail(f"job never started: {proc.stderr.read()}")
        pid = meta["pid"]
        jid = meta["id"]
        proc.kill()
        proc.wait(timeout=10)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            stream.close()
        self.assertTrue(Path(f"/proc/{pid}").exists(), "job died with the server")
        proc2 = self.server()
        proc2.stdin.write(json.dumps({
            "jsonrpc": "2.0", "id": 7, "method": "tools/call",
            "params": {"name": "result", "arguments": {"job": jid}},
        }) + "\n")
        proc2.stdin.flush()
        box = {}

        def read_line():
            box["line"] = proc2.stdout.readline()

        reader = threading.Thread(target=read_line)
        reader.start()
        reader.join(40)
        if reader.is_alive():
            proc2.kill()
            self.fail("result() did not return")
        proc2.stdin.close()
        proc2.stdin = None
        _, err = proc2.communicate(timeout=15)
        self.assertEqual(proc2.returncode, 0, err)
        msg = json.loads(box["line"])
        text = msg["result"]["content"][0]["text"]
        self.assertFalse(msg["result"]["isError"], text)
        self.assertTrue(text.startswith(f"job {jid} "), text)
        self.assertIn(" · done · ", text.splitlines()[0])
        ends = [rec for rec in self.events() if rec["event"] == "job_end"]
        self.assertTrue(ends)
        self.assertIsNone(ends[-1]["exit_code"])

    def test_finish_merge(self):
        text, err = self.call_run()
        self.assertFalse(err, text)
        jid = self.jid(text)
        before = int(self.git("rev-list", "--count", "HEAD").stdout)
        out, ferr = jobs.finish({"job": jid, "action": "merge", "message": "land the grok edit"})
        self.assertFalse(ferr, out)
        after = int(self.git("rev-list", "--count", "HEAD").stdout)
        self.assertEqual(after, before + 1)
        body = self.git("log", "-1", "--format=%B").stdout
        self.assertIn(f"Grok-Job: {jid}", body)
        self.assertFalse((self.repo / ".grok-jobs" / jid).exists())
        self.assertNotEqual(self.git("rev-parse", "--verify", "--quiet", f"grok/{jid}").returncode, 0)
        short = self.git("rev-parse", "--short=7", "HEAD").stdout.strip()
        self.assertIn(short, out)
        finishes = [rec for rec in self.events() if rec["event"] == "job_finish"]
        self.assertEqual(len(finishes), 1)
        self.assertEqual(finishes[0]["action"], "merge")
        self.assertTrue(finishes[0]["merged_sha"])
        self.assertIs(finishes[0]["conflict"], False)

    def test_finish_dirty_and_untracked(self):
        text, err = self.call_run()
        self.assertFalse(err, text)
        jid = self.jid(text)
        head = self.git("rev-parse", "HEAD").stdout
        (self.repo / "README").write_text("changed\n", encoding="utf-8")
        dirty = self.git("status", "--porcelain").stdout
        out, ferr = jobs.finish({"job": jid, "action": "merge", "message": "nope"})
        self.assertTrue(ferr, out)
        self.assertIn("commit your own changes first; nothing was changed", out)
        self.assertEqual(self.git("rev-parse", "HEAD").stdout, head)
        self.assertEqual(self.git("status", "--porcelain").stdout, dirty)
        self.git("checkout", "--", "README")
        (self.repo / "notes.txt").write_text("keep\n", encoding="utf-8")
        out, ferr = jobs.finish({"job": jid, "action": "merge", "message": "land it"})
        self.assertFalse(ferr, out)
        self.assertEqual((self.repo / "notes.txt").read_text(encoding="utf-8"), "keep\n")
        self.assertIn("notes.txt", self.git("status", "--porcelain").stdout)

    def test_finish_conflict(self):
        text, err = self.call_run()
        self.assertFalse(err, text)
        jid = self.jid(text)
        (self.repo / "made_by_grok.txt").write_text("main version\n", encoding="utf-8")
        self.git("add", "made_by_grok.txt")
        self.git("commit", "-qm", "main copy")
        before = self.fingerprint(self.repo)
        head = self.git("rev-parse", "HEAD").stdout
        out, ferr = jobs.finish({"job": jid, "action": "merge", "message": "conflict"})
        self.assertTrue(ferr, out)
        self.assertIn("branch kept; resolve by hand or discard", out)
        self.assertEqual(self.fingerprint(self.repo), before)
        self.assertEqual(self.git("rev-parse", "HEAD").stdout, head)
        self.assertEqual(self.git("rev-parse", "--verify", "--quiet", f"grok/{jid}").returncode, 0)

    def test_given_user_grok_use_during_a_quota_read_when_read_then_own_line_used_and_only_user_line_foreign(self):
        os.environ["GROK_MCP_FRESH"] = "on"
        log = self.home / "logs" / "unified.jsonl"
        offset = log.stat().st_size
        end = (datetime.now(timezone.utc) + timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        own, user = billing(40, end), dict(billing(55, end), pid=777)

        def read_with_user_overlap(*a, **k):
            with log.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(own) + "\n" + json.dumps(user) + "\n")
            return {own["pid"]}

        with unittest.mock.patch.object(doctor, "tui_startup", read_with_user_overlap):
            got = quota.grok_read(True)
        self.assertEqual(got["pct"], 40)
        self.assertEqual(jobs._foreign_billing(offset, set()), 1)

    def test_given_log_trimmed_below_offset_when_counting_foreign_billing_then_only_lines_since_start_count(self):
        log = self.home / "logs" / "unified.jsonl"
        started = time.time()
        end = (datetime.now(timezone.utc) + timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        stamp = lambda epoch: time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(epoch))
        old = dict(billing(20, end), pid=701, ts=stamp(started - 3600))
        new = dict(billing(21, end), pid=702, ts=stamp(started + 5))
        log.write_text(json.dumps(old) + "\n" + json.dumps(new) + "\n", encoding="utf-8")
        offset_before_trim = log.stat().st_size * 10
        self.assertEqual(jobs._foreign_billing(offset_before_trim, set(), started), 1)

    def test_given_cwd_not_in_head_when_run_then_refused_with_no_worktree_branch_or_job_dir(self):
        (self.repo / "untracked_dir").mkdir()
        out, err = self.call_run(cwd=self.repo / "untracked_dir")
        self.assertTrue(err, out)
        self.assertEqual(list((self.state / "jobs").iterdir()), [])
        self.assertEqual(self.git("branch", "--list", "grok/*").stdout.strip(), "")
        self.assertEqual(self.git("worktree", "list", "--porcelain").stdout.count("worktree "), 1)

    def test_quota(self):
        real = Path("~/.grok/statusline-quota.json").expanduser()
        before_mtime = real.stat().st_mtime_ns if real.exists() else None
        home = self.root / "quota-home"
        os.environ["GROK_HOME"] = str(home)
        os.environ["GROK_MCP_FRESH"] = "on"
        (home / "logs").mkdir(parents=True)
        (home / "logs" / "unified.jsonl").write_text("", encoding="utf-8")
        end = (datetime.now(timezone.utc) + timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        record = billing(32, end)

        def append(rec):
            with doctor.BILLING_LOG.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec) + "\n")

        with unittest.mock.patch.object(doctor, "tui_startup", lambda *a, **k: append(record)):
            got = quota.grok_read(True)
        self.assertEqual(got["pct"], 32)
        self.assertTrue(got["fresh"])
        self.assertEqual(got["period_end"], end)
        cache = json.loads((home / "statusline-quota.json").read_text(encoding="utf-8"))
        self.assertEqual(cache["pct"], 32)
        self.assertIn("size", cache)
        self.assertIn("mtime_ns", cache)
        self.assertEqual(load_statusline().weekly_used_pct(), 32)

        (home / "logs" / "unified.jsonl").write_text("", encoding="utf-8")
        null_rec = billing(None, end)
        with unittest.mock.patch.object(doctor, "tui_startup", lambda *a, **k: append(null_rec)):
            zero = quota.grok_read(True)
        self.assertEqual(zero["pct"], 0)

        os.environ["GROK_MCP_FRESH"] = "off"
        past = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        (home / "logs" / "unified.jsonl").write_text(
            json.dumps(billing(40, past)) + "\n", encoding="utf-8")
        self.assertEqual(quota.grok_read(False), "unknown")

        far = (datetime.now(timezone.utc) + timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%SZ")
        soon = (datetime.now(timezone.utc) + timedelta(hours=10)).strftime("%Y-%m-%dT%H:%M:%SZ")

        def reading(pct, when):
            return {"pct": pct, "period_end": when, "fresh": True, "age_s": 0}

        closed = {"burn_state": "closed"}
        self.assertEqual(quota.route("unknown", closed)[0], "claude-only")
        self.assertEqual(quota.route(reading(99, far), closed)[0], "claude-only")
        self.assertEqual(quota.route(reading(10, far), {"burn_state": "open"})[0], "claude-only")
        self.assertEqual(quota.route(reading(50, soon), closed)[0], "burn")
        self.assertEqual(quota.route(reading(90, far), closed)[0], "conserve")
        self.assertEqual(quota.route(reading(32, far), closed)[0], "normal")

        after_mtime = real.stat().st_mtime_ns if real.exists() else None
        self.assertEqual(before_mtime, after_mtime)

    def test_lockout_holds_until_reset_without_a_billing_read(self):
        os.environ["GROK_MCP_FRESH"] = "on"
        iso = lambda delta: (datetime.now(timezone.utc) + delta).strftime("%Y-%m-%dT%H:%M:%SZ")
        reset = iso(timedelta(days=3))
        quota.note_grok_out(reset)
        with unittest.mock.patch.object(doctor, "tui_startup", side_effect=AssertionError("billing read")):
            got = quota.grok_read(True)
        self.assertEqual((got["pct"], got["period_end"]), (100, reset))
        mode, reason = quota.route(got, {"burn_state": "closed"})
        self.assertEqual(mode, "claude-only")
        self.assertIn("out until reset", reason)
        text, err = self.call_run()
        self.assertTrue(err, text)
        self.assertIn("grok unavailable", text)
        self.assertEqual(self.prompts(), [])

    def test_lockout_lapses_once_the_reset_has_passed(self):
        quota.note_grok_out(
            (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"))
        data = json.loads(quota._grok_out_path().read_text(encoding="utf-8"))
        data["until"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        quota._grok_out_path().write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(quota.grok_read(False)["pct"], 10)
        self.assertFalse(quota._grok_out_path().exists())

    def test_credit_error_records_reset_from_the_period_the_job_started_in(self):
        os.environ["FAKE_GROK_MODE"] = "error"
        self.call_run()
        self.assertIsNotNone(quota._grok_out_until())
        self.assertEqual(quota.grok_read(True)["pct"], 100)

    def test_events(self):
        sentinel = "BRIEF_SENTINEL_do_not_log"
        text, err = self.call_run(task=sentinel)
        self.assertFalse(err, text)
        ends = [rec for rec in self.events() if rec["event"] == "job_end"]
        self.assertEqual(len(ends), 1)
        rec = ends[0]
        for key in JOB_END_FIELDS:
            self.assertIn(key, rec, key)
        self.assertIn("ts", rec)
        self.assertEqual(rec["event"], "job_end")
        self.assertEqual(rec["v"], 1)
        for key in USAGE_KEYS:
            self.assertIn(key, rec["usage"])
        self.assertIsInstance(rec["report_fields"], list)
        self.assertIs(rec["progress_token"], False)
        self.assertEqual(rec["exit_code"], 0)
        self.assertIsNone(rec["cost_usd"])
        self.assertEqual(rec["num_turns"], 2)
        self.assertEqual(rec["model_calls"], 1)
        self.assertEqual(rec["brief_chars"], len(sentinel))
        raw = (self.state / "events.jsonl").read_text(encoding="utf-8")
        self.assertNotIn(sentinel, raw)
        self.assertNotIn(str(self.repo), raw)
        self.assertNotIn("diff.patch", raw)

    def test_consult(self):
        cwd = self.root / "ask"
        cwd.mkdir()
        text, err = jobs.run({
            "task": "what is this", "cwd": str(cwd), "kind": "research", "mode": "consult",
        })
        self.assertFalse(err, text)
        jid = self.jid(text)
        meta = json.loads((self.state / "jobs" / jid / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["status"], "finished")
        finishes = [rec for rec in self.events() if rec["event"] == "job_finish"]
        self.assertEqual(len(finishes), 1)
        self.assertEqual(finishes[0]["action"], "consult")
        self.assertIsNone(finishes[0]["merged_sha"])
        listed, lerr = jobs.result({})
        self.assertFalse(lerr)
        self.assertNotIn(jid, listed)

    def test_prune(self):
        jid = "gold01"
        wt = self.repo / ".grok-jobs" / jid
        subprocess.check_call(
            ["git", "-C", str(self.repo), "worktree", "add", "-q", "-b", f"grok/{jid}", str(wt), "HEAD"])
        job = self.state / "jobs" / jid
        job.mkdir(parents=True)
        (job / "meta.json").write_text(json.dumps({
            "id": jid,
            "status": "cancelled",
            "mode": "edit",
            "repo_root": str(self.repo.resolve()),
            "branch": f"grok/{jid}",
            "round": 1,
        }), encoding="utf-8")
        old = time.time() - 15 * 86400
        os.utime(job / "meta.json", (old, old))
        proc = self.server()
        proc.stdin.close()
        proc.stdin = None
        _, err = proc.communicate(timeout=20)
        self.assertEqual(proc.returncode, 0, err)
        self.assertFalse(wt.exists())
        self.assertNotEqual(self.git("rev-parse", "--verify", "--quiet", f"grok/{jid}").returncode, 0)
        self.assertFalse(job.exists())


class ActivityTrackerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = unittest.mock.patch.object(jobs, "GROK_SESSIONS", Path(self.tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.workdir = "/work/app"
        self.session = Path(self.tmp.name) / "%2Fwork%2Fapp" / "sess-1"
        self.session.mkdir(parents=True)
        self.updates = self.session / "updates.jsonl"
        self.tracker = jobs.ActivityTracker(self.workdir, time.time())

    def write(self, *updates, newline=True):
        lines = [json.dumps({"params": {"update": u}}) for u in updates]
        text = "\n".join(lines) + ("\n" if newline else "")
        with open(self.updates, "a", encoding="utf-8") as handle:
            handle.write(text)

    def test_no_transcript_yields_nothing(self):
        self.assertIsNone(self.tracker.poll())

    def test_tool_updates_summarise_kind_and_target(self):
        self.write(
            {"sessionUpdate": "tool_call", "toolCallId": "a"},
            {"sessionUpdate": "tool_call_update", "kind": "read",
             "title": "Read `/work/app/src/Streak.kt`",
             "locations": [{"path": "/work/app/src/Streak.kt"}]},
            {"sessionUpdate": "tool_call", "toolCallId": "b"},
        )
        self.assertEqual(self.tracker.poll(), "2 tools · read Streak.kt")

    def test_search_and_execute_use_the_title(self):
        self.write({"sessionUpdate": "tool_call_update", "kind": "search",
                    "title": "fun getPref", "locations": []})
        self.assertEqual(self.tracker.poll(), "search fun getPref")
        self.write({"sessionUpdate": "tool_call_update", "kind": "execute",
                    "title": "Execute `./gradlew test`", "locations": []})
        self.assertEqual(self.tracker.poll(), "run ./gradlew test")

    def test_thought_and_message_chunks(self):
        self.write({"sessionUpdate": "agent_thought_chunk"})
        self.assertEqual(self.tracker.poll(), "thinking")
        self.write({"sessionUpdate": "agent_message_chunk"})
        self.assertEqual(self.tracker.poll(), "writing")

    def test_partial_trailing_line_is_picked_up_once_complete(self):
        full = json.dumps({"params": {"update": {"sessionUpdate": "tool_call"}}})
        with open(self.updates, "w", encoding="utf-8") as handle:
            handle.write(full[:20])
        self.assertIsNone(self.tracker.poll())
        with open(self.updates, "a", encoding="utf-8") as handle:
            handle.write(full[20:] + "\n")
        self.assertEqual(self.tracker.poll(), "1 tools")

    def test_garbage_lines_are_skipped(self):
        with open(self.updates, "w", encoding="utf-8") as handle:
            handle.write("not json\n{\"params\": 3}\n")
        self.write({"sessionUpdate": "agent_thought_chunk"})
        self.assertEqual(self.tracker.poll(), "thinking")

    def test_stale_transcript_from_before_the_job_is_ignored(self):
        self.write({"sessionUpdate": "agent_thought_chunk"})
        old = time.time() - 600
        os.utime(self.updates, (old, old))
        self.assertIsNone(self.tracker.poll())

    def test_progress_message_is_capped_and_degrades_without_summary(self):
        self.assertEqual(jobs._progress_message("g1", 75, None), "grok g1 01:15")
        long = jobs._progress_message("g1", 5, "x" * 200)
        self.assertEqual(len(long), jobs.PROGRESS_MAX_CHARS)


if __name__ == "__main__":
    unittest.main()
