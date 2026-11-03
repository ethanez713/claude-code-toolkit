import json
import unittest
from pathlib import Path

from orchestrator.sessions import Pane, live_sessions, parse_agents_output, resolve_pane
from support import TempCase


class AgentsParseTest(TempCase):
    def test_given_warning_prefix_when_parsed_then_json_array_is_used(self):
        rows = parse_agents_output('warning: stale\n[{"pid": 1, "status": "idle"}]\n')
        self.assertEqual(rows, [{"pid": 1, "status": "idle"}])

    def test_given_garbage_when_parsed_then_empty(self):
        self.assertEqual(parse_agents_output("nope"), [])
        self.assertEqual(parse_agents_output(""), [])

    def test_given_session_files_alone_when_loaded_then_they_are_not_liveness(self):
        (self.tmp / "sessions" / "99.json").write_text(
            json.dumps({"status": "waiting", "waitingFor": "Pick one", "cwd": "/tmp/sample"}),
            encoding="utf-8",
        )
        self.assertEqual(live_sessions(self.config()), [])

    def test_given_bad_session_file_when_agent_has_status_then_agent_status_is_used(self):
        (self.tmp / "sessions" / "7.json").write_text("{", encoding="utf-8")
        project = self.project("sample")

        def runner():
            return [{"pid": 7, "status": "waiting", "waitingFor": "Pick one", "cwd": str(project)}]

        sessions = live_sessions(self.config(agents_runner=runner))
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].status, "waiting")
        self.assertEqual(sessions[0].waiting_for, "Pick one")

    def test_given_session_file_when_agent_disagrees_then_file_wins(self):
        project = self.project("sample")
        (self.tmp / "sessions" / "7.json").write_text(
            json.dumps({"status": "idle", "cwd": str(project), "waitingFor": ""}),
            encoding="utf-8",
        )

        def runner():
            return [{"pid": 7, "status": "waiting", "waitingFor": "Pick one", "cwd": str(project)}]

        sessions = live_sessions(self.config(agents_runner=runner))
        self.assertEqual(sessions[0].status, "idle")

    def test_given_pane_ref_when_panes_file_matches_then_socket_is_resolved(self):
        project = self.project("sample")
        (self.tmp / "sessions" / "7.json").write_text(
            json.dumps({
                "status": "waiting",
                "pane": "%3",
                "waitingFor": "Hold on",
                "cwd": str(project),
            }),
            encoding="utf-8",
        )
        pane_file = self.tmp / "panes" / "ccar"
        pane_file.write_text(
            f"/tmp/tmux-1000/ccar\tcc\t%3\t{project}\n",
            encoding="utf-8",
        )

        def runner():
            return "note\n" + json.dumps([{"pid": 7, "status": "idle", "cwd": str(project)}])

        sessions = live_sessions(self.config(agents_runner=runner))
        self.assertEqual(sessions[0].status, "waiting")
        self.assertEqual(sessions[0].socket, "/tmp/tmux-1000/ccar")
        self.assertEqual(sessions[0].tmux_session, "cc")
        self.assertEqual(sessions[0].tmux_pane, "%3")
        self.assertEqual(sessions[0].waiting_for, "Hold on")

    def test_given_tmux_field_when_pane_is_absent_then_it_is_the_pane_ref(self):
        project = self.project("sample")
        (self.tmp / "sessions" / "8.json").write_text(
            json.dumps({"status": "shell", "tmux": "cc.%2", "cwd": str(project)}),
            encoding="utf-8",
        )
        (self.tmp / "panes" / "one").write_text(
            f"/tmp/tmux-1000/ccar\tcc\t%2\t{project}\n",
            encoding="utf-8",
        )

        def runner():
            return [{"pid": 8, "status": "idle"}]

        sessions = live_sessions(self.config(agents_runner=runner))
        self.assertEqual(sessions[0].status, "shell")
        self.assertEqual(sessions[0].socket, "/tmp/tmux-1000/ccar")

    def test_given_runner_raises_when_loaded_then_no_sessions(self):
        def runner():
            raise RuntimeError("claude failed")

        self.assertEqual(live_sessions(self.config(agents_runner=runner)), [])


class FullPaneRefTest(unittest.TestCase):
    def test_given_a_session_file_ref_when_resolved_then_the_registered_pane_matches(self):
        pane = Pane("/tmp/sock", "cc", "%24", "/p", Path("/reg/key"))
        self.assertIs(resolve_pane("cc:@24.%24", [pane]), pane)

    def test_given_the_same_pane_on_another_session_when_resolved_then_it_does_not_match(self):
        pane = Pane("/tmp/sock", "other", "%24", "/p", Path("/reg/key"))
        self.assertIsNone(resolve_pane("cc:@24.%24", [pane]))
