from __future__ import annotations

from pathlib import Path

import pytest

import satrap.orchestrator as orch_mod
from satrap.agents import PlannerResult, VerificationResult, WorkerOutcome, WorkerRun
from satrap.git_ops import GitWorktree
from satrap.orchestrator import SatrapConfig, SatrapOrchestrator, _append_under_section
from satrap.todo import TodoDoc, TodoItem, TodoItemSpec, TodoStatus


class FakePlannerBackend:
    def __init__(self, plans: dict[str | None, PlannerResult]) -> None:
        self.plans = plans
        self.calls: list[dict[str, object]] = []

    def plan(self, *, prompt_file: Path, schema_file: Path, step_number: str | None) -> PlannerResult:
        self.calls.append({"prompt_file": prompt_file, "schema_file": schema_file, "step_number": step_number})
        if step_number not in self.plans:
            raise AssertionError(f"Unexpected planner call for step {step_number!r}")
        return self.plans[step_number]


class FakeWorkerBackend:
    def __init__(self, outcomes: list[WorkerOutcome]) -> None:
        self._outcomes = list(outcomes)
        self.spawn_calls: list[dict[str, object]] = []
        self.watch_calls: list[WorkerRun] = []

    def spawn(self, *, tier: list[str], prompt_file: Path, cwd: Path) -> WorkerRun:
        run = WorkerRun(tier=tier, prompt_file=prompt_file, cwd=cwd)
        self.spawn_calls.append({"tier": list(tier), "prompt_file": prompt_file, "cwd": cwd})
        return run

    def watch(self, run: WorkerRun) -> WorkerOutcome:
        self.watch_calls.append(run)
        if not self._outcomes:
            raise AssertionError("No worker outcomes left")
        return self._outcomes.pop(0)


class FakeVerifierBackend:
    def __init__(self, verdicts: list[VerificationResult]) -> None:
        self._verdicts = list(verdicts)
        self.calls: list[dict[str, object]] = []

    def verify(self, *, prompt_file: Path, diff: str, commits: list[str], step: TodoItem) -> VerificationResult:
        self.calls.append({"prompt_file": prompt_file, "diff": diff, "commits": list(commits), "step": step.number})
        if not self._verdicts:
            raise AssertionError("No verifier verdicts left")
        return self._verdicts.pop(0)


class FakeGit:
    def __init__(
        self,
        *,
        control_root: Path,
        current_branch_name: str = "feature/test",
        merge_base_value: str = "BASE",
        diff_value: str = "DIFF",
        commits_value: list[str] | None = None,
    ) -> None:
        self.control_root = control_root
        self.current_branch_name = current_branch_name
        self.merge_base_value = merge_base_value
        self.diff_value = diff_value
        self.commits_value = commits_value or ["c1"]

        self.current_branch_calls: list[Path] = []
        self.ensure_worktree_calls: list[dict[str, object]] = []
        self.merge_base_calls: list[dict[str, object]] = []
        self.diff_calls: list[dict[str, object]] = []
        self.commits_calls: list[dict[str, object]] = []
        self.commit_calls: list[dict[str, object]] = []
        self.reset_calls: list[dict[str, object]] = []
        self.merge_into_calls: list[dict[str, object]] = []

    def current_branch(self, *, cwd: Path) -> str:
        self.current_branch_calls.append(cwd)
        return self.current_branch_name

    def ensure_worktree(self, *, branch: str, base_ref: str, worktrees_dir: Path, phrases_path: Path) -> GitWorktree:
        wt_path = (worktrees_dir / branch.replace("/", "__")).resolve()
        wt_path.mkdir(parents=True, exist_ok=True)
        self.ensure_worktree_calls.append(
            {
                "branch": branch,
                "base_ref": base_ref,
                "worktrees_dir": worktrees_dir,
                "phrases_path": phrases_path,
                "path": wt_path,
            }
        )
        return GitWorktree(branch=branch, path=wt_path)

    def merge_base(self, *, branch: str, other_ref: str, cwd: Path) -> str:
        self.merge_base_calls.append({"branch": branch, "other_ref": other_ref, "cwd": cwd})
        return self.merge_base_value

    def diff_since(self, base_commit: str, *, cwd: Path) -> str:
        self.diff_calls.append({"base_commit": base_commit, "cwd": cwd})
        return self.diff_value

    def commits_since(self, base_commit: str, *, cwd: Path) -> list[str]:
        self.commits_calls.append({"base_commit": base_commit, "cwd": cwd})
        return list(self.commits_value)

    def commit_all_if_needed(self, *, cwd: Path, message: str) -> None:
        self.commit_calls.append({"cwd": cwd, "message": message})

    def reset_hard(self, ref: str, *, cwd: Path) -> None:
        self.reset_calls.append({"ref": ref, "cwd": cwd})

    def merge_into(self, *, source_branch: str, target_branch: str, cwd: Path) -> None:
        self.merge_into_calls.append({"source_branch": source_branch, "target_branch": target_branch, "cwd": cwd})


