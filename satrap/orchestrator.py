from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
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
    def __init__(self, cfg: SatrapConfig) -> None:
        self.cfg = cfg

    def run(self, *, task_text: str, start_step: str | None, reset_todo: bool = False) -> None:
        """Entry point for the CLI.

        `task_text` is only used to initialize `todo.json` when it does not exist.
        """
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
            self._ensure_planned(todo=todo, step_number=None)
            todo = self._reload_todo()
            if todo.is_complete():
                print("[satrap] all steps DONE; nothing to do.", file=sys.stderr)
                return
            for batch in dependency_batches(todo.items, is_done=lambda n: self._reload_todo().is_done(n)):
                # Placeholder: this can be parallelized safely once git worktree concurrency rules are settled.
                for item in batch:
                    self._run_step(todo=todo, step_number=item.number, parent_branch=root_branch, parent_wt=root_wt)
                    todo = self._reload_todo()

            # Placeholder: verify full project, then merge root into base.
            # self._verify_and_merge_root(...)
            return

        # Resumed / focused run.
        item = todo.get_item(start_step)
        parent_branch = root_branch if "." not in item.number else f"satrap/{item.number.rsplit('.', 1)[0]}"
        parent_wt = self.cfg.git.ensure_worktree(
            branch=parent_branch,
            base_ref=root_branch,
            worktrees_dir=self.cfg.worktrees_dir,
            phrases_path=self.cfg.phrases_path,
        )
        self._run_step(todo=todo, step_number=item.number, parent_branch=parent_branch, parent_wt=parent_wt)

    def _run_step(self, *, todo: TodoDoc, step_number: str, parent_branch: str, parent_wt: GitWorktree) -> None:
        current = todo.get_item(step_number)
        if current.status == TodoStatus.DONE:
            return
        if current.status == TodoStatus.BLOCKED:
            return

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
        print(f"[satrap] step worktree: {step_wt.branch} -> {step_wt.path}", file=sys.stderr)

        self._ensure_planned(todo=todo, step_number=step_number)
        todo = self._reload_todo()
        step = todo.get_item(step_number)

        if not step.children:
            # Atomic: implement directly on this step branch (and retry tiers on failure).
            self._implement_atomic(todo=todo, step=step, step_wt=step_wt, parent_branch=parent_branch, parent_wt=parent_wt)
            return

        # Non-atomic: recurse into children in dependency order, merging each child into this step branch.
        for batch in dependency_batches(step.children, is_done=lambda n: self._reload_todo().is_done(n)):
            for child in batch:
                self._run_step(todo=todo, step_number=child.number, parent_branch=step_branch, parent_wt=step_wt)
                todo = self._reload_todo()

        # Placeholder: verify the aggregate step work, then merge into parent.
        # For now, treat completion as "all children done".
        self._merge_step_into_parent(todo=todo, step=step, step_branch=step_branch, parent_branch=parent_branch, parent_wt=parent_wt)

    def _ensure_planned(self, *, todo: TodoDoc, step_number: str | None) -> None:
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
    ) -> None:
        base_commit = self.cfg.git.merge_base(
            branch=step_wt.branch,
            other_ref=parent_branch,
            cwd=step_wt.path,
        )

        for tier in self.cfg.model_tiers:
            print(f"[satrap] worker tier: {' '.join(tier)} step={step.number}", file=sys.stderr)
            worker_prompt = write_agent_prompt(
                cfg=self.cfg,
                todo=todo,
                step_number=step.number,
                role=RenderRole.WORKER,
            )

            run = self.cfg.worker_backend.spawn(tier=tier, prompt_file=worker_prompt, cwd=step_wt.path)
            outcome = self.cfg.worker_backend.watch(run)
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
            verdict: VerificationResult = self.cfg.verifier_backend.verify(prompt_file=verifier_prompt, diff=diff, commits=commits, step=step)
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
                        "todo.json already exists for a different task and is not complete. "
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
