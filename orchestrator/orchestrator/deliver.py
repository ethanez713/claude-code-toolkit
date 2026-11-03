"""Decide where an answer goes, and type it only into an idle empty pane."""

from __future__ import annotations

import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from orchestrator.board import CommandError, load_state, resolve_ref
from orchestrator.config import Config
from orchestrator.markers import parse_worklog, read_text
from orchestrator.projects import (
    Project,
    discover_projects,
    git_facts,
    match_project,
    project_named,
    read_worklog_files,
)
from orchestrator.sessions import LiveSession, live_sessions, read_session_file


RULE = "\u2500" * 20
PROMPT = "\u276f"
_SGR = re.compile(r"\x1b\[([0-9;]*)m")
NBSP = "\u00a0"
TYPE_SECONDS = 0.5
ENTER_SECONDS = 0.8
POLL_SECONDS = 0.2
CLEAR_TIMEOUT = 30
COMPACT_TIMEOUT = 600
BUSY_TIMEOUT = 15


@dataclass
class TmuxResult:
    code: int
    stdout: str
    stderr: str = ""


@dataclass
class PaneView:
    readable: bool
    empty: bool | None
    text: str | None
    context_pct: int | None


@dataclass
class RouteReport:
    project: str
    session: dict | None
    context_pct: int | None
    input_empty: bool | None
    in_progress: bool
    tree_clean: bool
    action: str

    def as_dict(self) -> dict:
        return {
            "project": self.project,
            "session": self.session,
            "context_pct": self.context_pct,
            "input_empty": self.input_empty,
            "in_progress": self.in_progress,
            "tree_clean": self.tree_clean,
            "action": self.action,
        }

    def text(self) -> str:
        session = self.session
        if session is None:
            session_line = "none"
        else:
            session_line = (
                f"pid {_shown(session['pid'])} name {_shown(session['name'])} "
                f"status {_shown(session['status'])} pane {_shown(session['pane'])} "
                f"socket {_shown(session['socket'])}"
            )
        rows = (
            f"project: {self.project}",
            f"session: {session_line}",
            f"context_pct: {_shown(self.context_pct)}",
            f"input_empty: {_shown(self.input_empty)}",
            f"in_progress: {_shown(self.in_progress)}",
            f"tree_clean: {_shown(self.tree_clean)}",
            f"action: {self.action}",
        )
        return "\n".join(rows) + "\n"


