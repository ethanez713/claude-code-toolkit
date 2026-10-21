#!/usr/bin/env python3
"""Staleness tracker for assertions whose truth lives upstream. No LLM.

Prices, per-model prompting guidance and harness contracts change without
notice and without breaking anything visibly -- the file just quietly stops
being right. state/external-truth.json registers each one with a source and a
re-check cadence; this script says what is due and stamps what was checked.

  due [--all]           entries past cadence, or with a fired trigger
  models [--learn]      model ids in recent transcripts vs. the known set
  prompt-files          every file on this box carrying prompt guidance
  verified ID [--note]  stamp an entry re-checked today

Reads transcripts locally and never sends anything anywhere. Verification
itself is the agent's job and is read-only: fetch the cited source, compare,
edit. Never install or run third-party tooling to answer the question.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
CLAUDE_DIR = Path.home() / ".claude"
STATE = SKILL_DIR / "state"
# Testing `verified` or `models --learn` against the live registry would stamp
# dates and learn models that were never actually audited -- the exact rot this
# tracks. Point this at a scratch copy to exercise the write paths.
REGISTRY = Path(os.environ.get("DRIFT_REGISTRY") or STATE / "external-truth.json")
LOG = REGISTRY.parent / "drift.jsonl"
PROJECTS = CLAUDE_DIR / "projects"

MODEL_RE = re.compile(rb'"model":"(claude-[^"]+)"')

# Models are named in every assistant message, so the tail of a recent transcript
# answers "what are we running now" without reading ~118MB of history.
TAIL_BYTES = 256 * 1024
RECENT_FILES = 25

PRUNE_DIRS = {"node_modules", ".git", "venv", ".venv", "build", ".gradle",
              "__pycache__", "dist", "target", "vendor", ".next"}
MAX_DEPTH = 4


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_atomic(path: Path, text: str, mode: int = 0o600) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    os.replace(tmp, path)
    os.chmod(path, mode)


def load_registry() -> dict:
    return json.loads(REGISTRY.read_text())


def save_registry(reg: dict) -> None:
    write_atomic(REGISTRY, json.dumps(reg, indent=1, ensure_ascii=False) + "\n")


def append_log(record: dict) -> None:
    with open(LOG, "a") as fh:
        fh.write(json.dumps(record) + "\n")
    os.chmod(LOG, 0o600)


def recent_transcripts(limit: int) -> list[Path]:
    files = sorted(PROJECTS.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime,
                   reverse=True)
    return files[:limit]


def models_seen(limit: int = RECENT_FILES) -> dict[str, str]:
    """Model id -> ISO date of the newest transcript it appeared in."""
    seen: dict[str, str] = {}
    for path in recent_transcripts(limit):
        try:
            with open(path, "rb") as fh:
                fh.seek(max(0, path.stat().st_size - TAIL_BYTES))
                blob = fh.read()
        except OSError:
            continue
        when = date.fromtimestamp(path.stat().st_mtime).isoformat()
        for match in MODEL_RE.findall(blob):
            model = match.decode()
            if model not in seen:
                seen[model] = when
    return seen


def days_since(iso: str) -> int:
    return (date.today() - date.fromisoformat(iso)).days


def due_reason(entry: dict, unknown: dict[str, str]) -> str | None:
    if unknown and "new-model" in entry.get("triggers", []):
        return f"trigger new-model: {', '.join(sorted(unknown))}"
    age = days_since(entry["last_verified"])
    cadence = entry["cadence_days"]
    if age >= cadence:
        return f"cadence: {age}d since last check, every {cadence}d"
    return None


def cmd_due(args) -> int:
    reg = load_registry()
    known = set(reg.get("known_models", []))
    unknown = {m: d for m, d in models_seen().items() if m not in known}

    shown = 0
    for entry in reg["entries"]:
        reason = due_reason(entry, unknown)
        if reason is None and not args.all:
            continue
        shown += 1
        head = f"[{entry['id']}]"
        print(f"\n{head} {reason or 'not due'}")
        print(f"  claim:  {entry['claim']}")
        print(f"  files:  {', '.join(entry['files'])}")
        for src in entry["sources"]:
            print(f"  source: {src}")
        print(f"  verify: {entry['verify']}")
        if entry.get("note"):
            print(f"  note:   {entry['note']}")
        print(f"  stamp:  drift.py verified {entry['id']} --note '<what you found>'")

    if not shown:
        print("nothing due")
    if unknown:
        print(f"\nunknown model(s) in recent transcripts: "
              f"{', '.join(f'{m} ({d})' for m, d in sorted(unknown.items()))}")
        print("after auditing, record them with: drift.py models --learn")
    return 0


def cmd_models(args) -> int:
    reg = load_registry()
    known = set(reg.get("known_models", []))
    seen = models_seen(args.files)
    for model, when in sorted(seen.items()):
        print(f"{'known  ' if model in known else 'UNKNOWN'} {model}  (last seen {when})")
    unknown = sorted(m for m in seen if m not in known)
    if not seen:
        print("no model ids found in recent transcripts")
    if args.learn and unknown:
        reg["known_models"] = sorted(known | set(unknown))
        save_registry(reg)
        append_log({"ts": now_iso(), "action": "learn-models", "models": unknown})
        print(f"\nrecorded {len(unknown)} new model(s) as known")
    elif unknown:
        print(f"\n{len(unknown)} unknown; audit first, then: drift.py models --learn")
    return 0


def prompt_files() -> list[Path]:
    files = [CLAUDE_DIR / "CLAUDE.md"]
    files += sorted(CLAUDE_DIR.glob("skills/*/SKILL.md"))
    files += sorted(CLAUDE_DIR.glob("agents/*.md"))

    home = Path.home()
    for root, dirs, names in os.walk(home):
        rel_depth = len(Path(root).relative_to(home).parts)
        if rel_depth >= MAX_DEPTH:
            dirs[:] = []
            continue
        dirs[:] = [d for d in dirs
                   if d not in PRUNE_DIRS and (not d.startswith(".") or d == ".claude")]
        if "CLAUDE.md" in names and Path(root) != CLAUDE_DIR:
            files.append(Path(root) / "CLAUDE.md")
    return [f for f in files if f.exists()]


def cmd_prompt_files(args) -> int:
    for path in prompt_files():
        print(path)
    return 0


def cmd_verified(args) -> int:
    reg = load_registry()
    for entry in reg["entries"]:
        if entry["id"] == args.id:
            previous = entry["last_verified"]
            entry["last_verified"] = date.today().isoformat()
            save_registry(reg)
            append_log({"ts": now_iso(), "action": "verified", "id": args.id,
                        "previous": previous, "note": args.note[:300] if args.note else None})
            print(f"{args.id}: {previous} -> {entry['last_verified']}")
            return 0
    print(f"no such entry: {args.id}", file=sys.stderr)
    print(f"known: {', '.join(e['id'] for e in reg['entries'])}", file=sys.stderr)
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("due"); d.add_argument("--all", action="store_true")
    d.set_defaults(fn=cmd_due)
    m = sub.add_parser("models"); m.add_argument("--learn", action="store_true")
    m.add_argument("--files", type=int, default=RECENT_FILES); m.set_defaults(fn=cmd_models)
    sub.add_parser("prompt-files").set_defaults(fn=cmd_prompt_files)
    v = sub.add_parser("verified"); v.add_argument("id"); v.add_argument("--note", default=None)
    v.set_defaults(fn=cmd_verified)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
