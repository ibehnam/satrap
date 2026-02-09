import json

import pytest

from satrap.todo import TodoDoc, TodoItem, TodoItemSpec, TodoStatus


def test_todo_item_from_dict_coerces_fields_and_preserves_unknowns() -> None:
    raw = {
        "number": 1,
        "text": 2,
        "status": "not-a-real-status",
        "depends_on": [3, None],
        "done_when": [True, 7],
        "details": 99,
        "blocked_reason": 0,
        "children": [
            {
                "number": "1.1",
                "text": "child",
                "status": "done",
                "child_extra": "kept",
            }
        ],
        "unknown_field": {"k": "v"},
    }

    item = TodoItem.from_dict(raw)

    assert item.number == "1"
    assert item.text == "2"
    assert item.status == TodoStatus.PENDING
    assert item.depends_on == ["3", "None"]
    assert item.done_when == ["True", "7"]
    assert item.details == "99"
    assert item.blocked_reason == "0"
    assert item.extra == {"unknown_field": {"k": "v"}}
    assert [child.number for child in item.children] == ["1.1"]
    assert item.children[0].extra == {"child_extra": "kept"}


@pytest.mark.parametrize(
    ("status", "blocked_reason", "expect_key"),
    [
        (TodoStatus.PENDING, "still ignored", False),
        (TodoStatus.BLOCKED, "", False),
        (TodoStatus.BLOCKED, "waiting on API", True),
    ],
)
def test_todo_item_to_dict_emits_blocked_reason_only_when_blocked_with_truthy_reason(
    status: TodoStatus, blocked_reason: str, expect_key: bool
) -> None:
    item = TodoItem(number="1", text="x", status=status, blocked_reason=blocked_reason)

    serialized = item.to_dict()

    assert ("blocked_reason" in serialized) is expect_key


def test_tododoc_load_rejects_invalid_top_level_and_missing_title(tmp_path) -> None:
    not_object = tmp_path / "not-object.json"
    not_object.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="must be a JSON object"):
        TodoDoc.load(not_object)

    missing_title = tmp_path / "missing-title.json"
    missing_title.write_text(json.dumps({"items": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="missing required field: title"):
        TodoDoc.load(missing_title)


def test_tododoc_load_and_save_round_trip_preserves_unknowns_and_uses_ascii_json(tmp_path) -> None:
    src = tmp_path / "todo.json"
    src.write_text(
        json.dumps(
            {
                "title": "caf\u00e9",
                "items": [
                    {
                        "number": "1",
                        "text": "step",
                        "status": "doing",
                        "depends_on": [],
                        "done_when": ["ok"],
                        "item_extra": 5,
                    }
                ],
                "top_extra": {"feature": True},
            }
        ),
        encoding="utf-8",
    )

    doc = TodoDoc.load(src)
    out = tmp_path / "saved.json"
    doc.save(out)
    serialized = out.read_text(encoding="utf-8")
    saved = json.loads(serialized)

    assert doc.extra == {"top_extra": {"feature": True}}
    assert doc.items[0].extra == {"item_extra": 5}
    assert serialized.endswith("\n")
    assert "\\u00e9" in serialized
    assert saved["top_extra"] == {"feature": True}
    assert saved["items"][0]["item_extra"] == 5


def test_walk_items_is_preorder_depth_first() -> None:
    tree = [
        TodoItem(
            number="1",
            text="root-1",
            children=[
                TodoItem(
                    number="1.1",
                    text="child-1.1",
                    children=[TodoItem(number="1.1.1", text="leaf")],
                ),
                TodoItem(number="1.2", text="child-1.2"),
            ],
        ),
        TodoItem(number="2", text="root-2"),
    ]

    order = [item.number for item in TodoDoc._walk_items(tree)]
    assert order == ["1", "1.1", "1.1.1", "1.2", "2"]


def test_is_complete_accounts_for_descendants_and_empty_docs() -> None:
    assert TodoDoc(title="empty", items=[]).is_complete() is True

    doc = TodoDoc(
        title="nested",
        items=[
            TodoItem(
                number="1",
                text="root",
                status=TodoStatus.DONE,
                children=[TodoItem(number="1.1", text="child", status=TodoStatus.PENDING)],
            )
        ],
    )
    assert doc.is_complete() is False
    doc.set_status("1.1", TodoStatus.DONE)
    assert doc.is_complete() is True


def test_set_blocked_reason_sets_status_and_reason() -> None:
    doc = TodoDoc(title="t", items=[TodoItem(number="1", text="step")])

    doc.set_blocked_reason("1", "waiting for dependency")

    item = doc.get_item("1")
    assert item.status == TodoStatus.BLOCKED
    assert item.blocked_reason == "waiting for dependency"


def test_upsert_children_updates_existing_adds_new_and_preserves_omitted_children() -> None:
    existing_1 = TodoItem(
        number="1.1",
        text="old text",
        status=TodoStatus.DOING,
        depends_on=["old-dep"],
        done_when=["old criterion"],
        details="old details",
        blocked_reason="still blocked",
        children=[TodoItem(number="1.1.a", text="substep", status=TodoStatus.DONE)],
    )
    existing_2 = TodoItem(number="1.2", text="keep me", status=TodoStatus.DONE)
    doc = TodoDoc(title="plan", items=[TodoItem(number="1", text="parent", children=[existing_1, existing_2])])

    doc.upsert_children(
        "1",
        [
            TodoItemSpec(number="1.1", text="new text", details=None, depends_on=["new-dep"], done_when=None),
            TodoItemSpec(number="1.3", text="brand new child", done_when=["done"]),
        ],
    )

    parent = doc.get_item("1")
    assert [child.number for child in parent.children] == ["1.1", "1.3", "1.2"]

    updated = parent.children[0]
    assert updated.text == "new text"
    assert updated.status == TodoStatus.DOING
    assert updated.depends_on == ["new-dep"]
    assert updated.done_when == ["old criterion"]
    assert updated.details == "old details"
    assert updated.blocked_reason == "still blocked"
    assert [c.number for c in updated.children] == ["1.1.a"]

    new_child = parent.children[1]
    assert new_child.status == TodoStatus.PENDING
    assert new_child.done_when == ["done"]


def test_get_item_raises_key_error_for_missing_number() -> None:
    doc = TodoDoc(title="t", items=[TodoItem(number="1", text="one")])
    with pytest.raises(KeyError, match="Todo item not found"):
        doc.get_item("does-not-exist")
