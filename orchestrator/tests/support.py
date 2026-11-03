"""Shared temp dirs for orch tests. Never points at the real ~/.claude tree."""

import os
import shutil
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from orchestrator.config import Config


TODAY = date(2026, 11, 2)


def local_noon(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, 12, 0).astimezone()


class TempCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="orch-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.projects = self.tmp / "projects"
        self.worklogs = self.tmp / "worklogs"
        self.projects.mkdir()
        self.worklogs.mkdir()
        for name in ("sessions", "transcripts", "panes", "repo"):
            (self.tmp / name).mkdir()
        self.clock = {"now": datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)}

    def config(self, **kwargs) -> Config:
        cfg = Config(
            root=self.projects,
            worklog_dir=self.worklogs,
            sessions_dir=self.tmp / "sessions",
            transcripts_dir=self.tmp / "transcripts",
            panes_dir=self.tmp / "panes",
            state_dir=self.tmp / "state",
            status_path=self.tmp / "STATUS.md",
            repo_dir=self.tmp / "repo",
            agents_runner=lambda: [],
            today=lambda: self.clock["now"].date(),
            now=lambda: self.clock["now"],
        )
        for key, value in kwargs.items():
            setattr(cfg, key, value)
        return cfg

    def project(self, name: str) -> Path:
        path = self.projects / name
        path.mkdir(parents=True)
        return path

    def restore_mode(self, path: Path, mode: int = 0o644) -> None:
        self.addCleanup(_chmod, path, mode)


def _chmod(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        pass
