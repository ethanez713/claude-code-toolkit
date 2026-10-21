# grok-handoff

MCP server that hands implementation and reviews to Grok, whose weekly credit is separate from Claude's. Trust model: Grok is trusted like a capable engineer. Two protections only — private paths stay unreadable (sandbox deny list and refusing to start under one, both from `server/rules.py`), and edits land in a git worktree reviewed before merge. No other limits.

Credit lockout: a 100% reading or a credit error writes `grok_out` (state dir) with the period's reset time; `quota.grok_read` returns 100% without a billing read until then.

Layout: `server/` (protocol, jobs, quota, events, rules, doctor, sandbox profile), `scripts/report.py` (the measurement report; `scripts/prices.json` feeds it), `tests/`.

Tests: `python3 -m unittest discover -s tests -t .`
Doctor: `python3 server/doctor.py` (`--dry-run`, `--install`, `--quota`); rerun it after a Grok update or a change to `PRIVATE_PATHS`.
Report: `python3 scripts/report.py` (`--verdict` for self-improve).
Registration: user-scope `claude mcp add … grok`, allowed as `mcp__grok`, and `grok mcp disable grok` so Grok's own sessions don't load it. Check that with `grok mcp doctor grok`; `grok inspect` lists the server even when disabled.
