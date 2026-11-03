# Orchestrator

The hub for what is waiting on the user across all projects. `docs/design.md` is the contract;
`docs/plans/README.md` is the build plan while one exists.

When a session here starts, run `orch` and show the board. The `orchestrator` skill covers triage,
logging answers and kicking off agents.

Stdlib only, no third-party dependencies. Tests: `python3 -m unittest discover -s tests`. Tests use
synthetic fixtures and never read real worklogs, transcripts or tmux. `orch.toml`, `STATUS.md`
and `state/` stay gitignored because they can quote private project data.
