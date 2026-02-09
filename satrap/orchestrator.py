"""Satrap orchestrator: plan -> work -> verify -> merge, driven by a single JSON todo document.

This module coordinates three agent roles (planner, worker, verifier) and git worktrees to
execute a hierarchical task plan stored in `.satrap/todo.json`. The orchestrator is designed
to be restartable: it persists state transitions to the todo file and uses stable branch names
so interrupted runs can be resumed.

Lifecycle
1. Plan
  - If the todo file does not exist, initialize it with a title/context and an empty `items` list.
  - If running from the root (no `--step`), ensure the root plan exists:
    - If `todo.items` is empty, call the planner to produce top-level items.
  - If running a specific step, ensure that step is planned:
    - If the step already has children, do not re-plan.
    - Otherwise, call the planner to refine it:
      - If the planner returns exactly 1 item, treat it as an atomic refinement: update the step's
        fields and clear `children`.
      - If the planner returns >1 items, treat them as child steps and upsert them under the parent.

2. Work (implementation)
  - Steps are executed in dependency order using `dependency_batches(...)` over each level's items.
  - For each step that is not `done` or `blocked`, mark it `doing` immediately in `.satrap/todo.json`
    (the todo file is the single source of truth for progress).
  - Two execution modes exist:
    - Atomic step (no children): implement directly on the step branch, potentially retrying with
      progressively stronger worker "tiers" if verification fails.
    - Non-atomic step (has children): recurse into children in dependency order and merge each child
      back into the parent step branch; when all children are done, merge the parent step branch up.

3. Verify (atomic steps only, currently)
  - For atomic steps, compute `base_commit = merge-base(step_branch, parent_branch)` and run the worker
    in the step worktree. After a successful worker run:
    - Commit any uncommitted changes with a traceable message (`satrap: <step> <summary>`).
    - Build the verifier input from the diff and commit list since `base_commit`.
    - If the verifier rejects, append a lesson entry and `git reset --hard base_commit` to discard the
      attempt before trying the next tier.
  - If all tiers fail verification, the step is marked `blocked` with a human-readable reason and the
    verifier/worker notes are appended to `tasks/lessons.md`.

4. Merge
  - When a step is accepted (or when a non-atomic step's children are complete), merge the step branch
    into its parent branch via `git merge --no-ff --no-edit` in the parent worktree, then mark the step
    `done` in `.satrap/todo.json`.
  - Project-level "verify everything then merge satrap/root into the user's base branch" is currently a
    placeholder; today, the run ends after executing/merging steps into `satrap/root`.

Todo state transitions
`TodoStatus` is persisted in `.satrap/todo.json` and is the authoritative orchestration state:
- `pending`: default for newly planned steps.
- `doing`: set at step start, before any git/agent work begins.
- `done`: set only after the step branch has been merged into its parent branch.
- `blocked`: set only after exhausting all worker tiers for an atomic step (with `blocked_reason`).

Notes:
- Planner updates should be non-destructive to execution state: planning updates text/details/deps/
  acceptance criteria and (up)serts children, while preserving existing statuses and any unknown
  extra fields stored in the todo JSON.
- `dependency_batches` treats missing dependency numbers as "not done" and raises on deadlock (cycles
  or unmet prerequisites) rather than looping indefinitely.

Worktree and branching scheme
Satrap reserves the `satrap/` branch namespace and uses git worktrees for isolation:
- Base branch: the branch Satrap is invoked from (must not be detached HEAD).
- Root branch: `satrap/root`, created from the base branch and checked out into a worktree.
- Step branches: `satrap/<number>` (e.g. `satrap/1`, `satrap/2.3`, `satrap/2.3.1`).
- Parent selection:
  - Top-level step `N` merges into `satrap/root`.
  - Nested step `N.M...` merges into `satrap/N.M...`'s immediate ancestor branch
    (e.g. `2.3.1` merges into `satrap/2.3`).
- Worktree locations: `.worktrees/<unique-phrase>/` where the phrase is generated and recorded in
  `phrases.txt` to avoid collisions and make worktree directories human-recognizable.
- Worktree reuse: if a worktree for a branch already exists, Satrap reuses it; it does not implicitly
  rebase or delete branches/worktrees.

Key invariants (and current caveats)
- Single source of truth: `.satrap/todo.json` is the control plane; Satrap persists status changes
  immediately and frequently reloads the file to make scheduling decisions.
- Merge discipline: merges are performed in the parent branch's worktree; step worktrees are for
  implementation and verification inputs.
- Atomic safety: every failed worker attempt for an atomic step is reset back to `base_commit` to
  prevent accumulating partial changes across retries/tiers.
- Verified-before-merge (atomic): an atomic step's changes are merged into its parent only after the
  verifier returns `passed=true`.
- Non-atomic caveat: aggregate verification of parent steps (those that only orchestrate children) is
  currently a placeholder; such steps are considered complete once all children are `done` and the
  branch is merged upward.
- Reset/traceability: reinitializing the todo (via `--reset-todo` or when a previous plan is complete)
  archives the prior file under `.satrap/todo-history/` best-effort before writing the new plan.
"""

