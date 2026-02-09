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


def test_single_item_no_deps() -> None:
    items = [_item("1")]
    done: set[str] = set()
    batches = list(dependency_batches(items, is_done=lambda n: n in done))
    assert len(batches) == 1
    assert [i.number for i in batches[0]] == ["1"]


def test_self_dependency_deadlocks() -> None:
    items = [_item("1", ["1"])]
    with pytest.raises(RuntimeError, match="deadlock"):
        list(dependency_batches(items, is_done=lambda _: False))


def test_linear_chain_10_items() -> None:
    items = [_item("1")] + [_item(str(i), [str(i - 1)]) for i in range(2, 11)]
    done: set[str] = set()
    observed: list[list[str]] = []
    for batch in dependency_batches(items, is_done=lambda n: n in done):
        observed.append([i.number for i in batch])
        done.update(i.number for i in batch)
    assert len(observed) == 10
    assert all(len(b) == 1 for b in observed)
    assert observed[0] == ["1"]
    assert observed[-1] == ["10"]


def test_diamond_dependency() -> None:
    items = [_item("A"), _item("B", ["A"]), _item("C", ["A"]), _item("D", ["B", "C"])]
    done: set[str] = set()
    observed: list[list[str]] = []
    for batch in dependency_batches(items, is_done=lambda n: n in done):
        observed.append([i.number for i in batch])
        done.update(i.number for i in batch)
    assert observed == [["A"], ["B", "C"], ["D"]]


def test_preserves_input_order_within_batch() -> None:
    items = [_item("C"), _item("A"), _item("B")]
    batches = list(dependency_batches(items, is_done=lambda _: False))
    assert len(batches) == 1
    assert [i.number for i in batches[0]] == ["C", "A", "B"]


def test_empty_input() -> None:
    assert list(dependency_batches([], is_done=lambda _: False)) == []


def test_all_deps_already_done() -> None:
    items = [_item("1", ["x"]), _item("2", ["y"])]
    batches = list(dependency_batches(items, is_done=lambda _: True))
    assert len(batches) == 1
    assert [i.number for i in batches[0]] == ["1", "2"]


def test_dynamic_is_done_callback() -> None:
    items = [_item("1"), _item("2", ["x"])]
    done: set[str] = set()

    observed: list[list[str]] = []
    for batch in dependency_batches(items, is_done=lambda n: n in done):
        observed.append([i.number for i in batch])
        done.update(i.number for i in batch)
        done.add("x")  # External dep becomes done after first batch

    assert observed == [["1"], ["2"]]
