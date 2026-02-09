from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator

from .todo import TodoItem


def dependency_batches(items: Iterable[TodoItem], *, is_done: Callable[[str], bool]) -> Iterator[list[TodoItem]]:
    """Yield batches of items whose dependencies are satisfied.

    Notes:
    - Dependencies are checked by calling `is_done(dep_number)` at evaluation time, so callers can
      point this at a dynamic store (e.g., reload `todo.json` each check).
    - This is intentionally conservative: if nothing is runnable, we raise to avoid infinite loops.
    """
    remaining: dict[str, TodoItem] = {i.number: i for i in items}
    emitted: set[str] = set()

    while remaining:
        ready: list[TodoItem] = []
        for num, item in list(remaining.items()):
            if num in emitted:
                continue
            deps = list(item.depends_on or [])
            if all(_safe_is_done(is_done, d) for d in deps):
                ready.append(item)

        if not ready:
            # Deadlock: either a dependency cycle or an unmet external dependency.
            stuck = sorted(remaining.keys())
            raise RuntimeError(f"No runnable items; dependency deadlock or unmet prerequisite(s): {stuck}")

        # Stable-ish ordering: preserve caller order where possible.
        ready_nums = {i.number for i in ready}
        batch = [i for i in items if i.number in ready_nums]
        yield batch

        for item in batch:
            emitted.add(item.number)
            remaining.pop(item.number, None)


def _safe_is_done(is_done: Callable[[str], bool], dep_number: str) -> bool:
    try:
        return bool(is_done(dep_number))
    except KeyError:
        return False

