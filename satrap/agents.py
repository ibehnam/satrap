from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .claude_cli import run_claude_json_from_files
from .tmux import ensure_window, in_tmux, shell_argv, spawn_pane_remain_on_exit, wait_for
from .todo import TodoItem, TodoItemSpec

WorkerTier = list[str]


@dataclass(frozen=True)
class PlannerResult:
    title: str | None
    items: list[TodoItemSpec]


@dataclass(frozen=True)
class VerificationResult:
    passed: bool
    note: str | None = None


class PlannerBackend(Protocol):
    def plan(self, *, prompt_file: Path, schema_file: Path, step_number: str | None) -> PlannerResult: ...


class WorkerBackend(Protocol):
    def spawn(self, *, tier: WorkerTier, prompt_file: Path, cwd: Path) -> "WorkerRun": ...

    def watch(self, run: "WorkerRun") -> "WorkerOutcome": ...


class VerifierBackend(Protocol):
    def verify(self, *, prompt_file: Path, diff: str, commits: list[str], step: TodoItem) -> VerificationResult: ...


class StubPlannerBackend:
    """Deterministic planner for `--dry-run`."""

    def plan(self, *, prompt_file: Path, schema_file: Path, step_number: str | None) -> PlannerResult:
        if step_number is None:
            return PlannerResult(
                title="(stub) Plan",
                items=[
                    TodoItemSpec(
                        number="1",
                        text="(stub) First step",
                        details="(stub) Details for step 1.",
                        done_when=["(stub) Acceptable output exists."],
                    ),
                    TodoItemSpec(
                        number="2",
                        text="(stub) Second step",
                        details="(stub) Details for step 2.",
                        depends_on=["1"],
                        done_when=["(stub) Acceptable output exists."],
                    ),
                ]
            )
        return PlannerResult(
            title=None,
            items=[
                TodoItemSpec(
                    number=step_number,
                    text=f"(stub) Implement step {step_number}",
                    details=f"(stub) This is an atomic step: {step_number}",
                    done_when=["(stub) The step's acceptance criteria are met."],
                )
            ]
        )


class StubWorkerBackend:
    """No-op worker for `--dry-run`."""

    def spawn(self, *, tier: WorkerTier, prompt_file: Path, cwd: Path) -> "WorkerRun":
        return WorkerRun(tier=tier, prompt_file=prompt_file, cwd=cwd)

    def watch(self, run: "WorkerRun") -> "WorkerOutcome":
        return WorkerOutcome(exit_code=0, note="(stub) no-op worker")


class StubVerifierBackend:
    """Always-pass verifier for `--dry-run`."""

    def verify(self, *, prompt_file: Path, diff: str, commits: list[str], step: TodoItem) -> VerificationResult:
        return VerificationResult(passed=True, note=None)


class ExternalPlannerBackend:
    def __init__(self, *, cmd: str | None) -> None:
        self.cmd = cmd or "claude"
        self.model = "ccss-sonnet"

    def plan(self, *, prompt_file: Path, schema_file: Path, step_number: str | None) -> PlannerResult:
        res = run_claude_json_from_files(
            executable=self.cmd,
            model=self.model,
            prompt_file=prompt_file,
            schema_file=schema_file,
            cwd=schema_file.parent,
        )
        if res.exit_code != 0:
            raise RuntimeError(f"Planner command failed with exit code {res.exit_code}: {res.stderr}")
        if not isinstance(res.data, dict):
            raise ValueError(f"Planner returned non-object JSON: {type(res.data)}")

        title = res.data.get("title")
        if title is not None and not isinstance(title, str):
            title = None

        items_raw = res.data.get("items")
        if not isinstance(items_raw, list):
            raise ValueError("Planner JSON missing required field: items[]")
        if not items_raw:
            raise ValueError("Planner JSON field 'items' must contain at least 1 item.")

        items: list[TodoItemSpec] = []
        for idx, it in enumerate(items_raw):
            if not isinstance(it, dict):
                raise ValueError(f"Planner item {idx} is not an object")
            items.append(_parse_todo_item_spec(it))

        return PlannerResult(title=(title.strip() if isinstance(title, str) else None), items=items)


