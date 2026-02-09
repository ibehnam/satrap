"""satrap.todo

This module defines Satrap's on-disk todo model (`.satrap/todo.json`) and the "planner
schema" boundary used when ingesting LLM-produced plans.

There are two related JSON shapes:

1) Planner output (schema-validated)
The planner is required to emit a JSON object that validates against `todo-schema.json`.
Conceptually this is the `TodoItemSpec` shape:

- Top-level object:
  - `title` (string, optional): title for the overall plan (used only at root planning).
  - `items` (array[object], required): immediate children for the planned scope.
  - No nested `children` should be emitted (one level only).
- Each item object:
  - `number` (string): stable hierarchical identifier like "1", "1.2", "2.3.1".
  - `text` (string): one-line description.
  - `depends_on` (array[string]): prerequisite step numbers; `[]` when none.
  - `done_when` (array[string]): acceptance criteria (minimum 1).
  - `details` (string, optional): longer instructions/context.
- The planner must not emit Satrap-managed runtime fields like `status`, `blocked_reason`,
  or `children`.

2) Persisted `.satrap/todo.json` (single source of truth)
Satrap persists a superset of the planner spec that adds execution state and a tree:

- `TodoDoc` (top-level):
  - `title` (string, required, non-empty)
  - `context` (string, optional): original user task/context for prompting
  - `items` (array[TodoItem]): root-level steps
  - plus any unknown top-level keys captured in `TodoDoc.extra`
- `TodoItem` (node):
  - `number` (string): stable identifier; used as the primary key for lookups/merges
  - `text` (string)
  - `status` (string enum): "pending" | "doing" | "done" | "blocked"
  - `depends_on` (array[string]): prerequisite step numbers (may refer to siblings or other nodes)
  - `done_when` (array[string]): acceptance criteria strings
  - `details` (string, optional)
  - `blocked_reason` (string, optional): explanation when `status == "blocked"`
  - `children` (array[TodoItem]): nested sub-steps
  - plus any unknown per-item keys captured in `TodoItem.extra`

Schema boundary and ownership
- `TodoItemSpec` is the boundary type for planner input. Satrap treats planner JSON as
  untrusted and relies on JSON Schema validation (via the planner backend) to constrain
  its shape before converting it into `TodoItemSpec`.
- Satrap owns and mutates runtime fields (`status`, `blocked_reason`, `children`) inside
  `.satrap/todo.json`. Planner output should never attempt to set them.
- The persisted todo file is not itself validated against `todo-schema.json`; it is a
  richer, Satrap-internal document. This module preserves unknown keys for forward
  compatibility even if schemas are strict elsewhere.

Parsing rules (JSON -> dataclasses)
- `TodoDoc.load(path)` requires the top-level JSON to be an object and requires a non-empty
  `title`. `context` is optional. `items` is optional and defaults to `[]`.
- `TodoItem.from_dict(d)` splits fields into:
  - known keys: `number`, `text`, `status`, `depends_on`, `done_when`, `details`,
    `blocked_reason`, `children`
  - unknown keys: stored in `TodoItem.extra` and round-tripped on save
- Type coercion is intentionally forgiving:
  - `number` and `text` are coerced to `str` (and are required to exist).
  - `depends_on` / `done_when` are coerced to `list[str]` (missing/None -> `[]`).
  - `details` / `blocked_reason` are preserved as `str | None` (None stays None).
  - `status` is parsed as `TodoStatus`; unknown values fall back to "pending".
  - `children` is parsed recursively (missing/None -> `[]`).
- This module does not enforce numbering patterns, uniqueness, or dependency validity at
  parse time; those are contracts/invariants described below.

Serialization rules (dataclasses -> JSON)
- `TodoDoc.save(path)` writes UTF-8 JSON with:
  - `indent=2`, `ensure_ascii=True`, and a trailing newline
  - `title` and `items` always present
  - `context` present only when not None
  - `TodoDoc.extra` merged into the top-level object
- `TodoItem.to_dict()` always emits:
  - `number`, `text`, `status`, `depends_on`, `done_when`, `children`
  - `details` only when it is truthy (non-empty string)
  - `blocked_reason` only when `status == "blocked"` and it is truthy
  - `TodoItem.extra` merged last into the item object
- Invariant expectation: keys stored in `extra` should not collide with known keys. If
  they do collide, `extra` wins on serialization (because it is merged last), which can
  corrupt the persisted shape.

Tree walking and lookups
- The todo structure is a tree: `TodoDoc.items` are roots; each `TodoItem` may have nested
  `children`.
- `_walk_items(items)` yields items in pre-order depth-first traversal (a parent is yielded
  before its descendants). This traversal underpins:
  - `get_item(number)`: first matching `number` wins; otherwise raises KeyError
  - `is_complete()`: true when all nodes yielded by the walk are "done" (an empty todo list
    is treated as complete)

Upserts and spec application
- `TodoItem.update_from_spec(spec)` overwrites:
  - `text` always
  - `details`, `depends_on`, `done_when` only when the spec field is not None
  This allows "partial" refinement without discarding existing state.
- `TodoDoc.upsert_children(parent_number, children_specs)` merges planner-produced
  `TodoItemSpec` children under an existing parent node:
  - Matches existing children by `number`.
  - For matches: updates fields via `update_from_spec` and preserves runtime state
    (`status`, `blocked_reason`, existing `children` unless explicitly replaced elsewhere).
  - For new numbers: creates a fresh `TodoItem` with default runtime state.
  - Preserves any existing children not present in `children_specs` by appending them to
    the merged list (defensive behavior to avoid data loss if a planner omits items).
  - No deletion occurs here; pruning must be an explicit higher-level decision.

Invariants and contracts (expected by the orchestrator/scheduler)
- `number` is the stable identifier for a node. For correct behavior:
  - Numbers should be unique within the relevant scope (ideally globally across the doc).
  - Renumbering is effectively a delete+create from Satrap's perspective and may orphan
    runtime state (status/blocked_reason) and references.
- `depends_on` should reference valid step numbers and should not form cycles within the
  scheduled set. The scheduler (`satrap.dag.dependency_batches`) will deadlock (raise) if
  nothing is runnable due to cycles or unmet prerequisites.
- `blocked_reason` is meaningful only when `status == "blocked"`. If a reason is present
  while not blocked, it may be dropped on the next save (since serialization only emits
  `blocked_reason` for blocked items).
- Planner output must be one level deep. Nested hierarchy is built by Satrap over time by
  attaching planner-emitted specs as `children` of an existing step.
"""

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

    def is_complete(self) -> bool:
        """True when all items (including descendants) are DONE.

        Empty todo lists are treated as complete.
        """
        return all(it.status == TodoStatus.DONE for it in self._walk_items(self.items))

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
