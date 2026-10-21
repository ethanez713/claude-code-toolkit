"""Report joins, baseline rows, calibrations, and the pre-registered verdict.

Fixtures live in temp directories. Nothing here reads the real transcript store
or the real grok-mcp state.
"""

import importlib.util
import io
import json
import math
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("grok_handoff_report", SKILL / "scripts" / "report.py")
report = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(report)

DOT = "·"
TASK = "0123456789"
REPO_CWD = "/tmp/myrepo"
DROP_EMPTY = (
    f"unjoined 0/0 {DOT} contaminated 0/0 {DOT} baseline unresolved 0/0 {DOT} "
    f"null grok pct 0/0 {DOT} foreign billing 0/0"
)


def write_prices(path, models=None):
    path.write_text(json.dumps({
        "unit": "usd_per_token",
        "multipliers": {"cache_write_5m": 1.25, "cache_write_1h": 2.0, "cache_read": 0.1},
        "models": models or {
            "m": {"in": 1e-6, "out": 2e-6, "cache_read": 0.5},
            "n": {"in": 1e-6, "out": 2e-6},
        },
    }))


def dump_jsonl(path, rows, ensure_ascii=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=ensure_ascii) + "\n")


def iso(moment):
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def assistant(mid, when, cwd, model, usage, tools, sidechain=False):
    return {
        "type": "assistant",
        "timestamp": iso(when) if isinstance(when, datetime) else when,
        "sessionId": "ignored-field",
        "cwd": cwd,
        "isSidechain": sidechain,
        "message": {
            "id": mid,
            "model": model,
            "role": "assistant",
            "content": tools,
            "usage": usage,
        },
    }


def tool_use(name, tool_id, payload):
    return {"type": "tool_use", "name": name, "id": tool_id, "input": payload}


def tool_result(tool_id, content, when, is_error=False):
    return {
        "type": "user",
        "timestamp": when if isinstance(when, str) else iso(when),
        "message": {
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": tool_id,
                "is_error": is_error,
                "content": content,
            }],
        },
    }


def git_run(repo, *args, env=None):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )


def init_repo(path):
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True, capture_output=True)
    hooks = path / ".empty-hooks"
    hooks.mkdir()
    for key, value in (
        ("user.email", "t@example.com"),
        ("user.name", "Test"),
        ("commit.gpgsign", "false"),
        ("core.hooksPath", str(hooks)),
    ):
        git_run(path, "config", key, value)
    return path


def commit_env(when):
    env = os.environ.copy()
    env.update({
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "t@example.com",
        "GIT_AUTHOR_DATE": when,
        "GIT_COMMITTER_DATE": when,
    })
    return env


def git_commit(repo, name, text, subject, when):
    (repo / name).write_text(text)
    env = commit_env(when)
    git_run(repo, "add", "--", name, env=env)
    git_run(repo, "commit", "-q", "-m", subject, env=env)
    sha = git_run(repo, "rev-parse", "HEAD", env=env).stdout.strip()
    oneline = git_run(repo, "-c", "color.ui=never", "log", "--oneline", "-1", env=env).stdout
    committed = git_run(repo, "log", "-1", "--format=%cI", env=env).stdout.strip()
    return sha, oneline, committed


def measure_numstat(repo, sha):
    out = git_run(repo, "show", "--numstat", "--format=", sha).stdout
    files = added = deleted = 0
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        plus, minus = parts[0], parts[1]
        if plus == "-" or minus == "-":
            files += 1
            continue
        files += 1
        added += int(plus)
        deleted += int(minus)
    return files, added, deleted


def qualifying_and_foreign_events():
    base = datetime(2026, 8, 1, tzinfo=timezone.utc)
    events = [
        job_end(
            job,
            base + timedelta(seconds=index * 100),
            duration_s=10,
            grok_fresh_before=True,
            grok_fresh_after=True,
            concurrent_jobs=1,
            grok_foreign_billing=0,
            grok_pct_before=0,
            grok_pct_after=2,
            usage={"total_tokens": 1000},
        )
        for index, job in enumerate(("q1", "q2", "q3"))
    ]
    events.append(job_end(
        "fx",
        base + timedelta(seconds=400),
        duration_s=10,
        grok_fresh_before=True,
        grok_fresh_after=True,
        concurrent_jobs=1,
        grok_foreign_billing=4,
        grok_pct_before=0,
        grok_pct_after=10,
        usage={"total_tokens": 500},
    ))
    return events


