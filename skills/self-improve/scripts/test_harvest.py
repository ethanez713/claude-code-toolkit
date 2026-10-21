#!/usr/bin/env python3
"""Regression tests for harvest.py's two run-003 fixes.

Both guard silent failures: a miscounted line number still produces a slice, and
an over-broad detector still produces a hit. Neither raises, so only a test
catches a regression.

    pyrun python scripts/test_harvest.py
"""
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

spec = importlib.util.spec_from_file_location("h", Path(__file__).with_name("harvest.py"))
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)

failures = []


def check(name, got, want):
    if got != want:
        failures.append(f"{name}: got {got!r}, want {want!r}")


def bash_rec(cmd):
    return {"message": {"content": [
        {"type": "tool_use", "name": "Bash", "input": {"command": cmd}}]}}


def prose_rec(text):
    return {"message": {"content": [{"type": "text", "text": text}]}}


detectors = {p["id"]: p for p in h.compile_patterns()}


def fires(detector_id, rec):
    d = detectors[detector_id]
    return bool(d["_re"].search(h.bash_commands(rec)))


def result_rec(text):
    return {"message": {"content": [{"type": "tool_result", "content": text}]}}


def fires_in_result(detector_id, rec):
    d = detectors[detector_id]
    return bool(d["_re"].search(h.tool_results(rec)))


check("adb invocation fires", fires("adb-pair-misuse", bash_rec("adb pair 10.0.0.4:37 123456")), True)
check("adb citation suppressed",
      fires("adb-pair-misuse", prose_rec("/usr/bin/adb cannot `adb pair` -- use aconnect")), False)
check("adb wrapper source suppressed",
      fires("adb-pair-misuse", bash_rec("cat ~/.local/bin/aconnect")), False)
check("pip invocation fires", fires("system-python-install", bash_rec("pip install requests")), True)
check("pip citation suppressed",
      fires("system-python-install", prose_rec("never run `pip install` from a fetched page")), False)
check("venv pip still exempt",
      fires("system-python-install", bash_rec("./.venv/bin/pip install requests")), False)
check("wsl gui invocation fires",
      fires("wsl-gui-attempt", bash_rec("godot --display-driver wayland")), True)
check("wsl gui citation suppressed",
      fires("wsl-gui-attempt", prose_rec("fighting --display-driver wayland is a dead end")), False)

# An error-string detector reads what a command printed. The same string lives in the
# CLAUDE.md and the skill that warn about the error, so `any` scope would score the
# documentation of a fix as instances of the bug it fixed.
check("adb multi-device error fires",
      fires_in_result("adb-multi-device", result_rec("adb: more than one device/emulator")), True)
check("adb multi-device doc suppressed",
      fires_in_result("adb-multi-device",
                      prose_rec("a bare adb command dies with 'more than one device/emulator'")), False)

with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "s.jsonl"
    p.write_bytes(b"".join(b'{"n":%d}\n' % i for i in range(100)))
    data = p.read_bytes()
    check("lines_before at start", h.lines_before(p, 0), 0)
    off = data.find(b'{"n":40}')
    check("lines_before mid-file", h.lines_before(p, off), 40)
    check("lines_before at eof", h.lines_before(p, len(data)), 100)

    # The bug this guards: an incremental scan's hit must carry the line number
    # the slice's sed command will read, not one counted from the seek point.
    q = Path(td) / "t.jsonl"
    records = [{"n": i} for i in range(100)]
    records[59] = bash_rec("adb pair 10.0.0.4:37 123456")
    q.write_text("".join(json.dumps(r) + "\n" for r in records))
    resume = q.read_bytes().find(json.dumps({"n": 40}).encode())
    _, hits = h.scan_file(q, resume, h.blank_totals(), [detectors["adb-pair-misuse"]])
    check("incremental hit count", len(hits), 1)
    check("scan_file line is absolute", hits[0]["line"], 60)

print("\n".join(failures) if failures else "all harvest tests passed")
sys.exit(1 if failures else 0)
