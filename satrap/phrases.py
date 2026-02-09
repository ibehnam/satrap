from __future__ import annotations

import secrets
from pathlib import Path


_DICT_CANDIDATES = [
    Path("/usr/share/dict/words"),
    Path("/usr/share/dict/web2"),
    Path("/usr/dict/words"),
]


def generate_unique_phrase(*, phrases_path: Path, words_path: Path | None = None) -> str:
    """Return a unique `word-word-word` phrase and persist it to `phrases_path`.

    Uses the system dictionary on macOS when available. Uniqueness is best-effort and does not
    implement cross-process locking (placeholder for future concurrency hardening).
    """
    phrases_path.parent.mkdir(parents=True, exist_ok=True)
    existing = _load_existing_phrases(phrases_path)

    words = _load_words(words_path or _find_dictionary())
    if len(words) < 1000:
        raise RuntimeError("Dictionary word list too small; cannot generate phrases reliably.")

    for _ in range(10_000):
        phrase = "-".join(secrets.choice(words) for _ in range(3))
        if phrase in existing:
            continue
        phrases_path.write_text("\n".join(sorted(existing | {phrase})) + "\n", encoding="utf-8")
        return phrase

    raise RuntimeError("Failed to generate a unique 3-word phrase after many attempts.")


def _find_dictionary() -> Path:
    for p in _DICT_CANDIDATES:
        if p.exists() and p.is_file():
            return p
    raise FileNotFoundError("No system dictionary found (tried /usr/share/dict/words and common alternatives).")


def _load_existing_phrases(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}


def _load_words(path: Path) -> list[str]:
    raw = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    out: list[str] = []
    for w in raw:
        w = w.strip().lower()
        if not w:
            continue
        if not w.isalpha():
            continue
        if len(w) < 3 or len(w) > 12:
            continue
        out.append(w)
    return out

