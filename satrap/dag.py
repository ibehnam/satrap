"""Dependency batching utilities for Satrap's todo scheduler.

This module implements a minimal "frontier" scheduler over `TodoItem` dependencies.
Rather than constructing a full graph, it repeatedly scans a provided collection of
items and yields successive batches of work whose prerequisites are satisfied.

Batching semantics
- Each yielded batch contains every remaining item that is runnable *at the moment the
  batch is computed*: an item is runnable when all numbers in `item.depends_on` return
  truthy from `is_done(dep_number)`.
- Batches are greedy "levels" (a frontier), not a full topological ordering: items
  appear in the earliest batch for which their dependencies are satisfied.
- Dependency checks are dynamic. `is_done()` is invoked at evaluation time for each
  dependency, so callers may back `is_done()` with a changing data source (for example,
  reloading `.satrap/todo.json` between items/batches). This also means batching can
  change if `is_done()` changes between iterations.

Determinism guarantees and requirements
- Given:
  - a re-iterable, stably ordered `items` input (typically a `list[TodoItem]`), and
  - a deterministic `is_done()` for the duration of the generator,
  the yielded batch sequence is deterministic.
- Within a batch, item ordering is derived from the caller's input order (ties are
  broken by that order), so running items sequentially yields predictable behavior.
- Items are expected to have unique `TodoItem.number` values; duplicates make the
  schedule ambiguous and can lead to surprising results.

Deadlock and unmet prerequisites
- If there are remaining items but none are runnable, scheduling stops by raising
  `RuntimeError` (instead of spinning). This indicates:
  - a dependency cycle among remaining items, or
  - an unmet prerequisite that will not become done under the current execution,
    including dependencies that are "external" to the provided `items`.
- If `is_done(dep_number)` raises `KeyError`, the dependency is treated as not done,
  which will also force a deadlock if no other progress is possible.

Intended usage
- Iterate `dependency_batches(...)` to drive execution:
  - run all items in the batch (sequentially today; safely parallelizable when side
    effects allow), and
  - update the underlying done-state so the next batch can become runnable.
- Prefer passing a concrete sequence (e.g., `todo.items` or `step.children`) rather
  than a one-shot iterator, since stable ordering requires re-iterating `items`.
"""

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
