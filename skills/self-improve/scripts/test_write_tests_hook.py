#!/usr/bin/env python3
"""Path matching, gate behaviour and stdin contract for write_tests_hook.py.

    python3 scripts/test_write_tests_hook.py
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "write_tests_hook", Path(__file__).with_name("write_tests_hook.py"))
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)

tmp = Path(tempfile.mkdtemp(prefix="wt-hook-test-"))
h.MARKERS = str(tmp / "markers")
h.LOG = str(tmp / "gate.jsonl")

failures = []


def check(name, got, want):
    if got != want:
        failures.append(f"{name}: got {got!r}, want {want!r}")


def event(tool, path, path_key="file_path", session="s1", transcript=""):
    inp = {path_key: path} if path is not None else {}
    return {"tool_name": tool, "tool_input": inp, "session_id": session,
            "transcript_path": transcript}


check("Kotlin *Test.kt", h.is_test_path("app/src/test/java/FooTest.kt"), True)
check("Python test_*.py", h.is_test_path("test_parser.py"), True)
check("Python *_test.py", h.is_test_path("parser_test.py"), True)
check("tests/ directory", h.is_test_path("tests/conftest.py"), True)
check("src/test/ helper", h.is_test_path("src/test/java/com/foo/Helper.kt"), True)
check("androidTest", h.is_test_path("app/src/androidTest/java/Launch.kt"), True)
check("windows slashes", h.is_test_path(r"app\src\test\java\Foo.kt"), True)
check("production Kotlin", h.is_test_path("app/src/main/java/Foo.kt"), False)
check("latest.py not test_", h.is_test_path("latest.py"), False)
check("Contest.kt not *Test.kt", h.is_test_path("Contest.kt"), False)
check("testing/ is not test/", h.is_test_path("testing/foo.py"), False)
check("empty", h.is_test_path(""), False)

check("Write test path", h.is_test_write(event("Write", "FooTest.kt")), True)
check("Edit test path", h.is_test_write(event("Edit", "tests/test_foo.py")), True)
check("path key also works", h.is_test_write(event("Write", "foo_test.py", path_key="path")), True)
check("Write production", h.is_test_write(event("Write", "src/main/Foo.kt")), False)
check("Read of a test file", h.is_test_write(event("Read", "FooTest.kt")), False)
check("missing path", h.is_test_write({"tool_name": "Write", "tool_input": {}}), False)


def run_stdin(payload: str) -> tuple[int, str]:
    buf = io.StringIO()
    old = sys.stdin
    sys.stdin = io.StringIO(payload)
    try:
        with redirect_stdout(buf):
            code = h.main()
    finally:
        sys.stdin = old
    return code, buf.getvalue()


code, out = run_stdin(json.dumps(event("Write", "src/main/Foo.kt", session="gate")))
check("non-test Write silent", (code, out), (0, ""))

code, out = run_stdin(json.dumps(event("Write", "FooTest.kt", session="gate")))
check("first test write exits 0", code, 0)
payload = json.loads(out)["hookSpecificOutput"]
check("envelope event", payload["hookEventName"], "PreToolUse")
check("first test write is denied", payload["permissionDecision"], "deny")
check("deny carries the gate reason", payload["permissionDecisionReason"], h.GATE_REASON)

code, out = run_stdin(json.dumps(event("Edit", "tests/test_x.py", session="gate")))
check("second test write in session passes", (code, out), (0, ""))

code, out = run_stdin(json.dumps(event("Write", "BarTest.kt", session="other")))
check("a new session is gated again", json.loads(out)["hookSpecificOutput"]["permissionDecision"], "deny")

transcript = tmp / "t.jsonl"
transcript.write_text(json.dumps({"message": {"content": [
    {"type": "tool_use", "name": "Skill", "input": {"skill": "write-tests"}}]}}) + "\n")
code, out = run_stdin(json.dumps(event("Write", "FooTest.kt", session="done", transcript=str(transcript))))
check("skill already invoked passes", (code, out), (0, ""))

code, out = run_stdin(json.dumps(event("Write", "FooTest.kt", session="")))
check("missing session silent", (code, out), (0, ""))

code, out = run_stdin("")
check("empty stdin silent", (code, out), (0, ""))

code, out = run_stdin("not-json{")
check("malformed stdin silent", (code, out), (0, ""))

log = [json.loads(l) for l in Path(h.LOG).read_text().splitlines()]
check("one log line per gated session", [r["session"] for r in log], ["gate", "other", "done"])
check("log records prior invocation", [r["already_invoked"] for r in log], [False, False, True])

print("\n".join(failures) if failures else "all write-tests hook tests passed")
sys.exit(1 if failures else 0)
