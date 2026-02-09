from __future__ import annotations

from pathlib import Path

import pytest

from satrap import phrases


def _word_for_index(i: int) -> str:
    chars: list[str] = []
    n = i
    for _ in range(4):
        chars.append(chr(ord("a") + (n % 26)))
        n //= 26
    return "w" + "".join(chars)


def _write_dictionary(path: Path, *, size: int) -> None:
    words = [_word_for_index(i) for i in range(size)]
    path.write_text("\n".join(words) + "\n", encoding="utf-8")


def test_find_dictionary_returns_first_existing_candidate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first_missing = tmp_path / "missing"
    second = tmp_path / "second"
    third = tmp_path / "third"
    second.write_text("alpha\n", encoding="utf-8")
    third.write_text("beta\n", encoding="utf-8")
    monkeypatch.setattr(phrases, "_DICT_CANDIDATES", [first_missing, second, third])

    assert phrases._find_dictionary() == second


def test_find_dictionary_raises_when_no_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        phrases,
        "_DICT_CANDIDATES",
        [Path("/missing/dict/one"), Path("/missing/dict/two"), Path("/missing/dict/three")],
    )

    with pytest.raises(FileNotFoundError):
        phrases._find_dictionary()


def test_load_words_filters_normalizes_and_bounds(tmp_path: Path) -> None:
    dictionary = tmp_path / "dict.txt"
    dictionary.write_text(
        "\n".join(
            [
                "  Alpha  ",
                "be",
                "toolongwordhere",
                "two-words",
                "sp ace",
                "12345",
                "",
                "MiXeD",
                "valid",
            ]
        ),
        encoding="utf-8",
    )

    assert phrases._load_words(dictionary) == ["alpha", "mixed", "valid"]


def test_load_existing_phrases_ignores_blank_lines(tmp_path: Path) -> None:
    phrases_path = tmp_path / "phrases.txt"
    phrases_path.write_text("one-two-three\n\n  \nfour-five-six\n", encoding="utf-8")

    assert phrases._load_existing_phrases(phrases_path) == {"one-two-three", "four-five-six"}


def test_generate_unique_phrase_uses_discovered_dictionary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dictionary = tmp_path / "words.txt"
    _write_dictionary(dictionary, size=1000)
    phrases_path = tmp_path / "phrases.txt"
    monkeypatch.setattr(phrases, "_DICT_CANDIDATES", [dictionary])
    monkeypatch.setattr(phrases.secrets, "choice", lambda words: words[0])

    phrase = phrases.generate_unique_phrase(phrases_path=phrases_path)

    assert phrase == "waaaa-waaaa-waaaa"
    assert phrases_path.read_text(encoding="utf-8").endswith("\n")


def test_generate_unique_phrase_retries_collision_and_saves_sorted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    words_path = tmp_path / "words.txt"
    _write_dictionary(words_path, size=1000)
    phrases_path = tmp_path / "phrases.txt"
    phrases_path.write_text("zeta-eta-theta\n\nalpha-beta-gamma\n", encoding="utf-8")

    selections = iter(["alpha", "beta", "gamma", "delta", "epsilon", "zeta"])
    monkeypatch.setattr(phrases.secrets, "choice", lambda _words: next(selections))

    phrase = phrases.generate_unique_phrase(phrases_path=phrases_path, words_path=words_path)
    saved = phrases_path.read_text(encoding="utf-8")

    assert phrase == "delta-epsilon-zeta"
    assert saved.endswith("\n")
    assert saved.splitlines() == ["alpha-beta-gamma", "delta-epsilon-zeta", "zeta-eta-theta"]


def test_generate_unique_phrase_rejects_small_dictionary(tmp_path: Path) -> None:
    words_path = tmp_path / "words.txt"
    phrases_path = tmp_path / "phrases.txt"
    _write_dictionary(words_path, size=999)

    with pytest.raises(RuntimeError, match="too small"):
        phrases.generate_unique_phrase(phrases_path=phrases_path, words_path=words_path)
