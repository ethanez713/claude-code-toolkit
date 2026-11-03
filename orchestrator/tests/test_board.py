import hashlib
import json
import os
import shutil
import stat
from datetime import date, datetime, timezone

from orchestrator.board import CommandError, add, answer, done, scan, snooze
from orchestrator.markers import marker_id
from support import TempCase, local_noon
from test_markers import WORKLOG
from test_plans import FIXTURE
from test_projects import git


def digest(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:4]


class NumberingTest(TempCase):
    def write_board(self):
        self.project("sample")
        (self.worklogs / "sample.md").write_text(
            "- [?] decide | anywhere | Newer decide | since: 2026-11-01\n"
            "- [?] decide | anywhere | Older decide | since: 2026-10-01\n"
            "- [?] check | phone | Old check | since: 2026-09-01\n"
            "- [a] soak | anywhere | Answered one | since: 2026-10-15 | answer: noted | answered: 2026-10-20\n"
            "- [ ] B. Ready step\n",
            encoding="utf-8",
        )

    def test_given_sections_when_scanned_then_numbers_are_global_and_oldest_first(self):
        self.write_board()
        result = scan(self.config())
        by_text = {item["text"]: item for item in result.items}
        self.assertEqual(by_text["Older decide"]["number"], 1)
        self.assertEqual(by_text["Newer decide"]["number"], 2)
        self.assertEqual(by_text["Old check"]["number"], 3)
        self.assertEqual(by_text["Answered one"]["number"], 4)
        self.assertEqual(by_text["Answered one"]["section"], "Answered, awaiting pick-up")
        self.assertIsNone(by_text["B. Ready step"]["number"])
        self.assertEqual(by_text["B. Ready step"]["section"], "Runnable")
        self.assertEqual(result.waiting, 3)
        self.assertIn(
            f"  1  sample  decide  anywhere  Older decide  32d  sample#{digest('Older decide')}",
            result.text,
        )
        self.assertIn(
            f"  3  sample  check  phone  Old check  62d  sample#{digest('Old check')}",
            result.text,
        )
        self.assertIn("\n     sample  run  worklog  B. Ready step  0d  sample#step-B\n", result.text)
        self.assertIn("snoozed 0 · backlog 0 · orphans: none", result.text)
        self.assertEqual((self.tmp / "STATUS.md").read_text(encoding="utf-8"), result.text)

    def test_given_same_day_when_ordered_then_id_breaks_the_tie(self):
        self.project("sample")
        (self.worklogs / "sample.md").write_text(
            "- [?] decide | anywhere | Alpha item | since: 2026-11-01\n"
            "- [?] decide | anywhere | Beta item | since: 2026-11-01\n",
            encoding="utf-8",
        )
        result = scan(self.config())
        first, second = sorted([marker_id("sample", "Alpha item"), marker_id("sample", "Beta item")])
        numbers = {item["id"]: item["number"] for item in result.items}
        self.assertLess(numbers[first], numbers[second])

    def test_given_future_snooze_when_applied_then_item_hides_and_numbers_shift(self):
        self.write_board()
        cfg = self.config()
        scan(cfg)
        result = snooze(cfg, "1", "2026-11-03")
        texts = [item["text"] for item in result.items]
        self.assertNotIn("Older decide", texts)
        self.assertEqual(result.snoozed, 1)
        by_text = {item["text"]: item for item in result.items}
        self.assertEqual(by_text["Newer decide"]["number"], 1)
        self.assertEqual(by_text["Old check"]["number"], 2)
        self.assertEqual(by_text["Answered one"]["number"], 3)
        self.assertIn("snoozed 1 · backlog 0 · orphans: none", result.text)

    def test_given_snooze_of_one_day_when_the_day_arrives_then_item_returns(self):
        self.project("sample")
        (self.worklogs / "sample.md").write_text(
            "- [?] decide | anywhere | Pick one\n", encoding="utf-8"
        )
        cfg = self.config()
        hidden = snooze(cfg, "1", "1d")
        self.assertNotIn("Pick one", hidden.text)
        self.assertEqual(hidden.snoozed, 1)
        self.clock["now"] = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)
        shown = scan(cfg)
        self.assertIn("Pick one", shown.text)
        self.assertEqual(shown.snoozed, 0)

    def test_given_snooze_until_today_when_applied_then_item_stays(self):
        self.project("sample")
        (self.worklogs / "sample.md").write_text(
            "- [?] decide | anywhere | Pick one\n", encoding="utf-8"
        )
        cfg = self.config()
        for token in ("0d", "2026-11-02"):
            with self.subTest(token=token):
                result = snooze(cfg, "1", token)
                self.assertIn("Pick one", result.text)
                self.assertEqual(result.snoozed, 0)

    def test_given_no_since_when_scanned_again_then_age_uses_first_seen(self):
        self.project("sample")
        (self.worklogs / "sample.md").write_text(
            "- [?] decide | anywhere | Pick one\n", encoding="utf-8"
        )
        cfg = self.config()
        first = scan(cfg)
        self.assertIn(" 0d ", first.text)
        self.clock["now"] = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)
        second = scan(cfg)
        item = next(item for item in second.items if item["text"] == "Pick one")
        self.assertEqual(item["since"], "2026-11-02")
        self.assertEqual(item["age"], 3)

    def test_given_number_or_id_when_answered_then_the_same_line_changes(self):
        self.write_board()
        cfg = self.config()
        scan(cfg)
        answered = answer(cfg, "1", "Ship it")
        path = self.worklogs / "sample.md"
        text = path.read_text(encoding="utf-8")
        self.assertIn(
            "- [a] decide | anywhere | Older decide | since: 2026-10-01 | answer: Ship it | answered: 2026-11-02",
            text,
        )
        item = next(item for item in answered.items if item["text"] == "Older decide")
        self.assertEqual(item["id"], marker_id("sample", "Older decide"))
        self.assertEqual(item["section"], "Answered, awaiting pick-up")
        again = answer(cfg, marker_id("sample", "Newer decide"), "Later")
        self.assertIn("answer: Later", path.read_text(encoding="utf-8"))
        newer = next(item for item in again.items if item["text"] == "Newer decide")
        self.assertEqual(newer["marker"]["mark"], "a")
        self.assertEqual(newer["id"], marker_id("sample", "Newer decide"))

    def test_given_bad_date_when_snoozed_then_it_is_refused(self):
        self.project("sample")
        (self.worklogs / "sample.md").write_text(
            "- [?] decide | anywhere | Pick one\n", encoding="utf-8"
        )
        with self.assertRaises(CommandError):
            snooze(self.config(), "1", "tomorrow")


