import unittest
import io
import json
import os
from contextlib import redirect_stderr, redirect_stdout
from datetime import timedelta

from orchestrator.board import CommandError, scan
from orchestrator.cli import main
from orchestrator.deliver import (
    TmuxResult,
    deliver,
    open_target,
    parse_pane,
    route,
)
from orchestrator.markers import marker_id
from support import TempCase
from test_projects import git


SOCK = "/tmp/live"
DEFAULT_SOCK = "/tmp/default-sock"
PANE = "%3"
RULE = "\u2500" * 24
PROMPT = "\u276f"


def screen(lines, ctx=42, warning=None):
    body = [RULE, *lines, RULE]
    if warning is not None:
        body.append(warning)
    if ctx is None:
        body.append("    Sonnet 5.5 hi \U0001f4a1 · \U0001f5c0 /orchestrator")
    else:
        body.append(f"    Sonnet 5.5 hi \U0001f4a1 · Ctx {ctx}% · \U0001f5c0 /orchestrator")
    return "\n".join(body) + "\n"


def sends(calls):
    return [call for call in calls if "send-keys" in call]


def flat(calls):
    found = []
    for call in calls:
        found.extend(call)
    return found


def run(argv, cfg):
    out = io.StringIO()
    err = io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(argv, cfg)
    return code, out.getvalue(), err.getvalue()


class DimTextTest(unittest.TestCase):
    def test_given_a_dim_suggestion_when_parsed_then_the_box_counts_as_empty(self):
        view = parse_pane(screen(['\x1b[39m\u276f\xa0\x1b[2m1 drop it, 3 passed\x1b[0m']))
        self.assertTrue(view.empty)

    def test_given_typed_text_with_escape_codes_when_parsed_then_it_is_a_draft(self):
        view = parse_pane(screen(['\x1b[39m\u276f\xa0typed text']))
        self.assertFalse(view.empty)
        self.assertEqual(view.text, "typed text")

    def test_given_typed_text_after_dim_text_when_parsed_then_it_is_a_draft(self):
        view = parse_pane(screen(['\u276f \x1b[2mhint\x1b[22m typed']))
        self.assertFalse(view.empty)

    def test_given_typed_words_that_start_like_a_placeholder_when_parsed_then_it_is_a_draft(self):
        view = parse_pane(screen(['\u276f Try "this" instead']))
        self.assertFalse(view.empty)


class PaneParseTest(TempCase):
    def test_given_only_the_prompt_when_parsed_then_the_box_is_empty_and_ctx_is_read(self):
        view = parse_pane(screen([PROMPT]))
        self.assertTrue(view.readable)
        self.assertTrue(view.empty)
        self.assertEqual(view.text, "")
        self.assertEqual(view.context_pct, 42)

    def test_given_the_try_placeholder_when_parsed_then_the_box_counts_as_empty(self):
        view = parse_pane(screen(['\x1b[39m\u276f\xa0\x1b[2mTry "fix typecheck errors"\x1b[0m\x1b[39m\x1b[49m']))
        self.assertTrue(view.empty)
        self.assertEqual(view.context_pct, 42)

    def test_given_a_draft_when_parsed_then_the_box_is_not_empty(self):
        view = parse_pane(screen([PROMPT + " finish the migration"]))
        self.assertFalse(view.empty)
        self.assertEqual(view.text, "finish the migration")

    def test_given_several_lines_between_the_rules_when_parsed_then_all_of_them_are_the_draft(self):
        view = parse_pane(screen([PROMPT + " first line", "second line"]))
        self.assertFalse(view.empty)
        self.assertEqual(view.text, "first line\nsecond line")

    def test_given_a_placeholder_and_another_line_when_parsed_then_it_is_a_draft(self):
        view = parse_pane(screen(['\x1b[39m\u276f\xa0\x1b[2mTry "write a test"\x1b[0m', "and also this"]))
        self.assertFalse(view.empty)

    def test_given_no_rules_when_parsed_then_the_box_is_unreadable_and_ctx_is_not_scraped(self):
        view = parse_pane("the transcript mentions Ctx 99%\nno chrome here\n")
        self.assertFalse(view.readable)
        self.assertIsNone(view.empty)
        self.assertIsNone(view.text)
        self.assertIsNone(view.context_pct)

    def test_given_fewer_than_twenty_dashes_when_parsed_then_the_line_is_not_a_rule(self):
        short = "\u2500" * 19
        view = parse_pane(f"{short}\n{PROMPT} \n{short}\n  Ctx 5%\n")
        self.assertFalse(view.readable)
        self.assertIsNone(view.context_pct)

    def test_given_text_after_the_dashes_when_parsed_then_the_line_is_still_a_rule(self):
        rule = "\u2500" * 20 + " tail"
        view = parse_pane(f"{rule}\n{PROMPT} \n{rule}\n  Ctx 7%\n")
        self.assertTrue(view.readable)
        self.assertTrue(view.empty)
        self.assertEqual(view.context_pct, 7)

    def test_given_a_warning_between_the_rule_and_ctx_when_parsed_then_the_percentage_is_kept(self):
        view = parse_pane(screen([PROMPT], ctx=7, warning="  warning: still settling"))
        self.assertTrue(view.empty)
        self.assertEqual(view.context_pct, 7)

    def test_given_nbsp_padding_when_parsed_then_the_box_is_empty(self):
        view = parse_pane(screen([PROMPT + "\u00a0\u00a0\u00a0"]))
        self.assertTrue(view.empty)
        self.assertEqual(view.text, "")


