import json
import os
from datetime import date, timedelta

from orchestrator.board import acknowledge, scan
from orchestrator.sweep import candidates_for
from orchestrator.projects import discover_projects
from support import TempCase, local_noon


def transcript_dirname(path):
    return path.resolve().as_posix().replace("/", "-").replace("_", "-")


class SweepTest(TempCase):
    def setUp(self):
        super().setUp()
        self.project_path = self.project("sample")

    def write_jsonl(self, name, rows, directory=None, mtime=None):
        folder = directory if directory is not None else (
            self.tmp / "transcripts" / transcript_dirname(self.project_path)
        )
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{name}.jsonl"
        payload = "".join(json.dumps(row) + "\n" for row in rows)
        path.write_text(payload, encoding="utf-8")
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def assistant(self, text, **extra):
        row = {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": text}]},
            "timestamp": extra.pop("timestamp", "2026-11-01T12:00:00Z"),
        }
        row.update(extra)
        return row

    def endings(self, cfg=None):
        cfg = cfg or self.config()
        projects = discover_projects(cfg)
        return candidates_for(cfg, projects, {}, set())

    def test_given_question_in_the_tail_when_swept_then_it_is_a_candidate(self):
        path = self.write_jsonl("sess", [self.assistant("Are you ready?")])
        found = self.endings()
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].session, "sess")
        self.assertEqual(found[0].reason, "?")
        self.assertEqual(found[0].transcript, path)
        result = scan(self.config())
        self.assertEqual(result.waiting, 0)
        self.assertIn("Session endings to review", result.text)
        self.assertIsNone(next(item["number"] for item in result.items if item["source"] == "sweep"))

    def test_given_ask_phrases_when_swept_then_each_is_a_reason(self):
        phrases = [
            ("This needs you today", "needs you"),
            ("It is your call", "your call"),
            ("your check is next", "your check"),
            ("Want me to start", "want me to"),
            ("Let me know", "let me know"),
        ]
        for text, reason in phrases:
            with self.subTest(reason=reason):
                self.write_jsonl("sess", [self.assistant(text)])
                found = self.endings()
                self.assertEqual([item.reason for item in found], [reason])
                for child in (self.tmp / "transcripts" / transcript_dirname(self.project_path)).iterdir():
                    child.unlink()

    def test_given_plain_goodbye_when_swept_then_it_is_not_a_candidate(self):
        self.write_jsonl("sess", [self.assistant("All finished.")])
        self.assertEqual(self.endings(), [])

    def test_given_question_before_the_window_when_swept_then_it_is_not_a_candidate(self):
        self.write_jsonl("sess", [self.assistant("?" + ("x" * 700))])
        self.assertEqual(self.endings(), [])

    def test_given_user_text_after_when_swept_then_it_is_dropped(self):
        self.write_jsonl("sess", [
            self.assistant("Are you ready?"),
            {"type": "user", "message": {"content": [{"type": "text", "text": "done"}]},
             "timestamp": "2026-11-01T13:00:00Z"},
        ])
        self.assertEqual(self.endings(), [])

    def test_given_tool_result_user_when_swept_then_the_ask_stays(self):
        self.write_jsonl("sess", [
            self.assistant("Are you ready?"),
            {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]},
             "timestamp": "2026-11-01T13:00:00Z"},
        ])
        self.assertEqual(len(self.endings()), 1)

    def test_given_meta_user_text_when_swept_then_it_does_not_count_as_a_reply(self):
        self.write_jsonl("sess", [
            self.assistant("Are you ready?"),
            {"type": "user", "isMeta": True,
             "message": {"content": [{"type": "text", "text": "hello"}]},
             "timestamp": "2026-11-01T13:00:00Z"},
        ])
        self.assertEqual(len(self.endings()), 1)

    def test_given_sidechain_ask_after_a_goodbye_when_swept_then_it_is_ignored(self):
        self.write_jsonl("sess", [
            self.assistant("All finished."),
            self.assistant("Should I?", isSidechain=True),
        ])
        self.assertEqual(self.endings(), [])

    def test_given_only_a_sidechain_ask_when_swept_then_it_is_not_a_candidate(self):
        self.write_jsonl("sess", [self.assistant("Should I?", isSidechain=True)])
        self.assertEqual(self.endings(), [])

    def test_given_string_content_with_a_question_when_swept_then_it_is_a_candidate(self):
        row = {
            "type": "assistant",
            "message": {"content": "Are you sure?"},
            "timestamp": "2026-11-01T12:00:00Z",
        }
        self.write_jsonl("sess", [row])
        found = self.endings()
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].reason, "?")

    def test_given_bad_jsonl_line_when_swept_then_the_good_row_still_counts(self):
        folder = self.tmp / "transcripts" / transcript_dirname(self.project_path)
        folder.mkdir(parents=True)
        path = folder / "sess.jsonl"
        good = json.dumps(self.assistant("Are you ready?"))
        path.write_text("NOT JSON\n" + good + "\n", encoding="utf-8")
        self.assertEqual(len(self.endings()), 1)

    def test_given_age_equal_to_sweep_days_when_swept_then_it_is_kept(self):
        stamp = local_noon(date(2026, 10, 3)).isoformat()
        self.write_jsonl("sess", [self.assistant("Are you ready?", timestamp=stamp)])
        self.assertEqual(len(self.endings()), 1)

    def test_given_age_past_sweep_days_when_swept_then_it_is_dropped(self):
        for day in (date(2026, 10, 2), date(2026, 9, 1)):
            with self.subTest(day=day):
                stamp = local_noon(day).isoformat()
                self.write_jsonl("sess", [self.assistant("Are you ready?", timestamp=stamp)])
                self.assertEqual(self.endings(), [])
                folder = self.tmp / "transcripts" / transcript_dirname(self.project_path)
                for child in folder.iterdir():
                    child.unlink()

    def test_given_this_repo_when_swept_then_it_is_dropped(self):
        self.write_jsonl("sess", [self.assistant("Are you ready?")])
        other = self.project("other")
        other_dir = self.tmp / "transcripts" / transcript_dirname(other)
        self.write_jsonl("other-sess", [self.assistant("Let me know")], directory=other_dir)
        found = self.endings(self.config(repo_dir=self.project_path))
        self.assertEqual([item.project for item in found], ["other"])

    def _sweep_sessions(self):
        result = scan(self.config())
        return [item["session"] for item in result.items if item["source"] == "sweep"]

    def test_given_newer_question_worklog_when_swept_then_it_is_dropped(self):
        ended = local_noon(date(2026, 11, 1))
        self.write_jsonl("sess", [self.assistant("Are you ready?", timestamp=ended.isoformat())])
        worklog = self.worklogs / "sample.md"
        worklog.write_text("- [?] decide | anywhere | Noted\n", encoding="utf-8")
        later = (ended + timedelta(hours=2)).timestamp()
        os.utime(worklog, (later, later))
        self.assertEqual(self._sweep_sessions(), [])

    def test_given_older_question_worklog_when_swept_then_it_is_kept(self):
        ended = local_noon(date(2026, 11, 1))
        self.write_jsonl("sess", [self.assistant("Are you ready?", timestamp=ended.isoformat())])
        worklog = self.worklogs / "sample.md"
        worklog.write_text("- [?] decide | anywhere | Noted\n", encoding="utf-8")
        earlier = (ended - timedelta(days=2)).timestamp()
        os.utime(worklog, (earlier, earlier))
        self.assertEqual(self._sweep_sessions(), ["sess"])

    def test_given_newer_worklog_without_a_question_when_swept_then_it_is_kept(self):
        ended = local_noon(date(2026, 11, 1))
        self.write_jsonl("sess", [self.assistant("Are you ready?", timestamp=ended.isoformat())])
        worklog = self.worklogs / "sample.md"
        worklog.write_text("# notes\n- [x] A. done\n", encoding="utf-8")
        later = (ended + timedelta(hours=2)).timestamp()
        os.utime(worklog, (later, later))
        self.assertEqual(self._sweep_sessions(), ["sess"])

    def test_given_full_ack_when_another_session_shares_a_prefix_then_only_the_match_drops(self):
        self.write_jsonl("abcdef12", [self.assistant("Are you ready?")])
        other = self.project("other")
        other_dir = self.tmp / "transcripts" / transcript_dirname(other)
        self.write_jsonl("abc", [self.assistant("Let me know")], directory=other_dir)
        cfg = self.config()
        matched = acknowledge(cfg, "abcdef12")
        self.assertEqual(matched, ["abcdef12"])
        result = scan(cfg)
        sessions = {item["session"] for item in result.items if item["source"] == "sweep"}
        self.assertEqual(sessions, {"abc"})
        stored = json.loads((cfg.state_dir / "board.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["acked"], ["abcdef12"])

    def test_given_unreadable_transcript_when_swept_then_the_board_does_not_crash(self):
        if os.geteuid() == 0:
            self.skipTest("root ignores mode 000")
        path = self.write_jsonl("sess", [self.assistant("Are you ready?")])
        path.chmod(0)
        self.restore_mode(path)
        result = scan(self.config())
        self.assertNotIn("sess", result.text)
        self.assertIn("orphans: none", result.text)

    def test_given_older_question_and_newer_goodbye_when_swept_then_only_the_latest_counts(self):
        self.write_jsonl("old", [self.assistant("Are you ready?")], mtime=1_000)
        self.write_jsonl("new", [self.assistant("All finished.")], mtime=2_000)
        self.assertEqual(self.endings(), [])

    def test_given_nested_transcript_when_swept_then_it_is_ignored(self):
        folder = self.tmp / "transcripts" / transcript_dirname(self.project_path)
        nested = folder / "nested"
        self.write_jsonl("ask", [self.assistant("Are you ready?")], directory=nested)
        self.write_jsonl("top", [self.assistant("All finished.")], directory=folder, mtime=2_000)
        self.assertEqual(self.endings(), [])

    def test_given_underscore_in_the_project_name_when_swept_then_hyphen_dir_is_used(self):
        app = self.project("my_app")
        hyphen = self.tmp / "transcripts" / transcript_dirname(app)
        slash_only = app.resolve().as_posix().replace("/", "-")
        decoy = self.tmp / "transcripts" / slash_only
        self.write_jsonl("decoy", [self.assistant("Are you ready?")], directory=decoy)
        self.write_jsonl("real", [self.assistant("All finished.")], directory=hyphen)
        self.assertNotEqual(hyphen, decoy)
        self.assertEqual(self.endings(), [])
        for child in hyphen.iterdir():
            child.unlink()
        self.write_jsonl("real", [self.assistant("Are you ready?")], directory=hyphen)
        found = self.endings()
        self.assertEqual([item.session for item in found], ["real"])
        self.assertEqual(found[0].project, "my_app")
