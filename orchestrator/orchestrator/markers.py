"""Parse and rewrite worklog marker lines. Other lines stay byte-identical."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path


MARKER_RE = re.compile(r"^(?P<indent>[ \t]*)- \[(?P<mark>[?a])\][ \t]*(?P<rest>.*)$")
STEP_RE = re.compile(r"^(?P<indent>[ \t]*)- \[(?P<mark>[ xX>])\][ \t]*(?P<body>.*)$")
HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.*)$")
FIELD_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*):[ \t]*(.*)$")
NEEDS_RE = re.compile(r"needs:[ \t]*(.*?)(?:[ \t]+—[ \t]+|[ \t]+–[ \t]+|[ \t]+\|[ \t]+|$)")
BLOCK_HELD = re.compile(r"\bheld\b", re.IGNORECASE)


def line_blocked(line: str) -> bool:
    return "GATE" in line or BLOCK_HELD.search(line) is not None


LETTER_RANGE = re.compile(r"^([A-Za-z])-([A-Za-z])$")
NUMBER_RANGE = re.compile(r"^(\d+)-(\d+)$")

FIELD_ORDER = ("unblocks", "gate", "since", "answer", "answered")


def marker_id(project: str, text: str) -> str:
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:4]
    return f"{project}#{digest}"


@dataclass
class Marker:
    mark: str
    kind: str
    where: str
    text: str
    fields: dict[str, str]
    field_order: list[str]
    malformed: bool
    indent: str
    line: str
    line_no: int

    @property
    def gate(self) -> str | None:
        value = self.fields.get("gate")
        return value or None

    @property
    def since(self) -> str | None:
        value = self.fields.get("since")
        return value or None


@dataclass
class Step:
    mark: str
    step_id: str
    body: str
    needs: list[str]
    backlog: bool
    blocked: bool
    line: str
    line_no: int


@dataclass
class ParsedWorklog:
    markers: list[Marker] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)
    backlog: int = 0

    @property
    def has_question(self) -> bool:
        return any(marker.mark == "?" for marker in self.markers)


def parse_marker(line: str, line_no: int = 0) -> Marker | None:
    stripped = line[:-1] if line.endswith("\n") else line
    if stripped.endswith("\r"):
        stripped = stripped[:-1]
    match = MARKER_RE.match(stripped)
    if not match:
        return None
    indent = match.group("indent")
    mark = match.group("mark")
    rest = match.group("rest")
    parts = rest.split(" | ")
    fields: dict[str, str] = {}
    order: list[str] = []
    malformed = len(parts) < 3
    kind = where = "?"
    text = rest.strip()
    if not malformed:
        kind, where, text = (part.strip() for part in parts[:3])
        if not kind or not where or not text:
            malformed = True
        for extra in parts[3:]:
            field_match = FIELD_RE.match(extra.strip())
            if not field_match:
                malformed = True
                break
            name, value = field_match.group(1), field_match.group(2).strip()
            fields[name] = value
            order.append(name)
    if malformed:
        kind, where = "?", "?"
        text = rest.strip() or stripped
    return Marker(
        mark=mark,
        kind=kind,
        where=where,
        text=text,
        fields=fields,
        field_order=order,
        malformed=malformed,
        indent=indent,
        line=stripped,
        line_no=line_no,
    )


def format_marker(marker: Marker) -> str:
    parts = [marker.kind, marker.where, marker.text]
    seen: set[str] = set()
    ordered = list(marker.field_order)
    for name in FIELD_ORDER:
        if name in marker.fields and name not in ordered:
            ordered.append(name)
    for name in ordered:
        if name in seen or name not in marker.fields:
            continue
        parts.append(f"{name}: {marker.fields[name]}")
        seen.add(name)
    return f"{marker.indent}- [{marker.mark}] " + " | ".join(parts)


def step_id_of(body: str) -> str:
    body = body.strip()
    if not body:
        return ""
    token = body.split()[0]
    return token.rstrip(".:)")


def extract_needs(body: str) -> list[str]:
    match = NEEDS_RE.search(body)
    if not match:
        return []
    value = match.group(1).strip()
    value = re.sub(r"[ \t]*\([^)]*\)[ \t]*$", "", value).strip()
    tokens = []
    for part in value.split(","):
        token = part.strip()
        if token:
            tokens.append(token)
    return tokens


def expand_need(token: str, known: set[str]) -> list[str]:
    if token in known:
        return [token]
    letters = LETTER_RANGE.fullmatch(token)
    if letters:
        start, end = letters.group(1), letters.group(2)
        if start.isupper() == end.isupper() and ord(start) <= ord(end) and ord(end) - ord(start) <= 26:
            return [chr(code) for code in range(ord(start), ord(end) + 1)]
    numbers = NUMBER_RANGE.fullmatch(token)
    if numbers:
        start, end = int(numbers.group(1)), int(numbers.group(2))
        if start <= end and end - start <= 30:
            return [str(number) for number in range(start, end + 1)]
    return [token]


def _backlog_heading(text: str) -> bool:
    folded = text.casefold()
    return "findings" in folded or "reported, not fixed" in folded


def parse_worklog(text: str) -> ParsedWorklog:
    parsed = ParsedWorklog()
    fence = False
    stack: list[tuple[int, bool]] = []
    for line_no, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.rstrip("\r")
        fence_line = stripped.strip()
        if fence_line.startswith("```"):
            fence = not fence
            continue
        if fence:
            continue
        heading = HEADING_RE.match(stripped)
        if heading:
            level = len(heading.group(1))
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, _backlog_heading(heading.group(2))))
            continue
        marker = parse_marker(stripped, line_no)
        if marker is not None:
            parsed.markers.append(marker)
            continue
        step_match = STEP_RE.match(stripped)
        if not step_match:
            continue
        body = step_match.group("body")
        mark = step_match.group("mark")
        if mark == "X":
            mark = "x"
        backlog = any(flag for _, flag in stack)
        step = Step(
            mark=mark,
            step_id=step_id_of(body),
            body=body.strip(),
            needs=extract_needs(body),
            backlog=backlog,
            blocked=line_blocked(stripped),
            line=stripped,
            line_no=line_no,
        )
        parsed.steps.append(step)
        if backlog and mark in {" ", ">"}:
            parsed.backlog += 1
    return parsed


def runnable_steps(steps: list[Step]) -> list[Step]:
    known = {step.step_id for step in steps if step.step_id}
    done: set[str] = set()
    open_ids: set[str] = set()
    for step in steps:
        if not step.step_id:
            continue
        if step.mark == "x":
            done.add(step.step_id)
        else:
            open_ids.add(step.step_id)
    satisfied = done - open_ids
    ready = []
    for step in steps:
        if step.mark == "x" or step.backlog or step.blocked:
            continue
        needed: list[str] = []
        for token in step.needs:
            needed.extend(expand_need(token, known))
        if all(token in satisfied for token in needed):
            ready.append(step)
    return ready


def atomic_write(path: Path, data: bytes, mode: int | None = None) -> None:
    """Replace path atomically, keeping its current mode unless `mode` forces one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    tmp = Path(name)
    try:
        if mode is None:
            mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
        os.chmod(tmp, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def read_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def split_keep(data: bytes) -> list[bytes]:
    if not data:
        return []
    lines = []
    start = 0
    for index, byte in enumerate(data):
        if byte == 0x0A:
            lines.append(data[start:index + 1])
            start = index + 1
    if start < len(data):
        lines.append(data[start:])
    elif data.endswith(b"\n"):
        pass
    return lines


def _line_ending(raw: bytes) -> bytes:
    if raw.endswith(b"\r\n"):
        return b"\r\n"
    if raw.endswith(b"\n"):
        return b"\n"
    return b""


def _line_text(raw: bytes) -> str:
    text = raw.decode("utf-8", errors="replace")
    if text.endswith("\n"):
        text = text[:-1]
    if text.endswith("\r"):
        text = text[:-1]
    return text


def replace_line(path: Path, old_line: str, new_line: str) -> bool:
    data = read_bytes(path)
    if data is None:
        return False
    lines = split_keep(data)
    found = None
    for index, raw in enumerate(lines):
        if _line_text(raw) == old_line:
            found = index
            break
    if found is None:
        return False
    ending = _line_ending(lines[found]) or b"\n"
    lines[found] = new_line.encode("utf-8") + ending
    atomic_write(path, b"".join(lines))
    return True


def delete_line(path: Path, old_line: str) -> bool:
    data = read_bytes(path)
    if data is None:
        return False
    lines = split_keep(data)
    kept = []
    found = False
    for raw in lines:
        if not found and _line_text(raw) == old_line:
            found = True
            continue
        kept.append(raw)
    if not found:
        return False
    atomic_write(path, b"".join(kept))
    return True


def append_line(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = read_bytes(path)
    if existing is None:
        existing = b""
    prefix = b""
    if existing and not existing.endswith(b"\n"):
        prefix = b"\n"
    payload = existing + prefix + line.encode("utf-8")
    if not payload.endswith(b"\n"):
        payload += b"\n"
    atomic_write(path, payload)


def read_text(path: Path) -> str | None:
    data = read_bytes(path)
    if data is None:
        return None
    return data.decode("utf-8", errors="replace")
