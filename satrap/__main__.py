"""Module entrypoint for ``python -m satrap``.

How it works
------------
Running ``python -m satrap ...`` executes this module, which is intentionally a thin
wrapper around :func:`satrap.cli.main`. It delegates all argument parsing and orchestration
to the CLI module and then raises ``SystemExit(main())`` so that the CLI return code is
used as the process exit status (and so normal ``SystemExit`` semantics apply).

This is equivalent to invoking the console-script entrypoint ``satrap`` (configured as
``satrap.cli:main`` in ``pyproject.toml``).

Inputs
------
The CLI consumes:

- Command-line arguments (see ``python -m satrap --help``), notably:
  - ``task``: task input, interpreted as:
    - ``-``: read all text from stdin (``/dev/stdin``)
    - an existing file path: read UTF-8 text from that file
    - otherwise: treat the argument as the literal task string
  - ``--step``: run/resume from a specific todo step number (e.g. ``2.3.1``)
  - ``--todo-json``: path to the todo JSON (default ``.satrap/todo.json``)
  - ``--reset-todo``: overwrite the todo file with a fresh plan for the provided task input
  - ``--schema-json`` / ``--verifier-schema-json``: JSON Schemas used by planner/verifier
  - ``--dry-run``: use stub planner/worker/verifier and a no-op git client (no external CLIs, no git state changes)
  - ``--planner-cmd`` / ``--worker-cmd`` / ``--verifier-cmd``: external executables (defaults are wired to ``claude``)
  - ``--worker-tiers``: comma-separated model tiers, low to high, retried on failure
  - ``--max-parallel``: currently scaffolding; orchestration is effectively serial today

- Environment variables:
  - ``SATRAP_CONTROL_ROOT``: if set, satrap resolves ``--todo-json`` and schema paths relative to this directory;
    otherwise it uses the current working directory as the control root.

- On-disk state under the control root:
  - Existing todo JSON (loaded and updated as the single source of truth)
  - ``phrases.txt`` (used to generate unique worktree directory names)
  - ``tasks/lessons.md`` (used as an append-only learning log when worker/verifier reject work)

- External tools (when not in ``--dry-run``):
  - ``git`` for branches/worktrees/merges/commits
  - ``claude`` for planner/worker/verifier by default
  - ``jq`` for compacting JSON Schemas passed to ``claude --json-schema`` (planner/verifier)

Outputs and side effects
------------------------
A successful run returns exit status 0 and typically produces:

- Progress/log lines on stderr (prefixed with ``[satrap]``).
- Planner/verifier structured output may be printed to stdout (normalized JSON extracted from Claude Code's JSON envelope).
- Worker output is streamed through to stdout/stderr while the worker subprocess runs.

File and git side effects (when applicable):

- Creates/updates the todo JSON at ``.satrap/todo.json`` (or ``--todo-json``), including step status transitions
  (``pending`` -> ``doing`` -> ``done`` or ``blocked``).
- Renders prompt files under ``.satrap/renders/`` for planner/worker/verifier.
- When resetting a todo, attempts to archive the previous todo JSON under ``.satrap/todo-history/todo-<timestamp>.json``.
- Appends lessons under the ``## Satrap`` section in ``tasks/lessons.md`` when a worker tier fails or verification rejects.
- When not in ``--dry-run``, manages git branches/worktrees:
  - Ensures a root branch ``satrap/root`` and per-step branches like ``satrap/2.3.1``.
  - Creates git worktrees under ``.worktrees/<unique-phrase>/`` and merges completed step branches upward.
  - Commits work-in-progress in the step worktree if there are changes (commit message: ``satrap: <step> <summary>``).

Failure modes and exit behavior
-------------------------------
This module does not catch exceptions; failures generally surface as an uncaught exception and a non-zero
process exit (with a stack trace), except where explicitly modeled as "blocked" work.

Common failure cases include:

- Argument parsing errors: ``argparse`` raises ``SystemExit(2)`` and prints usage.
- Task input read errors (stdin/file) or invalid/unsupported encodings.
- Todo/task mismatch: if a todo file exists with a different task context and it is not complete,
  satrap raises a ``RuntimeError`` unless ``--reset-todo`` is used or a different ``--todo-json`` path is chosen.
- Invalid todo JSON on disk: ``ValueError`` during load (missing required fields, wrong types).
- Invalid ``--step``: ``KeyError`` if the requested step number does not exist in the todo tree.
- Missing external tools or command failures (non-``--dry-run``):
  - ``jq`` not found (planner/verifier schema compaction) raises ``RuntimeError``.
  - Planner/verifier command exits non-zero raises ``RuntimeError``.
  - Planner/verifier returns malformed/unexpected JSON raises ``ValueError``.
  - ``git`` failures (including merge conflicts) surface as ``subprocess.CalledProcessError``.
  - Detached HEAD when determining the current branch raises ``RuntimeError``.

Notably, "verification failed for all worker tiers" does not currently force a non-zero exit:
the step is marked ``blocked`` in the todo file (with an explanatory reason), lessons are appended,
and the run can still exit 0 after orchestration completes.
"""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
