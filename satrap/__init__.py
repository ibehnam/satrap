"""satrap: recursive, git-backed orchestration for agentic CLI tools.

This package implements a scaffold-first "planner -> worker -> verifier" loop as a CLI
(`satrap.cli:main`, runnable via `python -m satrap`). Given a task description, Satrap
maintains a hierarchical todo plan in JSON, executes each step in its own git branch/worktree,
and merges completed work up the step tree.

What Satrap provides
- A CLI entrypoint (`satrap.cli:main`) that wires together planning, execution, verification,
  and progress tracking.
- An orchestrator (`satrap.orchestrator.SatrapOrchestrator`) that:
  - loads/initializes the todo document (default `.satrap/todo.json`, configurable via flags),
  - creates/uses git worktrees under `.worktrees/` for isolation,
  - runs steps in dependency order and recurses into child steps when present,
  - retries atomic steps across configured worker "tiers",
  - merges step branches into their parent branch when verification passes.
- Protocol-style backend interfaces and implementations (`satrap.agents`) for planner/worker/verifier:
  - stub backends used by `--dry-run`,
  - external backends that shell out to CLIs (currently via the Claude Code CLI; planning/verifying
    also requires `jq` to compact JSON schemas).
- Prompt rendering (`satrap.render`) into `.satrap/renders/` and lesson logging into `tasks/lessons.md`.

What Satrap intentionally does not do (yet)
- Expose a broad, stable library API; most modules are internal implementation details and may change.
- Provide an embedded LLM/planner/verifier; non-`--dry-run` execution depends on external CLI tools.
- Guarantee safe parallel execution, robust merge-conflict handling, or a final "verify whole repo and
  merge satrap/root back to the starting branch" flow (these areas are explicitly marked as placeholders).

Key exports from this module
- `__version__`: the package version string. (`__all__` is intentionally limited to this.)

Important invariants and conventions
- `todo.json` is the single source of truth for step status; Satrap (not the planner) owns the `status`
  transitions (`pending` -> `doing` -> `done`/`blocked`).
- Step numbers are hierarchical strings like `1`, `1.2`, `2.3.1`; Satrap derives branch names from them
  (e.g. `satrap/root`, `satrap/1.2`).
- The todo format is treated as extensible: unknown JSON fields are preserved round-trip, and when re-planning
  a step, existing child items not returned by the latest planner run are retained to avoid data loss.
- Non-dry-run runs are allowed to be destructive within step worktrees (e.g. hard resets to undo failed worker
  attempts); Satrap also requires a non-detached HEAD when determining the starting branch.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"

