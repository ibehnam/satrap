from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping


class TodoStatus(str, Enum):
    PENDING = "pending"
    DOING = "doing"
    DONE = "done"
    BLOCKED = "blocked"


@dataclass
class TodoItemSpec:
    number: str
    text: str
    details: str | None = None
    depends_on: list[str] | None = None
    done_when: list[str] | None = None


@dataclass
class TodoItem:
    number: str
    text: str
    status: TodoStatus = TodoStatus.PENDING
    depends_on: list[str] = field(default_factory=list)
    done_when: list[str] = field(default_factory=list)
    details: str | None = None
    blocked_reason: str | None = None
    children: list["TodoItem"] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict, repr=False)

    @staticmethod
    def from_spec(spec: TodoItemSpec) -> "TodoItem":
        return TodoItem(
            number=spec.number,
            text=spec.text,
            details=spec.details,
            depends_on=list(spec.depends_on or []),
            done_when=list(spec.done_when or []),
        )

    @staticmethod
    def from_dict(d: Mapping[str, Any]) -> "TodoItem":
        known: dict[str, Any] = {}
        extra: dict[str, Any] = {}
        for k, v in d.items():
            if k in {"number", "text", "status", "depends_on", "done_when", "details", "blocked_reason", "children"}:
                known[k] = v
            else:
                extra[k] = v

        children = [TodoItem.from_dict(c) for c in known.get("children", []) or []]
        status_raw = known.get("status", TodoStatus.PENDING.value)
        try:
            status = TodoStatus(str(status_raw))
        except ValueError:
            status = TodoStatus.PENDING

        return TodoItem(
            number=str(known["number"]),
            text=str(known["text"]),
            status=status,
            depends_on=[str(x) for x in (known.get("depends_on") or [])],
            done_when=[str(x) for x in (known.get("done_when") or [])],
            details=(str(known["details"]) if "details" in known and known["details"] is not None else None),
            blocked_reason=(
                str(known["blocked_reason"]) if "blocked_reason" in known and known["blocked_reason"] is not None else None
            ),
            children=children,
            extra=extra,
        )

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "number": self.number,
            "text": self.text,
            "status": self.status.value,
            "depends_on": list(self.depends_on),
            "done_when": list(self.done_when),
            "children": [c.to_dict() for c in self.children],
        }
        if self.details:
            d["details"] = self.details
        if self.status == TodoStatus.BLOCKED and self.blocked_reason:
            d["blocked_reason"] = self.blocked_reason
        d.update(self.extra)
        return d

    def update_from_spec(self, spec: TodoItemSpec) -> None:
        self.text = spec.text
        if spec.details is not None:
            self.details = spec.details
        if spec.depends_on is not None:
            self.depends_on = list(spec.depends_on)
        if spec.done_when is not None:
            self.done_when = list(spec.done_when)


@dataclass
class TodoDoc:
    title: str
    items: list[TodoItem]
    context: str | None = None
    extra: dict[str, Any] = field(default_factory=dict, repr=False)

    @staticmethod
    def load(path: Path) -> "TodoDoc":
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"todo.json must be a JSON object, got {type(raw)}")

        known: dict[str, Any] = {}
        extra: dict[str, Any] = {}
        for k, v in raw.items():
            if k in {"title", "items", "context"}:
                known[k] = v
            else:
                extra[k] = v

        items = [TodoItem.from_dict(x) for x in (known.get("items") or [])]
        title = str(known.get("title") or "")
        if not title:
            raise ValueError("todo.json is missing required field: title")
        context = known.get("context")
        return TodoDoc(title=title, context=(str(context) if context is not None else None), items=items, extra=extra)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        d: dict[str, Any] = {"title": self.title, "items": [i.to_dict() for i in self.items]}
        if self.context is not None:
            d["context"] = self.context
        d.update(self.extra)
        path.write_text(json.dumps(d, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")

    def is_done(self, number: str) -> bool:
        return self.get_item(number).status == TodoStatus.DONE

    def get_item(self, number: str) -> TodoItem:
        for item in self._walk_items(self.items):
            if item.number == number:
                return item
        raise KeyError(f"Todo item not found: {number}")

    def set_status(self, number: str, status: TodoStatus) -> None:
        self.get_item(number).status = status

    def set_blocked_reason(self, number: str, reason: str) -> None:
        item = self.get_item(number)
        item.status = TodoStatus.BLOCKED
        item.blocked_reason = reason

    def update_item_from_spec(self, number: str, spec: TodoItemSpec) -> None:
        self.get_item(number).update_from_spec(spec)

    def upsert_children(self, parent_number: str, children_specs: list[TodoItemSpec]) -> None:
        parent = self.get_item(parent_number)
        existing_by_num = {c.number: c for c in parent.children}
        merged: list[TodoItem] = []
        for spec in children_specs:
            if spec.number in existing_by_num:
                child = existing_by_num[spec.number]
                child.update_from_spec(spec)
            else:
                child = TodoItem.from_spec(spec)
            merged.append(child)

        # Preserve any existing children not returned by the latest planner run (defensive, avoids data loss).
        new_nums = {c.number for c in merged}
        for old in parent.children:
            if old.number not in new_nums:
                merged.append(old)

        parent.children = merged

    @staticmethod
    def _walk_items(items: Iterable[TodoItem]) -> Iterable[TodoItem]:
        stack = list(items)
        while stack:
            item = stack.pop(0)
            yield item
            stack[0:0] = list(item.children)
