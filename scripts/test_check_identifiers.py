"""Hermetic tests for the identifier pre-commit guard.

Each case builds a temp git repo and points TOOLKIT_DENYLIST at a temp
file. HOME is a temp directory too, so the real denylist is never read.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name("check_identifiers.py")
CLOSING = (
    "remove the detail, or git commit --no-verify if it is a false positive\n"
)


class CheckIdentifiersTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="check-ids-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.denylist = self.tmp / "denylist"
        home = self.tmp / "home"
        planted = home / ".claude" / "toolkit-local"
        planted.mkdir(parents=True)
        # If a test reads this instead of TOOLKIT_DENYLIST, an allow-case fails.
        (planted / "denylist").write_text("zzzz-home-only\n", encoding="utf-8")
        (self.tmp / "template").mkdir()
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env["HOME"] = str(home)
        env["TOOLKIT_DENYLIST"] = str(self.denylist)
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        env["GIT_CONFIG_GLOBAL"] = str(self.tmp / "gitconfig")
        env["GIT_TEMPLATE_DIR"] = str(self.tmp / "template")
        env["GIT_AUTHOR_NAME"] = "Dev Example"
        env["GIT_AUTHOR_EMAIL"] = "dev@example.com"
        env["GIT_COMMITTER_NAME"] = "Dev Example"
        env["GIT_COMMITTER_EMAIL"] = "dev@example.com"
        self.env = env
        self.git("init", "-b", "main")
        self.git("config", "user.email", "dev@example.com")
        self.git("config", "user.name", "Dev Example")

    def git(self, *args):
        proc = subprocess.run(
            ["git", *args],
            cwd=self.repo,
            env=self.env,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise AssertionError(proc.stderr or proc.stdout)
        return proc

    def write_denylist(self, text):
        self.denylist.write_text(text, encoding="utf-8")

    def check(self, *args):
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            cwd=self.repo,
            env=self.env,
            capture_output=True,
            text=True,
        )
        return proc.returncode, proc.stdout, proc.stderr

    def test_given_a_matching_added_line_when_checked_then_the_commit_is_refused(self):
        self.write_denylist("alice\n")
        (self.repo / "notes.txt").write_text("okay\nsee alice\n", encoding="utf-8")
        self.git("add", "notes.txt")
        code, out, err = self.check()
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertEqual(err, "notes.txt:2: matches alice\n" + CLOSING)

    def test_given_a_hit_only_in_an_unchanged_line_when_checked_then_it_is_allowed(self):
        self.write_denylist("alice\n")
        path = self.repo / "notes.txt"
        path.write_text("hello alice\nclean\n", encoding="utf-8")
        self.git("add", "notes.txt")
        self.git("commit", "-m", "add notes")
        path.write_text("hello alice\ncleaner\n", encoding="utf-8")
        self.git("add", "notes.txt")
        code, out, err = self.check()
        self.assertEqual((code, out, err), (0, "", ""))

    def test_given_a_matching_staged_filename_when_checked_then_it_is_refused(self):
        self.write_denylist("acme-secret-project\n")
        path = self.repo / "acme-secret-project" / "notes.txt"
        path.parent.mkdir()
        path.write_text("nothing local here\n", encoding="utf-8")
        self.git("add", "acme-secret-project/notes.txt")
        code, out, err = self.check()
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertEqual(
            err,
            "acme-secret-project/notes.txt:0: matches acme-secret-project\n" + CLOSING,
        )

    def test_given_comments_and_blank_lines_when_the_denylist_is_loaded_then_they_are_ignored(self):
        self.write_denylist("# alice\n\n  # acme-secret-project\n")
        (self.repo / "notes.txt").write_text(
            "mail alice about acme-secret-project and zzzz-home-only\n",
            encoding="utf-8",
        )
        self.git("add", "notes.txt")
        code, out, err = self.check()
        self.assertEqual((code, out, err), (0, "", ""))

    def test_given_different_case_when_checked_then_it_matches(self):
        self.write_denylist("alice\n")
        (self.repo / "notes.txt").write_text("Hello ALICE\n", encoding="utf-8")
        self.git("add", "notes.txt")
        code, out, err = self.check()
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertEqual(err, "notes.txt:1: matches alice\n" + CLOSING)

    def test_given_a_missing_denylist_when_checked_then_it_is_refused(self):
        (self.repo / "notes.txt").write_text("hello alice\n", encoding="utf-8")
        self.git("add", "notes.txt")
        code, out, err = self.check()
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertEqual(err, f"missing denylist: {self.denylist}\n")

    def test_given_an_invalid_regex_when_the_denylist_is_loaded_then_it_is_refused(self):
        self.write_denylist("alice\n(\n")
        (self.repo / "notes.txt").write_text("hello alice\n", encoding="utf-8")
        self.git("add", "notes.txt")
        code, out, err = self.check()
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertEqual(err, f"invalid regex in {self.denylist}: (\n")

    def test_given_a_path_with_a_space_when_checked_then_the_reported_path_has_no_tab(self):
        self.write_denylist("alice\n")
        (self.repo / "my notes.txt").write_text("see alice\n", encoding="utf-8")
        self.git("add", "my notes.txt")
        code, out, err = self.check()
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertEqual(err, "my notes.txt:1: matches alice\n" + CLOSING)

    def test_given_a_non_ascii_path_when_checked_then_the_reported_path_is_decoded(self):
        self.write_denylist("alice\n")
        name = "caf\u00e9 notes.txt"
        (self.repo / name).write_text("see alice\n", encoding="utf-8")
        self.git("add", name)
        code, out, err = self.check()
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertEqual(err, f"{name}:1: matches alice\n" + CLOSING)

    def test_given_a_binary_blob_when_checked_then_its_bytes_are_skipped(self):
        self.write_denylist("alice\n")
        (self.repo / "picture.bin").write_bytes(b"prefix alice\0suffix\n")
        self.git("add", "picture.bin")
        code, out, err = self.check()
        self.assertEqual((code, out, err), (0, "", ""))

    def test_given_a_committed_hit_when_all_is_requested_then_it_is_found(self):
        self.write_denylist("alice\n")
        (self.repo / "notes.txt").write_text("fine\nsee alice\n", encoding="utf-8")
        self.git("add", "notes.txt")
        self.git("commit", "-m", "add notes")
        code, out, err = self.check()
        self.assertEqual((code, out, err), (0, "", ""))
        code, out, err = self.check("--all")
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertEqual(err, "notes.txt:2: matches alice\n" + CLOSING)