def _make_cfg(
    tmp_path: Path,
    *,
    planner: FakePlannerBackend | None = None,
    worker: FakeWorkerBackend | None = None,
    verifier: FakeVerifierBackend | None = None,
    git: FakeGit | None = None,
    model_tiers: list[list[str]] | None = None,
) -> SatrapConfig:
    todo_schema_path = tmp_path / "todo-schema.json"
    todo_schema_path.write_text("{}\n", encoding="utf-8")

    return SatrapConfig(
        control_root=tmp_path,
        todo_json_path=tmp_path / ".satrap" / "todo.json",
        todo_schema_path=todo_schema_path,
        model_tiers=model_tiers or [["tier-1"], ["tier-2"]],
        max_parallel=1,
        planner_backend=planner or FakePlannerBackend({}),
        worker_backend=worker or FakeWorkerBackend([WorkerOutcome(exit_code=0)]),
        verifier_backend=verifier or FakeVerifierBackend([VerificationResult(passed=True)]),
        git=git or FakeGit(control_root=tmp_path),
    )


def _save(cfg: SatrapConfig, todo: TodoDoc) -> None:
    todo.save(cfg.todo_json_path)


def test_run_root_flow_executes_batches_and_root_branch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    git = FakeGit(control_root=tmp_path, current_branch_name="feature/main")
    cfg = _make_cfg(tmp_path, git=git)
    todo = TodoDoc(
        title="run",
        context="ctx",
        items=[
            TodoItem(number="1", text="first", done_when=["a"]),
            TodoItem(number="2", text="second", depends_on=["1"], done_when=["b"]),
        ],
    )
    _save(cfg, todo)

    orch = SatrapOrchestrator(cfg)
    monkeypatch.setattr(orch, "_load_or_init_todo", lambda **_: TodoDoc.load(cfg.todo_json_path))

    ensured: list[str | None] = []
    run_calls: list[tuple[str, str, str]] = []

    def fake_ensure_planned(*, todo: TodoDoc, step_number: str | None) -> None:
        ensured.append(step_number)

    def fake_run_step(*, todo: TodoDoc, step_number: str, parent_branch: str, parent_wt: GitWorktree) -> None:
        run_calls.append((step_number, parent_branch, parent_wt.branch))
        todo.set_status(step_number, TodoStatus.DONE)
        _save(cfg, todo)

    monkeypatch.setattr(orch, "_ensure_planned", fake_ensure_planned)
    monkeypatch.setattr(orch, "_run_step", fake_run_step)

    orch.run(task_text="ctx", start_step=None)

    assert ensured == [None]
    assert [c[0] for c in run_calls] == ["1", "2"]
    assert all(item.status == TodoStatus.DONE for item in TodoDoc.load(cfg.todo_json_path).items)
    assert git.ensure_worktree_calls[0]["branch"] == "satrap/root"
    assert git.ensure_worktree_calls[0]["base_ref"] == "feature/main"


