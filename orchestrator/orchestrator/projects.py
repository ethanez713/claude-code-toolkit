"""Project discovery, worklog stem mapping, and git facts."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from orchestrator.config import Config, encoded_prefix


def swap_dashes(name: str) -> str:
    return name.replace("-", "\0").replace("_", "-").replace("\0", "_")


def resolve_stem(stem: str, root: Path, aliases: dict[str, Path]) -> Path | None:
    prefix = encoded_prefix(root)
    stripped = stem[len(prefix):] if prefix and stem.startswith(prefix) else stem
    for name in (stripped, swap_dashes(stripped)):
        candidate = root / name
        try:
            if candidate.is_dir():
                return candidate.resolve()
        except OSError:
            continue
    for key in (stem, stripped):
        target = aliases.get(key)
        if target is None:
            continue
        try:
            if target.is_dir():
                return target.resolve()
        except OSError:
            continue
    return None


@dataclass(frozen=True)
class Project:
    name: str
    path: Path


@dataclass
class WorklogFile:
    stem: str
    path: Path
    project: Project | None


def discover_projects(config: Config) -> list[Project]:
    found: dict[Path, Project] = {}
    root = config.root
    try:
        children = list(root.iterdir())
    except OSError:
        children = []
    for child in children:
        name = child.name
        if name.startswith(".") or name in config.exclude:
            continue
        try:
            if not child.is_dir():
                continue
            path = child.resolve()
        except OSError:
            continue
        found[path] = Project(name=name, path=path)
    for target in config.aliases.values():
        try:
            if not target.is_dir():
                continue
            path = target.resolve()
        except OSError:
            continue
        if path not in found:
            found[path] = Project(name=path.name, path=path)
    return sorted(found.values(), key=lambda project: project.name)


def project_named(projects: list[Project], name: str) -> Project | None:
    for project in projects:
        if project.name == name:
            return project
    return None


def match_project(cwd: str, projects: list[Project]) -> Project | None:
    try:
        path = Path(cwd).expanduser().resolve()
    except OSError:
        return None
    best = None
    best_len = -1
    for project in projects:
        try:
            path.relative_to(project.path)
        except ValueError:
            continue
        length = len(project.path.parts)
        if length > best_len:
            best = project
            best_len = length
    return best


def read_worklog_files(config: Config, projects: list[Project]) -> tuple[list[WorklogFile], list[str]]:
    by_path = {project.path: project for project in projects}
    files: list[WorklogFile] = []
    orphans: list[str] = []
    try:
        entries = sorted(config.worklog_dir.iterdir(), key=lambda item: item.name)
    except OSError:
        return [], []
    for entry in entries:
        if not entry.is_file() or entry.suffix != ".md":
            continue
        stem = entry.stem
        try:
            resolved = resolve_stem(stem, config.root, config.aliases)
        except OSError:
            resolved = None
        project = by_path.get(resolved) if resolved is not None else None
        if project is None and resolved is not None:
            project = Project(name=resolved.name, path=resolved)
            by_path[resolved] = project
            projects.append(project)
        files.append(WorklogFile(stem=stem, path=entry, project=project))
        if project is None:
            orphans.append(stem)
    return files, orphans


def worklog_for(project: Project, files: list[WorklogFile], worklog_dir: Path, root: Path) -> Path:
    matches = [item for item in files if item.project and item.project.path == project.path]
    if not matches:
        return worklog_dir / f"{project.name}.md"
    prefix = encoded_prefix(root)

    def rank(item: WorklogFile) -> tuple[int, str]:
        stem = item.stem
        stripped = stem[len(prefix):] if prefix and stem.startswith(prefix) else stem
        if stem == project.name:
            return (0, stem)
        if stripped == project.name:
            return (1, stem)
        if swap_dashes(stem) == project.name:
            return (2, stem)
        if swap_dashes(stripped) == project.name:
            return (3, stem)
        return (4, stem)

    matches.sort(key=rank)
    return matches[0].path


def _git_env(path: Path) -> dict[str, str]:
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_PREFIX"):
        env.pop(key, None)
    # Stop discovery at the parent so a non-repo directory does not inherit one.
    env["GIT_CEILING_DIRECTORIES"] = str(path.resolve().parent)
    return env


def git_facts(path: Path, timeout: float) -> tuple[bool, bool]:
    """Return (dirty, ahead). A non-repo or a failed git command is neither."""
    env = _git_env(path)
    try:
        status = subprocess.run(
            ["git", "-C", str(path), "status", "--porcelain", "--untracked-files=normal"],
            capture_output=True,
            timeout=timeout,
            check=False,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return False, False
    if status.returncode != 0:
        return False, False
    dirty = bool(status.stdout.strip())
    try:
        upstream = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--abbrev-ref", "@{upstream}"],
            capture_output=True,
            timeout=timeout,
            check=False,
            env=env,
        )
        if upstream.returncode != 0:
            return dirty, False
        count = subprocess.run(
            ["git", "-C", str(path), "rev-list", "--count", "@{upstream}..HEAD"],
            capture_output=True,
            timeout=timeout,
            check=False,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return dirty, False
    if count.returncode != 0:
        return dirty, False
    ahead = count.stdout.strip() not in {b"", b"0"}
    return dirty, ahead