def plant_duplicated_sidechain(transcripts, state):
    base = datetime(2026, 2, 1, tzinfo=timezone.utc)
    messages = [
        assistant(
            f"sub-{index}",
            base + timedelta(seconds=1000 * index + 100),
            "/tmp",
            "m",
            {"input_tokens": 2, "output_tokens": 0},
            [],
            sidechain=True,
        )
        for index in range(3)
    ]
    dump_jsonl(transcripts / "proj" / "sess.jsonl", messages)
    dump_jsonl(transcripts / "proj" / "sess" / "subagents" / "agent-1.jsonl", messages)
    dump_jsonl(state / "events.jsonl", [
        job_end(
            f"c{index}",
            base + timedelta(seconds=1000 * index),
            claude_7d_pct=2 * index,
            claude_captured_age_s=10,
        )
        for index in range(4)
    ])


def job_end(job, when, **extra):
    row = {
        "event": "job_end",
        "job": job,
        "round": 1,
        "ts": iso(when) if isinstance(when, datetime) else when,
        "duration_s": 30,
        "brief_chars": len(TASK),
        "repo": "myrepo",
        "mode": "edit",
        "kind": "implement",
    }
    row.update(extra)
    return row


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.transcripts = self.root / "transcripts"
        self.state.mkdir()
        self.transcripts.mkdir()
        self.prices = self.root / "prices.json"
        write_prices(self.prices)

    def write_edit(self, job, extras, finish=True, background=False, extra_tool=None,
                   ensure_ascii=True, events_name="events.jsonl"):
        end = datetime(2026, 7, 1, 12, 0, 30, tzinfo=timezone.utc)
        handoff_at = end - timedelta(seconds=40)
        lines = [
            assistant(
                f"{job}-partial",
                handoff_at - timedelta(seconds=200),
                REPO_CWD,
                "n",
                {"input_tokens": 1, "output_tokens": 1},
                [tool_use("Read", f"{job}-read", {"file_path": "a"})],
            ),
            assistant(
                f"{job}-handoff",
                handoff_at,
                REPO_CWD,
                "n",
                {"input_tokens": 1, "output_tokens": 1},
                [tool_use("mcp__grok__run", f"{job}-run", {"task": TASK, "cwd": REPO_CWD})],
            ),
        ]
        marker = f"job {job} r1 {DOT} done"
        if background:
            lines.append({"type": "queue-operation", "content": marker})
            lines.append({
                "type": "user",
                "origin": {"kind": "task-notification"},
                "timestamp": iso(end - timedelta(seconds=20)),
                "message": {"role": "user", "content": marker},
            })
        else:
            lines.append(tool_result(f"{job}-run", marker, end - timedelta(seconds=20)))
        for index in range(extras):
            tools = [extra_tool] if index == 0 and extra_tool else []
            lines.append(assistant(
                f"{job}-rev-{index}",
                end - timedelta(seconds=19 - index),
                REPO_CWD,
                "n",
                {"input_tokens": 1, "output_tokens": 1},
                tools,
            ))
        if finish:
            lines.append(assistant(
                f"{job}-finish",
                end - timedelta(seconds=1),
                REPO_CWD,
                "n",
                {"input_tokens": 1, "output_tokens": 1},
                [tool_use("mcp__grok__finish", f"{job}-fin", {"job": job})],
            ))
        session = self.transcripts / "proj" / f"{job}.jsonl"
        dump_jsonl(session, lines, ensure_ascii=ensure_ascii)
        dump_jsonl(self.state / events_name, [job_end(job, end)])
        return session

    def test_given_streamed_handoff_when_built_then_last_line_sets_cost_and_three_review_turns(self):
        end = datetime(2026, 6, 1, 12, 0, 30, tzinfo=timezone.utc)
        handoff_at = end - timedelta(seconds=40)
        usage = {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_read_input_tokens": 10,
            "cache_creation_input_tokens": 20,
            "cache_creation": {"ephemeral_5m_input_tokens": 8, "ephemeral_1h_input_tokens": 4},
        }
        lines = [
            assistant(
                "msg-handoff",
                handoff_at - timedelta(seconds=200),
                REPO_CWD,
                "n",
                {"input_tokens": 1, "output_tokens": 1},
                [tool_use("Read", "tu-early", {"file_path": "nope"})],
            ),
            assistant(
                "msg-handoff",
                handoff_at,
                REPO_CWD,
                "m",
                usage,
                [tool_use("mcp__grok__run", "tu-run", {"task": TASK, "cwd": REPO_CWD})],
            ),
            tool_result("tu-run", f"job J1 r1 {DOT} done", end - timedelta(seconds=20)),
            assistant(
                "msg-a",
                end - timedelta(seconds=15),
                REPO_CWD,
                "n",
                {"input_tokens": 10, "output_tokens": 10, "cache_creation_input_tokens": 50000},
                [],
            ),
            assistant(
                "msg-b",
                end - timedelta(seconds=10),
                REPO_CWD,
                "<synthetic>",
                {"input_tokens": 9000000, "output_tokens": 9000000, "cache_read_input_tokens": 9000000},
                [],
            ),
            assistant(
                "msg-fin",
                end - timedelta(seconds=1),
                REPO_CWD,
                "n",
                {"input_tokens": 5, "output_tokens": 7},
                [tool_use("mcp__grok__finish", "tu-fin", {"job": "J1"})],
            ),
        ]
        session = self.transcripts / "proj" / "priced.jsonl"
        dump_jsonl(session, lines)
        self.assertIn("\\u00b7", session.read_text(encoding="utf-8"))
        events = self.state / "events.1.jsonl"
        events.write_text("not-json\n" + json.dumps(job_end("J1", end)) + "\n", encoding="utf-8")
        built = report.build(self.state, self.transcripts, self.prices)
        saved = json.loads((self.state / "claude_side.jsonl").read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(saved["review_turns"], 3)
        self.assertEqual(saved["handoff_output_tokens"], 50)
        self.assertAlmostEqual(saved["handoff_cost"], 0.000223)
        self.assertAlmostEqual(saved["review_cost"], 0.000049)
        self.assertIs(saved["contaminated"], False)
        self.assertIn("unjoined 0/1", built["drop"])

    def test_given_backgrounded_notification_when_built_then_review_still_joins(self):
        session = self.write_edit("bgjob", extras=2, background=True, ensure_ascii=False)
        text = session.read_text(encoding="utf-8")
        self.assertIn("queue-operation", text)
        self.assertIn("task-notification", text)
        self.assertIn(DOT, text)
        self.assertNotIn("tool_result", text)
        report.build(self.state, self.transcripts, self.prices)
        saved = json.loads((self.state / "claude_side.jsonl").read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(saved["review_turns"], 3)
        self.assertIs(saved["contaminated"], False)

    def test_given_cwd_in_a_repo_subdir_when_matched_then_consult_and_edit_names_both_join(self):
        top = init_repo(self.root / "toprepo")
        sub = top / "skills" / "tool"
        sub.mkdir(parents=True)
        self.assertTrue(report.cwd_in_repo(str(sub), "tool"))
        self.assertTrue(report.cwd_in_repo(str(sub), "toprepo"))
        self.assertFalse(report.cwd_in_repo(str(sub), "elsewhere"))

    def test_given_sixteen_review_turns_when_built_then_contaminated_is_persisted(self):
        self.write_edit("wide", extras=15)
        report.build(self.state, self.transcripts, self.prices)
        saved = json.loads((self.state / "claude_side.jsonl").read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(saved["review_turns"], 16)
        self.assertIs(saved["contaminated"], True)

    def test_given_fifteen_review_turns_when_built_then_not_contaminated(self):
        self.write_edit("edge", extras=14)
        report.build(self.state, self.transcripts, self.prices)
        saved = json.loads((self.state / "claude_side.jsonl").read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(saved["review_turns"], 15)
        self.assertIs(saved["contaminated"], False)

    def test_given_another_jobs_run_in_the_window_when_built_then_contaminated(self):
        other = tool_use("mcp__grok__run", "other-run", {"task": "z"})
        self.write_edit("host", extras=1, extra_tool=other)
        report.build(self.state, self.transcripts, self.prices)
        saved = json.loads((self.state / "claude_side.jsonl").read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(saved["review_turns"], 2)
        self.assertIs(saved["contaminated"], True)

    def test_given_an_open_review_when_built_then_it_is_not_persisted(self):
        self.write_edit("openjob", extras=1, finish=False)
        built = report.build(self.state, self.transcripts, self.prices)
        self.assertFalse((self.state / "claude_side.jsonl").exists())
        self.assertIs(built["rounds"][("openjob", 1)]["closed"], False)
        self.assertEqual(built["rounds"][("openjob", 1)]["review_turns"], 1)

    def test_given_inline_commits_when_scanned_then_numstat_matches_and_non_attempts_drop_out(self):
        repo = init_repo(self.root / "widget")
        nonrepo = self.root / "not-a-repo"
        nonrepo.mkdir()
        sha1, oneline, _ci1 = git_commit(repo, "a.txt", "alpha\n", "baseline subject one", "2020-05-02T15:00:00+00:00")
        sha2, _oneline2, ci2 = git_commit(repo, "a.txt", "alpha\nbeta\n", "baseline subject two", "2020-05-02T15:04:05+00:00")
        files1, added1, deleted1 = measure_numstat(repo, sha1)
        files2, added2, deleted2 = measure_numstat(repo, sha2)
        oneline_cmd = "git commit -q -F - <<'EOF'\nbaseline subject one\nEOF\n&& git log --oneline -1"
        subject_cmd = f"git -C {repo} commit -q -F - <<'EOF'\nbaseline subject two\nEOF\n"
        lines = [
            {"type": "user", "timestamp": "2020-05-02T14:59:00Z", "message": {"role": "user", "content": "please commit"}},
            assistant("c1", "2020-05-02T15:00:01Z", str(repo), "n", {"input_tokens": 3, "output_tokens": 1},
                      [tool_use("Bash", "bash-1", {"command": oneline_cmd})]),
            tool_result("bash-1", [{"type": "text", "text": oneline}], "2020-05-02T15:00:02Z"),
            assistant("c2", ci2, str(nonrepo), "n", {"input_tokens": 3, "output_tokens": 1},
                      [tool_use("Bash", "bash-2", {"command": subject_cmd})]),
            tool_result("bash-2", "", ci2),
            assistant("grok-between", "2020-05-02T15:05:00Z", str(repo), "n", {"input_tokens": 1, "output_tokens": 1},
                      [tool_use("mcp__grok__run", "g-between", {"task": TASK, "cwd": str(repo)})]),
            assistant("c3", "2020-05-02T15:06:00Z", str(repo), "n", {"input_tokens": 1, "output_tokens": 1},
                      [tool_use("Bash", "bash-3", {"command": "git commit -m excluded"})]),
            tool_result("bash-3", "[main 0123456789abcdef]", "2020-05-02T15:06:01Z"),
            assistant("c4", "2020-05-02T15:07:00Z", str(repo), "n", {"input_tokens": 1, "output_tokens": 1},
                      [tool_use("Bash", "bash-4", {"command": "git commit --amend -m rewritten"})]),
            tool_result("bash-4", "[main deadbeef1]", "2020-05-02T15:07:01Z"),
            assistant("c5", "2020-05-02T15:08:00Z", str(repo), "n", {"input_tokens": 1, "output_tokens": 1},
                      [tool_use("Bash", "bash-5", {"command": "git commit -m 'will fail'"})]),
            tool_result("bash-5", "fatal: boom\n", "2020-05-02T15:08:01Z"),
            assistant("c6", "2020-05-02T15:09:00Z", str(repo), "n", {"input_tokens": 1, "output_tokens": 1},
                      [tool_use("Bash", "bash-6", {"command": "git commit -m 'no result'"})]),
        ]
        dump_jsonl(self.transcripts / "proj" / "sess-baseline.jsonl", lines)
        built = report.build(self.state, self.transcripts, self.prices)
        text = (self.state / "baseline.jsonl").read_text(encoding="utf-8").splitlines()
        first = json.loads(text[0])
        second = json.loads(text[1])
        self.assertEqual(len(text), 2)
        self.assertEqual(first["sha"], sha1)
        self.assertEqual(first["files"], files1)
        self.assertEqual(first["added"], added1)
        self.assertEqual(first["deleted"], deleted1)
        self.assertEqual(first["repo"], repo.name)
        self.assertEqual(first["sessionId"], "sess-baseline")
        self.assertEqual(second["sha"], sha2)
        self.assertEqual(second["files"], files2)
        self.assertEqual(second["added"], added2)
        self.assertEqual(second["deleted"], deleted2)
        self.assertEqual(second["repo"], repo.name)
        self.assertEqual(
            built["drop"],
            f"unjoined 0/0 {DOT} contaminated 0/0 {DOT} baseline unresolved 0/2 {DOT} "
            f"null grok pct 0/0 {DOT} foreign billing 0/0 {DOT} repo gone 0",
        )

    def test_given_commit_in_a_deleted_repo_when_scanned_then_counted_as_gone_not_unresolved(self):
        gone = self.root / "deleted-repo"
        lines = [
            assistant("c1", "2020-05-04T10:00:01Z", str(self.root), "n", {"input_tokens": 2, "output_tokens": 1},
                      [tool_use("Bash", "bash-1", {"command": f"cd {gone}; git commit -qm 'lost work'"})]),
            tool_result("bash-1", "[main 0123456] lost work", "2020-05-04T10:00:02Z"),
        ]
        dump_jsonl(self.transcripts / "proj" / "gone.jsonl", lines)
        built = report.build(self.state, self.transcripts, self.prices)
        self.assertIn("baseline unresolved 0/0", built["drop"])
        self.assertIn("repo gone 1", built["drop"])

    def test_given_amend_only_inside_the_message_when_scanned_then_the_commit_counts(self):
        repo = init_repo(self.root / "widget")
        sha, oneline, _ci = git_commit(repo, "b.txt", "beta\n", "keep this subject", "2020-05-03T10:00:00+00:00")
        command = "git commit -q -F - <<'EOF'\nkeep this subject\n--amend\nEOF\n&& git log --oneline -1"
        lines = [
            assistant("c1", "2020-05-03T10:00:01Z", str(repo), "n", {"input_tokens": 2, "output_tokens": 1},
                      [tool_use("Bash", "bash-1", {"command": command})]),
            tool_result("bash-1", oneline, "2020-05-03T10:00:02Z"),
        ]
        dump_jsonl(self.transcripts / "proj" / "amend-body.jsonl", lines)
        built = report.build(self.state, self.transcripts, self.prices)
        saved = json.loads((self.state / "baseline.jsonl").read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(saved["sha"], sha)
        self.assertIn("baseline unresolved 0/1", built["drop"])

    def test_given_a_closed_join_and_a_commit_when_built_twice_then_files_stay_identical(self):
        self.write_edit("once", extras=1)
        repo = init_repo(self.root / "widget")
        sha, oneline, _ci = git_commit(repo, "c.txt", "gamma\n", "once subject", "2020-04-01T00:00:00+00:00")
        command = "git commit -q -F - <<'EOF'\nonce subject\nEOF\n&& git log --oneline -1"
        dump_jsonl(self.transcripts / "proj" / "once-base.jsonl", [
            assistant("base-1", "2020-04-01T00:00:01Z", str(repo), "n", {"input_tokens": 2, "output_tokens": 1},
                      [tool_use("Bash", "bash-1", {"command": command})]),
            tool_result("bash-1", oneline, "2020-04-01T00:00:02Z"),
        ])
        report.build(self.state, self.transcripts, self.prices)
        side = (self.state / "claude_side.jsonl").read_text(encoding="utf-8")
        base = (self.state / "baseline.jsonl").read_text(encoding="utf-8")
        report.build(self.state, self.transcripts, self.prices)
        self.assertEqual((self.state / "claude_side.jsonl").read_text(encoding="utf-8"), side)
        self.assertEqual((self.state / "baseline.jsonl").read_text(encoding="utf-8"), base)
        self.assertEqual(len(side.splitlines()), 1)
        self.assertEqual(len(base.splitlines()), 1)
        self.assertEqual(json.loads(base)["sha"], sha)

    def plant_exchange(self, jobs, merges):
        repo = init_repo(self.root / "widget")
        _sha, oneline, _ci = git_commit(repo, "d.txt", "hello\n", "priced subject", "2020-01-15T00:00:00+00:00")
        files, added, deleted = measure_numstat(repo, _sha)
        command = "git commit -q -F - <<'EOF'\npriced subject\nEOF\n&& git log --oneline -1"
        dump_jsonl(self.transcripts / "proj" / "inline2020.jsonl", [
            {"type": "user", "timestamp": "2020-01-14T23:00:00Z", "message": {"role": "user", "content": "commit it"}},
            assistant(
                "base-turn",
                "2020-01-15T00:00:01Z",
                str(repo),
                "m",
                {"input_tokens": 1_000_000, "output_tokens": 0},
                [tool_use("Bash", "bash-base", {"command": command})],
            ),
            tool_result("bash-base", oneline, "2020-01-15T00:00:02Z"),
        ])
        names = ["ga", "gb", "gc", "gd", "ge"]
        base = datetime(2026, 6, 1, tzinfo=timezone.utc)
        lines = []
        events = []
        for index in range(jobs):
            job = names[index]
            end = base + timedelta(seconds=10000 * (index + 1))
            lines.append(assistant(
                f"h-{job}",
                end - timedelta(seconds=40),
                str(repo),
                "m",
                {"input_tokens": 10000, "output_tokens": 0},
                [tool_use("mcp__grok__run", f"run-{job}", {"task": TASK, "cwd": str(repo)})],
            ))
            lines.append({
                "type": "user",
                "origin": {"kind": "task-notification"},
                "timestamp": iso(end - timedelta(seconds=20)),
                "message": {"role": "user", "content": f"job {job} r1 {DOT} done"},
            })
            lines.append(assistant(
                f"f-{job}",
                end - timedelta(seconds=5),
                str(repo),
                "m",
                {"input_tokens": 0, "output_tokens": 0},
                [tool_use("mcp__grok__finish", f"fin-{job}", {"job": job})],
            ))
            events.append(job_end(
                job,
                end,
                repo=repo.name,
                kind="implement",
                mode="edit",
                claude_7d_pct=10 + 2 * index,
                claude_captured_age_s=12,
                grok_pct_before=30,
                grok_pct_after=31,
                grok_fresh_before=True,
                grok_fresh_after=True,
                concurrent_jobs=1,
                grok_foreign_billing=0,
                usage={"total_tokens": 1_000_000},
            ))
            events.append({
                "event": "job_finish",
                "job": job,
                "ts": iso(end),
                "action": "merge" if index < merges else "discard",
                "rounds": 1,
                "diff_added": 100 * added,
                "diff_deleted": 0,
                "kind": "implement",
            })
        dump_jsonl(self.transcripts / "proj" / "grokwork.jsonl", lines)
        dump_jsonl(self.state / "events.jsonl", events)
        return files, added, deleted

    def test_given_five_jobs_with_four_merges_when_verdict_then_delegate(self):
        self.plant_exchange(jobs=5, merges=4)
        built = report.build(self.state, self.transcripts, self.prices)
        row = built["edit"][0]
        self.assertEqual(row["kind"], "implement")
        self.assertEqual(row["n"], 5)
        self.assertEqual(row["merged_r1"], 4)
        self.assertEqual(row["discarded"], 1)
        self.assertEqual(row["verdict"], "delegate")
        self.assertAlmostEqual(row["saved"], 0.99, places=2)
        self.assertGreaterEqual(built["calibrations"]["claude_per_dollar"]["n"], 3)
        self.assertEqual(built["calibrations"]["grok_per_mtok"]["n"], 5)
        self.assertIn("unjoined 0/5", built["drop"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = report.main([
                "--verdict", "--state", str(self.state),
                "--transcripts", str(self.transcripts), "--prices", str(self.prices),
            ])
        self.assertEqual(code, 0)
        self.assertIn("implement delegate", buf.getvalue())

    def test_given_commits_of_many_sizes_when_predicted_then_only_similar_sizes_count(self):
        def row(lines, cost, repo="widget"):
            return {"repo": repo, "added": lines, "deleted": 0, "cost": cost}
        small = [row(10, 1.0) for _ in range(6)]
        similar = [row(lines, cost) for lines, cost in ((500, 2.0), (700, 3.0), (900, 4.0), (1200, 5.0), (1500, 6.0))]
        self.assertEqual(report.predicted_dollars("widget", 800, small + similar), 4.0)
        elsewhere = [row(800, 9.0, repo="other") for _ in range(5)]
        self.assertEqual(report.predicted_dollars("widget", 800, small + elsewhere), 9.0)
        self.assertEqual(report.predicted_dollars("widget", 800, small), 1.0)
        self.assertIsNone(report.predicted_dollars("widget", 0, small))
        self.assertIsNone(report.predicted_dollars("widget", 800, [row(800, 0.0)]))

    def test_given_merge_rate_and_exchange_ratio_when_decided_then_ratio_never_switches_a_kind_off(self):
        self.assertEqual(report.decide_verdict(10, 9, 9.5), "delegate")
        self.assertEqual(report.decide_verdict(10, 9, math.inf), "inline")
        self.assertEqual(report.decide_verdict(10, 4, 1.0), "inline")
        self.assertEqual(report.decide_verdict(10, 6, 1.0), "unchanged")
        self.assertEqual(report.decide_verdict(10, 9, None), "unchanged")
        self.assertIsNone(report.decide_verdict(4, 4, 1.0))

    def test_given_four_jobs_when_verdict_then_the_kind_is_omitted(self):
        self.plant_exchange(jobs=4, merges=4)
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = report.main([
                "--verdict", "--state", str(self.state),
                "--transcripts", str(self.transcripts), "--prices", str(self.prices),
            ])
        text = buf.getvalue()
        self.assertEqual(code, 0)
        self.assertNotIn("implement", text)
        self.assertNotIn("delegate", text)
        self.assertIn("break-even: insufficient", text)
        self.assertIn("unjoined ", text)
        self.assertIn(DOT, text)

    def test_given_empty_inputs_when_run_then_drop_line_and_insufficient_break_even(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = report.main([
                "--state", str(self.state),
                "--transcripts", str(self.transcripts), "--prices", str(self.prices),
            ])
        text = buf.getvalue()
        self.assertEqual(code, 0)
        self.assertIn(DROP_EMPTY, text)
        self.assertIn("break-even: insufficient", text)
        self.assertFalse((self.state / "claude_side.jsonl").exists())
        self.assertFalse((self.state / "baseline.jsonl").exists())

    def test_given_foreign_billing_when_calibrated_then_its_tokens_are_priced_outside_the_rate(self):
        dump_jsonl(self.state / "events.jsonl", qualifying_and_foreign_events())
        built = report.build(self.state, self.transcripts, self.prices)
        self.assertEqual(built["calibrations"]["grok_per_mtok"]["n"], 3)
        self.assertAlmostEqual(built["calibrations"]["grok_per_mtok"]["value"], 2000)
        self.assertAlmostEqual(built["calibrations"]["grok_per_hour"]["value"], 720)
        self.assertAlmostEqual(built["rounds"][("q1", 1)]["grok_pct"], 2.0)
        self.assertAlmostEqual(built["rounds"][("fx", 1)]["grok_pct"], 1.0)
        buf = io.StringIO()
        with redirect_stdout(buf):
            report.main([
                "--state", str(self.state),
                "--transcripts", str(self.transcripts), "--prices", str(self.prices),
            ])
        self.assertIn("grok % per Mtok: 2000 (n=3)", buf.getvalue())

    def test_given_subagent_dollars_duplicated_on_a_sidechain_when_calibrated_then_counted_once(self):
        write_prices(self.prices, {"m": {"in": 1.0, "out": 1.0}})
        plant_duplicated_sidechain(self.transcripts, self.state)
        built = report.build(self.state, self.transcripts, self.prices)
        self.assertEqual(built["calibrations"]["claude_per_dollar"]["n"], 3)
        self.assertAlmostEqual(built["calibrations"]["claude_per_dollar"]["value"], 1.0)

    def test_given_too_few_samples_when_break_even_then_insufficient(self):
        self.assertEqual(report.break_even_text([1.0], [1.0], 9, 20), "break-even: insufficient")
        self.assertEqual(report.break_even_text([1.0], [1.0], 10, 19), "break-even: insufficient")

    def test_given_twenty_baseline_rows_and_ten_jobs_when_break_even_then_tool_calls(self):
        self.assertEqual(
            report.break_even_text([2.0, 4.0], [1.0, 1.0], 10, 20),
            "break-even: 3.00 tool calls",
        )


class CommitParseTest(unittest.TestCase):
    def only_commit(self, command):
        commits = list(report.iter_commits(command))
        self.assertEqual(len(commits), 1, commits)
        return commits[0]

    def test_given_cd_then_semicolon_when_parsed_then_repo_path_has_no_separator(self):
        self.assertEqual(self.only_commit("cd ~/chinese; git commit -q -F msg")["cd_path"], "~/chinese")

    def test_given_message_from_cat_heredoc_when_parsed_then_subject_is_the_heredoc_first_line(self):
        command = "git commit -m \"$(cat <<'EOF'\nHeredoc subject\n\nbody\nEOF\n)\""
        self.assertEqual(self.only_commit(command)["subject"], "Heredoc subject")

    def test_given_combined_short_flags_when_parsed_then_m_value_is_the_subject(self):
        self.assertEqual(self.only_commit('git commit -qm "Short subject"')["subject"], "Short subject")


if __name__ == "__main__":
    unittest.main()
