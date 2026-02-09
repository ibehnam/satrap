# Workflow Orchestration

## 1. Plan Mode Default

- Enter plan mode for ANY non-trivial task (3+ steps or architectural decisions)
- If something goes sideways, STOP and re-plan immediately - don't keep pushing
- Use plan mode for verification steps, not just building
- Write detailed specs upfront to reduce ambiguity

## 2. Subagent Strategy to keep main context window clean

- Offload research, exploration, and parallel analysis to subagents
- For complex problems, throw more compute at it via subagents
- One task per subagent for focused execution

## 3. Self-Improvement Loop

- After ANY correction from the user: update '.ai/notes.md' with the pattern (make it if it doesn't exist)
- Write rules for yourself that prevent the same mistake
- Ruthlessly iterate on these lessons until mistake rate drops
- Review lessons at session start for relevant project

## 4. Verification Before Done

- Never mark a task complete without proving it works
- Diff behavior between main and your changes when relevant
- Ask yourself: "Would a staff engineer approve this?"
- Run tests, check logs, demonstrate correctness

## 5. Demand Elegance (Balanced)

- For non-trivial changes: pause and ask "is there a more elegant way?"
- If a fix feels hacky: "Knowing everything I know now, implement the elegant solution"
- Skip this for simple, obvious fixes - _don't over-engineer_
- Challenge your own work before presenting it

## 6. Autonomous Bug Fixing

- When given a bug report: just fix it. Don't ask for hand-holding
- Point at logs, errors, failing tests -> then resolve them
- Zero context switching required from the user
- Go fix failing CI tests without being told how

# Task Management

1. **Plan First**: Write plan to '.ai/todo.md' with checkable items
2. **Verify Plan**: Check in before starting implementation
3. **Track Progress**: Mark items complete as you go
4. **Explain Changes**: High-level summary at each step
5. **Document Results**: Add review to '.ai/todo.md'
6. **Capture Lessons**: Update '.ai/lessons.md' after corrections

# Core Principles

- **Simplicity First**: Make every change as simple as possible. Impact minimal code.
- **No Laziness**: Find root causes. No temporary fixes. Senior developer standards.
- **Minimal Impact**: Changes should only touch what's necessary. Avoid introducing bugs.

# Rules to Always Follow

- Zero redundancy, no duplicate code
- Elegant coding style
- No special-casing
- Cover all edge cases, defensive programming
- Always have _one_ source of truth
- _Do only what the user specifically asked you to do, never deviate from that or make additional changes w/o explicit approval of the user. Keep the scope of changes laser-focused to the user's request_

# Git

- Before making new changes, make a new git branch, switch to it, do your work, stage and commit, and when the user approves the changes, merge with main and push to remote.
- Never make changes directly to the "main" branch.

---

# Repository Guidelines

## Project Structure & Module Organization

- `satrap/`: Python package and CLI entrypoints (`satrap.cli:main`, `python3 -m satrap`).
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

- Python 3.13+; use type hints and keep modules small and single-purpose (see `satrap/*.py`).
- Indentation: 4 spaces; line endings: LF.
- Naming: `snake_case` for functions/files, `PascalCase` for classes, `UPPER_SNAKE_CASE` for constants.
- Do not hand-edit `.satrap/renders/*`; treat it as generated output.

## Testing Guidelines

- Automated tests live under `tests/` and use `pytest`.
- Run the full suite with:
  `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q`
- Keep unit tests deterministic; avoid invoking `git`, `tmux`, or `claude` unless explicitly writing integration tests.

## Commit & Pull Request Guidelines

- Git history is not established yet; keep commits small and scoped.
- Preferred commit subject: `satrap: <area> <imperative summary>` (matches the orchestrator’s auto-commit pattern).
- PRs should include what changed and why (link issues if any), plus how you verified locally (e.g., `--dry-run`) and any new config/env vars.

## Security & Configuration Tips

- External CLIs used by default: `git`, `jq`, `claude`; tmux is optional (`--no-tmux`).
- Useful env vars: `SATRAP_CONTROL_ROOT` (run root), `SATRAP_TMUX_WINDOW` (tmux window name).
- When started inside tmux, Satrap auto-spawns into the `SATRAP_TMUX_WINDOW` window (default `satrap`) and now keeps that pane open by default.
- Use `--kill-pane` to auto-close the top-level auto-spawned Satrap pane; it closes 5 seconds after Satrap exits.