class ExternalWorkerBackend:
    """Worker backend (currently Claude Code).

    This is intentionally kept behind the WorkerBackend protocol so we can swap in other
    CLIs later (e.g. Codex) without changing orchestration code.
    """

    def __init__(
        self,
        *,
        cmd: str | None = None,
        control_root: Path,
        use_tmux_panes: bool = True,
        tmux_window_name: str = "satrap",
    ) -> None:
        self.cmd = cmd or "claude"
        self.control_root = control_root
        self.use_tmux_panes = use_tmux_panes
        self.tmux_window_name = tmux_window_name

    def spawn(self, *, tier: WorkerTier, prompt_file: Path, cwd: Path) -> "WorkerRun":
        import subprocess
        import uuid
        import shlex

        model = tier[0] if tier else "ccss-sonnet"
        prompt = prompt_file.read_text(encoding="utf-8")
        argv = [self.cmd, "--model", model, "-p", prompt, "--dangerously-skip-permissions"]
        if in_tmux() and self.use_tmux_panes:
            runs_dir = (self.control_root / ".satrap" / "runs").resolve()
            runs_dir.mkdir(parents=True, exist_ok=True)
            run_id = uuid.uuid4().hex
            exit_file = runs_dir / f"worker-{run_id}.exit"
            wait_key = f"satrap-worker-{run_id}"

            # Run the worker in its own pane, inside the worktree dir, and signal completion.
            # We write the exit code to a file so the parent process can read it reliably.
            step_key = prompt_file.name
            # "<step>-worker.md" where step uses "-" instead of "." (e.g. "1-2-worker.md").
            if step_key.endswith("-worker.md"):
                step_key = step_key[: -len("-worker.md")]
            step_label = step_key.replace("-", ".")

            script = "\n".join(
                [
                    f"exit_file={shlex.quote(str(exit_file))}",
                    f"wait_key={shlex.quote(wait_key)}",
                    # Ensure the parent is always unblocked, even if the worker crashes early.
                    "trap 'code=$?; echo $code > \"$exit_file\"; tmux wait-for -S \"$wait_key\"' EXIT",
                    " ".join(shlex.quote(a) for a in argv),
                ]
            )
            window_target = ensure_window(window_name=self.tmux_window_name, cwd=self.control_root)
            pane_id = spawn_pane_remain_on_exit(
                window_target=window_target,
                argv=shell_argv(script=script),
                cwd=cwd,
                title=f"{step_label} {model}",
                env={"SATRAP_CONTROL_ROOT": str(self.control_root)},
                select=True,
            )
            return WorkerRun(
                tier=tier,
                prompt_file=prompt_file,
                cwd=cwd,
                opaque={"kind": "tmux", "pane_id": pane_id, "wait_key": wait_key, "exit_file": str(exit_file)},
            )

        p = subprocess.Popen(argv, cwd=str(cwd), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=1)
        return WorkerRun(tier=tier, prompt_file=prompt_file, cwd=cwd, opaque={"kind": "proc", "p": p})

    def watch(self, run: "WorkerRun") -> "WorkerOutcome":
        import selectors
        import sys
        from pathlib import Path as _Path

        opaque = run.opaque
        if not isinstance(opaque, dict):
            raise RuntimeError("WorkerRun.opaque is missing/invalid.")

        kind = opaque.get("kind")
        if kind == "tmux":
            wait_key = opaque.get("wait_key")
            exit_file = opaque.get("exit_file")
            if not isinstance(wait_key, str) or not wait_key:
                raise RuntimeError("WorkerRun.opaque.wait_key is missing for tmux worker.")
            if not isinstance(exit_file, str) or not exit_file:
                raise RuntimeError("WorkerRun.opaque.exit_file is missing for tmux worker.")

            wait_for(key=wait_key)
            try:
                code_s = _Path(exit_file).read_text(encoding="utf-8").strip()
                code = int(code_s)
            except Exception:
                code = 1
            return WorkerOutcome(exit_code=code, note=None)

        if kind != "proc":
            raise RuntimeError(f"Unknown worker run kind: {kind!r}")

        p = opaque.get("p")
        if p is None:
            raise RuntimeError("WorkerRun.opaque is missing (expected subprocess handle).")

        # Avoid importing subprocess at module import time to keep agent protocols lightweight.
        stdout = getattr(p, "stdout", None)
        stderr = getattr(p, "stderr", None)
        if stdout is None or stderr is None:
            raise RuntimeError("Worker process is missing stdout/stderr pipes.")

        sel = selectors.DefaultSelector()
        sel.register(stdout, selectors.EVENT_READ)
        sel.register(stderr, selectors.EVENT_READ)

        while sel.get_map():
            for key, _ in sel.select(timeout=0.1):
                line = key.fileobj.readline()
                if line == "":
                    sel.unregister(key.fileobj)
                    continue
                if key.fileobj is stdout:
                    sys.stdout.write(line)
                    sys.stdout.flush()
                else:
                    sys.stderr.write(line)
                    sys.stderr.flush()

            if p.poll() is not None and not sel.get_map():
                break

        code = p.wait()
        return WorkerOutcome(exit_code=int(code), note=None)


