import pytest

from satrap.dag import dependency_batches
from satrap.todo import TodoItem


def _item(number: str, depends_on: list[str] | None = None) -> TodoItem:
    return TodoItem(number=number, text=f"step {number}", depends_on=depends_on or [])


def test_dependency_batches_emits_frontier_batches_in_input_order() -> None:
    items = [
        _item("1"),
        _item("2", ["1"]),
        _item("3"),
        _item("4", ["2", "3"]),
    ]
    done: set[str] = set()

    def is_done(number: str) -> bool:
        return number in done

    observed: list[list[str]] = []
    for batch in dependency_batches(items, is_done=is_done):
        observed.append([item.number for item in batch])
        done.update(item.number for item in batch)

    assert observed == [["1", "3"], ["2"], ["4"]]


def test_dependency_batches_raises_deadlock_for_cycle() -> None:
    items = [_item("1", ["2"]), _item("2", ["1"])]

    with pytest.raises(RuntimeError, match="deadlock or unmet prerequisite"):
        list(dependency_batches(items, is_done=lambda _: False))


def test_dependency_batches_raises_deadlock_for_unmet_external_dependency() -> None:
    items = [_item("1"), _item("2", ["external"])]
    done: set[str] = set()

    def is_done(number: str) -> bool:
        return number in done

    gen = dependency_batches(items, is_done=is_done)
    first = next(gen)
    assert [item.number for item in first] == ["1"]
    done.update(item.number for item in first)

    with pytest.raises(RuntimeError, match="\\['2'\\]"):
        next(gen)


def test_dependency_batches_treats_key_error_from_is_done_as_not_done() -> None:
    items = [_item("1"), _item("2", ["missing"])]
    done: set[str] = set()

    def is_done(number: str) -> bool:
        if number == "missing":
            raise KeyError(number)
        return number in done

    gen = dependency_batches(items, is_done=is_done)
    first = next(gen)
    assert [item.number for item in first] == ["1"]
    done.update(item.number for item in first)

    with pytest.raises(RuntimeError, match="\\['2'\\]"):
        next(gen)
