"""Plan index tables: open gates and rows whose needs are clear."""

from __future__ import annotations

import re
from dataclasses import dataclass, field


# The word after a gate id closes it only when that word is passed or accepted.
_GATE = re.compile(r"\bGATE-([A-Za-z0-9]+)|\bGATE[ \t]+([A-Za-z0-9]+)")
_NEED_GATE = re.compile(r"^GATE-([A-Za-z0-9]+)$|^GATE[ \t]+([A-Za-z0-9]+)$")
_BARE_GATE = re.compile(r"\bGATE:")
# A finished row can name a later gate that checks part of it; that gate has its own row.
_CLOSED_ROW = re.compile(r"(DONE|CLOSED)\b")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_STATUS_WORD = re.compile(r"^(DONE|TODO|IN PROGRESS)\b")
_SEPARATOR = re.compile(r"^:?-+:?$")
_EMPTY_NEED = {"", "—", "–", "-", "n/a", "none"}


@dataclass
class PlanRow:
    row_id: str
    phase: str
    outcome: str
    needs: list[str]
    status: str
    gate: str | None
    done: bool
    runnable_status: bool

    @property
    def text(self) -> str:
        phase = _LINK.sub(r"\1", self.phase).strip()
        outcome = _LINK.sub(r"\1", self.outcome).strip()
        if phase and outcome:
            return f"{phase} — {outcome}"
        return phase or outcome


@dataclass
class Plan:
    rows: list[PlanRow] = field(default_factory=list)

    @property
    def open_gates(self) -> list[PlanRow]:
        return [row for row in self.rows if row.gate]

    def unfinished_ids(self) -> set[str]:
        return {row.row_id for row in self.rows if not row.done}

    def open_gate_ids(self) -> set[str]:
        return {row.gate for row in self.rows if row.gate}


def open_gate_id(status: str, row_id: str = "") -> str | None:
    if _CLOSED_ROW.match(status.strip()):
        return None
    if _BARE_GATE.search(status):
        return f"row-{row_id}"
    found = None
    for match in _GATE.finditer(status):
        gate = match.group(1) or match.group(2)
        rest = status[match.end():]
        word = re.match(r"[^A-Za-z]*([A-Za-z]+)", rest)
        if word and word.group(1).lower() in {"passed", "accepted"}:
            continue
        found = gate
    return found


def _status_flags(status: str) -> tuple[bool, bool]:
    match = _STATUS_WORD.match(status.strip())
    if not match:
        return False, False
    word = match.group(1)
    return word == "DONE", word in {"TODO", "IN PROGRESS"}


def _cells(line: str) -> list[str] | None:
    stripped = line.strip()
    if not stripped.startswith("|"):
        return None
    parts = stripped.split("|")
    if parts and parts[0].strip() == "":
        parts = parts[1:]
    if parts and parts[-1].strip() == "":
        parts = parts[:-1]
    return [part.strip() for part in parts]


def _is_separator(cells: list[str]) -> bool:
    return bool(cells) and all(_SEPARATOR.match(cell.replace(" ", "")) for cell in cells)


def _header_index(headers: list[str], name: str) -> int | None:
    for index, header in enumerate(headers):
        if header.casefold() == name.casefold():
            return index
    return None


def _need_tokens(cell: str) -> list[str]:
    if cell.strip() in _EMPTY_NEED:
        return []
    tokens = []
    for part in cell.split(","):
        token = part.strip()
        if token and token not in _EMPTY_NEED:
            tokens.append(token)
    return tokens


def parse_plan(text: str) -> Plan:
    plan = Plan()
    lines = text.splitlines()
    fence = False
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.strip().startswith("```"):
            fence = not fence
            index += 1
            continue
        if fence:
            index += 1
            continue
        cells = _cells(line)
        if cells is None or _is_separator(cells):
            index += 1
            continue
        if index + 1 >= len(lines):
            break
        separator = _cells(lines[index + 1])
        if separator is None or not _is_separator(separator):
            index += 1
            continue
        headers = cells
        id_at = None
        for column, header in enumerate(headers):
            if header == "#":
                id_at = column
                break
        needs_at = _header_index(headers, "needs")
        status_at = _header_index(headers, "status")
        if id_at is None or needs_at is None or status_at is None:
            index += 2
            while index < len(lines) and _cells(lines[index]) is not None:
                index += 1
            continue
        phase_at = _header_index(headers, "phase")
        if phase_at is None:
            phase_at = _header_index(headers, "name")
        outcome_at = _header_index(headers, "outcome")
        width = len(headers)
        index += 2
        while index < len(lines):
            row_cells = _cells(lines[index])
            if row_cells is None:
                break
            if _is_separator(row_cells):
                index += 1
                continue
            if len(row_cells) < width:
                index += 1
                continue
            status = row_cells[status_at]
            done, runnable_status = _status_flags(status)
            plan.rows.append(PlanRow(
                row_id=row_cells[id_at].strip(),
                phase=row_cells[phase_at] if phase_at is not None else "",
                outcome=row_cells[outcome_at] if outcome_at is not None else "",
                needs=_need_tokens(row_cells[needs_at]),
                status=status,
                gate=open_gate_id(status, row_cells[id_at].strip()),
                done=done,
                runnable_status=runnable_status,
            ))
            index += 1
    return plan


def _need_blocks(token: str, unfinished: set[str], open_gates: set[str]) -> bool:
    match = _NEED_GATE.match(token)
    if match:
        return (match.group(1) or match.group(2)) in open_gates
    if token in unfinished:
        return True
    return token in open_gates


def runnable_rows(plan: Plan) -> list[PlanRow]:
    unfinished = plan.unfinished_ids()
    open_gates = plan.open_gate_ids()
    ready = []
    for row in plan.rows:
        if not row.runnable_status or row.gate:
            continue
        if any(_need_blocks(token, unfinished, open_gates) for token in row.needs):
            continue
        ready.append(row)
    return ready