def test_run_start_step_uses_nested_parent_branch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    git = FakeGit(control_root=tmp_path)
    cfg = _make_cfg(tmp_path, git=git)
    todo = TodoDoc(
        title="run",
        context="ctx",
        items=[
            TodoItem(
                number="2",
                text="parent",
                done_when=["done"],
                children=[
                    TodoItem(
                        number="2.3",
                        text="mid",
                        done_when=["done"],
                        children=[TodoItem(number="2.3.1", text="leaf", done_when=["done"])],
                    )
                ],
            )
        ],
    )
    _save(cfg, todo)

    orch = SatrapOrchestrator(cfg)
    monkeypatch.setattr(orch, "_load_or_init_todo", lambda **_: TodoDoc.load(cfg.todo_json_path))

    run_calls: list[tuple[str, str, str]] = []

    def fake_run_step(*, todo: TodoDoc, step_number: str, parent_branch: str, parent_wt: GitWorktree) -> None:
        run_calls.append((step_number, parent_branch, parent_wt.branch))

    monkeypatch.setattr(orch, "_run_step", fake_run_step)

    orch.run(task_text="ctx", start_step="2.3.1")

    assert len(git.ensure_worktree_calls) == 2
    assert git.ensure_worktree_calls[0]["branch"] == "satrap/root"
    assert git.ensure_worktree_calls[1]["branch"] == "satrap/2.3"
    assert git.ensure_worktree_calls[1]["base_ref"] == "satrap/root"
    assert run_calls == [("2.3.1", "satrap/2.3", "satrap/2.3")]


