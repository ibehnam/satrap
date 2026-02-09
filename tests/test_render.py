from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from satrap.render import (
    RenderRole,
    _ancestors,
    _append_instructions,
    _extract_section,
    _load_satrap_lessons,
    render_root,
    render_todo,
    write_verifier_prompt,
)
from satrap.todo import TodoDoc, TodoItem, TodoStatus


@dataclass
class _Cfg:
    renders_dir: Path
    lessons_path: Path


def test_ancestors_builds_full_path_chain() -> None:
    assert _ancestors("1") == ["1"]
    assert _ancestors("1.2.3") == ["1", "1.2", "1.2.3"]


def test_render_todo_path_view_shows_siblings_and_target_done_when() -> None:
    todo = TodoDoc(
        title="Task",
        context="Use path-aware rendering",
        items=[
            TodoItem(
                number="1",
                text="Parent",
                status=TodoStatus.DONE,
                details=" parent details ",
                done_when=["parent done"],
                children=[
                    TodoItem(
                        number="1.1",
                        text="Target child",
                        status=TodoStatus.DOING,
                        details=" child details ",
                        done_when=["child done"],
                    ),
                    TodoItem(number="1.2", text="Sibling child", status=TodoStatus.PENDING),
                ],
            ),
            TodoItem(number="2", text="Other top item", status=TodoStatus.BLOCKED),
        ],
    )

    rendered = render_todo(todo, step_number="1.1")

    assert "# Task" in rendered
    assert "## Task Context" in rendered
    assert "[✓] 1. Parent" in rendered
    assert "[✗] 2. Other top item" in rendered
    assert "[>] 1.1. Target child" in rendered
    assert "[ ] 1.2. Sibling child" in rendered
    assert 'Details: """\nparent details\n"""' in rendered
    assert 'Details: """\nchild details\n"""' in rendered
    assert "Done when:\n- child done" in rendered
    assert "parent done" not in rendered


def test_render_root_uses_space_glyph_for_unknown_status_value() -> None:
    todo = TodoDoc(
        title="Task",
        items=[
            TodoItem(number="1", text="Unknown status", status="mystery"),  # type: ignore[arg-type]
        ],
    )

    rendered = render_root(todo)
    assert "[ ] 1. Unknown status" in rendered


def test_append_instructions_include_role_specific_constraints() -> None:
    planner_root = _append_instructions(RenderRole.PLANNER, step_number=None)
    planner_step = _append_instructions(RenderRole.PLANNER, step_number="2.1")
    worker = _append_instructions(RenderRole.WORKER, step_number="2.1")
    verifier = _append_instructions(RenderRole.VERIFIER, step_number="2.1")

    assert "Planner Instructions" in planner_root
    assert "overall task" in planner_root
    assert "Return only valid JSON on stdout." in planner_root
    assert "step 2.1" in planner_step
    assert "Worker Instructions" in worker
    assert "I am in charge of step 2.1." in worker
    assert "Verifier Instructions" in verifier
    assert "Verify that step 2.1 is completed" in verifier


def test_extract_section_and_lessons_loading_behavior(tmp_path: Path) -> None:
    raw = "## Intro\nhello\n\n## Satrap\nline1\n## Tail\nline2\n"
    assert _extract_section(raw, header="## Satrap") == "## Satrap\nline1\n## Tail\nline2"
    assert _extract_section(raw, header="## Missing") == ""

    lessons = tmp_path / "lessons.md"
    lessons.write_text(raw, encoding="utf-8")
    cfg = _Cfg(renders_dir=tmp_path / "renders", lessons_path=lessons)

    loaded = _load_satrap_lessons(cfg)
    assert "Lessons" in loaded
    assert "## Satrap\nline1\n## Tail\nline2" in loaded
    assert "## Intro" not in loaded


def test_load_lessons_falls_back_to_full_text_when_satrap_header_missing(tmp_path: Path) -> None:
    lessons = tmp_path / "lessons.md"
    lessons.write_text("General note\nKeep it short.\n", encoding="utf-8")
    cfg = _Cfg(renders_dir=tmp_path / "renders", lessons_path=lessons)

    loaded = _load_satrap_lessons(cfg)
    assert loaded.endswith("General note\nKeep it short.\n")


def test_write_verifier_prompt_formats_empty_commits_and_trims_diff(tmp_path: Path) -> None:
    cfg = _Cfg(renders_dir=tmp_path / "renders", lessons_path=tmp_path / "missing-lessons.md")
    todo = TodoDoc(
        title="Task",
        items=[TodoItem(number="1.2", text="Verify me", done_when=["criterion"])],
    )

    out = write_verifier_prompt(
        cfg=cfg,
        todo=todo,
        step_number="1.2",
        diff="diff --git a/a.txt b/a.txt\n+new line\n\n   \n",
        commits=[],
    )

    content = out.read_text(encoding="utf-8")
    assert out.name == "1-2-verifier.md"
    assert "Commits since branch creation:\n- (none)" in content
    assert "```diff\ndiff --git a/a.txt b/a.txt\n+new line\n```" in content
    assert "Verifier Instructions" in content