class CommandTest(TempCase):
    def test_given_add_when_run_then_line_order_is_unblocks_gate_since(self):
        self.project("sample")
        result = add(
            self.config(), "sample", "decide", "anywhere", "Pick one",
            unblocks="4", gate="r6",
        )
        path = self.worklogs / "sample.md"
        self.assertEqual(
            path.read_text(encoding="utf-8"),
            "- [?] decide | anywhere | Pick one | unblocks: 4 | gate: r6 | since: 2026-11-02\n",
        )
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertIn("Pick one", result.text)

    def test_given_separator_in_text_when_added_or_answered_then_it_is_refused(self):
        self.project("sample")
        cfg = self.config()
        with self.assertRaises(CommandError):
            add(cfg, "sample", "decide", "anywhere", "left | right")
        self.assertFalse((self.worklogs / "sample.md").exists())
        (self.worklogs / "sample.md").write_text(
            "- [?] decide | anywhere | Pick one\n", encoding="utf-8"
        )
        with self.assertRaises(CommandError):
            answer(cfg, marker_id("sample", "Pick one"), "left | right")

    def test_given_malformed_marker_when_scanned_then_it_shows_in_decide(self):
        self.project("sample")
        (self.worklogs / "sample.md").write_text(
            "- [?] this is not split\n", encoding="utf-8"
        )
        result = scan(self.config())
        item = result.items[0]
        self.assertEqual(item["kind"], "?")
        self.assertEqual(item["section"], "Decide")
        self.assertEqual(item["text"], "this is not split")

    def test_given_plan_gate_when_worklog_has_the_gate_then_the_row_is_replaced(self):
        dest = self.project("sample") / "docs" / "plans"
        dest.mkdir(parents=True)
        shutil.copy(FIXTURE, dest / "README.md")
        (self.worklogs / "sample.md").write_text(
            "- [?] decide | anywhere | Richer gate text | gate: r6\n",
            encoding="utf-8",
        )
        result = scan(self.config())
        r6 = [item for item in result.items if item.get("gate") == "r6"]
        self.assertEqual(len(r6), 1)
        self.assertEqual(r6[0]["source"], "worklog")
        self.assertEqual(r6[0]["text"], "Richer gate text")
        self.assertEqual(r6[0]["section"], "Decide")
        self.assertTrue(any(item["id"] == "sample#G4" and item["source"] == "gate" for item in result.items))

    def test_given_plan_gate_when_answered_then_an_answered_line_is_appended(self):
        project = self.project("sample")
        readme = project / "docs" / "plans" / "README.md"
        readme.parent.mkdir(parents=True)
        shutil.copy(FIXTURE, readme)
        before = readme.read_bytes()
        cfg = self.config()
        result = answer(cfg, "sample#G4", "Looks good")
        self.assertEqual(readme.read_bytes(), before)
        line = (self.worklogs / "sample.md").read_text(encoding="utf-8")
        self.assertIn(
            "- [a] check | phone | Welcome — Schema flag | gate: G4 | answer: Looks good | answered: 2026-11-02",
            line,
        )
        self.assertFalse(any(item["source"] == "gate" and item["gate"] == "G4" for item in result.items))
        answered = [item for item in result.items if item.get("gate") == "G4"]
        self.assertEqual(len(answered), 1)
        self.assertEqual(answered[0]["section"], "Answered, awaiting pick-up")

    def test_given_gate_or_git_or_runnable_when_done_then_it_is_refused(self):
        project = self.project("sample")
        readme = project / "docs" / "plans" / "README.md"
        readme.parent.mkdir(parents=True)
        shutil.copy(FIXTURE, readme)
        before = readme.read_bytes()
        git(project, "init", "-b", "main")
        (project / "note.txt").write_text("synthetic\n", encoding="utf-8")
        cfg = self.config()
        scan(cfg)
        with self.assertRaises(CommandError) as gate_error:
            done(cfg, "sample#G4")
        self.assertIn("gates", str(gate_error.exception))
        self.assertEqual(readme.read_bytes(), before)
        with self.assertRaises(CommandError) as git_error:
            done(cfg, "sample#git-dirty")
        self.assertIn("git", str(git_error.exception))
        with self.assertRaises(CommandError) as run_error:
            done(cfg, "sample#row-14")
        self.assertIn("not a worklog line", str(run_error.exception))
        (self.worklogs / "sample.md").write_text(
            "- [?] decide | anywhere | Pick one | gate: r6\n", encoding="utf-8"
        )
        scan(cfg)
        with self.assertRaises(CommandError):
            done(cfg, marker_id("sample", "Pick one"))

    def test_given_worklog_line_when_done_then_only_that_line_is_removed(self):
        self.project("sample")
        path = self.worklogs / "sample.md"
        path.write_bytes(b"keep me  \n- [?] decide | anywhere | Pick one\nkeep2  \n")
        path.chmod(0o640)
        cfg = self.config()
        scan(cfg)
        done(cfg, "1")
        self.assertEqual(path.read_bytes(), b"keep me  \nkeep2  \n")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)

    def test_given_answer_when_neighbour_lines_exist_then_they_stay(self):
        self.project("sample")
        path = self.worklogs / "sample.md"
        path.write_bytes(b"keep me  \n- [?] decide | anywhere | Pick one | since: 2026-10-01\n")
        cfg = self.config()
        scan(cfg)
        answer(cfg, marker_id("sample", "Pick one"), "Ship it")
        data = path.read_bytes()
        self.assertTrue(data.startswith(b"keep me  \n"))
        self.assertIn(b"answer: Ship it", data)
        self.assertIn(b"since: 2026-10-01", data)