def test_ensure_planned_root_noop_when_items_exist(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    planner = FakePlannerBackend({})
    cfg = _make_cfg(tmp_path, planner=planner)
    todo = TodoDoc(title="existing", context="ctx", items=[TodoItem(number="1", text="x", done_when=["d"])])

    orch = SatrapOrchestrator(cfg)
    monkeypatch.setattr(orch_mod, "write_agent_prompt", lambda **_: tmp_path / "planner.md")
    orch._ensure_planned(todo=todo, step_number=None)

    assert planner.calls == []


def test_ensure_planned_root_updates_title_and_items(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    plan = PlannerResult(
        title="New root title",
        items=[
            TodoItemSpec(number="1", text="first", done_when=["a"]),
            TodoItemSpec(number="2", text="second", depends_on=["1"], done_when=["b"]),
        ],
    )
    planner = FakePlannerBackend({None: plan})
    cfg = _make_cfg(tmp_path, planner=planner)
    todo = TodoDoc(title="old", context="ctx", items=[])

    orch = SatrapOrchestrator(cfg)
    monkeypatch.setattr(orch_mod, "write_agent_prompt", lambda **_: tmp_path / "planner-root.md")

    orch._ensure_planned(todo=todo, step_number=None)

    loaded = TodoDoc.load(cfg.todo_json_path)
    assert todo.title == "New root title"
    assert [item.number for item in todo.items] == ["1", "2"]
    assert loaded.title == "New root title"
    assert [item.number for item in loaded.items] == ["1", "2"]


def test_ensure_planned_specific_noop_when_children_exist(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    planner = FakePlannerBackend({})
    cfg = _make_cfg(tmp_path, planner=planner)
    todo = TodoDoc(
        title="x",
        context="ctx",
        items=[
            TodoItem(
                number="1",
                text="parent",
                done_when=["done"],
                children=[TodoItem(number="1.1", text="child", done_when=["done"])],
            )
        ],
    )

    orch = SatrapOrchestrator(cfg)
    monkeypatch.setattr(orch_mod, "write_agent_prompt", lambda **_: tmp_path / "planner-step.md")

    orch._ensure_planned(todo=todo, step_number="1")

    assert planner.calls == []


def test_ensure_planned_specific_atomic_refinement_replaces_children(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    planner = FakePlannerBackend(
        {
            "1": PlannerResult(
                title=None,
                items=[
                    TodoItemSpec(number="1", text="refined", details="new details", depends_on=["9"], done_when=["ship"])
                ],
            )
        }
    )
    cfg = _make_cfg(tmp_path, planner=planner)
    todo = TodoDoc(
        title="x",
        context="ctx",
        items=[
            TodoItem(
                number="1",
                text="old",
                details="old details",
                status=TodoStatus.DOING,
                depends_on=["0"],
                done_when=["old"],
                children=[],
            )
        ],
    )

    orch = SatrapOrchestrator(cfg)
    monkeypatch.setattr(orch_mod, "write_agent_prompt", lambda **_: tmp_path / "planner-step-atomic.md")

    orch._ensure_planned(todo=todo, step_number="1")

    item = todo.get_item("1")
    assert item.text == "refined"
    assert item.details == "new details"
    assert item.depends_on == ["9"]
    assert item.done_when == ["ship"]
    assert item.status == TodoStatus.DOING
    assert item.children == []


def test_ensure_planned_specific_child_upsert_adds_children_from_multi_item_plan(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    planner = FakePlannerBackend(
        {
            "1": PlannerResult(
                title=None,
                items=[
                    TodoItemSpec(number="1.1", text="updated child", done_when=["a"]),
                    TodoItemSpec(number="1.2", text="new child", done_when=["b"]),
                ],
            )
        }
    )
    cfg = _make_cfg(tmp_path, planner=planner)
    todo = TodoDoc(
        title="x",
        context="ctx",
        items=[
            TodoItem(
                number="1",
                text="parent",
                done_when=["d"],
                children=[],
            )
        ],
    )

    orch = SatrapOrchestrator(cfg)
    monkeypatch.setattr(orch_mod, "write_agent_prompt", lambda **_: tmp_path / "planner-step-children.md")

    orch._ensure_planned(todo=todo, step_number="1")

    parent = todo.get_item("1")
    assert [child.number for child in parent.children] == ["1.1", "1.2"]
    assert parent.children[0].text == "updated child"
    assert parent.children[0].status == TodoStatus.PENDING
    assert parent.children[1].status == TodoStatus.PENDING


def test_implement_atomic_retries_on_worker_exit_and_verifier_reject_then_merges(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    git = FakeGit(control_root=tmp_path, merge_base_value="abc123", diff_value="diff body", commits_value=["c1", "c2"])
    worker = FakeWorkerBackend(
        [
            WorkerOutcome(exit_code=17),
            WorkerOutcome(exit_code=0),
            WorkerOutcome(exit_code=0),
        ]
    )
    verifier = FakeVerifierBackend([VerificationResult(passed=False, note="needs fix"), VerificationResult(passed=True)])
    cfg = _make_cfg(tmp_path, git=git, worker=worker, verifier=verifier, model_tiers=[["t1"], ["t2"], ["t3"]])
    todo = TodoDoc(title="t", context="ctx", items=[TodoItem(number="1", text="step text", done_when=["done"])])

    orch = SatrapOrchestrator(cfg)
    monkeypatch.setattr(orch_mod, "write_agent_prompt", lambda **_: tmp_path / "worker.md")
    monkeypatch.setattr(orch_mod, "write_verifier_prompt", lambda **_: tmp_path / "verifier.md")

    step = todo.get_item("1")
    step_wt = GitWorktree(branch="satrap/1", path=tmp_path / "wt-step")
    parent_wt = GitWorktree(branch="satrap/root", path=tmp_path / "wt-root")
    step_wt.path.mkdir(parents=True, exist_ok=True)
    parent_wt.path.mkdir(parents=True, exist_ok=True)

    orch._implement_atomic(todo=todo, step=step, step_wt=step_wt, parent_branch="satrap/root", parent_wt=parent_wt)

    assert [call["tier"] for call in worker.spawn_calls] == [["t1"], ["t2"], ["t3"]]
    assert len(git.reset_calls) == 2
    assert len(git.commit_calls) == 2
    assert len(verifier.calls) == 2
    assert git.merge_into_calls == [
        {
            "source_branch": "satrap/1",
            "target_branch": "satrap/root",
            "cwd": parent_wt.path,
        }
    ]
    assert todo.get_item("1").status == TodoStatus.DONE

    lessons = cfg.lessons_path.read_text(encoding="utf-8")
    assert "Worker CLI exited non-zero." in lessons
    assert "needs fix" in lessons


def test_implement_atomic_marks_blocked_after_all_tiers_fail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    git = FakeGit(control_root=tmp_path, merge_base_value="base123")
    worker = FakeWorkerBackend([WorkerOutcome(exit_code=5), WorkerOutcome(exit_code=0)])
    verifier = FakeVerifierBackend([VerificationResult(passed=False, note="reject note")])
    cfg = _make_cfg(tmp_path, git=git, worker=worker, verifier=verifier, model_tiers=[["t1"], ["t2"]])
    todo = TodoDoc(title="t", context="ctx", items=[TodoItem(number="1", text="step", done_when=["done"])])

    orch = SatrapOrchestrator(cfg)
    monkeypatch.setattr(orch_mod, "write_agent_prompt", lambda **_: tmp_path / "worker-blocked.md")
    monkeypatch.setattr(orch_mod, "write_verifier_prompt", lambda **_: tmp_path / "verifier-blocked.md")

    step = todo.get_item("1")
    step_wt = GitWorktree(branch="satrap/1", path=tmp_path / "wt-step")
    parent_wt = GitWorktree(branch="satrap/root", path=tmp_path / "wt-root")
    step_wt.path.mkdir(parents=True, exist_ok=True)
    parent_wt.path.mkdir(parents=True, exist_ok=True)

    orch._implement_atomic(todo=todo, step=step, step_wt=step_wt, parent_branch="satrap/root", parent_wt=parent_wt)

    assert todo.get_item("1").status == TodoStatus.BLOCKED
    assert "All worker tiers failed verification" in (todo.get_item("1").blocked_reason or "")
    assert len(git.reset_calls) == 2
    assert git.merge_into_calls == []

    loaded = TodoDoc.load(cfg.todo_json_path)
    assert loaded.get_item("1").status == TodoStatus.BLOCKED
    assert "All worker tiers failed verification" in (loaded.get_item("1").blocked_reason or "")

    lessons = cfg.lessons_path.read_text(encoding="utf-8")
    assert "Worker CLI exited non-zero." in lessons
    assert "reject note" in lessons


def test_merge_step_into_parent_marks_step_done_and_saves(tmp_path: Path) -> None:
    git = FakeGit(control_root=tmp_path)
    cfg = _make_cfg(tmp_path, git=git)
    todo = TodoDoc(title="t", context="ctx", items=[TodoItem(number="1", text="step", done_when=["done"])])

    orch = SatrapOrchestrator(cfg)
    parent_wt = GitWorktree(branch="satrap/root", path=tmp_path / "wt-root")
    parent_wt.path.mkdir(parents=True, exist_ok=True)

    orch._merge_step_into_parent(
        todo=todo,
        step=todo.get_item("1"),
        step_branch="satrap/1",
        parent_branch="satrap/root",
        parent_wt=parent_wt,
    )

    assert todo.get_item("1").status == TodoStatus.DONE
    assert git.merge_into_calls == [
        {
            "source_branch": "satrap/1",
            "target_branch": "satrap/root",
            "cwd": parent_wt.path,
        }
    ]
    assert TodoDoc.load(cfg.todo_json_path).get_item("1").status == TodoStatus.DONE


def test_load_or_init_todo_creates_new_doc_when_missing(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path)
    orch = SatrapOrchestrator(cfg)

    todo = orch._load_or_init_todo(task_text="First line\nsecond", reset_todo=False)

    assert todo.title == "First line"
    assert todo.context == "First line\nsecond"
    assert cfg.todo_json_path.exists()


def test_load_or_init_todo_rejects_mismatched_incomplete_plan(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path)
    existing = TodoDoc(
        title="old",
        context="old task",
        items=[TodoItem(number="1", text="pending", done_when=["d"], status=TodoStatus.PENDING)],
    )
    _save(cfg, existing)
    orch = SatrapOrchestrator(cfg)

    with pytest.raises(RuntimeError, match="todo file already exists for a different task"):
        orch._load_or_init_todo(task_text="new task", reset_todo=False)


def test_load_or_init_todo_forced_reset_archives_previous(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _make_cfg(tmp_path)
    existing = TodoDoc(title="old", context="old", items=[TodoItem(number="1", text="x", done_when=["d"])])
    _save(cfg, existing)

    class FrozenDateTime:
        @classmethod
        def now(cls) -> object:
            class _Now:
                def strftime(self, fmt: str) -> str:
                    assert fmt == "%Y%m%d-%H%M%S"
                    return "20260209-130000"

            return _Now()

    monkeypatch.setattr(orch_mod, "datetime", FrozenDateTime)

    orch = SatrapOrchestrator(cfg)
    todo = orch._load_or_init_todo(task_text="new forced task", reset_todo=True)

    assert todo.context == "new forced task"
    assert todo.items == []

    archive = tmp_path / ".satrap" / "todo-history" / "todo-20260209-130000.json"
    assert archive.exists()
    assert "\"context\": \"old\"" in archive.read_text(encoding="utf-8")


def test_load_or_init_todo_replaces_complete_mismatched_plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _make_cfg(tmp_path)
    existing = TodoDoc(
        title="old",
        context="old",
        items=[TodoItem(number="1", text="done", done_when=["d"], status=TodoStatus.DONE)],
    )
    _save(cfg, existing)

    class FrozenDateTime:
        @classmethod
        def now(cls) -> object:
            class _Now:
                def strftime(self, fmt: str) -> str:
                    return "20260209-130100"

            return _Now()

    monkeypatch.setattr(orch_mod, "datetime", FrozenDateTime)

    orch = SatrapOrchestrator(cfg)
    todo = orch._load_or_init_todo(task_text="brand new task", reset_todo=False)

    assert todo.context == "brand new task"
    assert todo.items == []
    archive = tmp_path / ".satrap" / "todo-history" / "todo-20260209-130100.json"
    assert archive.exists()


def test_append_under_section_bootstraps_empty_text() -> None:
    out = _append_under_section("", header="## Satrap", content="\n### 1 (tier)\n\nnote\n")

    assert "# Lessons" in out
    assert "## Satrap" in out
    assert "### 1 (tier)" in out
    assert "## Satrap\n- (empty)" not in out


def test_append_under_section_replaces_placeholder_under_existing_section() -> None:
    text = "# Lessons\n\n## Satrap\n- (empty)\n"
    out = _append_under_section(text, header="## Satrap", content="\nentry\n")

    assert "## Satrap\nentry\n" in out
    assert "## Satrap\n- (empty)" not in out


def test_append_under_section_inserts_missing_header() -> None:
    text = "# Lessons\n\n## Codex\n- (empty)\n"
    out = _append_under_section(text, header="## Satrap", content="\nnew entry\n")

    assert "## Satrap" in out
    assert "new entry" in out
    assert "## Satrap\n- (empty)" not in out


def test_append_under_section_existing_content() -> None:
    text = "# Lessons\n\n## Satrap\n\n### 1 (tier)\n\nexisting note\n"
    out = _append_under_section(text, header="## Satrap", content="\n### 2 (tier)\n\nnew note\n")
    assert "existing note" in out
    assert "new note" in out


def test_append_under_section_multiple_sections() -> None:
    text = "# Lessons\n\n## Codex\n\ncodex stuff\n\n## Satrap\n- (empty)\n"
    out = _append_under_section(text, header="## Satrap", content="\nnew entry\n")
    assert "codex stuff" in out
    assert "new entry" in out


def test_load_or_init_todo_same_context_returns_existing(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path)
    existing = TodoDoc(
        title="My Task",
        context="task",
        items=[TodoItem(number="1", text="step", done_when=["d"], status=TodoStatus.PENDING)],
    )
    _save(cfg, existing)
    orch = SatrapOrchestrator(cfg)
    todo = orch._load_or_init_todo(task_text="task", reset_todo=False)
    assert len(todo.items) == 1
    assert todo.title == "My Task"


def test_load_or_init_todo_empty_task_text_fallback(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path)
    orch = SatrapOrchestrator(cfg)
    todo = orch._load_or_init_todo(task_text="", reset_todo=False)
    assert todo.title == "satrap task"
    assert todo.items == []


def test_run_step_skips_done(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    git = FakeGit(control_root=tmp_path)
    cfg = _make_cfg(tmp_path, git=git)
    todo = TodoDoc(
        title="t",
        context="ctx",
        items=[TodoItem(number="1", text="done step", done_when=["d"], status=TodoStatus.DONE)],
    )
    _save(cfg, todo)
    orch = SatrapOrchestrator(cfg)
    parent_wt = GitWorktree(branch="satrap/root", path=tmp_path)

    # Should return early, no worktree or planner calls
    orch._run_step(todo=todo, step_number="1", parent_branch="satrap/root", parent_wt=parent_wt)
    assert git.ensure_worktree_calls == []


def test_run_step_skips_blocked(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    git = FakeGit(control_root=tmp_path)
    cfg = _make_cfg(tmp_path, git=git)
    todo = TodoDoc(
        title="t",
        context="ctx",
        items=[TodoItem(number="1", text="blocked step", done_when=["d"], status=TodoStatus.BLOCKED)],
    )
    _save(cfg, todo)
    orch = SatrapOrchestrator(cfg)
    parent_wt = GitWorktree(branch="satrap/root", path=tmp_path)

    orch._run_step(todo=todo, step_number="1", parent_branch="satrap/root", parent_wt=parent_wt)
    assert git.ensure_worktree_calls == []


def test_satrap_config_computed_paths(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path)
    assert cfg.lessons_path == tmp_path / "tasks" / "lessons.md"
    assert cfg.phrases_path == tmp_path / "phrases.txt"
    assert cfg.renders_dir == tmp_path / ".satrap" / "renders"
    assert cfg.worktrees_dir == tmp_path / ".worktrees"


def test_load_or_init_todo_archive_failure_best_effort(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _make_cfg(tmp_path)
    existing = TodoDoc(title="old", context="old", items=[TodoItem(number="1", text="x", done_when=["d"])])
    _save(cfg, existing)

    class FrozenDateTime:
        @classmethod
        def now(cls) -> object:
            class _Now:
                def strftime(self, fmt: str) -> str:
                    return "20260209-140000"

            return _Now()

    monkeypatch.setattr(orch_mod, "datetime", FrozenDateTime)

    original_write_text = Path.write_text

    def flaky_write_text(self: Path, data: str, encoding: str = "utf-8") -> int:
        if "todo-history" in self.parts:
            raise OSError("archive write failed")
        return original_write_text(self, data, encoding=encoding)

    monkeypatch.setattr(Path, "write_text", flaky_write_text, raising=False)

    orch = SatrapOrchestrator(cfg)
    todo = orch._load_or_init_todo(task_text="new task", reset_todo=True)

    assert todo.context == "new task"
    assert todo.items == []
    assert TodoDoc.load(cfg.todo_json_path).context == "new task"


def test_implement_atomic_all_tiers_fail_blocks_step(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    git = FakeGit(control_root=tmp_path, merge_base_value="base123")
    worker = FakeWorkerBackend([WorkerOutcome(exit_code=0), WorkerOutcome(exit_code=0)])
    verifier = FakeVerifierBackend([VerificationResult(passed=False, note="r1"), VerificationResult(passed=False, note="r2")])
    cfg = _make_cfg(tmp_path, git=git, worker=worker, verifier=verifier, model_tiers=[["t1"], ["t2"]])
    todo = TodoDoc(title="t", context="ctx", items=[TodoItem(number="1", text="step", done_when=["done"])])

    orch = SatrapOrchestrator(cfg)
    monkeypatch.setattr(orch_mod, "write_agent_prompt", lambda **_: tmp_path / "worker-blocked-2.md")
    monkeypatch.setattr(orch_mod, "write_verifier_prompt", lambda **_: tmp_path / "verifier-blocked-2.md")

    step = todo.get_item("1")
    step_wt = GitWorktree(branch="satrap/1", path=tmp_path / "wt-step-2")
    parent_wt = GitWorktree(branch="satrap/root", path=tmp_path / "wt-root-2")
    step_wt.path.mkdir(parents=True, exist_ok=True)
    parent_wt.path.mkdir(parents=True, exist_ok=True)

    orch._implement_atomic(todo=todo, step=step, step_wt=step_wt, parent_branch="satrap/root", parent_wt=parent_wt)

    assert todo.get_item("1").status == TodoStatus.BLOCKED
    assert len(git.reset_calls) == 2
    assert git.merge_into_calls == []


def test_ensure_planned_single_item_refinement(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    planner = FakePlannerBackend(
        {
            "1": PlannerResult(
                title=None,
                items=[TodoItemSpec(number="1", text="refined", details="d", depends_on=["9"], done_when=["ok"])],
            )
        }
    )
    cfg = _make_cfg(tmp_path, planner=planner)
    todo = TodoDoc(
        title="x",
        context="ctx",
        items=[
            TodoItem(
                number="1",
                text="old",
                status=TodoStatus.PENDING,
                done_when=["old"],
                children=[TodoItem(number="1.1", text="child", done_when=["c"])],
            )
        ],
    )

    orch = SatrapOrchestrator(cfg)
    monkeypatch.setattr(orch_mod, "write_agent_prompt", lambda **_: tmp_path / "planner-single-refinement.md")

    # No-op due to existing children.
    orch._ensure_planned(todo=todo, step_number="1")

    # Clear children and re-run to force single-item refinement behavior.
    todo.get_item("1").children = []
    orch._ensure_planned(todo=todo, step_number="1")

    item = todo.get_item("1")
    assert item.text == "refined"
    assert item.details == "d"
    assert item.depends_on == ["9"]
    assert item.done_when == ["ok"]
    assert item.children == []
