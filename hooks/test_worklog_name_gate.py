import json
import os
import subprocess
import sys
import unittest

HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "worklog_name_gate.py")
WORKLOG = os.path.expanduser("~/.claude/worklog")


def run(event):
    proc = subprocess.run([sys.executable, HOOK], input=json.dumps(event), capture_output=True, text=True)
    return proc.returncode, proc.stderr


class WorklogNameGate(unittest.TestCase):
    def test_given_slug_named_worklog_when_written_then_blocked_with_basename(self):
        code, err = run({"tool_name": "Write", "cwd": "/home/x/garden",
                         "tool_input": {"file_path": f"{WORKLOG}/-home-x-garden.md"}})
        self.assertEqual(code, 2)
        self.assertIn("worklog/garden.md", err)

    def test_given_basename_worklog_when_edited_then_allowed(self):
        self.assertEqual(run({"tool_name": "Edit", "cwd": "/home/x/garden",
                              "tool_input": {"file_path": f"{WORKLOG}/garden.md"}})[0], 0)

    def test_given_dash_file_outside_worklog_when_written_then_allowed(self):
        self.assertEqual(run({"tool_name": "Write",
                              "tool_input": {"file_path": "/tmp/-home-x-garden.md"}})[0], 0)

    def test_given_unparseable_event_when_run_then_allowed(self):
        proc = subprocess.run([sys.executable, HOOK], input="not json", capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0)


if __name__ == "__main__":
    unittest.main()