class RunnableScanTest(TempCase):
    def test_given_old_worklog_when_scanned_then_markers_show_and_steps_do_not(self):
        self.project("sample")
        path = self.worklogs / "sample.md"
        path.write_text("- [?] decide | anywhere | Keep this visible\n" + WORKLOG, encoding="utf-8")
        stamp = local_noon(date(2026, 10, 1)).timestamp()
        os.utime(path, (stamp, stamp))
        result = scan(self.config())
        self.assertIn("Keep this visible", result.text)
        self.assertNotIn("sample#step-", result.text)
        self.assertNotIn("Fenced question", result.text)

    def test_given_recent_worklog_when_scanned_then_ready_steps_and_backlog_show(self):
        self.project("sample")
        path = self.worklogs / "sample.md"
        path.write_text(WORKLOG, encoding="utf-8")
        stamp = local_noon(date(2026, 10, 25)).timestamp()
        os.utime(path, (stamp, stamp))
        result = scan(self.config())
        self.assertIn("sample#step-B", result.text)
        self.assertIn("sample#step-L", result.text)
        self.assertIn("sample#step-M", result.text)
        self.assertIn("sample#step-I", result.text)
        self.assertNotIn("sample#step-C", result.text)
        self.assertNotIn("sample#step-E", result.text)
        self.assertNotIn("sample#step-F", result.text)
        self.assertNotIn("sample#step-G", result.text)
        self.assertNotIn("sample#step-Z", result.text)
        self.assertNotIn("sample#step-N", result.text)
        self.assertNotIn("Fenced question", result.text)
        self.assertIn("backlog 2", result.text)

    def test_given_mtime_on_the_runnable_boundary_when_scanned_then_that_day_counts(self):
        self.project("sample")
        path = self.worklogs / "sample.md"
        path.write_text("- [ ] B. Go\n", encoding="utf-8")
        included = local_noon(date(2026, 10, 19)).timestamp()
        os.utime(path, (included, included))
        self.assertIn("sample#step-B", scan(self.config()).text)
        excluded = local_noon(date(2026, 10, 18)).timestamp()
        os.utime(path, (excluded, excluded))
        self.assertNotIn("sample#step-B", scan(self.config()).text)

    def test_given_orphan_steps_when_recent_then_they_are_runnable(self):
        path = self.worklogs / "orphan.md"
        path.write_text("- [ ] B. Go\n", encoding="utf-8")
        stamp = local_noon(date(2026, 10, 25)).timestamp()
        os.utime(path, (stamp, stamp))
        result = scan(self.config())
        self.assertIn("orphan#step-B", result.text)
        self.assertEqual(result.orphans, ["orphan"])


