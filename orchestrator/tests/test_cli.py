import io
import json
import shutil
import stat
import subprocess
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

from orchestrator.cli import main
from orchestrator.config import load_config
from support import TempCase


def run(argv, cfg):
    out = io.StringIO()
    err = io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(argv, cfg)
    return code, out.getvalue(), err.getvalue()


class CliTest(TempCase):
    def test_given_a_marker_when_count_runs_then_it_prints_the_flag(self):
        self.project("sample")
        (self.worklogs / "sample.md").write_text(
            "- [?] decide | anywhere | Pick one\n"
            "- [a] decide | anywhere | Answered one | answer: yes | answered: 2026-11-01\n",
            encoding="utf-8",
        )
        code, out, err = run(["count"], self.config())
        self.assertEqual(code, 0)
        self.assertEqual(out, "⚑ 1\n")
        self.assertEqual(err, "")

    def test_given_nothing_waiting_when_counted_then_output_is_empty(self):
        code, out, err = run(["count"], self.config())
        self.assertEqual((code, out, err), (0, "", ""))

    def test_given_a_fresh_cache_when_the_worklog_changes_then_count_waits_for_ttl(self):
        self.project("sample")
        path = self.worklogs / "sample.md"
        path.write_text("- [?] decide | anywhere | Pick one\n", encoding="utf-8")
        cfg = self.config()
        code, out, _err = run(["count"], cfg)
        self.assertEqual((code, out), (0, "⚑ 1\n"))
        path.write_text(
            path.read_text(encoding="utf-8") + "- [?] decide | anywhere | Second\n",
            encoding="utf-8",
        )
        self.clock["now"] = datetime(2026, 11, 2, 12, 1, tzinfo=timezone.utc)
        code, out, _err = run(["count"], cfg)
        self.assertEqual((code, out), (0, "⚑ 1\n"))
        self.clock["now"] = datetime(2026, 11, 2, 12, 6, tzinfo=timezone.utc)
        code, out, _err = run(["count"], cfg)
        self.assertEqual((code, out), (0, "⚑ 2\n"))

    def test_given_only_a_sweep_item_when_counted_then_output_is_empty(self):
        project = self.project("sample")
        folder = self.tmp / "transcripts" / project.resolve().as_posix().replace("/", "-").replace("_", "-")
        folder.mkdir(parents=True)
        row = {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "Are you ready?"}]},
            "timestamp": "2026-11-01T12:00:00Z",
        }
        (folder / "sessabcd.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
        code, out, _err = run(["count"], self.config())
        self.assertEqual((code, out), (0, ""))

    def test_given_state_dir_is_a_file_when_counted_then_it_stays_quiet(self):
        blocked = self.tmp / "blocked-state"
        blocked.write_text("x", encoding="utf-8")
        cfg = self.config(state_dir=blocked)
        code, out, err = run(["count"], cfg)
        self.assertEqual((code, out, err), (0, "", ""))

    def test_given_add_when_invoked_then_the_worklog_line_is_appended(self):
        self.project("sample")
        code, _out, err = run(
            ["add", "sample", "decide", "anywhere", "Pick one", "--unblocks", "4", "--gate", "r6"],
            self.config(),
        )
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(
            (self.worklogs / "sample.md").read_text(encoding="utf-8"),
            "- [?] decide | anywhere | Pick one | unblocks: 4 | gate: r6 | since: 2026-11-02\n",
        )

    def test_given_scan_when_invoked_then_stdout_matches_the_status_file(self):
        self.project("sample")
        (self.worklogs / "sample.md").write_text(
            "- [?] decide | anywhere | Pick one\n", encoding="utf-8"
        )
        cfg = self.config()
        code, out, err = run(["scan"], cfg)
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(out, cfg.status_path.read_text(encoding="utf-8"))
        self.assertIn("Pick one", out)

    def test_given_a_candidate_when_swept_then_json_and_text_include_the_transcript(self):
        project = self.project("sample")
        folder = self.tmp / "transcripts" / project.resolve().as_posix().replace("/", "-").replace("_", "-")
        folder.mkdir(parents=True)
        transcript = folder / "sessabcd.jsonl"
        row = {
            "type": "assistant",
            "message": {"content": "Are you ready?"},
            "timestamp": "2026-11-01T12:00:00Z",
        }
        transcript.write_text(json.dumps(row) + "\n", encoding="utf-8")
        cfg = self.config()
        code, out, err = run(["sweep", "--json"], cfg)
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        payload = json.loads(out)
        self.assertEqual(payload[0]["project"], "sample")
        self.assertEqual(payload[0]["session"], "sessabcd")
        self.assertEqual(payload[0]["transcript"], str(transcript))
        self.assertEqual(payload[0]["reason"], "?")
        self.assertIn("2026-11-01", payload[0]["ended"])
        code, out, err = run(["sweep"], cfg)
        self.assertEqual(code, 0)
        self.assertIn(str(transcript), out)
        code, out, err = run(["sweep", "--ack", "sessab"], cfg)
        self.assertEqual(code, 0)
        self.assertIn("acked sessabcd", out)
        code, out, _err = run(["sweep", "--json"], cfg)
        self.assertEqual(json.loads(out), [])

    def test_given_unknown_ref_when_done_then_exit_status_is_failure(self):
        code, _out, err = run(["done", "missing"], self.config())
        self.assertEqual(code, 1)
        self.assertIn("no item", err)

    def test_given_only_the_example_when_loaded_then_its_values_apply(self):
        repo = self.tmp / "repodir"
        repo.mkdir()
        raw = (Path(__file__).resolve().parents[1] / "orch.example.toml").read_text(encoding="utf-8")
        (repo / "orch.example.toml").write_text(raw.replace("~", str(self.tmp)), encoding="utf-8")
        cfg = load_config(repo)
        self.assertEqual(cfg.runnable_days, 14)
        self.assertEqual(cfg.sweep_days, 30)
        self.assertEqual(cfg.count_ttl_seconds, 300)
        self.assertEqual(cfg.reset_context_pct, 50)
        self.assertEqual(cfg.no_git_check, ("dotfiles", ".claude"))
        self.assertIn("downloads", cfg.exclude)
        self.assertIn("dot-claude", cfg.aliases)
        self.assertEqual(cfg.worklog_dir, Path.home() / ".claude" / "worklog")
        self.assertEqual(cfg.state_dir, (repo / "state").resolve())

    def test_given_a_local_config_and_the_example_when_loaded_then_local_wins(self):
        repo = self.tmp / "repodir"
        repo.mkdir()
        (repo / "orch.example.toml").write_text("runnable_days = 14\n", encoding="utf-8")
        (repo / "orch.toml").write_text("runnable_days = 3\n", encoding="utf-8")
        self.assertEqual(load_config(repo).runnable_days, 3)

    def test_given_a_symlink_in_another_directory_when_help_runs_then_it_exits_clean(self):
        script = Path(__file__).resolve().parents[1] / "bin" / "orch"
        mode = script.stat().st_mode
        script.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        link_dir = Path(tempfile.mkdtemp(prefix="orch-link-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, link_dir, ignore_errors=True)
        link = link_dir / "orch"
        link.symlink_to(script)
        proc = subprocess.run(
            [str(link), "--help"],
            cwd=link_dir,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("usage", proc.stdout.lower())
        self.assertNotIn("Traceback", proc.stderr)
