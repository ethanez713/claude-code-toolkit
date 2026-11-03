"""Calls before the first edit, from fixture events and Grok session transcripts."""

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("first_edit", SKILL / "scripts" / "first_edit.py")
first_edit = importlib.util.module_from_spec(_spec)
sys.modules["first_edit"] = first_edit
_spec.loader.exec_module(first_edit)

JOB = "g1a2b3c"
SESSION = "01a18b9a-cf27-7731-b73f-241ee3b8e526"


def call_id(model_call, index=0):
    return f"call-{model_call:08x}-0000-0000-0000-000000000000-{index}"


def tool(kind, model_call, index=0):
    return {"sessionUpdate": "tool_call", "toolCallId": call_id(model_call, index),
            "_meta": {"x.ai/tool": {"kind": kind}}}


def update(body, timestamp="1000"):
    return json.dumps({"timestamp": timestamp, "params": {"update": body}})


class FirstEditCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name) / "state"
        self.sessions = Path(tmp.name) / "sessions"
        self.state.mkdir()
        self.sessions.mkdir()
        self.events = []

    def job_end(self, job=JOB, mode="edit", round_no=1):
        self.events.append({"event": "job_end", "job": job, "mode": mode, "round": round_no,
                            "repo": "demo", "grok_version": "grok 1.0.70 (abc) [stable]",
                            "diff_added": 90, "diff_deleted": 10})

    def transcript(self, lines, job=JOB, session=SESSION, suffix="", prompt="Goal: x"):
        workdir = self.sessions / f"%2Frepo%2F.grok-jobs%2F{job}{suffix}"
        (workdir / session).mkdir(parents=True)
        (workdir / session / "updates.jsonl").write_text("\n".join(lines) + "\n")
        with open(workdir / "prompt_history.jsonl", "a") as fh:
            fh.write(json.dumps({"session_id": session, "prompt": prompt}) + "\n")

    def collect(self):
        (self.state / "events.jsonl").write_text(
            "\n".join(json.dumps(event) for event in self.events) + "\n")
        return first_edit.collect(self.state, self.sessions)

    def test_given_parallel_tools_when_measured_then_they_count_as_one_model_call(self):
        self.job_end()
        self.transcript([
            update(tool("read", 1, 0)), update(tool("search", 1, 1)), update(tool("list", 2)),
            update(tool("edit", 3)), update(tool("execute", 4)),
        ])
        jobs, _, _ = self.collect()
        self.assertEqual((jobs[0].before, jobs[0].searches, jobs[0].total), (2, 2, 5))

    def test_given_a_write_tool_when_measured_then_it_counts_as_the_first_edit(self):
        self.job_end()
        self.transcript([update(tool("read", 1)), update(tool("write", 2))])
        jobs, _, _ = self.collect()
        self.assertEqual(jobs[0].before, 1)

    def test_given_an_edit_only_in_a_resumed_round_when_collected_then_round_one_has_no_edit(self):
        self.job_end()
        self.transcript([
            update(tool("read", 1)), update({"sessionUpdate": "turn_completed"}),
            update(tool("edit", 2)),
        ])
        jobs, unread, no_edit = self.collect()
        self.assertEqual((jobs, unread, no_edit), ([], 0, 1))

    def test_given_a_start_at_line_when_collected_then_the_job_is_marked(self):
        self.job_end()
        self.transcript([update(tool("edit", 1))], prompt="Goal: x\nStart at: a.py\nDone when: y")
        jobs, _, _ = self.collect()
        self.assertTrue(jobs[0].start_at)

    def test_given_start_at_only_in_prose_when_collected_then_the_job_is_not_marked(self):
        self.job_end()
        self.transcript([update(tool("edit", 1))], prompt="Goal: fix the Start at parser")
        jobs, _, _ = self.collect()
        self.assertFalse(jobs[0].start_at)

    def test_given_no_transcript_when_collected_then_the_job_is_reported_unread(self):
        self.job_end()
        jobs, unread, no_edit = self.collect()
        self.assertEqual((jobs, unread, no_edit), ([], 1, 0))

    def test_given_consults_and_later_rounds_when_collected_then_they_are_ignored(self):
        self.job_end(job="gconsult", mode="consult")
        self.job_end(job="gresume", round_no=2)
        jobs, unread, no_edit = self.collect()
        self.assertEqual((jobs, unread, no_edit), ([], 0, 0))

    def test_given_a_job_started_in_a_subdirectory_when_collected_then_it_still_joins(self):
        self.job_end()
        self.transcript([update(tool("edit", 1))], suffix="%2Fskills%2Fgrok-handoff")
        jobs, _, _ = self.collect()
        self.assertEqual(len(jobs), 1)

    def test_given_malformed_lines_and_foreign_ids_when_measured_then_they_are_skipped(self):
        self.job_end()
        foreign = {"sessionUpdate": "tool_call", "toolCallId": "tool_7",
                   "_meta": {"x.ai/tool": {"kind": "edit"}}}
        self.transcript(["{not json", update(foreign), update(tool("read", 1)),
                         update(tool("edit", 2))])
        jobs, _, _ = self.collect()
        self.assertEqual((jobs[0].before, jobs[0].total), (1, 3))

    def test_given_two_sessions_when_collected_then_the_numerically_earliest_is_round_one(self):
        self.job_end()
        self.transcript([update(tool("edit", 1), timestamp="1000")], session="b")
        self.transcript([update(tool("read", 1), timestamp="999"), update(tool("edit", 2))],
                        session="a")
        jobs, _, _ = self.collect()
        self.assertEqual(jobs[0].before, 1)


if __name__ == "__main__":
    unittest.main()
