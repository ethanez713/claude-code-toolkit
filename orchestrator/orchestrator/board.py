"""Assemble, number, and render the board, and apply the item commands."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from orchestrator.config import Config
from orchestrator.markers import (
    Marker,
    append_line,
    atomic_write,
    delete_line,
    format_marker,
    marker_id,
    parse_worklog,
    read_text,
    replace_line,
    runnable_steps,
)
from orchestrator.plans import parse_plan, runnable_rows
from orchestrator.projects import (
    discover_projects,
    git_facts,
    match_project,
    project_named,
    read_worklog_files,
    worklog_for,
)
from orchestrator.sessions import live_sessions
from orchestrator.sweep import candidates_for


SECTIONS = (
    "Right now",
    "Decide",
    "Check",
    "Next real use",
    "Over days",
    "Desk",
    "Answered, awaiting pick-up",
    "Runnable",
    "Running",
    "Session endings to review",
)
WAITING = set(SECTIONS[:6])
NUMBERED = WAITING | {"Answered, awaiting pick-up"}
KIND_SECTION = {
    "decide": "Decide",
    "check": "Check",
    "event": "Next real use",
    "soak": "Over days",
    "desk": "Desk",
}
WHEN_RE = re.compile(r"^(\d+)d$")


class CommandError(Exception):
    pass


@dataclass
class ScanResult:
    text: str
    items: list[dict] = field(default_factory=list)
    waiting: int = 0
    snoozed: int = 0
    backlog: int = 0
    orphans: list[str] = field(default_factory=list)


def _empty_state() -> dict:
    return {
        "scanned_at": None,
        "first_seen": {},
        "snooze": {},
        "acked": [],
        "items": [],
        "waiting": 0,
    }


def _state_path(config: Config) -> Path:
    return config.state_dir / "board.json"


def load_state(config: Config) -> dict:
    path = _state_path(config)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return _empty_state()
    if not isinstance(data, dict):
        return _empty_state()
    state = _empty_state()
    if isinstance(data.get("first_seen"), dict):
        state["first_seen"] = {str(key): str(value) for key, value in data["first_seen"].items()}
    if isinstance(data.get("snooze"), dict):
        state["snooze"] = {str(key): str(value) for key, value in data["snooze"].items()}
    if isinstance(data.get("acked"), list):
        state["acked"] = [str(item) for item in data["acked"] if isinstance(item, str) and item]
    if isinstance(data.get("items"), list):
        state["items"] = [item for item in data["items"] if isinstance(item, dict)]
    if isinstance(data.get("scanned_at"), str):
        state["scanned_at"] = data["scanned_at"]
    if isinstance(data.get("waiting"), int):
        state["waiting"] = data["waiting"]
    return state


def save_state(config: Config, state: dict) -> None:
    config.state_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(config.state_dir, 0o700)
    payload = json.dumps(state, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    atomic_write(_state_path(config), payload, mode=0o600)


def parse_day(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def parse_until(token: str, today: date) -> date | None:
    text = token.strip()
    match = WHEN_RE.fullmatch(text)
    if match:
        return today + timedelta(days=int(match.group(1)))
    return parse_day(text)


def _recent(path: Path, today: date, days: int) -> bool:
    try:
        modified = datetime.fromtimestamp(path.stat().st_mtime).date()
    except OSError:
        return False
    return (today - modified).days <= days


def _section_for_marker(marker: Marker) -> str:
    if marker.mark == "a":
        return "Answered, awaiting pick-up"
    return KIND_SECTION.get(marker.kind, "Decide")


def _base_item(item_id: str, section: str, project: str, kind: str, where: str, text: str, source: str) -> dict:
    return {
        "id": item_id,
        "section": section,
        "project": project,
        "kind": kind,
        "where": where,
        "text": text,
        "source": source,
        "since": "",
        "age": 0,
        "number": None,
        "worklog": None,
        "line": None,
        "gate": None,
        "session": None,
        "transcript": None,
        "reason": None,
        "marker": None,
    }


def _stamp(item: dict, explicit: str | None, state: dict, today: date) -> None:
    parsed = parse_day(explicit)
    state["first_seen"].setdefault(item["id"], today.isoformat())
    if parsed is None:
        parsed = parse_day(state["first_seen"].get(item["id"])) or today
    item["since"] = parsed.isoformat()
    item["age"] = max(0, (today - parsed).days)


def _marker_record(marker: Marker) -> dict:
    return {
        "mark": marker.mark,
        "kind": marker.kind,
        "where": marker.where,
        "text": marker.text,
        "fields": dict(marker.fields),
        "field_order": list(marker.field_order),
        "malformed": marker.malformed,
        "indent": marker.indent,
        "line": marker.line,
    }


def _collect(config: Config, state: dict) -> tuple[list[dict], int, list[str]]:
    today = config.today()
    projects = discover_projects(config)
    files, orphans = read_worklog_files(config, projects)
    sessions = live_sessions(config)
    busy_paths = set()
    for session in sessions:
        if session.status != "busy" or not session.cwd:
            continue
        project = match_project(session.cwd, projects)
        if project is not None:
            busy_paths.add(project.path)

    items: list[dict] = []
    backlog = 0
    worklogs_by_project: dict[Path, list[Path]] = {}
    gates_in_worklog: set[tuple[Path, str]] = set()

    for worklog in files:
        text = read_text(worklog.path)
        if text is None:
            continue
        try:
            parsed = parse_worklog(text)
        except (ValueError, re.error):
            continue
        project = worklog.project
        name = project.name if project is not None else worklog.stem
        if project is not None:
            worklogs_by_project.setdefault(project.path, []).append(worklog.path)
        backlog += parsed.backlog
        for marker in parsed.markers:
            if marker.gate and project is not None:
                gates_in_worklog.add((project.path, marker.gate))
            item = _base_item(
                marker_id(name, marker.text),
                _section_for_marker(marker),
                name,
                marker.kind,
                marker.where,
                marker.text,
                "worklog",
            )
            item["worklog"] = str(worklog.path)
            item["line"] = marker.line
            item["gate"] = marker.gate
            item["marker"] = _marker_record(marker)
            _stamp(item, marker.since, state, today)
            items.append(item)
        if not _recent(worklog.path, today, config.runnable_days):
            continue
        if project is not None and project.path in busy_paths:
            continue
        for step in runnable_steps(parsed.steps):
            step_key = step.step_id or marker_id(name, step.body).split("#", 1)[1]
            item = _base_item(
                f"{name}#step-{step_key}",
                "Runnable",
                name,
                "run",
                "worklog",
                step.body,
                "runnable",
            )
            item["worklog"] = str(worklog.path)
            _stamp(item, None, state, today)
            items.append(item)

    for project in projects:
        plan_text = read_text(project.path / "docs" / "plans" / "README.md")
        if plan_text is not None:
            plan = parse_plan(plan_text)
            for row in plan.open_gates:
                if (project.path, row.gate) in gates_in_worklog:
                    continue
                item = _base_item(
                    f"{project.name}#{row.gate}",
                    "Check",
                    project.name,
                    "check",
                    "phone",
                    row.text,
                    "gate",
                )
                item["gate"] = row.gate
                item["worklog"] = str(worklog_for(project, files, config.worklog_dir, config.root))
                _stamp(item, None, state, today)
                items.append(item)
            for row in [] if project.path in busy_paths else runnable_rows(plan):
                item = _base_item(
                    f"{project.name}#row-{row.row_id}",
                    "Runnable",
                    project.name,
                    "run",
                    "plan",
                    row.text,
                    "runnable",
                )
                _stamp(item, None, state, today)
                items.append(item)
        if project.name in config.no_git_check or project.path in busy_paths:
            continue
        dirty, ahead = git_facts(project.path, config.git_timeout)
        if dirty:
            item = _base_item(
                f"{project.name}#git-dirty",
                "Desk",
                project.name,
                "desk",
                "desk",
                "Uncommitted changes",
                "git",
            )
            _stamp(item, None, state, today)
            items.append(item)
        if ahead:
            item = _base_item(
                f"{project.name}#git-ahead",
                "Desk",
                project.name,
                "desk",
                "desk",
                "Unpushed commits",
                "git",
            )
            _stamp(item, None, state, today)
            items.append(item)

    for session in sessions:
        if session.status not in {"waiting", "busy"}:
            continue
        project = match_project(session.cwd, projects) if session.cwd else None
        name = project.name if project is not None else (Path(session.cwd).name if session.cwd else "unknown")
        key = session.pid or session.session_id or "live"
        waiting = session.status == "waiting"
        item = _base_item(
            f"{name}#{'wait' if waiting else 'busy'}-{key}",
            "Right now" if waiting else "Running",
            name,
            "waiting" if waiting else "busy",
            "live",
            session.waiting_for or "Session is waiting" if waiting else session.name or "Session is busy",
            "session",
        )
        item["session"] = session.session_id
        _stamp(item, None, state, today)
        items.append(item)

    acked = set(state["acked"])
    for ending in candidates_for(config, projects, worklogs_by_project, acked):
        item = _base_item(
            f"{ending.project}#end-{ending.session}",
            "Session endings to review",
            ending.project,
            "sweep",
            "session",
            ending.snippet,
            "sweep",
        )
        item["session"] = ending.session
        item["transcript"] = str(ending.transcript)
        item["reason"] = ending.reason
        _stamp(item, ending.ended.date().isoformat(), state, today)
        items.append(item)
    return items, backlog, sorted(orphans)


def _apply_snooze(items: list[dict], state: dict, today: date) -> tuple[list[dict], int]:
    kept = []
    hidden = 0
    active = {}
    for key, value in state["snooze"].items():
        until = parse_day(value)
        if until is not None and today < until:
            active[key] = until.isoformat()
    state["snooze"] = active
    for item in items:
        if item["id"] in active:
            hidden += 1
            continue
        kept.append(item)
    return kept, hidden


def _arrange(items: list[dict]) -> list[dict]:
    ordered = []
    number = 1
    for section in SECTIONS:
        group = sorted(
            (item for item in items if item["section"] == section),
            key=lambda item: (item["since"], item["id"]),
        )
        for item in group:
            if section in NUMBERED:
                item["number"] = number
                number += 1
            else:
                item["number"] = None
            ordered.append(item)
    return ordered


def render(items: list[dict], snoozed: int, backlog: int, orphans: list[str]) -> str:
    lines: list[str] = []
    for section in SECTIONS:
        group = [item for item in items if item["section"] == section]
        if not group:
            continue
        lines.append(section)
        for item in group:
            number = f"{item['number']:3d}" if item.get("number") else "   "
            lines.append(
                f"{number}  {item['project']}  {item['kind']}  {item['where']}  "
                f"{item['text']}  {item['age']}d  {item['id']}"
            )
        lines.append("")
    orphan_text = ", ".join(orphans) if orphans else "none"
    lines.append(f"snoozed {snoozed} · backlog {backlog} · orphans: {orphan_text}")
    return "\n".join(lines).rstrip() + "\n"


def scan(config: Config) -> ScanResult:
    state = load_state(config)
    today = config.today()
    items, backlog, orphans = _collect(config, state)
    visible, hidden = _apply_snooze(items, state, today)
    visible = _arrange(visible)
    text = render(visible, hidden, backlog, orphans)
    waiting = sum(1 for item in visible if item["section"] in WAITING)
    state["items"] = visible
    state["waiting"] = waiting
    state["scanned_at"] = config.now().astimezone().isoformat(timespec="seconds")
    save_state(config, state)
    status = text.encode("utf-8")
    atomic_write(config.status_path, status, mode=0o600)
    return ScanResult(text=text, items=visible, waiting=waiting, snoozed=hidden, backlog=backlog, orphans=orphans)


def _cache_fresh(state: dict, config: Config) -> bool:
    stamp = state.get("scanned_at")
    if not isinstance(stamp, str):
        return False
    try:
        scanned = datetime.fromisoformat(stamp)
    except ValueError:
        return False
    if scanned.tzinfo is None:
        scanned = scanned.astimezone()
    age = (config.now().astimezone() - scanned).total_seconds()
    return age <= config.count_ttl_seconds


def count_waiting(config: Config) -> int:
    state = load_state(config)
    if not _cache_fresh(state, config):
        return scan(config).waiting
    return sum(1 for item in state.get("items", []) if item.get("section") in WAITING)


def resolve_ref(config: Config, ref: str) -> dict:
    state = load_state(config)
    if not state["items"]:
        scan(config)
        state = load_state(config)
    token = ref.strip()
    if token.isdigit():
        number = int(token)
        for item in state["items"]:
            if item.get("number") == number:
                return item
        raise CommandError(f"no item {ref}")
    for item in state["items"]:
        if item.get("id") == token:
            return item
    raise CommandError(f"no item {ref}")


def _clean_text(text: str) -> str:
    cleaned = " ".join(text.split())
    if not cleaned:
        raise CommandError("empty text")
    if " | " in cleaned:
        raise CommandError("text cannot contain ' | '")
    return cleaned


def _marker_from_record(record: dict) -> Marker:
    return Marker(
        mark=record["mark"],
        kind=record["kind"],
        where=record["where"],
        text=record["text"],
        fields=dict(record.get("fields") or {}),
        field_order=list(record.get("field_order") or []),
        malformed=bool(record.get("malformed")),
        indent=record.get("indent") or "",
        line=record.get("line") or "",
        line_no=0,
    )


def answer(config: Config, ref: str, text: str) -> ScanResult:
    cleaned = _clean_text(text)
    item = resolve_ref(config, ref)
    today = config.today().isoformat()
    if item["source"] == "git":
        raise CommandError("git items are derived from the worktree")
    if item["source"] == "gate":
        marker = Marker(
            mark="a",
            kind="check",
            where="phone",
            text=item["text"].replace(" | ", " / "),
            fields={"gate": item["gate"], "answer": cleaned, "answered": today},
            field_order=["gate", "answer", "answered"],
            malformed=False,
            indent="",
            line="",
            line_no=0,
        )
        append_line(Path(item["worklog"]), format_marker(marker))
        return scan(config)
    if item["source"] != "worklog" or not item.get("marker"):
        raise CommandError("not a worklog line")
    marker = _marker_from_record(item["marker"])
    marker.mark = "a"
    marker.fields["answer"] = cleaned
    marker.fields["answered"] = today
    for name in ("answer", "answered"):
        if name not in marker.field_order:
            marker.field_order.append(name)
    if not replace_line(Path(item["worklog"]), marker.line, format_marker(marker)):
        raise CommandError("line is gone from the worklog")
    return scan(config)


def done(config: Config, ref: str) -> ScanResult:
    item = resolve_ref(config, ref)
    if item["source"] in {"git", "gate"} or item.get("gate"):
        raise CommandError("gates and git items stay; edit the plan or the repo")
    if item["source"] != "worklog" or not item.get("line"):
        raise CommandError("not a worklog line")
    if not delete_line(Path(item["worklog"]), item["line"]):
        raise CommandError("line is gone from the worklog")
    return scan(config)


def snooze(config: Config, ref: str, when: str) -> ScanResult:
    item = resolve_ref(config, ref)
    until = parse_until(when, config.today())
    if until is None:
        raise CommandError(f"bad date {when}")
    state = load_state(config)
    state["snooze"][item["id"]] = until.isoformat()
    save_state(config, state)
    return scan(config)


def add(config: Config, project_name: str, kind: str, where: str, text: str, unblocks: str | None = None, gate: str | None = None) -> ScanResult:
    cleaned = _clean_text(text)
    projects = discover_projects(config)
    files, _orphans = read_worklog_files(config, projects)
    project = project_named(projects, project_name)
    if project is None:
        path = config.worklog_dir / f"{project_name}.md"
    else:
        path = worklog_for(project, files, config.worklog_dir, config.root)
    fields = {}
    order = []
    if unblocks:
        fields["unblocks"] = unblocks
        order.append("unblocks")
    if gate:
        fields["gate"] = gate
        order.append("gate")
    fields["since"] = config.today().isoformat()
    order.append("since")
    marker = Marker(
        mark="?",
        kind=kind,
        where=where,
        text=cleaned,
        fields=fields,
        field_order=order,
        malformed=False,
        indent="",
        line="",
        line_no=0,
    )
    append_line(path, format_marker(marker))
    return scan(config)


def acknowledge(config: Config, prefix: str) -> list[str]:
    token = prefix.strip()
    if not token:
        raise CommandError("missing session prefix")
    current = scan(config)
    matches = [
        item["session"]
        for item in current.items
        if item["source"] == "sweep" and item.get("session") and str(item["session"]).startswith(token)
    ]
    if not matches:
        raise CommandError(f"no session matches {prefix}")
    state = load_state(config)
    acked = set(state["acked"])
    acked.update(matches)
    state["acked"] = sorted(acked)
    save_state(config, state)
    scan(config)
    return matches