class RouteTest(TempCase):
    def setUp(self):
        super().setUp()
        self.calls = []
        self.screen_text = screen([PROMPT])

    def serve(self, args):
        self.calls.append(list(args))
        if "capture-pane" in args:
            return TmuxResult(0, self.screen_text, "")
        return TmuxResult(1, "", "unexpected tmux")

    def sleep(self, _seconds):
        raise AssertionError("route slept")

    def attach(self, project, pid, status, name="focus", pane=PANE, socket=SOCK, mtime=None, agent_status=None):
        payload = {
            "status": status,
            "cwd": str(project.resolve()),
            "sessionId": f"s{pid}",
        }
        if pane is not None:
            payload["pane"] = pane
            with (self.tmp / "panes" / "ccar").open("a", encoding="utf-8") as handle:
                handle.write(f"{socket}\tcc\t{pane}\t{project.resolve()}\n")
        path = self.tmp / "sessions" / f"{pid}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return {
            "pid": pid,
            "status": agent_status or status,
            "cwd": str(project.resolve()),
            "name": name,
        }

    def configured(self, entries):
        return self.config(
            agents_runner=lambda: entries,
            tmux_runner=self.serve,
            sleeper=self.sleep,
            wrapper="/tmp/wrap",
            tmux_session="cc",
            default_socket=DEFAULT_SOCK,
        )

    def test_given_idle_under_the_threshold_when_routed_then_the_report_says_send(self):
        project = self.project("sample")
        entries = [self.attach(project, 7, "idle")]
        report = route(self.configured(entries), "sample")
        self.assertEqual(report.as_dict(), {
            "project": "sample",
            "session": {
                "pid": 7,
                "name": "focus",
                "status": "idle",
                "pane": PANE,
                "socket": SOCK,
            },
            "context_pct": 42,
            "input_empty": True,
            "in_progress": False,
            "tree_clean": True,
            "action": "send",
        })
        self.assertEqual(
            report.text(),
            "project: sample\n"
            "session: pid 7 name focus status idle pane %3 socket /tmp/live\n"
            "context_pct: 42\n"
            "input_empty: true\n"
            "in_progress: false\n"
            "tree_clean: true\n"
            "action: send\n",
        )
        self.assertEqual(self.calls, [["tmux", "-S", SOCK, "capture-pane", "-p", "-e", "-t", PANE]])
        self.assertFalse((self.tmp / "STATUS.md").exists())
        self.assertFalse((self.tmp / "state" / "board.json").exists())

    def test_given_context_just_under_the_threshold_and_in_progress_when_routed_then_action_is_send(self):
        project = self.project("sample")
        (self.worklogs / "sample.md").write_text("- [>] 2. Wire the route\n", encoding="utf-8")
        self.screen_text = screen([PROMPT], ctx=49)
        entries = [self.attach(project, 7, "idle")]
        report = route(self.configured(entries), "sample")
        self.assertEqual(report.action, "send")
        self.assertEqual(report.context_pct, 49)
        self.assertTrue(report.in_progress)

    def test_given_no_ctx_when_routed_then_unknown_context_stays_under_the_threshold(self):
        project = self.project("sample")
        (self.worklogs / "sample.md").write_text("- [>] 2. Wire the route\n", encoding="utf-8")
        self.screen_text = screen([PROMPT], ctx=None)
        entries = [self.attach(project, 7, "idle")]
        report = route(self.configured(entries), "sample")
        self.assertIsNone(report.context_pct)
        self.assertTrue(report.in_progress)
        self.assertEqual(report.action, "send")

    def test_given_context_at_the_threshold_and_a_clean_tree_when_routed_then_action_is_clear(self):
        project = self.project("sample")
        git(project, "init", "-b", "main")
        (project / "README").write_text("synthetic\n", encoding="utf-8")
        git(project, "add", "README")
        git(project, "commit", "-m", "init")
        (self.worklogs / "sample.md").write_text("- [x] 1. Done already\n", encoding="utf-8")
        self.screen_text = screen([PROMPT], ctx=50)
        entries = [self.attach(project, 7, "idle")]
        report = route(self.configured(entries), "sample")
        self.assertEqual(report.action, "clear")
        self.assertEqual(report.context_pct, 50)
        self.assertFalse(report.in_progress)
        self.assertTrue(report.tree_clean)

    def test_given_context_over_the_threshold_and_an_arrow_line_when_routed_then_action_is_compact(self):
        project = self.project("sample")
        git(project, "init", "-b", "main")
        (project / "README").write_text("synthetic\n", encoding="utf-8")
        git(project, "add", "README")
        git(project, "commit", "-m", "init")
        (self.worklogs / "sample.md").write_text("- [>] 2. Wire the route\n", encoding="utf-8")
        self.screen_text = screen([PROMPT], ctx=80)
        entries = [self.attach(project, 7, "idle")]
        report = route(self.configured(entries), "sample")
        self.assertEqual(report.action, "compact")
        self.assertTrue(report.in_progress)
        self.assertTrue(report.tree_clean)

    def test_given_context_over_the_threshold_and_a_dirty_tree_when_routed_then_action_is_compact(self):
        project = self.project("sample")
        git(project, "init", "-b", "main")
        (project / "extra.txt").write_text("synthetic\n", encoding="utf-8")
        self.screen_text = screen([PROMPT], ctx=80)
        entries = [self.attach(project, 7, "idle")]
        report = route(self.configured(entries), "sample")
        self.assertEqual(report.action, "compact")
        self.assertFalse(report.in_progress)
        self.assertFalse(report.tree_clean)

    def test_given_a_busy_session_when_routed_then_action_is_wait(self):
        project = self.project("sample")
        self.screen_text = screen([PROMPT], ctx=10)
        entries = [self.attach(project, 7, "busy")]
        report = route(self.configured(entries), "sample")
        self.assertEqual(report.action, "wait")
        self.assertEqual(report.context_pct, 10)
        self.assertEqual(report.session["status"], "busy")

    def test_given_a_waiting_session_when_routed_then_action_is_attend(self):
        project = self.project("sample")
        self.screen_text = "What do you want to do?\nCtx 80%\n"
        entries = [self.attach(project, 7, "waiting", name="ask")]
        report = route(self.configured(entries), "sample")
        self.assertEqual(report.action, "attend")
        self.assertIsNone(report.input_empty)
        self.assertIsNone(report.context_pct)
        self.assertEqual(report.session["name"], "ask")

    def test_given_no_tmux_pane_when_routed_then_action_is_unreachable_and_tmux_is_not_called(self):
        project = self.project("sample")
        entries = [self.attach(project, 7, "idle", pane=None)]
        report = route(self.configured(entries), "sample")
        self.assertEqual(report.action, "unreachable")
        self.assertIsNone(report.session["pane"])
        self.assertIsNone(report.session["socket"])
        self.assertIsNone(report.input_empty)
        self.assertEqual(self.calls, [])

    def test_given_no_live_session_when_routed_then_action_is_new(self):
        self.project("sample")
        report = route(self.configured([]), "sample")
        self.assertEqual(report.action, "new")
        self.assertIsNone(report.session)
        self.assertIsNone(report.context_pct)
        self.assertIsNone(report.input_empty)
        self.assertEqual(self.calls, [])

    def test_given_a_shell_session_when_routed_then_action_is_unreachable(self):
        project = self.project("sample")
        entries = [self.attach(project, 7, "shell")]
        report = route(self.configured(entries), "sample")
        self.assertEqual(report.action, "unreachable")
        self.assertEqual(report.session["status"], "shell")

    def test_given_an_older_idle_and_a_newer_busy_when_routed_then_idle_wins(self):
        project = self.project("sample")
        entries = [
            self.attach(project, 1, "idle", name="older", pane="%1", mtime=1_700_000_000),
            self.attach(project, 2, "busy", name="newer", pane="%2", mtime=1_700_000_100),
        ]
        report = route(self.configured(entries), "sample")
        self.assertEqual(report.session["pid"], 1)
        self.assertEqual(report.session["name"], "older")
        self.assertEqual(report.action, "send")

    def test_given_two_idle_sessions_when_routed_then_the_newest_wins(self):
        project = self.project("sample")
        entries = [
            self.attach(project, 1, "idle", name="older", pane="%1", mtime=1_700_000_000),
            self.attach(project, 2, "idle", name="newer", pane="%2", mtime=1_700_000_100),
        ]
        report = route(self.configured(entries), "sample")
        self.assertEqual(report.session["pid"], 2)
        self.assertEqual(report.session["name"], "newer")

    def test_given_two_busy_sessions_when_routed_then_the_newest_wins(self):
        project = self.project("sample")
        entries = [
            self.attach(project, 1, "busy", name="older", pane="%1", mtime=1_700_000_000),
            self.attach(project, 2, "busy", name="newer", pane="%2", mtime=1_700_000_100),
        ]
        report = route(self.configured(entries), "sample")
        self.assertEqual(report.session["pid"], 2)
        self.assertEqual(report.action, "wait")

    def test_given_another_projects_session_when_routed_then_it_is_ignored(self):
        sample = self.project("sample")
        other = self.project("other")
        entries = [
            self.attach(sample, 1, "idle", name="ours", pane="%1", mtime=1_700_000_000),
            self.attach(other, 2, "idle", name="theirs", pane="%2", mtime=1_700_000_500),
        ]
        report = route(self.configured(entries), "sample")
        self.assertEqual(report.session["pid"], 1)
        self.assertEqual(report.project, "sample")

    def test_given_a_draft_when_routed_then_the_text_stays_out_of_the_report(self):
        project = self.project("sample")
        self.screen_text = screen([PROMPT + " SECRET DRAFT"])
        entries = [self.attach(project, 7, "idle")]
        report = route(self.configured(entries), "sample")
        self.assertFalse(report.input_empty)
        self.assertEqual(report.action, "send")
        self.assertNotIn("SECRET DRAFT", report.text())
        self.assertNotIn("SECRET DRAFT", json.dumps(report.as_dict()))

    def test_given_the_agent_says_busy_and_the_file_says_idle_when_routed_then_the_file_wins(self):
        project = self.project("sample")
        entries = [self.attach(project, 7, "idle", agent_status="busy")]
        report = route(self.configured(entries), "sample")
        self.assertEqual(report.session["status"], "idle")
        self.assertEqual(report.action, "send")

    def test_given_a_number_or_an_id_when_routed_then_that_items_project_is_used(self):
        self.project("alpha")
        beta = self.project("beta")
        (self.worklogs / "alpha.md").write_text(
            "- [?] decide | anywhere | Alpha ask | since: 2026-10-01\n", encoding="utf-8",
        )
        (self.worklogs / "beta.md").write_text(
            "- [?] decide | anywhere | Beta ask | since: 2026-11-01\n", encoding="utf-8",
        )
        entries = [self.attach(beta, 9, "idle", name="beta-session", pane="%9")]
        cfg = self.configured(entries)
        scan(cfg)
        before_status = cfg.status_path.read_bytes()
        before_state = (cfg.state_dir / "board.json").read_bytes()
        by_number = route(cfg, "2")
        by_id = route(cfg, marker_id("beta", "Beta ask"))
        self.assertEqual(by_number.project, "beta")
        self.assertEqual(by_number.session["pid"], 9)
        self.assertEqual(by_id.project, "beta")
        self.assertEqual(by_id.session["pid"], 9)
        self.assertEqual(route(cfg, "1").action, "new")
        self.assertEqual(cfg.status_path.read_bytes(), before_status)
        self.assertEqual((cfg.state_dir / "board.json").read_bytes(), before_state)

    def test_given_an_unknown_number_when_routed_then_it_is_refused_without_writing(self):
        self.project("sample")
        with self.assertRaises(CommandError) as caught:
            route(self.configured([]), "1")
        self.assertEqual(str(caught.exception), "no item 1")
        self.assertFalse((self.tmp / "STATUS.md").exists())