class SessionBoardTest(TempCase):
    def dirty_plan(self):
        path = self.project("sample")
        git(path, "init", "-b", "main")
        (path / "note.txt").write_text("synthetic\n", encoding="utf-8")
        plan = path / "docs" / "plans"
        plan.mkdir(parents=True)
        (plan / "README.md").write_text(
            "| # | Phase | Outcome | needs | Status |\n"
            "|---|---|---|---|---|\n"
            "| 1 | Paint | Walls | — | TODO |\n",
            encoding="utf-8",
        )
        return path

    def test_given_busy_session_when_scanned_then_git_and_runnable_hide_and_session_runs(self):
        path = self.dirty_plan()

        def runner():
            return [{"pid": 4, "status": "busy", "cwd": str(path)}]

        result = scan(self.config(agents_runner=runner))
        self.assertFalse(any(item["source"] == "git" for item in result.items))
        self.assertFalse(any(item["section"] == "Runnable" for item in result.items))
        running = [item for item in result.items if item["section"] == "Running"]
        self.assertEqual([item["project"] for item in running], ["sample"])

    def test_given_waiting_session_when_scanned_then_right_now_shows_and_git_stays(self):
        path = self.dirty_plan()

        def runner():
            return [{"pid": 4, "status": "waiting", "waitingFor": "Hold on", "cwd": str(path)}]

        result = scan(self.config(agents_runner=runner))
        self.assertTrue(any(
            item["section"] == "Right now" and item["text"] == "Hold on" for item in result.items
        ))
        self.assertTrue(any(item["id"] == "sample#git-dirty" for item in result.items))
        row = next(item for item in result.items if item["id"] == "sample#row-1")
        self.assertEqual(row["section"], "Runnable")

    def test_given_idle_session_when_scanned_then_git_stays_and_right_now_is_empty(self):
        path = self.dirty_plan()

        def runner():
            return [{"pid": 4, "status": "idle", "cwd": str(path)}]

        result = scan(self.config(agents_runner=runner))
        self.assertFalse(any(item["section"] == "Right now" for item in result.items))
        self.assertTrue(any(item["id"] == "sample#git-dirty" for item in result.items))

    def test_given_shell_session_when_scanned_then_git_stays_and_right_now_is_empty(self):
        path = self.dirty_plan()

        def runner():
            return [{"pid": 4, "status": "shell", "cwd": str(path)}]

        result = scan(self.config(agents_runner=runner))
        self.assertFalse(any(item["section"] == "Right now" for item in result.items))
        self.assertTrue(any(item["id"] == "sample#git-dirty" for item in result.items))

    def test_given_unknown_status_when_scanned_then_it_is_not_busy(self):
        path = self.dirty_plan()

        def runner():
            return [{"pid": 4, "status": "thinking", "cwd": str(path)}]

        result = scan(self.config(agents_runner=runner))
        self.assertTrue(any(item["id"] == "sample#git-dirty" for item in result.items))
        row = next(item for item in result.items if item["id"] == "sample#row-1")
        self.assertEqual(row["section"], "Runnable")

    def test_given_no_git_check_when_dirty_then_git_items_are_skipped(self):
        self.dirty_plan()
        result = scan(self.config(no_git_check=("sample",)))
        self.assertFalse(any(item["source"] == "git" for item in result.items))
        self.assertTrue(any(item["id"] == "sample#row-1" for item in result.items))

    def test_given_failing_agents_when_scanned_then_worklog_items_remain(self):
        self.project("sample")
        (self.worklogs / "sample.md").write_text(
            "- [?] decide | anywhere | Pick one\n", encoding="utf-8"
        )

        def runner():
            raise RuntimeError("claude failed")

        result = scan(self.config(agents_runner=runner))
        self.assertIn("Pick one", result.text)

    def test_given_warning_prefixed_agents_when_scanned_then_waiting_shows(self):
        path = self.project("sample")

        def runner():
            return "warning: stale\n" + json.dumps([{
                "pid": 4,
                "status": "waiting",
                "waitingFor": "Pick a colour",
                "cwd": str(path),
            }])

        result = scan(self.config(agents_runner=runner))
        self.assertIn("Pick a colour", result.text)
        self.assertIn("Right now", result.text)


