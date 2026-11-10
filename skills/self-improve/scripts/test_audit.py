"""Hermetic tests for audit.py's digest and fixes commands.

Each case builds transcripts and state under a temp directory and points
SELF_IMPROVE_PROJECTS and SELF_IMPROVE_STATE at them. HOME is a temp directory
too, so a missed override cannot read the real ~/.claude tree.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

AUDIT = Path(__file__).with_name("audit.py")
SHIPPED = "2026-06-01T00:00:00+00:00"


def days_ago(n: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=n)).replace(microsecond=0).isoformat()


def user_error(ts, text, content=None):
    body = text if content is None else content
    return {
        "type": "user",
        "timestamp": ts,
        "message": {"role": "user", "content": [
            {"type": "tool_result", "is_error": True, "content": body},
        ]},
    }


def replay_error(ts, text):
    return {
        "type": "assistant",
        "timestamp": ts,
        "message": {"role": "assistant", "content": [
            {"type": "tool_result", "is_error": True, "content": text},
        ]},
    }


def user_ok(ts, text):
    return {
        "type": "user",
        "timestamp": ts,
        "message": {"role": "user", "content": [
            {"type": "tool_result", "is_error": False, "content": text},
        ]},
    }


def tool_use(ts, name, payload):
    return {
        "type": "assistant",
        "timestamp": ts,
        "message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": name, "input": payload},
        ]},
    }


def slash(ts, command):
    return {
        "type": "user",
        "timestamp": ts,
        "message": {"role": "user", "content": f"<command-name>/{command}</command-name>"},
    }


def fix_record(signature="boom matched", baseline_rate=0.4, hook=None, evidence=None):
    return {
        "id": "sample",
        "shipped": SHIPPED,
        "mechanism": "hook",
        "commit": "abc1234",
        "problem": "sample problem",
        "signature": signature,
        "opportunity": None,
        "hook_signature": hook,
        "baseline": {"rate": baseline_rate, "sessions": 10, "window_days": 60},
        "status": "pending",
        "evidence": evidence,
    }


class AuditTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="audit-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / "home"
        self.projects = self.tmp / "projects"
        self.state = self.tmp / "state"
        self.projects.mkdir()
        self.state.mkdir()
        decoy_projects = self.home / ".claude" / "projects" / "decoy"
        decoy_projects.mkdir(parents=True)
        (decoy_projects / "decoy.jsonl").write_text(
            json.dumps(user_error("2020-01-01T00:00:00+00:00", "decoy must not be read")) + "\n",
            encoding="utf-8",
        )
        decoy_state = self.home / ".claude" / "skills" / "self-improve" / "state"
        decoy_state.mkdir(parents=True)
        (decoy_state / "fixes.json").write_text(json.dumps({
            "fixes": [{
                "id": "decoy-fix",
                "shipped": "2020-01-01T00:00:00+00:00",
                "mechanism": "x",
                "commit": "x",
                "problem": "x",
                "signature": ".*",
                "opportunity": None,
                "hook_signature": None,
                "baseline": {"rate": 1, "sessions": 1, "window_days": 60},
                "status": "pending",
                "evidence": "decoy must not be read",
            }],
            "dismissed": [{"signature": ".*", "reason": "decoy must not be read"}],
        }) + "\n", encoding="utf-8")

    def write_session(self, project, session, records):
        dest = self.projects / project
        dest.mkdir(parents=True, exist_ok=True)
        (dest / f"{session}.jsonl").write_text(
            "".join(json.dumps(rec) + "\n" for rec in records),
            encoding="utf-8",
        )

    def write_state(self, data):
        (self.state / "fixes.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    def audit(self, *args):
        env = os.environ.copy()
        env["HOME"] = str(self.home)
        env["SELF_IMPROVE_STATE"] = str(self.state)
        env["SELF_IMPROVE_PROJECTS"] = str(self.projects)
        return subprocess.run(
            [sys.executable, str(AUDIT), *args],
            env=env,
            capture_output=True,
            text=True,
        )

    def write_after(self, count, hits, text="boom matched"):
        for i in range(count):
            ts = f"2026-07-01T{i:02d}:00:00+00:00"
            records = [tool_use(ts, "Bash", {"command": "make widget"})]
            if i < hits:
                records.append(user_error(ts, text))
            self.write_session("garden", f"after{i:02d}", records)

    def write_baseline_corpus(self):
        rows = [
            ("before", "2026-03-01T00:00:00+00:00", "make widget", True),
            ("in-hit-a", "2026-04-15T00:00:00+00:00", "make widget", True),
            ("in-hit-b", "2026-05-01T00:00:00+00:00", "make widget", True),
            ("in-miss-a", "2026-05-10T00:00:00+00:00", "make widget", False),
            ("in-miss-b", "2026-05-20T00:00:00+00:00", "make widget", False),
            ("in-other", "2026-05-15T00:00:00+00:00", "make salad", True),
            ("at-ship", SHIPPED, "make widget", True),
            ("after", "2026-07-01T00:00:00+00:00", "make widget", True),
        ]
        for name, ts, command, failed in rows:
            records = [tool_use(ts, "Bash", {"command": command})]
            if name == "in-miss-a":
                records.append(replay_error(ts, "widget exploded"))
            if failed:
                records.append(user_error(ts, "widget exploded"))
            self.write_session("garden", name, records)

    def test_given_the_same_error_in_a_non_user_record_when_digested_then_it_is_not_counted(self):
        self.write_session("garden", "sessuser", [
            user_error("2026-08-01T00:00:00+00:00", "user side failure"),
            replay_error("2026-08-01T00:00:01+00:00", "user side failure"),
            replay_error("2026-08-01T00:00:02+00:00", "replay side failure"),
        ])
        proc = self.audit("digest")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("# digest: 1 error result ", proc.stdout)
        self.assertIn("user side failure", proc.stdout)
        self.assertIn("[1 sessions, 1 proj: garden]", proc.stdout)
        self.assertNotIn("replay side failure", proc.stdout)
        self.assertNotIn("decoy-fix", proc.stdout)
        self.assertNotIn("decoy must not be read", proc.stdout)

    def test_given_error_content_as_blocks_when_digested_then_the_text_is_counted(self):
        self.write_session("garden", "blockses", [
            user_error(
                "2026-08-01T00:00:00+00:00",
                None,
                content=[{"type": "text", "text": "block form failure"}],
            ),
        ])
        proc = self.audit("digest")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("sig: block form failure", proc.stdout)
        self.assertIn("# digest: 1 error result ", proc.stdout)

    def test_given_a_session_that_invokes_the_self_improve_skill_when_digested_then_the_session_is_skipped(self):
        self.write_session("garden", "selfses1", [
            user_error("2026-08-01T00:00:00+00:00", "skipped failure skill"),
            tool_use("2026-08-01T01:00:00+00:00", "Skill", {"skill": "self-improve"}),
        ])
        self.write_session("orchard", "keepses1", [
            user_error("2026-08-01T00:00:00+00:00", "ordinary failure skill"),
        ])
        proc = self.audit("digest")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("# digest: 1 error result across 2 sessions (1 self-improve skipped)", proc.stdout)
        self.assertIn("ordinary failure skill", proc.stdout)
        self.assertNotIn("skipped failure skill", proc.stdout)

    def test_given_a_self_improve_slash_command_when_digested_then_the_session_is_skipped(self):
        self.write_session("garden", "selfses2", [
            user_error("2026-08-01T00:00:00+00:00", "skipped failure slash"),
            slash("2026-08-01T01:00:00+00:00", "self-improve"),
        ])
        self.write_session("orchard", "keepses2", [
            user_error("2026-08-01T00:00:00+00:00", "ordinary failure slash"),
        ])
        proc = self.audit("digest")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("ordinary failure slash", proc.stdout)
        self.assertNotIn("skipped failure slash", proc.stdout)
        self.assertIn("(1 self-improve skipped)", proc.stdout)

    def test_given_a_tool_use_under_the_self_improve_tree_when_digested_then_the_session_is_skipped(self):
        self.write_session("garden", "selfses3", [
            user_error("2026-08-01T00:00:00+00:00", "skipped failure tree"),
            tool_use("2026-08-01T01:00:00+00:00", "Bash", {"command": "cat /home/u/garden/patterns.json"}),
        ])
        self.write_session("orchard", "keepses3", [
            user_error("2026-08-01T00:00:00+00:00", "ordinary failure tree"),
        ])
        proc = self.audit("digest")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("ordinary failure tree", proc.stdout)
        self.assertNotIn("skipped failure tree", proc.stdout)
        self.assertIn("(1 self-improve skipped)", proc.stdout)

    def test_given_an_error_first_seen_within_recent_days_when_digested_then_it_is_tagged_new(self):
        ts = days_ago(1)
        self.write_session("garden", "abcdef012345", [
            user_error(ts, "fresh failure alpha"),
        ])
        proc = self.audit("digest")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("NEW", proc.stdout)
        self.assertIn("fresh failure alpha", proc.stdout)
        self.assertIn(f"loc: garden abcdef01 {ts}", proc.stdout)
        self.assertIn(f"first {ts[:10]}", proc.stdout)

    def test_given_an_error_first_seen_before_recent_days_when_digested_then_it_is_not_tagged_new(self):
        self.write_session("garden", "oldses01", [
            user_error(days_ago(40), "stale failure beta"),
        ])
        proc = self.audit("digest", "--recent-days", "14")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("stale failure beta", proc.stdout)
        self.assertNotIn("NEW", proc.stdout)

    def test_given_a_fix_signature_when_digested_then_the_raw_error_is_labelled_with_the_fix(self):
        self.write_state({"fixes": [{
            "id": "retry-cap",
            "shipped": "2026-01-01T00:00:00+00:00",
            "mechanism": "wrapper",
            "commit": "abc1234",
            "problem": "retries",
            "signature": "failed after 3 retries",
            "opportunity": None,
            "hook_signature": None,
            "baseline": {"rate": 1.0, "sessions": 4, "window_days": 60},
            "status": "pending",
            "evidence": None,
        }], "dismissed": []})
        self.write_session("garden", "retryfix", [
            user_error("2026-08-02T03:04:05+00:00", "failed after 3 retries"),
        ])
        proc = self.audit("digest")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("[fix retry-cap pending]", proc.stdout)
        self.assertIn("sig: failed after N retries", proc.stdout)
        self.assertIn("loc: garden retryfix 2026-08-02T03:04:05+00:00", proc.stdout)

    def test_given_a_dismissed_signature_when_digested_then_it_is_hidden_and_counted(self):
        self.write_state({
            "fixes": [],
            "dismissed": [{"signature": "exit code 2", "reason": "known noise"}],
        })
        self.write_session("garden", "noisy001", [
            user_error("2026-08-02T00:00:00+00:00", "exit code 2 from the tool"),
        ])
        self.write_session("orchard", "kept0001", [
            user_error("2026-08-02T00:00:00+00:00", "real failure kept"),
        ])
        proc = self.audit("digest")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("# dismissed signatures hidden: 1", proc.stdout)
        self.assertNotIn("exit code", proc.stdout)
        self.assertIn("real failure kept", proc.stdout)

    def test_given_a_dismissed_signature_and_all_when_digested_then_it_is_shown(self):
        self.write_state({
            "fixes": [],
            "dismissed": [{"signature": "exit code 2", "reason": "known noise"}],
        })
        self.write_session("garden", "noisy001", [
            user_error("2026-08-02T00:00:00+00:00", "exit code 2 from the tool"),
        ])
        proc = self.audit("digest", "--all")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("sig: exit code N from the tool", proc.stdout)
        self.assertNotIn("dismissed signatures hidden", proc.stdout)

    def test_given_fewer_than_20_sessions_after_shipping_when_fixes_are_reported_then_the_verdict_is_pending(self):
        self.write_state({"fixes": [fix_record()], "dismissed": []})
        self.write_after(19, 19)
        proc = self.audit("fixes")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("after: 1.000 over 19 sessions", proc.stdout)
        self.assertIn("verdict: pending", proc.stdout)
        self.assertNotIn("verdict: works", proc.stdout)
        self.assertNotIn("verdict: failing", proc.stdout)

    def test_given_a_halved_rate_over_at_least_20_sessions_when_fixes_are_reported_then_the_verdict_is_works(self):
        self.write_state({"fixes": [fix_record(baseline_rate=0.4)], "dismissed": []})
        before = (self.state / "fixes.json").read_text(encoding="utf-8")
        self.write_after(20, 4)
        proc = self.audit("fixes")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("baseline: 0.400 over 10 sessions (60d)", proc.stdout)
        self.assertIn("after: 0.200 over 20 sessions", proc.stdout)
        self.assertIn("verdict: works", proc.stdout)
        self.assertEqual((self.state / "fixes.json").read_text(encoding="utf-8"), before)

    def test_given_a_rate_above_half_the_baseline_when_fixes_are_reported_then_the_verdict_is_failing(self):
        self.write_state({"fixes": [fix_record(baseline_rate=0.4)], "dismissed": []})
        self.write_after(20, 5)
        proc = self.audit("fixes")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("after: 0.250 over 20 sessions", proc.stdout)
        self.assertIn("verdict: failing", proc.stdout)

    def test_given_a_fix_without_a_signature_when_reported_then_evidence_is_printed_without_a_verdict(self):
        self.write_state({"fixes": [{
            "id": "manual",
            "shipped": SHIPPED,
            "mechanism": "prose",
            "commit": "abc1234",
            "problem": "something unregexable",
            "signature": None,
            "opportunity": None,
            "hook_signature": None,
            "baseline": None,
            "status": "pending",
            "evidence": "counted by hand in the notebook",
        }], "dismissed": []})
        proc = self.audit("fixes")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("measured by: counted by hand in the notebook", proc.stdout)
        self.assertNotIn("verdict", proc.stdout)

    def test_given_hook_errors_after_shipping_when_fixes_are_reported_then_firings_are_counted(self):
        self.write_state({
            "fixes": [fix_record(signature="no such error", hook="gated once")],
            "dismissed": [],
        })
        before = "2026-05-01T00:00:00+00:00"
        after = "2026-07-01T00:00:00+00:00"
        later = "2026-07-02T00:00:00+00:00"
        self.write_session("garden", "hookpre1", [
            user_error(before, "gated once per session"),
        ])
        self.write_session("garden", "hookpost", [
            user_error(after, "gated once per session"),
            replay_error(after, "gated once per session"),
        ])
        self.write_session("garden", "hookok01", [
            user_ok(after, "gated once per session"),
        ])
        self.write_session("orchard", "hookmiss", [
            user_error(later, "ordinary boom"),
        ])
        proc = self.audit("fixes")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("hook firings since shipped: 1", proc.stdout)

    def test_given_no_state_file_when_fixes_are_reported_then_nothing_is_written(self):
        proc = self.audit("fixes")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("# fixes: 0 registered", proc.stdout)
        self.assertFalse((self.state / "fixes.json").exists())

    def test_given_sessions_around_the_ship_date_when_a_fix_is_added_then_the_baseline_is_the_window_rate(self):
        self.write_baseline_corpus()
        proc = self.audit(
            "fixes", "add",
            "--id", "widget-boom",
            "--problem", "widget explodes",
            "--mechanism", "a wrapper refuses the bad flag",
            "--commit", "abc1234",
            "--signature", "widget exploded",
            "--opportunity", "widget",
            "--shipped", SHIPPED,
            "--window-days", "60",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads((self.state / "fixes.json").read_text(encoding="utf-8"))
        self.assertEqual(data["fixes"], [{
            "id": "widget-boom",
            "shipped": SHIPPED,
            "mechanism": "a wrapper refuses the bad flag",
            "commit": "abc1234",
            "problem": "widget explodes",
            "signature": "widget exploded",
            "opportunity": "widget",
            "hook_signature": None,
            "baseline": {"rate": 0.5, "sessions": 4, "window_days": 60},
            "status": "pending",
            "evidence": None,
        }])
        self.assertEqual(data["dismissed"], [])

    def test_given_a_self_improve_session_in_the_window_when_a_fix_is_added_then_it_is_not_an_opportunity(self):
        self.write_baseline_corpus()
        self.write_session("garden", "in-self", [
            tool_use("2026-05-05T00:00:00+00:00", "Skill", {"skill": "self-improve"}),
            tool_use("2026-05-05T00:00:01+00:00", "Bash", {"command": "grep widget transcripts"}),
        ])
        proc = self.audit("fixes", "add", "--id", "widget-boom", "--problem", "p", "--mechanism", "m",
                          "--commit", "abc1234", "--signature", "widget exploded",
                          "--opportunity", "widget", "--shipped", SHIPPED, "--window-days", "60")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads((self.state / "fixes.json").read_text(encoding="utf-8"))
        self.assertEqual(data["fixes"][0]["baseline"], {"rate": 0.5, "sessions": 4, "window_days": 60})

    def test_given_an_existing_id_when_add_is_repeated_then_it_is_refused(self):
        first = self.audit(
            "fixes", "add",
            "--id", "dup",
            "--problem", "first problem",
            "--mechanism", "hook",
            "--commit", "abc1234",
            "--shipped", SHIPPED,
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self.audit(
            "fixes", "add",
            "--id", "dup",
            "--problem", "second problem",
            "--mechanism", "hook",
            "--commit", "abc1234",
            "--shipped", SHIPPED,
        )
        self.assertEqual(second.returncode, 1)
        self.assertIn("duplicate id: dup", second.stderr)
        data = json.loads((self.state / "fixes.json").read_text(encoding="utf-8"))
        self.assertEqual(len(data["fixes"]), 1)
        self.assertEqual(data["fixes"][0]["problem"], "first problem")
        self.assertEqual(data["fixes"][0]["status"], "pending")

    def test_given_a_signature_and_a_reason_when_dismissed_then_it_is_appended(self):
        proc = self.audit("fixes", "dismiss", "--signature", "exit code 2", "--reason", "known noise")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads((self.state / "fixes.json").read_text(encoding="utf-8"))
        self.assertEqual(data["fixes"], [])
        self.assertEqual(data["dismissed"], [{"signature": "exit code 2", "reason": "known noise"}])


if __name__ == "__main__":
    unittest.main()
