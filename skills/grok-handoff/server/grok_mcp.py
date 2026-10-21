"""Newline-delimited JSON-RPC MCP server for the grok tools."""

import json
import sys
import threading
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from server import jobs, quota  # noqa: E402

INSTRUCTIONS = (
    "Hands implementation, review or research to Grok, whose quota is separate from Claude's. "
    "Load the grok-handoff skill before the first run call in a session."
)
VERSION = "0.1.0"

_KINDS = ["implement", "fix", "refactor", "tests", "build", "research", "review", "other"]
_EFFORTS = ["low", "medium", "high", "xhigh"]

TOOLS = [
    {
        "name": "run",
        "description": (
            "Hand a task to Grok and block until the job ends. "
            "New jobs require cwd and kind; pass job to resume a session."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {"type": "string"},
                "cwd": {"type": "string"},
                "kind": {"type": "string", "enum": _KINDS},
                "mode": {"type": "string", "enum": ["edit", "consult"], "default": "edit"},
                "job": {"type": "string"},
                "effort": {"type": "string", "enum": _EFFORTS, "default": "high"},
                "model": {"type": "string"},
                "max_turns": {"type": "integer"},
            },
            "required": ["task"],
        },
    },
    {
        "name": "result",
        "description": "Wait for a job and return its result, or cancel it. Omit job to list unfinished jobs.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job": {"type": "string"},
                "cancel": {"type": "boolean", "default": False},
            },
        },
    },
    {
        "name": "finish",
        "description": (
            "Merge an edit job into the repo, or discard its branch and worktree. "
            "message is required to merge."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "job": {"type": "string"},
                "action": {"type": "string", "enum": ["merge", "discard"]},
                "message": {"type": "string"},
            },
            "required": ["job", "action"],
        },
    },
    {
        "name": "quota",
        "description": "Show Grok and Claude quota and the route. refresh false uses the cached Grok reading.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "refresh": {"type": "boolean", "default": True},
            },
        },
    },
]


class Server:
    def __init__(self, stdin=None, stdout=None):
        self.stdin = stdin or sys.stdin
        self.stdout = stdout or sys.stdout
        self.lock = threading.Lock()
        self.stops = {}
        self.threads = []

    def write(self, msg):
        data = json.dumps(msg, ensure_ascii=False) + "\n"
        with self.lock:
            try:
                self.stdout.write(data)
                self.stdout.flush()
            except BrokenPipeError:
                pass

    def serve(self):
        jobs.prune()
        for line in self.stdin:
            line = line.strip()
            if not line:
                continue
            self._handle_line(line)
        with self.lock:
            stops = list(self.stops.values())
            threads = list(self.threads)
        for ev in stops:
            ev.set()
        for thread in threads:
            thread.join(timeout=30)

    def _handle_line(self, line):
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            self.write({"jsonrpc": "2.0", "id": None,
                        "error": {"code": -32700, "message": "Parse error"}})
            return
        if not isinstance(msg, dict) or not isinstance(msg.get("method"), str):
            ident = msg.get("id") if isinstance(msg, dict) else None
            code = -32601 if isinstance(msg, dict) else -32700
            message = "Method not found" if code == -32601 else "Parse error"
            self.write({"jsonrpc": "2.0", "id": ident, "error": {"code": code, "message": message}})
            return
        method = msg["method"]
        if method == "notifications/initialized":
            return
        if method == "notifications/cancelled":
            self._cancel(msg.get("params") or {})
            return
        if "id" not in msg:
            return
        if method == "initialize":
            self._initialize(msg)
        elif method == "ping":
            self.write({"jsonrpc": "2.0", "id": msg["id"], "result": {}})
        elif method == "tools/list":
            self.write({"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": TOOLS}})
        elif method == "tools/call":
            self._tools_call(msg)
        else:
            self.write({"jsonrpc": "2.0", "id": msg["id"],
                        "error": {"code": -32601, "message": "Method not found"}})

    def _initialize(self, msg):
        params = msg.get("params") or {}
        self.write({
            "jsonrpc": "2.0",
            "id": msg["id"],
            "result": {
                "protocolVersion": params.get("protocolVersion"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "grok", "version": VERSION},
                "instructions": INSTRUCTIONS,
            },
        })

    def _cancel(self, params):
        # Stop waiting only. The job is detached and must keep running.
        rid = params.get("requestId", params.get("id"))
        with self.lock:
            ev = self.stops.get(rid)
            if ev is None and isinstance(rid, str) and rid.isdigit():
                ev = self.stops.get(int(rid))
            if ev is None and isinstance(rid, int):
                ev = self.stops.get(str(rid))
        if ev is not None:
            ev.set()

    def _tools_call(self, msg):
        req_id = msg["id"]
        params = msg.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
        token = meta.get("progressToken") if "progressToken" in meta else None
        stop = threading.Event()

        def notify(payload, token=token, stop=stop):
            if token is None or stop.is_set():
                return
            self.write({
                "jsonrpc": "2.0",
                "method": "notifications/progress",
                "params": {
                    "progressToken": token,
                    "progress": payload["progress"],
                    "message": payload["message"],
                },
            })

        def worker():
            try:
                out = self._dispatch(name, args, stop, token, notify if token is not None else None)
                if out is None:
                    return
                text, is_error = out
                self.write({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": text}],
                        "isError": bool(is_error),
                    },
                })
            except Exception as exc:
                sys.stderr.write(f"grok-mcp: {type(exc).__name__}: {exc}\n")
                if not stop.is_set():
                    self.write({
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "content": [{"type": "text", "text": f"internal error: {exc}"}],
                            "isError": True,
                        },
                    })
            finally:
                with self.lock:
                    self.stops.pop(req_id, None)
                    try:
                        self.threads.remove(thread)
                    except ValueError:
                        pass

        thread = threading.Thread(target=worker, name=f"call-{req_id}", daemon=True)
        with self.lock:
            self.stops[req_id] = stop
            self.threads.append(thread)
        thread.start()

    def _dispatch(self, name, args, stop, token, notify):
        if not isinstance(args, dict):
            return ("arguments must be an object", True)
        if name == "run":
            return jobs.run(args, stop_event=stop, progress_token=token is not None,
                            notify_progress=notify)
        if name == "result":
            return jobs.result(args, stop_event=stop, notify_progress=notify)
        if name == "finish":
            return jobs.finish(args)
        if name == "quota":
            refresh = args.get("refresh", True)
            return (quota.tool_text(bool(refresh)), False)
        return (f"unknown tool {name}", True)


def main():
    Server().serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