class FailSoftTest(TempCase):
    def test_given_unreadable_worklog_when_scanned_then_other_items_show(self):
        if os.geteuid() == 0:
            self.skipTest("root ignores mode 000")
        self.project("sample")
        good = self.worklogs / "sample.md"
        good.write_text("- [?] decide | anywhere | Visible line\n", encoding="utf-8")
        bad = self.worklogs / "other.md"
        bad.write_text("- [?] decide | anywhere | Hidden line\n", encoding="utf-8")
        bad.chmod(0)
        self.restore_mode(bad)
        result = scan(self.config())
        self.assertIn("Visible line", result.text)
        self.assertNotIn("Hidden line", result.text)

    def test_given_corrupt_state_when_scanned_then_board_still_renders(self):
        self.project("sample")
        (self.worklogs / "sample.md").write_text(
            "- [?] decide | anywhere | Visible line\n", encoding="utf-8"
        )
        cfg = self.config()
        cfg.state_dir.mkdir()
        (cfg.state_dir / "board.json").write_text("{", encoding="utf-8")
        result = scan(cfg)
        self.assertIn("Visible line", result.text)
        stored = json.loads((cfg.state_dir / "board.json").read_text(encoding="utf-8"))
        self.assertIn("first_seen", stored)
        self.assertEqual(stat.S_IMODE(cfg.state_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((cfg.state_dir / "board.json").stat().st_mode), 0o600)
        self.assertEqual(cfg.status_path.read_text(encoding="utf-8"), result.text)

    def test_given_non_repo_project_when_scanned_then_there_is_no_traceback(self):
        self.project("sample")
        (self.worklogs / "sample.md").write_text(
            "- [?] decide | anywhere | Pick one\n", encoding="utf-8"
        )
        result = scan(self.config())
        self.assertIn("Pick one", result.text)
        self.assertFalse(any(item["source"] == "git" for item in result.items))
