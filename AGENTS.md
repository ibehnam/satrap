# Repository Guidelines

## Project Structure & Module Organization

- `satrap/`: Python package and CLI entrypoints (`satrap.cli:main`, `python3 -m satrap`).
- `tasks/`: Human-maintained notes (`tasks/todo.md`, `tasks/lessons.md`).
- `.satrap/todo.json`: Single source of truth for the orchestration plan and step status.
- `todo-schema.json`, `verifier-schema.json`: JSON Schemas used for structured planner/verifier output.
- `phrases.txt`: Phrase uniqueness ledger used when creating git worktrees.
- Generated (ignored): `.worktrees/` (git worktrees), `.satrap/` (rendered prompts), `__pycache__/`.

## Build, Test, and Development Commands

- `python3 -m satrap --help`: CLI usage and flags.
- `python3 -m satrap --dry-run "task"`: Smoke test without calling external CLIs or mutating git state.
- `python3 -m satrap "task"`: Full run (uses git worktrees; may call `claude`, `jq`, and `tmux`).
- Optional editable install:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
```

## Coding Style & Naming Conventions

- Python 3.11+; use type hints and keep modules small and single-purpose (see `satrap/*.py`).
- Indentation: 4 spaces; line endings: LF.
- Naming: `snake_case` for functions/files, `PascalCase` for classes, `UPPER_SNAKE_CASE` for constants.
- Do not hand-edit `.satrap/renders/*`; treat it as generated output.

## Testing Guidelines

- No automated test suite is checked in yet.
- If adding tests, prefer `pytest` and place files under `tests/` as `test_*.py`. Keep unit tests deterministic; avoid invoking `git`, `tmux`, or `claude` unless explicitly writing integration tests.

## Commit & Pull Request Guidelines

- Git history is not established yet; keep commits small and scoped.
- Preferred commit subject: `satrap: <area> <imperative summary>` (matches the orchestrator’s auto-commit pattern).
- PRs should include what changed and why (link issues if any), plus how you verified locally (e.g., `--dry-run`) and any new config/env vars.

## Security & Configuration Tips

- External CLIs used by default: `git`, `jq`, `claude`; tmux is optional (`--no-tmux`).
- Useful env vars: `SATRAP_CONTROL_ROOT` (run root), `SATRAP_TMUX_WINDOW` (tmux window name).
