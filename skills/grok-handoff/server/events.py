"""JSONL event log under the grok-mcp state root."""

import fcntl
import json
import os
from datetime import datetime, timezone
from pathlib import Path

MAX_BYTES = 10 * 1024 * 1024


def state_root():
    root = Path(os.environ.get("GROK_MCP_STATE") or "~/.local/state/grok-mcp").expanduser()
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    jobs = root / "jobs"
    jobs.mkdir(exist_ok=True)
    os.chmod(jobs, 0o700)
    return root


def write_private(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(path, 0o600)


def write_json_atomic(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_name(path.name + ".tmp")
    data = json.dumps(obj, indent=2)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(data)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def log(event, **fields):
    root = state_root()
    path = root / "events.jsonl"
    rec = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "event": event,
        "v": 1,
    }
    rec.update(fields)
    data = (json.dumps(rec, ensure_ascii=False) + "\n").encode()
    lock_fd = os.open(root / "events.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        os.chmod(root / "events.lock", 0o600)
        if path.exists() and path.stat().st_size >= MAX_BYTES:
            rotated = root / "events.1.jsonl"
            os.replace(path, rotated)
            os.chmod(rotated, 0o600)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
        os.chmod(path, 0o600)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