from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import hashlib
import os
import sys
from .agents import (
    PlannerBackend,
    PlannerResult,
    VerificationResult,
    VerifierBackend,
    WorkerBackend,
    WorkerTier,
)
from .dag import dependency_batches
from .git_ops import GitClient, GitWorktree
from .render import RenderRole, write_agent_prompt, write_verifier_prompt
from .tmux import PaneContext, ensure_window, in_tmux, kill_pane, pane_target, spawn_worktree_pane
from .todo import TodoDoc, TodoItem, TodoStatus


@dataclass(frozen=True)
class SatrapConfig:
    control_root: Path
    todo_json_path: Path
    todo_schema_path: Path
    model_tiers: list[WorkerTier]
    max_parallel: int
    planner_backend: PlannerBackend
    worker_backend: WorkerBackend
    verifier_backend: VerifierBackend
    git: GitClient

    @property
    def lessons_path(self) -> Path:
        return self.control_root / "tasks" / "lessons.md"

    @property
    def phrases_path(self) -> Path:
        return self.control_root / "phrases.txt"

    @property
    def renders_dir(self) -> Path:
        return self.control_root / ".satrap" / "renders"

    @property
    def worktrees_dir(self) -> Path:
        return self.control_root / ".worktrees"


class SatrapOrchestrator:
    WORKTREE_COLORS = ["green", "yellow", "blue", "magenta", "cyan", "red", "white"]
    ANSI_COLORS = {
        "black": "30",
        "red": "31",
        "green": "32",
        "yellow": "33",
        "blue": "34",
        "magenta": "35",
        "cyan": "36",
        "white": "37",
    }

    def __init__(self, cfg: SatrapConfig) -> None:
        self.cfg = cfg
        self._pane_by_step: dict[str, PaneContext] = {}

    def _color_for_step(self, *, step_number: str) -> str:
        idx = int(hashlib.sha1(step_number.encode("utf-8")).hexdigest(), 16) % len(self.WORKTREE_COLORS)
        return self.WORKTREE_COLORS[idx]

    def _colorize(self, *, text: str, color: str) -> str:
        code = self.ANSI_COLORS.get(color)
        if code is None:
            return text
        return f"\033[{code}m{text}\033[0m"

    def _tmux_window_name(self) -> str:
        return os.environ.get("SATRAP_TMUX_WINDOW", "satrap")

    def _worktree_panes_enabled(self) -> bool:
        if not in_tmux():
            return False
        return bool(getattr(self.cfg.worker_backend, "use_tmux_panes", False))

    def _get_or_create_step_pane(self, *, step: TodoItem, step_wt: GitWorktree) -> PaneContext | None:
        if step.number in self._pane_by_step:
            return self._pane_by_step[step.number]
        if not self._worktree_panes_enabled():
            return None

        pane_id: str | None = None
        try:
            window_target = ensure_window(window_name=self._tmux_window_name(), cwd=self.cfg.control_root)
            color = self._color_for_step(step_number=step.number)
            pane_id = spawn_worktree_pane(
                window_target=window_target,
                cwd=step_wt.path,
                title=f"{step.number} {step_wt.path.name}",
                color=color,
                select=False,
            )
            ctx = PaneContext(
                pane_id=pane_id,
                window_target=window_target,
                label=step.number,
                worktree_path=step_wt.path,
                color=color,
            )
            self._pane_by_step[step.number] = ctx

            target = pane_id
            try:
                target = pane_target(pane_id=pane_id)
            except Exception:
                pass
            color_label = self._colorize(text=color, color=color)
            print(f"[satrap] pane open [{color_label}] step={step.number} pane={target}", file=sys.stderr)
            return ctx
        except Exception as exc:
            if pane_id:
                kill_pane(pane_id=pane_id)
            print(f"[satrap] pane open failed step={step.number}: {exc}", file=sys.stderr)
            return None

    def _close_step_pane(self, *, step_number: str) -> None:
        pane = self._pane_by_step.pop(step_number, None)
        if pane is None:
            return
        try:
            kill_pane(pane_id=pane.pane_id)
            color = pane.color or "white"
            color_label = self._colorize(text=color, color=color)
            print(f"[satrap] pane close [{color_label}] step={step_number}", file=sys.stderr)
        except Exception as exc:
            print(f"[satrap] pane close failed step={step_number}: {exc}", file=sys.stderr)

    def _close_all_panes(self) -> None:
        for step_number in list(self._pane_by_step.keys()):
            self._close_step_pane(step_number=step_number)

    def _step_tag(self, *, step_number: str) -> str:
        color = self._color_for_step(step_number=step_number)
        return self._colorize(text=f"step {step_number}", color=color)

    def run(self, *, task_text: str, start_step: str | None, reset_todo: bool = False) -> None:
        """Entry point for the CLI.

        `task_text` is only used to initialize the todo file when it does not exist.
        """
        try:
            todo = self._load_or_init_todo(task_text=task_text, reset_todo=reset_todo)
            print(f"[satrap] todo: {self.cfg.todo_json_path}", file=sys.stderr)
            print(f"[satrap] todo context: {(todo.context or '').strip()!r}", file=sys.stderr)
            print(f"[satrap] todo stats: items={len(todo.items)} complete={todo.is_complete()}", file=sys.stderr)

            base_branch = self.cfg.git.current_branch(cwd=self.cfg.control_root)
            root_branch = "satrap/root"
            root_wt = self.cfg.git.ensure_worktree(
                branch=root_branch,
                base_ref=base_branch,
                worktrees_dir=self.cfg.worktrees_dir,
                phrases_path=self.cfg.phrases_path,
            )
            print(f"[satrap] root worktree: {root_wt.branch} -> {root_wt.path}", file=sys.stderr)

            if start_step is None:
                self._ensure_planned(todo=todo, step_number=None, pane=None)
                todo = self._reload_todo()
                if todo.is_complete():
                    print("[satrap] all steps DONE; nothing to do.", file=sys.stderr)
                    return
                for batch in dependency_batches(todo.items, is_done=lambda n: self._reload_todo().is_done(n)):
                    for item in batch:
                        self._run_step(todo=todo, step_number=item.number, parent_branch=root_branch, parent_wt=root_wt)
                        todo = self._reload_todo()
                return

            item = todo.get_item(start_step)
            parent_branch = root_branch if "." not in item.number else f"satrap/{item.number.rsplit('.', 1)[0]}"
            parent_wt = self.cfg.git.ensure_worktree(
                branch=parent_branch,
                base_ref=root_branch,
                worktrees_dir=self.cfg.worktrees_dir,
                phrases_path=self.cfg.phrases_path,
            )
            self._run_step(todo=todo, step_number=item.number, parent_branch=parent_branch, parent_wt=parent_wt)
        finally:
            self._close_all_panes()

    def _run_step(self, *, todo: TodoDoc, step_number: str, parent_branch: str, parent_wt: GitWorktree) -> None:
        current = todo.get_item(step_number)
        if current.status == TodoStatus.DONE:
            return
        if current.status == TodoStatus.BLOCKED:
            return

        tag = self._step_tag(step_number=step_number)
        print(f"[satrap] {tag} step start", file=sys.stderr)

        # Mark doing early (single source of truth).
        todo.set_status(step_number, TodoStatus.DOING)
        self._save_todo(todo)

        step_branch = f"satrap/{step_number}"
        step_wt = self.cfg.git.ensure_worktree(
            branch=step_branch,
            base_ref=parent_branch,
            worktrees_dir=self.cfg.worktrees_dir,
            phrases_path=self.cfg.phrases_path,
        )
        pane = self._get_or_create_step_pane(step=current, step_wt=step_wt)

        try:
            self._ensure_planned(todo=todo, step_number=step_number, pane=pane)
            todo = self._reload_todo()
            step = todo.get_item(step_number)

            if not step.children:
                self._implement_atomic(
                    todo=todo,
                    step=step,
                    step_wt=step_wt,
                    parent_branch=parent_branch,
                    parent_wt=parent_wt,
                    pane=pane,
                )
                return

            for batch in dependency_batches(step.children, is_done=lambda n: self._reload_todo().is_done(n)):
                for child in batch:
                    self._run_step(todo=todo, step_number=child.number, parent_branch=step_branch, parent_wt=step_wt)
                    todo = self._reload_todo()

            self._merge_step_into_parent(
                todo=todo,
                step=step,
                step_branch=step_branch,
                parent_branch=parent_branch,
                parent_wt=parent_wt,
            )
        finally:
            print(f"[satrap] {tag} step end", file=sys.stderr)
            self._close_step_pane(step_number=step_number)

    def _ensure_planned(self, *, todo: TodoDoc, step_number: str | None, pane: PaneContext | None = None) -> None:
        """Planner phase: break down current task into smaller tasks (children)."""
        if step_number is None:
            if todo.items:
                return
        else:
            if todo.get_item(step_number).children:
                return

        target = "root" if step_number is None else f"step {step_number}"
        print(f"[satrap] planning: {target}", file=sys.stderr)
        planner_prompt = write_agent_prompt(
            cfg=self.cfg,
            todo=todo,
            step_number=step_number,
            role=RenderRole.PLANNER,
        )
        plan: PlannerResult = self.cfg.planner_backend.plan(
            prompt_file=planner_prompt,
            schema_file=self.cfg.todo_schema_path,
            step_number=step_number,
            pane=pane,
        )

        # Single source of truth update.
        if step_number is None:
            if plan.title:
                todo.title = plan.title
            todo.items = [TodoItem.from_spec(s) for s in plan.items]
        else:
            if len(plan.items) == 1:
                # Atomic refinement: update this step's fields (no children).
                todo.update_item_from_spec(step_number, plan.items[0])
                todo.get_item(step_number).children = []
            else:
                todo.upsert_children(step_number, plan.items)
        self._save_todo(todo)

    def _implement_atomic(
        self,
        *,
        todo: TodoDoc,
        step: TodoItem,
        step_wt: GitWorktree,
        parent_branch: str,
        parent_wt: GitWorktree,
        pane: PaneContext | None = None,
    ) -> None:
        base_commit = self.cfg.git.merge_base(
            branch=step_wt.branch,
            other_ref=parent_branch,
            cwd=step_wt.path,
        )

        tag = self._step_tag(step_number=step.number)
        for tier in self.cfg.model_tiers:
            print(f"[satrap] {tag} worker start tier={' '.join(tier)}", file=sys.stderr)
            worker_prompt = write_agent_prompt(
                cfg=self.cfg,
                todo=todo,
                step_number=step.number,
                role=RenderRole.WORKER,
            )

            run = self.cfg.worker_backend.spawn(tier=tier, prompt_file=worker_prompt, cwd=step_wt.path, pane=pane)
            outcome = self.cfg.worker_backend.watch(run)
            print(f"[satrap] {tag} worker end exit={outcome.exit_code}", file=sys.stderr)
            if outcome.exit_code != 0:
                self._append_lesson(
                    step=step,
                    tier=tier,
                    note=(
                        "Worker CLI exited non-zero.\n\n"
                        f"- exit_code: {outcome.exit_code}\n"
                        "- action: reset worktree to base and retry with next tier\n"
                    ),
                )
                self.cfg.git.reset_hard(base_commit, cwd=step_wt.path)
                continue
            self.cfg.git.commit_all_if_needed(cwd=step_wt.path, message=f"satrap: {step.number} {step.text[:72]}")

            diff = self.cfg.git.diff_since(base_commit, cwd=step_wt.path)
            commits = self.cfg.git.commits_since(base_commit, cwd=step_wt.path)
            verifier_prompt = write_verifier_prompt(
                cfg=self.cfg,
                todo=todo,
                step_number=step.number,
                diff=diff,
                commits=commits,
            )
            verdict: VerificationResult = self.cfg.verifier_backend.verify(
                prompt_file=verifier_prompt,
                diff=diff,
                commits=commits,
                step=step,
                pane=pane,
            )
            print(f"[satrap] {tag} verifier passed={verdict.passed}", file=sys.stderr)
            if verdict.passed:
                self._merge_step_into_parent(
                    todo=todo,
                    step=step,
                    step_branch=step_wt.branch,
                    parent_branch=parent_branch,
                    parent_wt=parent_wt,
                )
                return

            self._append_lesson(step=step, tier=tier, note=verdict.note or "Verifier rejected with no note.")
            self.cfg.git.reset_hard(base_commit, cwd=step_wt.path)

        todo.set_status(step.number, TodoStatus.BLOCKED)
        todo.set_blocked_reason(step.number, "All worker tiers failed verification. See tasks/lessons.md for notes.")
        self._save_todo(todo)

    def _merge_step_into_parent(
        self,
        *,
        todo: TodoDoc,
        step: TodoItem,
        step_branch: str,
        parent_branch: str,
        parent_wt: GitWorktree,
    ) -> None:
        # Placeholder: real implementation should handle merge conflicts and potentially retry/rebase strategies.
        self.cfg.git.merge_into(
            source_branch=step_branch,
            target_branch=parent_branch,
            cwd=parent_wt.path,
        )
        print(f"[satrap] {self._step_tag(step_number=step.number)} merge {step_branch} -> {parent_branch}", file=sys.stderr)
        todo.set_status(step.number, TodoStatus.DONE)
        self._save_todo(todo)

    def _append_lesson(self, *, step: TodoItem, tier: WorkerTier, note: str) -> None:
        self.cfg.lessons_path.parent.mkdir(parents=True, exist_ok=True)
        tier_str = " ".join(tier)
        entry = f"\n### {step.number} ({tier_str})\n\n{note.strip()}\n"

        existing = ""
        if self.cfg.lessons_path.exists():
            existing = self.cfg.lessons_path.read_text(encoding="utf-8")

        updated = _append_under_section(existing, header="## Satrap", content=entry)
        self.cfg.lessons_path.write_text(updated, encoding="utf-8")

    def _load_or_init_todo(self, *, task_text: str, reset_todo: bool) -> TodoDoc:
        if self.cfg.todo_json_path.exists():
            todo = TodoDoc.load(self.cfg.todo_json_path)
            incoming = task_text.strip()
            existing = (todo.context or "").strip()
            if reset_todo or (incoming and incoming != existing):
                if not reset_todo and (not todo.is_complete()) and todo.items:
                    raise RuntimeError(
                        "todo file already exists for a different task and is not complete. "
                        "Use --reset-todo to overwrite, or pass --todo-json to use a separate file."
                    )

                # Archive the prior todo.json for traceability.
                hist_dir = (self.cfg.control_root / ".satrap" / "todo-history").resolve()
                hist_dir.mkdir(parents=True, exist_ok=True)
                ts = datetime.now().strftime("%Y%m%d-%H%M%S")
                archive = hist_dir / f"todo-{ts}.json"
                try:
                    archive.write_text(self.cfg.todo_json_path.read_text(encoding="utf-8"), encoding="utf-8")
                except Exception:
                    # Best-effort archival; do not block reset on history write failures.
                    pass

                reason = "forced by --reset-todo" if reset_todo else "new task input and previous plan complete"
                print(f"[satrap] resetting todo.json ({reason})", file=sys.stderr)
                title = (incoming.splitlines() or ["satrap task"])[0][:512] or "satrap task"
                todo = TodoDoc(title=title, context=task_text, items=[])
                self._save_todo(todo)
                return todo
            return todo
        title = (task_text.strip().splitlines() or ["satrap task"])[0][:512] or "satrap task"
        todo = TodoDoc(title=title, context=task_text, items=[])
        self._save_todo(todo)
        return todo

    def _reload_todo(self) -> TodoDoc:
        return TodoDoc.load(self.cfg.todo_json_path)

    def _save_todo(self, todo: TodoDoc) -> None:
        todo.save(self.cfg.todo_json_path)


def _append_under_section(text: str, *, header: str, content: str) -> str:
    """Append `content` under the given markdown section header (create it if missing)."""
    if not text.strip():
        text = "# Lessons\n\n## Codex\n- (empty)\n\n## Satrap\n- (empty)\n"

    lines = text.splitlines()
    try:
        idx = next(i for i, ln in enumerate(lines) if ln.strip() == header)
    except StopIteration:
        if not text.endswith("\n"):
            text += "\n"
        text += f"\n{header}\n- (empty)\n"
        lines = text.splitlines()
        idx = next(i for i, ln in enumerate(lines) if ln.strip() == header)

    # Find insertion point: end of file (conservative).
    out = text

    # Remove the placeholder "- (empty)" under this section if present and there is no other content yet.
    section_text = "\n".join(lines[idx :])
    if section_text.splitlines()[1:2] == ["- (empty)"]:
        out_lines = lines[: idx + 1] + lines[idx + 2 :]
        out = "\n".join(out_lines).rstrip() + "\n"

    if not out.endswith("\n"):
        out += "\n"
    return out + content.lstrip("\n")