@dataclass(frozen=True)
class WorkerRun:
    tier: WorkerTier
    prompt_file: Path
    cwd: Path
    # Placeholder: keep backend-specific state here (e.g., subprocess handle, tmux pane id, etc.).
    opaque: object | None = None


@dataclass(frozen=True)
class WorkerOutcome:
    exit_code: int
    note: str | None = None


class ExternalVerifierBackend:
    def __init__(self, *, cmd: str | None, schema_file: Path) -> None:
        self.cmd = cmd or "claude"
        self.model = "ccss-sonnet"
        self.schema_file = schema_file

    def verify(self, *, prompt_file: Path, diff: str, commits: list[str], step: TodoItem) -> VerificationResult:
        res = run_claude_json_from_files(
            executable=self.cmd,
            model=self.model,
            prompt_file=prompt_file,
            schema_file=self.schema_file,
            cwd=self.schema_file.parent,
        )
        if res.exit_code != 0:
            raise RuntimeError(f"Verifier command failed with exit code {res.exit_code}: {res.stderr}")
        if not isinstance(res.data, dict):
            raise ValueError(f"Verifier returned non-object JSON: {type(res.data)}")

        passed = res.data.get("passed")
        note = res.data.get("note")
        if not isinstance(passed, bool):
            raise ValueError("Verifier JSON missing required boolean field: passed")
        if note is not None and not isinstance(note, str):
            raise ValueError("Verifier JSON field 'note' must be a string when present")
        if not passed and (note is None or not note.strip()):
            note = "Rejected with no note."

        return VerificationResult(passed=passed, note=(note.strip() if isinstance(note, str) else None))


def _parse_todo_item_spec(it: dict) -> TodoItemSpec:
    number = it.get("number")
    text = it.get("text")
    if not isinstance(number, str) or not number.strip():
        raise ValueError("Planner todo item missing required string: number")
    if not isinstance(text, str) or not text.strip():
        raise ValueError(f"Planner todo item {number} missing required string: text")

    details = it.get("details")
    if details is not None and (not isinstance(details, str) or not details.strip()):
        details = None

    depends_on = it.get("depends_on")
    if depends_on is None:
        deps: list[str] | None = None
    else:
        if not isinstance(depends_on, list) or any(not isinstance(x, str) for x in depends_on):
            raise ValueError(f"Planner todo item {number} has invalid depends_on; expected array of strings")
        deps = [x for x in depends_on if x.strip()]

    done_when = it.get("done_when")
    if done_when is None:
        dw: list[str] | None = None
    else:
        if not isinstance(done_when, list) or any(not isinstance(x, str) for x in done_when):
            raise ValueError(f"Planner todo item {number} has invalid done_when; expected array of strings")
        dw = [x for x in done_when if x.strip()]

    return TodoItemSpec(number=number.strip(), text=text.strip(), details=(details.strip() if details else None), depends_on=deps, done_when=dw)
