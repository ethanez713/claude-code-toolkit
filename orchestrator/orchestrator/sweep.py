"""Session endings whose last ask never got a user reply."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from orchestrator.config import Config
from orchestrator.projects import Project


PHRASES = ("needs you", "your call", "your check", "want me to", "let me know")
TAIL = 600
QUESTION_LINE = re.compile(r"^[ \t]*- \[\?\]")


@dataclass
class Ending:
    project: str
    project_path: Path | None
    session: str
    transcript: Path
    ended: datetime
    reason: str
    snippet: str


def transcript_dir_name(path: Path) -> str:
    text = path.resolve().as_posix()
    return text.replace("/", "-").replace("_", "-")


def _parse_dt(value: str) -> datetime | None:
    text = value.strip()
    if not text:
        return None
    normalised = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalised)
    except ValueError:
        if len(text) >= 10:
            try:
                day = date.fromisoformat(text[:10])
            except ValueError:
                return None
            return datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _text_blocks(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    chunks = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
            chunks.append(block["text"])
    return "\n".join(chunks)


def _row_text(row: dict) -> str:
    message = row.get("message")
    if not isinstance(message, dict):
        return ""
    return _text_blocks(message.get("content")).strip()


def _skipped(row: dict) -> bool:
    return bool(row.get("isSidechain") or row.get("isMeta"))


def read_ending(path: Path) -> tuple[datetime | None, str, str] | None:
    """Return (ended, reason, snippet) when the latest assistant ask is unanswered."""
    try:
        handle = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return None
    last_assistant = None
    last_assistant_at = None
    user_after = False
    ended = None
    try:
        for raw in handle:
            raw = raw.strip()
            if not raw:
                continue
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            stamp = row.get("timestamp")
            if isinstance(stamp, str):
                parsed = _parse_dt(stamp)
                if parsed is not None:
                    ended = parsed
            if _skipped(row):
                continue
            kind = row.get("type")
            if kind == "assistant":
                text = _row_text(row)
                if text:
                    last_assistant = text
                    last_assistant_at = ended
                    user_after = False
            elif kind == "user" and _row_text(row):
                if last_assistant is not None:
                    user_after = True
    except OSError:
        return None
    finally:
        handle.close()
    if not last_assistant or user_after:
        return None
    tail = last_assistant[-TAIL:]
    folded = tail.casefold()
    reason = None
    if "?" in tail:
        reason = "?"
    else:
        for phrase in PHRASES:
            if phrase in folded:
                reason = phrase
                break
    if reason is None:
        return None
    when = last_assistant_at or ended
    snippet = " ".join(tail.split())
    if len(snippet) > 160:
        snippet = snippet[:157] + "..."
    return when, reason, snippet


def _latest_transcript(directory: Path) -> Path | None:
    try:
        files = [item for item in directory.iterdir() if item.is_file() and item.suffix == ".jsonl"]
    except OSError:
        return None
    if not files:
        return None
    try:
        return max(files, key=lambda item: item.stat().st_mtime)
    except OSError:
        return None


def _worklog_question_after(worklogs: list[Path], ended: datetime) -> bool:
    for path in worklogs:
        try:
            modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if modified <= ended:
            continue
        if any(QUESTION_LINE.match(line) for line in text.splitlines()):
            return True
    return False


def candidates_for(
    config: Config,
    projects: list[Project],
    worklogs_by_project: dict[Path, list[Path]],
    acked: set[str],
) -> list[Ending]:
    today = config.today()
    found = []
    repo = config.repo_dir.resolve()
    for project in projects:
        try:
            if project.path.resolve() == repo:
                continue
        except OSError:
            continue
        directory = config.transcripts_dir / transcript_dir_name(project.path)
        latest = _latest_transcript(directory)
        if latest is None:
            continue
        session = latest.stem
        if any(item and (session == item or session.startswith(item)) for item in acked):
            continue
        parsed = read_ending(latest)
        if parsed is None:
            continue
        ended, reason, snippet = parsed
        if ended is None:
            try:
                ended = datetime.fromtimestamp(latest.stat().st_mtime, timezone.utc)
            except OSError:
                continue
        age = (today - ended.astimezone().date()).days
        if age > config.sweep_days:
            continue
        if _worklog_question_after(worklogs_by_project.get(project.path, []), ended):
            continue
        found.append(Ending(
            project=project.name,
            project_path=project.path,
            session=session,
            transcript=latest,
            ended=ended,
            reason=reason,
            snippet=snippet,
        ))
    found.sort(key=lambda item: (item.ended, item.project, item.session))
    return found
