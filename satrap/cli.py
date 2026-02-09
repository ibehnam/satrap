"""satrap.cli

Command-line entrypoint for Satrap, a recursive orchestrator that:
1) plans work into a structured todo tree,
2) executes steps in isolated git worktrees/branches via an external "worker" CLI,
3) verifies results via an external "verifier" CLI,
4) updates `.satrap/todo.json` as the single source of truth for step status.

Entry points
- `satrap.cli:main`
- `python3 -m satrap ...` (delegates to this module)

Usage (conceptual)
- `python3 -m satrap "do the thing"`
- `python3 -m satrap path/to/task.txt`
- `python3 -m satrap -` (read task text from stdin)

Positional argument
- `task`: Task input source.
  - If `task == "-"`, reads UTF-8 from `/dev/stdin`.
  - Else if `task` names an existing file, reads that file as UTF-8.
  - Otherwise treats `task` as a literal task string.
  Note: the task text is primarily used to initialize/reset the todo file; when a todo
  already exists and matches the current task context, subsequent runs operate from that
  persisted plan.

Flags (surface area)
- `--step <N>`: Start/resume from a specific todo step number (e.g. `2.3.1`) instead of
  running the whole plan.
- `--todo-json <path>`: Path to the todo JSON file (default: `.satrap/todo.json`).
- `--reset-todo`: Overwrite the todo JSON with a fresh plan for the provided task input.
  When resetting an existing todo, Satrap best-effort archives the previous file under
  `.satrap/todo-history/todo-<timestamp>.json`.
- `--schema-json <path>`: Path to the todo JSON schema passed to the planner backend
  (default: `todo-schema.json`).
- `--verifier-schema-json <path>`: Path to the verifier JSON schema file
  (default: `verifier-schema.json`).
- `--dry-run`: Smoke-test mode. Uses stub planner/worker/verifier backends and a no-op
  git client (details below).
- `--planner-cmd <exe>`: Planner CLI executable/command (default: `claude`).
- `--worker-cmd <exe>`: Worker CLI executable (default: `claude`).
- `--verifier-cmd <exe>`: Verifier CLI executable/command (default: `claude`).
- `--worker-tiers <csv>`: Comma-separated "tier" list (low -> high), default
  `ccss-haiku,ccss-sonnet,ccss-opus,ccss-default`. Each tier currently maps to a single
  worker model name and is tried in order until verification passes.
- `--max-parallel <int>`: Upper bound for parallel step execution when dependencies allow.
  This is scaffolding; the current orchestrator runs batches sequentially but still stores
  the value in config.

Control root and path resolution
Satrap operates relative to a "control root", which is:
- `Path($SATRAP_CONTROL_ROOT).resolve()` when the `SATRAP_CONTROL_ROOT` environment variable
  is set, otherwise
- `Path.cwd().resolve()` at process start.

The control root is used for:
- Resolving CLI paths: `--todo-json`, `--schema-json`, and `--verifier-schema-json` are
  resolved as `(control_root / <arg>).resolve()`, so relative paths are anchored at the
  control root, and absolute paths bypass it.
- Files/directories the orchestrator reads/writes:
  - `.satrap/todo.json` and `.satrap/todo-history/` (todo state and archives)
  - `.satrap/renders/` (rendered planner/worker/verifier prompts)
  - `tasks/lessons.md` (verifier/worker failure notes)
  - `phrases.txt` (worktree-name uniqueness ledger)
  - `.worktrees/` (git worktree directories)
- Git operations: the git client is constructed with `control_root` and runs git commands
  with `cwd=control_root`.

How CLI args map to config/backends
`main()` wires parsed arguments into `SatrapConfig` as follows:
- `control_root` is derived from `SATRAP_CONTROL_ROOT` / `cwd`.
- `todo_json_path` = resolved `--todo-json`
- `todo_schema_path` = resolved `--schema-json` (passed to planner)
- `model_tiers` = `--worker-tiers` split on commas; each tier becomes `[tier]` and the
  worker uses the first element as the model name.
- `max_parallel` = `max(1, --max-parallel)`
- Backends are selected based on `--dry-run`:
  Non-dry-run ("real") backends:
  - Planner: `ExternalPlannerBackend(cmd=--planner-cmd)` runs a schema-validated JSON
    planning call (currently via Claude Code with a fixed model) and uses `jq` to compact
    the JSON schema before passing it to the planner CLI.
  - Worker: `ExternalWorkerBackend(cmd=--worker-cmd)` spawns the worker CLI in the step
    worktree directory using the selected tier model; it passes the rendered prompt via
    `-p` and includes `--dangerously-skip-permissions`.
  - Verifier: `ExternalVerifierBackend(cmd=--verifier-cmd, schema_file=verifier_schema_json)`
    runs a schema-validated JSON verification call (currently via Claude Code with a fixed
    model) and expects `{ "passed": <bool>, "note": <optional str> }`.
  - Git: `GitClient(control_root=control_root)` manages branches/worktrees and merging.
  Dry-run backends:
  - Planner: `StubPlannerBackend()` returns deterministic placeholder steps.
  - Worker: `StubWorkerBackend()` is a no-op that exits successfully.
  - Verifier: `StubVerifierBackend()` always passes.
  - Git: `DryRunGitClient(control_root=control_root)` performs no git operations and
    returns the control root as the "worktree" path.

Dry-run semantics
`--dry-run` is intended as a smoke test of orchestration wiring and file/path behavior:
- No external CLIs are invoked (no `git`, no planner/worker/verifier executables, no `jq`).
- No git state is mutated (no branches/worktrees/commits/merges).
- The orchestrator still reads/writes control-root files:
  - It creates/updates `.satrap/todo.json` and may archive prior todos on reset.
  - It writes rendered prompts under `.satrap/renders/`.
  - It advances todo step statuses (e.g., PENDING -> DOING -> DONE) as if steps ran, even
    though no repository changes are produced by the stub worker.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .agents import (
    ExternalPlannerBackend,
    ExternalVerifierBackend,
    ExternalWorkerBackend,
    StubPlannerBackend,
    StubVerifierBackend,
    StubWorkerBackend,
)
from .git_ops import DryRunGitClient, GitClient
from .orchestrator import SatrapConfig, SatrapOrchestrator
from .tmux import ensure_window, in_tmux, spawn_pane


def _read_task_input(arg: str) -> str:
    if arg == "-":
        return Path("/dev/stdin").read_text(encoding="utf-8")
    p = Path(arg)
    if p.exists() and p.is_file():
        return p.read_text(encoding="utf-8")
    return arg


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="satrap", description="Recursive agentic CLI orchestrator (scaffolding).")
    p.add_argument(
        "task",
        help="Task input. Either a filepath or a literal string. Use '-' to read from stdin.",
    )
    p.add_argument(
        "--step",
        default=None,
        help="Run satrap starting from a specific todo step number (e.g. '2.3.1').",
    )
    p.add_argument(
        "--todo-json",
        default=".satrap/todo.json",
        help="Path to the single source of truth todo JSON (default: ./.satrap/todo.json).",
    )
    p.add_argument(
        "--reset-todo",
        action="store_true",
        help="Overwrite the todo JSON with a fresh plan for the provided task input.",
    )
    p.add_argument(
        "--schema-json",
        default="todo-schema.json",
        help="Path to the todo JSON schema passed to the planner backend.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Use stub planner/worker/verifier backends. Does not call external CLIs.",
    )
    p.add_argument(
        "--planner-cmd",
        default=None,
        help="Planner CLI command (placeholder). Example: 'codex-low'.",
    )
    p.add_argument(
        "--verifier-cmd",
        default=None,
        help="Verifier CLI command (placeholder).",
    )
    p.add_argument(
        "--verifier-schema-json",
        default="verifier-schema.json",
        help="Path to the verifier JSON schema file (default: ./verifier-schema.json).",
    )
    p.add_argument(
        "--worker-tiers",
        default="ccss-haiku,ccss-sonnet,ccss-opus,ccss-default",
        help="Comma-separated worker model tiers (low->high).",
    )
    p.add_argument(
        "--worker-cmd",
        default="claude",
        help="Worker CLI executable (default: claude).",
    )
    p.add_argument(
        "--no-worktree-panes",
        action="store_true",
        help="When running inside tmux, do not spawn a new pane per worktree worker attempt.",
    )
    p.add_argument(
        "--max-parallel",
        type=int,
        default=1,
        help="Maximum parallel children to run when dependencies allow (scaffolding; default 1).",
    )
    p.add_argument(
        "--no-tmux",
        action="store_true",
        help="Do not auto-run satrap inside a new tmux pane.",
    )
    pane_group = p.add_mutually_exclusive_group()
    pane_group.add_argument(
        "--keep-pane",
        action="store_true",
        help="Keep the auto-spawned satrap pane open after exit (default).",
    )
    pane_group.add_argument(
        "--kill-pane",
        action="store_true",
        help="When auto-running in tmux, kill the satrap pane after it exits.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(raw_argv)

    control_root_env = os.environ.get("SATRAP_CONTROL_ROOT")
    control_root = (Path(control_root_env) if control_root_env else Path.cwd()).resolve()
    todo_json = (control_root / args.todo_json).resolve()
    schema_json = (control_root / args.schema_json).resolve()
    verifier_schema_json = (control_root / args.verifier_schema_json).resolve()

    if in_tmux() and not args.no_tmux:
        print("[satrap] spawning tmux pane...", file=sys.stderr)
        window_name = os.environ.get("SATRAP_TMUX_WINDOW", "satrap")
        window = ensure_window(window_name=window_name, cwd=control_root)
        spawn_pane(
            window_target=window,
            argv=[sys.executable, "-m", "satrap", *raw_argv, "--no-tmux"],
            cwd=control_root,
            title="satrap",
            env={"SATRAP_CONTROL_ROOT": str(control_root)},
            keep_pane=(not bool(args.kill_pane)),
        )
        return 0

    worker_tiers = [s.strip() for s in str(args.worker_tiers).split(",") if s.strip()]
    worker_tier_cmds = [[tier] for tier in worker_tiers]

    if args.dry_run:
        git = DryRunGitClient(control_root=control_root)
        planner = StubPlannerBackend()
        worker = StubWorkerBackend()
        verifier = StubVerifierBackend()
    else:
        git = GitClient(control_root=control_root)
        planner = ExternalPlannerBackend(cmd=args.planner_cmd)
        tmux_window_name = os.environ.get("SATRAP_TMUX_WINDOW", "satrap")
        worker = ExternalWorkerBackend(
            cmd=args.worker_cmd,
            control_root=control_root,
            use_tmux_panes=(not bool(args.no_worktree_panes)),
            tmux_window_name=tmux_window_name,
        )
        verifier = ExternalVerifierBackend(cmd=args.verifier_cmd, schema_file=verifier_schema_json)

    cfg = SatrapConfig(
        control_root=control_root,
        todo_json_path=todo_json,
        todo_schema_path=schema_json,
        model_tiers=worker_tier_cmds,
        max_parallel=max(1, int(args.max_parallel)),
        planner_backend=planner,
        worker_backend=worker,
        verifier_backend=verifier,
        git=git,
    )
    orch = SatrapOrchestrator(cfg)

    task_text = _read_task_input(args.task)
    orch.run(task_text=task_text, start_step=args.step, reset_todo=bool(args.reset_todo))
    return 0