class DeliveryTest(TempCase):
    def setUp(self):
        super().setUp()
        self.calls = []
        self.sleeps = []
        self.screens = []
        self.literals = []
        self.project_path = self.project("sample")
        self.pid = 7
        self.release_clear = True
        self.become_busy = True
        self.flip_busy_on_capture = False
        self.phase = "idle"
        self.mark = None
        self.write_file("idle", "orig")
        with (self.tmp / "panes" / "ccar").open("a", encoding="utf-8") as handle:
            handle.write(f"{SOCK}\tcc\t{PANE}\t{self.project_path.resolve()}\n")

    def write_file(self, status, session_id):
        payload = {
            "status": status,
            "cwd": str(self.project_path.resolve()),
            "pane": PANE,
            "sessionId": session_id,
        }
        (self.tmp / "sessions" / f"{self.pid}.json").write_text(
            json.dumps(payload), encoding="utf-8",
        )

    def current_status(self):
        return json.loads(
            (self.tmp / "sessions" / f"{self.pid}.json").read_text(encoding="utf-8")
        )["status"]

    def serve(self, args):
        self.calls.append(list(args))
        if "capture-pane" in args:
            if self.flip_busy_on_capture:
                self.write_file("busy", "orig")
            return TmuxResult(0, self.screens.pop(0), "")
        if "send-keys" in args and "-l" in args:
            text = args[-1]
            self.literals.append((text, self.current_status()))
            if text.startswith("/"):
                self.phase = "clearing"
                self.mark = self.clock["now"]
                self.write_file("busy", "orig")
            else:
                self.phase = "prompted"
                self.mark = self.clock["now"]
                self.write_file("idle", "after-clear")
        return TmuxResult(0, "", "")

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.clock["now"] = self.clock["now"] + timedelta(seconds=seconds)
        if self.mark is None:
            return
        elapsed = (self.clock["now"] - self.mark).total_seconds()
        if self.phase == "clearing" and self.release_clear and elapsed >= 2:
            self.phase = "settled"
            self.write_file("idle", "after-clear")
        elif self.phase == "prompted" and self.become_busy and elapsed >= 1:
            self.write_file("busy", "after-clear")

    def configured(self, entries=None):
        if entries is None:
            entries = [{
                "pid": self.pid,
                "status": "idle",
                "cwd": str(self.project_path.resolve()),
                "name": "focus",
            }]
        return self.config(
            agents_runner=lambda: entries,
            tmux_runner=self.serve,
            sleeper=self.sleep,
            wrapper="/tmp/wrap",
            tmux_session="cc",
            default_socket=DEFAULT_SOCK,
        )

    def test_given_text_still_in_the_box_when_sent_then_a_second_enter_follows(self):
        self.screens = [screen([PROMPT]), screen([PROMPT + " hello"])]
        result = deliver(self.configured(), "sample", "hello")
        self.assertEqual(result, f"sent to pid 7 pane {PANE} on {SOCK}\n")
        self.assertEqual(self.calls, [
            ["tmux", "-S", SOCK, "capture-pane", "-p", "-e", "-t", PANE],
            ["tmux", "-S", SOCK, "send-keys", "-t", PANE, "-l", "--", "hello"],
            ["tmux", "-S", SOCK, "send-keys", "-t", PANE, "Enter"],
            ["tmux", "-S", SOCK, "capture-pane", "-p", "-e", "-t", PANE],
            ["tmux", "-S", SOCK, "send-keys", "-t", PANE, "Enter"],
        ])
        self.assertEqual(self.sleeps, [0.5, 0.8])
        self.assertNotIn("C-u", flat(self.calls))

    def test_given_an_empty_box_after_enter_when_sent_then_there_is_no_second_enter(self):
        self.screens = [screen([PROMPT]), screen([PROMPT])]
        deliver(self.configured(), "sample", "hello")
        self.assertEqual(sends(self.calls), [
            ["tmux", "-S", SOCK, "send-keys", "-t", PANE, "-l", "--", "hello"],
            ["tmux", "-S", SOCK, "send-keys", "-t", PANE, "Enter"],
        ])

    def test_given_a_slash_and_a_newline_when_sent_then_it_is_one_line_and_not_a_command(self):
        self.screens = [screen([PROMPT]), screen([PROMPT])]
        deliver(self.configured(), "sample", "/clear\nstay")
        self.assertEqual(self.literals, [("clear stay", "idle")])
        self.assertNotIn("/clear", flat(self.calls))

    def test_given_a_draft_when_delivering_then_nothing_is_typed(self):
        self.screens = [screen([PROMPT + " SECRET DRAFT"])]
        with self.assertRaises(CommandError) as caught:
            deliver(self.configured(), "sample", "hello")
        self.assertEqual(str(caught.exception), "input box holds a draft")
        self.assertEqual(sends(self.calls), [])

    def test_given_a_busy_session_when_delivering_then_nothing_is_typed(self):
        self.write_file("busy", "orig")
        entries = [{
            "pid": self.pid,
            "status": "busy",
            "cwd": str(self.project_path.resolve()),
            "name": "focus",
        }]
        with self.assertRaises(CommandError) as caught:
            deliver(self.configured(entries), "sample", "hello")
        self.assertEqual(str(caught.exception), "session is busy")
        self.assertEqual(sends(self.calls), [])

    def test_given_a_waiting_session_when_delivering_then_nothing_is_typed(self):
        self.write_file("waiting", "orig")
        entries = [{
            "pid": self.pid,
            "status": "waiting",
            "cwd": str(self.project_path.resolve()),
            "name": "focus",
        }]
        with self.assertRaises(CommandError) as caught:
            deliver(self.configured(entries), "sample", "hello")
        self.assertEqual(str(caught.exception), "session is waiting")
        self.assertEqual(sends(self.calls), [])

    def test_given_a_shell_session_when_delivering_then_nothing_is_typed(self):
        self.write_file("shell", "orig")
        entries = [{
            "pid": self.pid,
            "status": "shell",
            "cwd": str(self.project_path.resolve()),
            "name": "focus",
        }]
        with self.assertRaises(CommandError) as caught:
            deliver(self.configured(entries), "sample", "hello")
        self.assertEqual(str(caught.exception), "session is shell")
        self.assertEqual(sends(self.calls), [])

    def test_given_no_rules_when_delivering_then_the_box_is_refused(self):
        self.screens = ["no chrome here\nCtx 88%\n"]
        with self.assertRaises(CommandError) as caught:
            deliver(self.configured(), "sample", "hello")
        self.assertEqual(str(caught.exception), "input box is unreadable")
        self.assertEqual(sends(self.calls), [])

    def test_given_status_turns_busy_while_the_box_is_read_when_delivering_then_nothing_is_typed(self):
        self.flip_busy_on_capture = True
        self.screens = [screen([PROMPT])]
        with self.assertRaises(CommandError) as caught:
            deliver(self.configured(), "sample", "hello")
        self.assertEqual(str(caught.exception), "session is busy")
        self.assertEqual(sends(self.calls), [])

    def test_given_the_session_never_goes_busy_when_sent_then_nothing_more_is_typed(self):
        self.become_busy = False
        self.screens = [screen([PROMPT]), screen([PROMPT + " hello"])]
        with self.assertRaises(CommandError) as caught:
            deliver(self.configured(), "sample", "hello")
        self.assertEqual(str(caught.exception), "session did not become busy")
        self.assertEqual(self.calls, [
            ["tmux", "-S", SOCK, "capture-pane", "-p", "-e", "-t", PANE],
            ["tmux", "-S", SOCK, "send-keys", "-t", PANE, "-l", "--", "hello"],
            ["tmux", "-S", SOCK, "send-keys", "-t", PANE, "Enter"],
            ["tmux", "-S", SOCK, "capture-pane", "-p", "-e", "-t", PANE],
            ["tmux", "-S", SOCK, "send-keys", "-t", PANE, "Enter"],
        ])

    def test_given_clear_returns_to_idle_when_delivering_then_the_prompt_follows(self):
        self.screens = [
            screen([PROMPT]),
            screen([PROMPT + " /clear"]),
            screen([PROMPT]),
            screen([PROMPT + " hello"]),
        ]
        result = deliver(self.configured(), "sample", "hello", reset="clear")
        self.assertEqual(result, f"sent to pid 7 pane {PANE} on {SOCK}\n")
        self.assertEqual(self.literals, [("/clear", "idle"), ("hello", "idle")])
        self.assertEqual(sends(self.calls), [
            ["tmux", "-S", SOCK, "send-keys", "-t", PANE, "-l", "--", "/clear"],
            ["tmux", "-S", SOCK, "send-keys", "-t", PANE, "Enter"],
            ["tmux", "-S", SOCK, "send-keys", "-t", PANE, "Enter"],
            ["tmux", "-S", SOCK, "send-keys", "-t", PANE, "-l", "--", "hello"],
            ["tmux", "-S", SOCK, "send-keys", "-t", PANE, "Enter"],
            ["tmux", "-S", SOCK, "send-keys", "-t", PANE, "Enter"],
        ])
        self.assertEqual(self.sleeps[:2], [0.5, 0.8])
        self.assertIn(0.2, self.sleeps)
        self.assertEqual(self.sleeps[-2:], [0.5, 0.8])
        self.assertIn("after-clear", (self.tmp / "sessions" / "7.json").read_text(encoding="utf-8"))

    def test_given_clear_stays_busy_when_delivering_then_the_prompt_is_not_typed(self):
        self.release_clear = False
        self.screens = [screen([PROMPT]), screen([PROMPT])]
        with self.assertRaises(CommandError) as caught:
            deliver(self.configured(), "sample", "hello", reset="clear")
        self.assertEqual(str(caught.exception), "session did not return to idle after /clear")
        self.assertEqual(self.literals, [("/clear", "idle")])

    def test_given_a_dry_run_when_delivering_then_the_steps_are_printed_and_no_keys_are_sent(self):
        self.screens = [screen([PROMPT]), screen([PROMPT])]
        result = deliver(self.configured(), "sample", "hello", reset="clear", dry_run=True)
        self.assertEqual(result, (
            "tmux -S /tmp/live send-keys -t %3 -l -- /clear\n"
            "sleep 0.5\n"
            "tmux -S /tmp/live send-keys -t %3 Enter\n"
            "sleep 0.8\n"
            "if-remains: tmux -S /tmp/live send-keys -t %3 Enter\n"
            "wait status idle 30\n"
            "tmux -S /tmp/live send-keys -t %3 -l -- hello\n"
            "sleep 0.5\n"
            "tmux -S /tmp/live send-keys -t %3 Enter\n"
            "sleep 0.8\n"
            "if-remains: tmux -S /tmp/live send-keys -t %3 Enter\n"
            "wait status busy 15\n"
        ))
        self.assertEqual(sends(self.calls), [])
        self.assertEqual(self.sleeps, [])

    def test_given_a_compact_dry_run_when_delivering_then_the_idle_wait_is_600_seconds(self):
        self.screens = [screen([PROMPT]), screen([PROMPT])]
        result = deliver(self.configured(), "sample", "hello", reset="compact", dry_run=True)
        self.assertIn("wait status idle 600\n", result)
        self.assertIn("-l -- /compact\n", result)
        self.assertEqual(sends(self.calls), [])

    def test_given_no_live_session_when_delivering_then_a_new_window_gets_the_text(self):
        calls = []

        def runner(args):
            calls.append(list(args))
            return TmuxResult(0, "", "")

        cfg = self.config(
            agents_runner=lambda: [],
            tmux_runner=runner,
            sleeper=self.sleep,
            wrapper="/tmp/wrap",
            tmux_session="cc",
            default_socket=DEFAULT_SOCK,
        )
        result = deliver(cfg, "sample", "hello world")
        self.assertEqual(result, "started /tmp/wrap in cc for sample\n")
        self.assertEqual(calls, [[
            "tmux", "-S", DEFAULT_SOCK, "new-window", "-t", "cc",
            "-c", str(self.project_path.resolve()),
            "/tmp/wrap 'hello world'",
        ]])

    def test_given_no_pane_when_delivering_then_a_second_window_is_not_opened(self):
        self.write_file("idle", "orig")
        payload = json.loads((self.tmp / "sessions" / "7.json").read_text(encoding="utf-8"))
        del payload["pane"]
        (self.tmp / "sessions" / "7.json").write_text(json.dumps(payload), encoding="utf-8")
        (self.tmp / "panes" / "ccar").write_text("", encoding="utf-8")
        with self.assertRaises(CommandError) as caught:
            deliver(self.configured(), "sample", "hello")
        self.assertEqual(str(caught.exception), "no tmux pane")
        self.assertEqual(self.calls, [])

    def test_given_a_live_pane_when_opened_then_that_window_is_selected(self):
        result = open_target(self.configured(), "sample")
        self.assertEqual(result, f"focused pane {PANE} on {SOCK}\n")
        self.assertEqual(self.calls, [
            ["tmux", "-S", SOCK, "select-window", "-t", PANE],
        ])

    def test_given_no_live_session_when_opened_then_the_wrapper_continues(self):
        calls = []

        def runner(args):
            calls.append(list(args))
            return TmuxResult(0, "", "")

        cfg = self.config(
            agents_runner=lambda: [],
            tmux_runner=runner,
            wrapper="/tmp/wrap",
            tmux_session="cc",
            default_socket=DEFAULT_SOCK,
        )
        result = open_target(cfg, "sample")
        self.assertEqual(result, "opened /tmp/wrap -c for sample\n")
        self.assertEqual(calls, [[
            "tmux", "-S", DEFAULT_SOCK, "new-window", "-t", "cc",
            "-c", str(self.project_path.resolve()),
            "/tmp/wrap -c",
        ]])

    def test_given_no_pane_when_opened_then_a_second_window_is_not_opened(self):
        payload = json.loads((self.tmp / "sessions" / "7.json").read_text(encoding="utf-8"))
        del payload["pane"]
        (self.tmp / "sessions" / "7.json").write_text(json.dumps(payload), encoding="utf-8")
        (self.tmp / "panes" / "ccar").write_text("", encoding="utf-8")
        with self.assertRaises(CommandError) as caught:
            open_target(self.configured(), "sample")
        self.assertEqual(str(caught.exception), "no tmux pane")
        self.assertEqual(self.calls, [])

    def test_given_only_slashes_when_delivering_then_it_is_refused(self):
        with self.assertRaises(CommandError) as caught:
            deliver(self.configured(), "sample", "///")
        self.assertEqual(str(caught.exception), "empty text")
        self.assertEqual(self.calls, [])

    def test_given_a_draft_when_the_cli_delivers_then_the_exit_status_is_failure(self):
        self.screens = [screen([PROMPT + " SECRET DRAFT"])]
        code, out, err = run(["deliver", "sample", "hello"], self.configured())
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertEqual(err, "input box holds a draft\n")
        self.assertEqual(sends(self.calls), [])

    def test_given_a_live_session_when_the_cli_routes_json_then_the_action_is_included(self):
        self.screens = [screen([PROMPT])]
        code, out, err = run(["route", "sample", "--json"], self.configured())
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        payload = json.loads(out)
        self.assertEqual(payload["action"], "send")
        self.assertEqual(payload["session"]["pid"], 7)
        self.assertEqual(payload["context_pct"], 42)
        self.assertIs(payload["input_empty"], True)

    def test_given_compact_on_the_cli_when_dry_run_then_no_keys_are_sent(self):
        self.screens = [screen([PROMPT]), screen([PROMPT])]
        code, out, err = run(
            ["deliver", "sample", "hello", "--reset", "compact", "--dry-run"],
            self.configured(),
        )
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("wait status idle 600\n", out)
        self.assertIn("-l -- /compact\n", out)
        self.assertEqual(sends(self.calls), [])
