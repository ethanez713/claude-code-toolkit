"""Live sessions: claude agents for liveness, session files for status and pane."""

from __future__ import annotations

import re
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from orchestrator.config import Config


STATUSES = {"idle", "busy", "waiting", "shell"}


# A session file writes its pane as <session>:@<window>.%<pane>.
_FULL_PANE_REF = re.compile(r"([^:]+):@\d+\.(%\d+)")


@dataclass
class Pane:
    socket: str
    session: str
    pane: str
    cwd: str
    path: Path


@dataclass
class LiveSession:
    pid: int | None
    cwd: str | None
    status: str | None
    waiting_for: str | None
    session_id: str | None
    pane_ref: str | None
    socket: str | None
    tmux_session: str | None
    tmux_pane: str | None
    name: str | None = None


def parse_agents_output(text: str) -> list[dict]:
    raw = text.strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("[")
        end = raw.rfind("]")
        if start < 0 or end <= start:
            return []
        try:
            data = json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def default_agents() -> list[dict]:
    try:
        proc = subprocess.run(
            ["claude", "agents", "--json"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    return parse_agents_output(proc.stdout or "")


def _pid(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.isdigit():
        number = int(value)
        return number if number > 0 else None
    return None


def _status(value: object) -> str | None:
    if isinstance(value, str) and value in STATUSES:
        return value
    return None


def read_session_file(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    pane = data.get("pane")
    if not isinstance(pane, str) or not pane:
        pane = data.get("tmux") if isinstance(data.get("tmux"), str) else None
    waiting = data.get("waitingFor")
    cwd = data.get("cwd")
    session_id = data.get("sessionId")
    return {
        "status": _status(data.get("status")),
        "waiting_for": waiting if isinstance(waiting, str) and waiting else None,
        "cwd": cwd if isinstance(cwd, str) and cwd and cwd != "?" else None,
        "pane": pane or None,
        "session_id": session_id if isinstance(session_id, str) and session_id else None,
    }


def load_panes(panes_dir: Path) -> list[Pane]:
    panes = []
    try:
        entries = list(panes_dir.iterdir())
    except OSError:
        return []
    for entry in entries:
        if not entry.is_file():
            continue
        try:
            text = entry.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for raw in text.splitlines():
            parts = raw.split("\t")
            if len(parts) != 4 or not all(parts):
                continue
            panes.append(Pane(
                socket=parts[0],
                session=parts[1],
                pane=parts[2],
                cwd=parts[3],
                path=entry,
            ))
    return panes


def resolve_pane(ref: str | None, panes: list[Pane]) -> Pane | None:
    if not ref:
        return None
    full_ref = _FULL_PANE_REF.fullmatch(ref)
    if full_ref:
        ref = f"{full_ref.group(1)}:{full_ref.group(2)}"
    for pane in panes:
        if ref in {pane.pane, f"{pane.session}:{pane.pane}", f"{pane.session}.{pane.pane}", pane.path.name}:
            return pane
    return None


def live_sessions(config: Config) -> list[LiveSession]:
    if config.agents_runner is None:
        entries = default_agents()
    else:
        try:
            result = config.agents_runner()
        except Exception:
            return []
        if isinstance(result, str):
            entries = parse_agents_output(result)
        elif isinstance(result, list):
            entries = [item for item in result if isinstance(item, dict)]
        else:
            return []
    panes = load_panes(config.panes_dir)
    sessions = []
    for entry in entries:
        pid = _pid(entry.get("pid"))
        file_info = None
        if pid is not None:
            file_info = read_session_file(config.sessions_dir / f"{pid}.json")
        status = (file_info or {}).get("status") or _status(entry.get("status"))
        waiting = (file_info or {}).get("waiting_for")
        if waiting is None and isinstance(entry.get("waitingFor"), str):
            waiting = entry.get("waitingFor") or None
        cwd = (file_info or {}).get("cwd")
        if cwd is None and isinstance(entry.get("cwd"), str):
            cwd = entry.get("cwd")
        session_id = (file_info or {}).get("session_id")
        if session_id is None and isinstance(entry.get("sessionId"), str):
            session_id = entry.get("sessionId")
        pane_ref = (file_info or {}).get("pane")
        resolved = resolve_pane(pane_ref, panes)
        if cwd is None and resolved is not None:
            cwd = resolved.cwd
        sessions.append(LiveSession(
            pid=pid,
            cwd=cwd,
            status=status,
            waiting_for=waiting,
            name=entry.get("name") if isinstance(entry.get("name"), str) else None,
            session_id=session_id,
            pane_ref=pane_ref,
            socket=resolved.socket if resolved else None,
            tmux_session=resolved.session if resolved else None,
            tmux_pane=resolved.pane if resolved else None,
        ))
    return sessions
