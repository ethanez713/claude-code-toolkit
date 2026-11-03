"""Load orch.toml (or the committed orch.example.toml) and the paths a scan may touch."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Callable


def encoded_prefix(root: Path) -> str:
    """Worklog stems that mirror a path replace '/' and '_' with '-'."""
    text = root.expanduser().resolve().as_posix().rstrip("/")
    return text.replace("/", "-").replace("_", "-") + "-"


def default_repo() -> Path:
    return Path(__file__).resolve().parents[1]


@dataclass
class Config:
    root: Path
    exclude: tuple[str, ...] = ()
    aliases: dict[str, Path] = field(default_factory=dict)
    no_git_check: tuple[str, ...] = ()
    runnable_days: int = 14
    sweep_days: int = 30
    count_ttl_seconds: int = 300
    reset_context_pct: int = 50
    wrapper: str = ""
    tmux_session: str = ""
    default_socket: str = ""
    worklog_dir: Path = field(default_factory=lambda: Path.home() / ".claude" / "worklog")
    sessions_dir: Path = field(default_factory=lambda: Path.home() / ".claude" / "sessions")
    transcripts_dir: Path = field(default_factory=lambda: Path.home() / ".claude" / "projects")
    panes_dir: Path = field(default_factory=lambda: Path.home() / ".claude" / "autoresume" / "panes")
    state_dir: Path = field(default_factory=lambda: default_repo() / "state")
    status_path: Path = field(default_factory=lambda: default_repo() / "STATUS.md")
    repo_dir: Path = field(default_factory=default_repo)
    agents_runner: Callable[[], list] | None = None
    today: Callable[[], date] = date.today
    now: Callable[[], datetime] = datetime.now
    git_timeout: float = 5.0
    tmux_runner: Callable[..., object] | None = None
    sleeper: Callable[[float], None] | None = None

    def expand(self) -> Config:
        self.root = self.root.expanduser().resolve()
        self.aliases = {key: path.expanduser().resolve() for key, path in self.aliases.items()}
        self.worklog_dir = self.worklog_dir.expanduser()
        self.sessions_dir = self.sessions_dir.expanduser()
        self.transcripts_dir = self.transcripts_dir.expanduser()
        self.panes_dir = self.panes_dir.expanduser()
        self.state_dir = self.state_dir.expanduser()
        self.status_path = self.status_path.expanduser()
        self.repo_dir = self.repo_dir.expanduser().resolve()
        return self


def _path_value(raw: str) -> Path:
    return Path(os.path.expanduser(raw))


def config_file(repo: Path) -> Path:
    local = repo / "orch.toml"
    return local if local.exists() else repo / "orch.example.toml"


def load_config(repo: Path | None = None) -> Config:
    repo = (repo or default_repo()).expanduser().resolve()
    with config_file(repo).open("rb") as handle:
        data = tomllib.load(handle)
    aliases = {
        str(key): _path_value(str(value))
        for key, value in dict(data.get("aliases") or {}).items()
    }
    cfg = Config(
        root=_path_value(str(data.get("root", "~"))),
        exclude=tuple(str(item) for item in data.get("exclude") or ()),
        aliases=aliases,
        no_git_check=tuple(str(item) for item in data.get("no_git_check") or ()),
        runnable_days=int(data.get("runnable_days", 14)),
        sweep_days=int(data.get("sweep_days", 30)),
        count_ttl_seconds=int(data.get("count_ttl_seconds", 300)),
        reset_context_pct=int(data.get("reset_context_pct", 50)),
        wrapper=str(data.get("wrapper", "")),
        tmux_session=str(data.get("tmux_session", "")),
        default_socket=str(data.get("default_socket", "")),
        state_dir=repo / "state",
        status_path=repo / "STATUS.md",
        repo_dir=repo,
    )
    return cfg.expand()