def _shown(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _without_dim(line: str) -> str:
    """Drop escape codes and any text drawn dim, which is the TUI's placeholder or suggestion."""
    kept: list[str] = []
    dim = False
    position = 0
    for code in _SGR.finditer(line):
        if not dim:
            kept.append(line[position:code.start()])
        params = code.group(1).split(";") if code.group(1) else ["0"]
        if "2" in params:
            dim = True
        elif "0" in params or "22" in params:
            dim = False
        position = code.end()
    if not dim:
        kept.append(line[position:])
    return "".join(kept)


def parse_pane(text: str) -> PaneView:
    """The input box is the text between the last two rule lines, minus the prompt."""
    plain = _SGR.sub("", text).splitlines()
    rules = [index for index, line in enumerate(plain) if RULE in line]
    if len(rules) < 2:
        return PaneView(False, None, None, None)
    typed_lines = [_without_dim(line) for line in text.splitlines()[rules[-2] + 1:rules[-1]]]
    contents = _box_contents(plain[rules[-2] + 1:rules[-1]])
    return PaneView(True, not _box_contents(typed_lines).strip(), contents, _context_pct(plain[rules[-1] + 1:]))


def _box_contents(lines: list[str]) -> str:
    parts: list[str] = []
    stripped_prompt = False
    for raw in lines:
        # Claude pads an empty box with non-breaking spaces, which are not drafts.
        line = raw.replace(NBSP, " ").rstrip()
        if not stripped_prompt and line.strip():
            line = _strip_prompt(line)
            stripped_prompt = True
        parts.append(line)
    while parts and not parts[0].strip():
        del parts[0]
    while parts and not parts[-1].strip():
        del parts[-1]
    return "\n".join(parts)


def _strip_prompt(line: str) -> str:
    trimmed = line.lstrip(" \t")
    if not trimmed.startswith(PROMPT):
        return line
    rest = trimmed[len(PROMPT):]
    if rest.startswith(" "):
        rest = rest[1:]
    return rest


def _context_pct(lines: list[str]) -> int | None:
    marker = "Ctx "
    for line in lines:
        start = line.find(marker)
        if start < 0:
            continue
        number = []
        for char in line[start + len(marker):].lstrip():
            if char.isdigit():
                number.append(char)
                continue
            if char == "%" and number:
                return int("".join(number))
            break
    return None


def _holds(contents: str, sent: str) -> bool:
    if not contents or not sent:
        return False
    return sent in " ".join(contents.split())


def default_tmux(args: list[str]) -> TmuxResult:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return TmuxResult(1, "", str(exc))
    return TmuxResult(proc.returncode, proc.stdout or "", proc.stderr or "")


def _run(config: Config, args: list[str]) -> TmuxResult:
    runner = config.tmux_runner or default_tmux
    result = runner(args)
    if isinstance(result, TmuxResult):
        return result
    raise CommandError("tmux runner failed")


def _sleep(config: Config, seconds: float) -> None:
    sleeper = config.sleeper or time.sleep
    sleeper(seconds)


def _invoke(config: Config, args: list[str]) -> None:
    result = _run(config, args)
    if result.code != 0:
        raise CommandError(_first_line(result.stderr) or "tmux failed")


def _first_line(text: str) -> str:
    for line in text.splitlines():
        cleaned = line.strip()
        if cleaned:
            return cleaned
    return ""


def _load_projects(config: Config):
    projects = discover_projects(config)
    files, _orphans = read_worklog_files(config, projects)
    return projects, files


def _resolve(config: Config, token: str, projects: list[Project]) -> Project:
    text = token.strip()
    if not text:
        raise CommandError("no project")
    if text.isdigit() or "#" in text:
        # A populated board is enough. Scanning here would write STATUS.md.
        if not load_state(config)["items"]:
            raise CommandError(f"no item {text}")
        item = resolve_ref(config, text)
        name = item.get("project")
        project = project_named(projects, name) if isinstance(name, str) else None
        if project is None:
            raise CommandError(f"no project {name}")
        return project
    project = project_named(projects, text)
    if project is None:
        raise CommandError(f"no project {text}")
    return project


def _mtime(config: Config, session: LiveSession) -> float:
    if session.pid is None:
        return 0.0
    path = config.sessions_dir / f"{session.pid}.json"
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _choose(config: Config, project: Project, projects: list[Project]) -> LiveSession | None:
    found = []
    for session in live_sessions(config):
        if not session.cwd:
            continue
        match = match_project(session.cwd, projects)
        if match is not None and match.path == project.path:
            found.append(session)
    if not found:
        return None
    # Idle beats busy. Within that, the newest session file wins.
    found.sort(key=lambda session: (
        0 if session.status == "idle" else 1,
        -_mtime(config, session),
        -(session.pid or 0),
    ))
    return found[0]


def _in_progress(files, project: Project) -> bool:
    for item in files:
        if item.project is None or item.project.path != project.path:
            continue
        text = read_text(item.path)
        if not text:
            continue
        for step in parse_worklog(text).steps:
            if step.mark == ">":
                return True
    return False


def _capture(config: Config, session: LiveSession) -> PaneView:
    if not session.socket or not session.tmux_pane:
        return PaneView(False, None, None, None)
    result = _run(config, [
        "tmux", "-S", session.socket, "capture-pane", "-p", "-e", "-t", session.tmux_pane,
    ])
    if result.code != 0:
        return PaneView(False, None, None, None)
    return parse_pane(result.stdout)


def _session_row(session: LiveSession | None) -> dict | None:
    if session is None:
        return None
    return {
        "pid": session.pid,
        "name": session.name,
        "status": session.status,
        "pane": session.tmux_pane,
        "socket": session.socket,
    }


def _action(session: LiveSession | None, view: PaneView, in_progress: bool, tree_clean: bool, threshold: int) -> str:
    if session is None:
        return "new"
    if not session.socket or not session.tmux_pane:
        return "unreachable"
    if session.status == "waiting":
        return "attend"
    if session.status == "busy":
        return "wait"
    # A shell is live, but it is not a Claude prompt. Typing would hit the
    # shell, and a second session can edit the same tree.
    if session.status != "idle":
        return "unreachable"
    if view.context_pct is None or view.context_pct < threshold:
        return "send"
    if not in_progress and tree_clean:
        return "clear"
    return "compact"


def route(config: Config, ref: str) -> RouteReport:
    projects, files = _load_projects(config)
    project = _resolve(config, ref, projects)
    session = _choose(config, project, projects)
    view = _capture(config, session) if session is not None else PaneView(False, None, None, None)
    in_progress = _in_progress(files, project)
    tree_clean = not git_facts(project.path, config.git_timeout)[0]
    return RouteReport(
        project=project.name,
        session=_session_row(session),
        context_pct=view.context_pct,
        input_empty=view.empty if view.readable else None,
        in_progress=in_progress,
        tree_clean=tree_clean,
        action=_action(session, view, in_progress, tree_clean, config.reset_context_pct),
    )


def _status(config: Config, pid: int) -> str | None:
    info = read_session_file(config.sessions_dir / f"{pid}.json")
    if info is None:
        return None
    return info.get("status")


def _require_idle(config: Config, pid: int) -> None:
    status = _status(config, pid)
    if status == "idle":
        return
    if status is None:
        raise CommandError("session status is unknown")
    raise CommandError(f"session is {status}")


def _require_ready(config: Config, session: LiveSession) -> None:
    if session.pid is None:
        raise CommandError("session has no pid")
    _require_idle(config, session.pid)
    view = _capture(config, session)
    # The capture is a round trip. The file can have gone busy during it.
    _require_idle(config, session.pid)
    if not view.readable:
        raise CommandError("input box is unreadable")
    if not view.empty:
        raise CommandError("input box holds a draft")


def _prepare_prompt(text: str) -> str:
    flat = " ".join(text.split())
    while flat.startswith("/"):
        flat = flat[1:].lstrip()
    if not flat:
        raise CommandError("empty text")
    return flat


def _wrapper(config: Config) -> str:
    raw = (config.wrapper or "").strip()
    if not raw:
        raise CommandError("wrapper is not configured")
    return str(Path(raw).expanduser())


def _window(config: Config, project: Project, args: list[str]) -> list[str]:
    if not config.default_socket or not config.tmux_session:
        raise CommandError("tmux session is not configured")
    command = shlex.join([_wrapper(config), *args])
    return [
        "tmux", "-S", config.default_socket, "new-window",
        "-t", config.tmux_session, "-c", str(project.path), command,
    ]


def _plan_send(socket: str, pane: str, text: str) -> list[str]:
    target = ["tmux", "-S", socket, "send-keys", "-t", pane]
    return [
        shlex.join([*target, "-l", "--", text]),
        f"sleep {TYPE_SECONDS}",
        shlex.join([*target, "Enter"]),
        f"sleep {ENTER_SECONDS}",
        "if-remains: " + shlex.join([*target, "Enter"]),
    ]


def _send_literal(config: Config, session: LiveSession, text: str) -> None:
    # Literal text only. A clearing key would wipe whatever the user is typing.
    socket = session.socket or ""
    pane = session.tmux_pane or ""
    _invoke(config, ["tmux", "-S", socket, "send-keys", "-t", pane, "-l", "--", text])
    _sleep(config, TYPE_SECONDS)
    _invoke(config, ["tmux", "-S", socket, "send-keys", "-t", pane, "Enter"])
    _sleep(config, ENTER_SECONDS)
    view = _capture(config, session)
    if view.readable and not view.empty and _holds(view.text or "", text):
        _invoke(config, ["tmux", "-S", socket, "send-keys", "-t", pane, "Enter"])


def _wait_for(config: Config, pid: int, want: str, timeout: float) -> bool:
    elapsed = 0.0
    while True:
        if _status(config, pid) == want:
            return True
        if elapsed >= timeout:
            return False
        stamp = config.now()
        _sleep(config, POLL_SECONDS)
        delta = (config.now() - stamp).total_seconds()
        elapsed += delta if delta > 0 else POLL_SECONDS


def _wait_until_idle(config: Config, pid: int, reset: str) -> None:
    # /clear keeps this pid and replaces the session id. The file can also
    # read busy for a moment, so the idle check starts only after the keys
    # are out; the idle seen before the command does not count.
    timeout = CLEAR_TIMEOUT if reset == "clear" else COMPACT_TIMEOUT
    if not _wait_for(config, pid, "idle", timeout):
        raise CommandError(f"session did not return to idle after /{reset}")


def _start(config: Config, project: Project, prompt: str, dry_run: bool) -> str:
    command = _window(config, project, [prompt])
    if dry_run:
        return shlex.join(command) + "\n"
    _invoke(config, command)
    return f"started {_wrapper(config)} in {config.tmux_session} for {project.name}\n"


def deliver(
    config: Config,
    project_name: str,
    text: str,
    reset: str = "none",
    dry_run: bool = False,
) -> str:
    if reset not in {"none", "clear", "compact"}:
        raise CommandError(f"bad reset {reset}")
    prompt = _prepare_prompt(text)
    projects, _files = _load_projects(config)
    project = project_named(projects, project_name)
    if project is None:
        raise CommandError(f"no project {project_name}")
    session = _choose(config, project, projects)
    if session is None:
        return _start(config, project, prompt, dry_run)
    if session.pid is None:
        raise CommandError("session has no pid")
    if not session.socket or not session.tmux_pane:
        raise CommandError("no tmux pane")
    lines: list[str] = []
    if reset != "none":
        _require_ready(config, session)
        if dry_run:
            lines.extend(_plan_send(session.socket, session.tmux_pane, f"/{reset}"))
            timeout = CLEAR_TIMEOUT if reset == "clear" else COMPACT_TIMEOUT
            lines.append(f"wait status idle {timeout}")
        else:
            _send_literal(config, session, f"/{reset}")
            _wait_until_idle(config, session.pid, reset)
    _require_ready(config, session)
    if dry_run:
        lines.extend(_plan_send(session.socket, session.tmux_pane, prompt))
        lines.append(f"wait status busy {BUSY_TIMEOUT}")
        return "\n".join(lines) + "\n"
    _send_literal(config, session, prompt)
    if not _wait_for(config, session.pid, "busy", BUSY_TIMEOUT):
        raise CommandError("session did not become busy")
    return f"sent to pid {session.pid} pane {session.tmux_pane} on {session.socket}\n"


def open_target(config: Config, ref: str) -> str:
    projects, _files = _load_projects(config)
    project = _resolve(config, ref, projects)
    session = _choose(config, project, projects)
    if session is not None:
        if not session.socket or not session.tmux_pane:
            raise CommandError("no tmux pane")
        _invoke(config, [
            "tmux", "-S", session.socket, "select-window", "-t", session.tmux_pane,
        ])
        return f"focused pane {session.tmux_pane} on {session.socket}\n"
    command = _window(config, project, ["-c"])
    _invoke(config, command)
    return f"opened {_wrapper(config)} -c for {project.name}\n"
