from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .claude_cli import run_claude_json_from_files
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

    def __init__(self, *, cmd: str | None = None) -> None:
        self.cmd = cmd or "claude"

    def spawn(self, *, tier: WorkerTier, prompt_file: Path, cwd: Path) -> "WorkerRun":
        import subprocess

        model = tier[0] if tier else "ccss-sonnet"
        prompt = prompt_file.read_text(encoding="utf-8")
        argv = [self.cmd, "--model", model, "-p", prompt, "--dangerously-skip-permissions"]
        p = subprocess.Popen(
            argv,
            cwd=str(cwd),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=1,
        )
        return WorkerRun(tier=tier, prompt_file=prompt_file, cwd=cwd, opaque=p)

    def watch(self, run: "WorkerRun") -> "WorkerOutcome":
        import selectors
        import sys

        p = run.opaque
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
