import os
import subprocess
from pathlib import Path

from orchestrator.board import scan
from orchestrator.config import encoded_prefix
from orchestrator.projects import (
    discover_projects,
    git_facts,
    match_project,
    project_named,
    read_worklog_files,
    resolve_stem,
    swap_dashes,
    worklog_for,
)
from support import TempCase


def git(path, *args):
    subprocess.run(
        [
            "git", "-C", str(path),
            "-c", "commit.gpgsign=false",
            "-c", "user.name=Test User",
            "-c", "user.email=test@example.com",
            *args,
        ],
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
    )


class StemTest(TempCase):
    def test_given_a_home_when_encoded_then_slashes_become_dashes(self):
        self.assertEqual(encoded_prefix(Path("/home/alice")), "-home-alice-")

    def test_given_dashes_and_underscores_when_swapped_then_both_change_together(self):
        self.assertEqual(swap_dashes("my-app"), "my_app")
        self.assertEqual(swap_dashes("my_app"), "my-app")
        self.assertEqual(swap_dashes("a-b_c"), "a_b-c")

    def test_given_direct_stem_when_resolved_then_directory_matches(self):
        self.project("sample")
        found = resolve_stem("sample", self.projects, {})
        self.assertEqual(found, (self.projects / "sample").resolve())

    def test_given_encoded_swapped_stem_when_resolved_then_underscore_dir_matches(self):
        self.project("my_app")
        stem = encoded_prefix(self.projects) + "my-app"
        found = resolve_stem(stem, self.projects, {})
        self.assertEqual(found, (self.projects / "my_app").resolve())

    def test_given_alias_when_dir_is_excluded_then_alias_still_resolves(self):
        target = self.project("portfolio")
        aliases = {"portfolio-screenshots": target}
        found = resolve_stem("portfolio-screenshots", self.projects, aliases)
        self.assertEqual(found, target.resolve())

    def test_given_real_directory_and_alias_when_resolved_then_directory_wins(self):
        real = self.project("real")
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        found = resolve_stem("real", self.projects, {"real": elsewhere})
        self.assertEqual(found, real.resolve())

    def test_given_unknown_stem_when_resolved_then_it_is_an_orphan(self):
        self.assertIsNone(resolve_stem("missing", self.projects, {}))

    def test_given_encoded_worklog_when_scanned_then_it_groups_under_the_directory(self):
        self.project("my_app")
        stem = encoded_prefix(self.projects) + "my-app"
        (self.worklogs / f"{stem}.md").write_text(
            "- [?] decide | anywhere | See the swap\n", encoding="utf-8"
        )
        result = scan(self.config())
        self.assertIn("See the swap", result.text)
        self.assertIn("my_app", result.text)
        self.assertEqual(result.orphans, [])
        self.assertIn("orphans: none", result.text)

    def test_given_orphan_worklog_when_scanned_then_footer_names_it_and_marker_shows(self):
        (self.worklogs / "missing.md").write_text(
            "- [?] decide | anywhere | Still here\n", encoding="utf-8"
        )
        result = scan(self.config())
        self.assertEqual(result.orphans, ["missing"])
        self.assertIn("Still here", result.text)
        self.assertIn("orphans: missing", result.text)

    def test_given_hidden_alias_target_when_scanned_then_display_name_is_the_basename(self):
        hidden = self.tmp / ".claude"
        hidden.mkdir()
        (self.worklogs / "dot-claude.md").write_text(
            "- [?] decide | anywhere | Dot name\n", encoding="utf-8"
        )
        result = scan(self.config(aliases={"dot-claude": hidden}))
        self.assertIn(".claude  decide  anywhere  Dot name", result.text)
        self.assertEqual(result.orphans, [])

    def test_given_two_worklogs_when_choosing_one_then_plain_name_beats_encoded(self):
        self.project("sample")
        prefix = encoded_prefix(self.projects)
        encoded = self.worklogs / f"{prefix}sample.md"
        plain = self.worklogs / "sample.md"
        encoded.write_text("encoded\n", encoding="utf-8")
        plain.write_text("plain\n", encoding="utf-8")
        cfg = self.config()
        projects = discover_projects(cfg)
        files, orphans = read_worklog_files(cfg, projects)
        chosen = worklog_for(project_named(projects, "sample"), files, cfg.worklog_dir, cfg.root)
        self.assertEqual(chosen, plain)
        self.assertEqual(orphans, [])

    def test_given_nested_cwd_when_matched_then_longest_project_wins(self):
        app = self.project("app")
        plugin = app / "plugin"
        plugin.mkdir()
        from orchestrator.projects import Project
        projects = [Project("app", app.resolve()), Project("plugin", plugin.resolve())]
        self.assertEqual(match_project(str(plugin / "src"), projects).name, "plugin")
        self.assertEqual(match_project(str(app / "src"), projects).name, "app")
        self.assertIsNone(match_project(str(self.projects / "nope"), projects))


class GitFactsTest(TempCase):
    def test_given_non_repo_when_queried_then_no_facts(self):
        empty = self.project("empty")
        self.assertEqual(git_facts(empty, 5), (False, False))

    def test_given_untracked_file_when_queried_then_dirty_and_not_ahead(self):
        path = self.project("sample")
        git(path, "init", "-b", "main")
        (path / "extra.txt").write_text("synthetic\n", encoding="utf-8")
        self.assertEqual(git_facts(path, 5), (True, False))

    def test_given_commit_without_upstream_when_queried_then_not_ahead(self):
        path = self.project("sample")
        git(path, "init", "-b", "main")
        (path / "README").write_text("synthetic\n", encoding="utf-8")
        git(path, "add", "README")
        git(path, "commit", "-m", "init")
        self.assertEqual(git_facts(path, 5), (False, False))

    def test_given_unpushed_commit_when_queried_then_ahead(self):
        path = self.project("sample")
        bare = self.tmp / "bare.git"
        git(path, "init", "-b", "main")
        subprocess.run(
            ["git", "init", "--bare", "-b", "main", str(bare)],
            check=True,
            capture_output=True,
        )
        (path / "README").write_text("synthetic\n", encoding="utf-8")
        git(path, "add", "README")
        git(path, "commit", "-m", "init")
        git(path, "remote", "add", "origin", str(bare))
        git(path, "push", "-u", "origin", "HEAD")
        (path / "README").write_text("synthetic\nmore\n", encoding="utf-8")
        git(path, "add", "README")
        git(path, "commit", "-m", "second")
        self.assertEqual(git_facts(path, 5), (False, True))
