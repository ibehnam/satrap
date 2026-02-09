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
