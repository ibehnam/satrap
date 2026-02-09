from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Protocol

from .todo import TodoDoc, TodoItem, TodoStatus


class RenderRole(str, Enum):
    PLANNER = "planner"
    WORKER = "worker"
    VERIFIER = "verifier"


STATUS_GLYPH: dict[TodoStatus, str] = {
    TodoStatus.DONE: "\u2713",  # ✓
    TodoStatus.DOING: ">",
    TodoStatus.PENDING: " ",
    TodoStatus.BLOCKED: "\u2717",  # ✗
}


def _step_key(step_number: str | None) -> str:
    if step_number is None:
        return "root"
    return step_number.replace(".", "-")


def _ancestors(step_number: str) -> list[str]:
    parts = step_number.split(".")
    return [".".join(parts[:i]) for i in range(1, len(parts) + 1)]


def render_todo(todo: TodoDoc, *, step_number: str) -> str:
    """Render a path-aware view of the todo tree for a specific step."""
    lines: list[str] = []
    lines.append(f"# {todo.title}\n")

    if todo.context:
        lines.append("## Task Context\n")
        lines.append(todo.context.strip() + "\n")

    path = _ancestors(step_number)
    current_items = todo.items

    for n in path:
        # Render the sibling list at this level.
        for item in current_items:
            glyph = STATUS_GLYPH.get(item.status, " ")
            lines.append(f"[{glyph}] {item.number}. {item.text}")
        lines.append("")

        # Emit details for the path node.
        node = next((i for i in current_items if i.number == n), None)
        if node is None:
            break
        if node.details:
            lines.append("Details: \"\"\"")
            lines.append(node.details.strip())
            lines.append("\"\"\"")
            lines.append("")

        if n == step_number:
            if node.done_when:
                lines.append("Done when:")
                for crit in node.done_when:
                    lines.append(f"- {crit}")
                lines.append("")

        current_items = node.children

    return "\n".join(lines).rstrip() + "\n"


class RenderConfig(Protocol):
    @property
    def renders_dir(self) -> Path: ...

    @property
    def lessons_path(self) -> Path: ...


def _append_instructions(role: RenderRole, *, step_number: str | None) -> str:
    if role == RenderRole.PLANNER:
        target = f"step {step_number}" if step_number else "the overall task"
        return (
            "\n---\n\n"
            "Planner Instructions\n\n"
            f"I am in charge of {target}. I break it down into a series of steps according to the provided JSON schema.\n\n"
            "Output must be a JSON object that validates against the provided JSON schema. In particular, return:\n"
            "- `title`: a short title for this plan\n"
            "- `items`: the immediate todo items for this task/step (one level only; do not pre-fill nested `children`)\n\n"
            "Each item must be an object with:\n"
            "- `number`: hierarchical numbering like `1`, `1.2`, `2.3.1`\n"
            "- `text`: one-line description\n"
            "- `depends_on`: array of prerequisite step numbers (use `[]` when none)\n"
            "- `done_when`: array of acceptance criteria strings (min 1)\n"
            "Optional:\n"
            "- `details`: long-form instructions/context for this step\n\n"
            "Do not include a `status` field; satrap manages status.\n\n"
            "If the task is simple and can be done in one step, produce exactly one todo item in `items`.\n\n"
            "If the task/context is underspecified, make reasonable assumptions and proceed. Do not ask questions.\n\n"
            "Return only valid JSON on stdout. No markdown fences or extra commentary.\n"
        )
    if role == RenderRole.WORKER:
        return (
            "\n---\n\n"
            "Worker Instructions\n\n"
            f"I am in charge of step {step_number}.\n"
        )
    if role == RenderRole.VERIFIER:
        return (
            "\n---\n\n"
            "Verifier Instructions\n\n"
            f"Verify that step {step_number} is completed according to its `done_when` criteria and the provided diffs.\n"
            "Return pass/fail and a concise note when failing.\n"
        )
    raise ValueError(f"Unknown role: {role}")


def write_agent_prompt(*, cfg: RenderConfig, todo: TodoDoc, step_number: str | None, role: RenderRole) -> Path:
    cfg.renders_dir.mkdir(parents=True, exist_ok=True)

    key = _step_key(step_number)
    out = cfg.renders_dir / f"{key}-{role.value}.md"

    if step_number is None:
        body = render_root(todo)
    else:
        body = render_todo(todo, step_number=step_number)

    lessons = _load_satrap_lessons(cfg)

    out.write_text(body + _append_instructions(role, step_number=step_number) + lessons, encoding="utf-8")
    return out


def write_verifier_prompt(
    *,
    cfg: RenderConfig,
    todo: TodoDoc,
    step_number: str,
    diff: str,
    commits: list[str],
) -> Path:
    """Write the verifier prompt including diffs/commits for the current step."""
    cfg.renders_dir.mkdir(parents=True, exist_ok=True)
    key = _step_key(step_number)
    out = cfg.renders_dir / f"{key}-{RenderRole.VERIFIER.value}.md"

    body = render_todo(todo, step_number=step_number)
    changes: list[str] = []
    changes.append("## Git Changes\n")
    changes.append("Commits since branch creation:")
    if commits:
        for c in commits:
            changes.append(f"- {c}")
    else:
        changes.append("- (none)")
    changes.append("")
    changes.append("Diff:")
    changes.append("```diff")
    changes.append(diff.rstrip())
    changes.append("```")
    changes.append("")

    lessons = _load_satrap_lessons(cfg)

    out.write_text(
        body + "\n".join(changes) + _append_instructions(RenderRole.VERIFIER, step_number=step_number) + lessons,
        encoding="utf-8",
    )
    return out


def render_root(todo: TodoDoc) -> str:
    """Render the top-level view (no active step)."""
    lines: list[str] = []
    lines.append(f"# {todo.title}\n")
    if todo.context:
        lines.append("## Task Context\n")
        lines.append(todo.context.strip() + "\n")

    for item in todo.items:
        glyph = STATUS_GLYPH.get(item.status, " ")
        lines.append(f"[{glyph}] {item.number}. {item.text}")
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _load_satrap_lessons(cfg: RenderConfig) -> str:
    if not cfg.lessons_path.exists():
        return ""
    raw = cfg.lessons_path.read_text(encoding="utf-8").strip()
    if not raw:
        return ""
    satrap = _extract_section(raw, header="## Satrap")
    if not satrap:
        satrap = raw
    return "\n---\n\nLessons\n\n" + satrap.strip() + "\n"


def _extract_section(text: str, *, header: str) -> str:
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip() == header:
            start = i
            break
    if start is None:
        return ""
    return "\n".join(lines[start:])
