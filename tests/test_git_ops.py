from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from satrap import git_ops


def test_worktrees_parses_porcelain_and_ignores_non_branch_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = git_ops.GitClient(control_root=tmp_path)
    porcelain = "\n".join(
        [
            f"worktree {tmp_path}",
            "HEAD 1111111",
            "branch refs/heads/main",
            "",
            f"worktree {tmp_path / '.worktrees' / 'feature'}",
            "HEAD 2222222",
            "branch refs/heads/feature/test",
            "",
            f"worktree {tmp_path / '.worktrees' / 'detached'}",
            "HEAD 3333333",
            "detached",
            "",
        ]
    )
    monkeypatch.setattr(client, "_git", lambda args, cwd: porcelain)

    result = client.worktrees()

    assert result == {
        "main": tmp_path.resolve(),
        "feature/test": (tmp_path / ".worktrees" / "feature").resolve(),
    }


@pytest.mark.parametrize(
    ("returncode", "expected"),
    [
        (0, True),
        (1, False),
    ],
)
def test_branch_exists_uses_show_ref_and_return_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, returncode: int, expected: bool
) -> None:
    client = git_ops.GitClient(control_root=tmp_path)
    calls: list[tuple[list[str], Path, bool]] = []

    def fake_run(cmd: list[str], cwd: Path, check: bool) -> SimpleNamespace:
        calls.append((cmd, cwd, check))
        return SimpleNamespace(returncode=returncode)

    monkeypatch.setattr(git_ops.subprocess, "run", fake_run)

    assert client.branch_exists("feature/x") is expected
    assert calls == [(["git", "show-ref", "--verify", "--quiet", "refs/heads/feature/x"], tmp_path, False)]


def test_current_branch_raises_on_detached_head(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = git_ops.GitClient(control_root=tmp_path)
    monkeypatch.setattr(client, "_git", lambda args, cwd: "HEAD\n")

    with pytest.raises(RuntimeError, match="Detached HEAD"):
        client.current_branch(cwd=tmp_path)


def test_ensure_worktree_reuses_existing_branch_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = git_ops.GitClient(control_root=tmp_path)
    existing = (tmp_path / ".worktrees" / "existing").resolve()
    monkeypatch.setattr(client, "worktrees", lambda: {"feature/existing": existing})
    monkeypatch.setattr(git_ops, "generate_unique_phrase", lambda **kwargs: (_ for _ in ()).throw(AssertionError()))
    monkeypatch.setattr(client, "_git", lambda args, cwd: (_ for _ in ()).throw(AssertionError()))

    result = client.ensure_worktree(
        branch="feature/existing",
        base_ref="main",
        worktrees_dir=tmp_path / ".worktrees",
        phrases_path=tmp_path / "phrases.txt",
    )

    assert result == git_ops.GitWorktree(branch="feature/existing", path=existing)


@pytest.mark.parametrize("branch_exists", [True, False])
def test_ensure_worktree_adds_existing_or_new_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, branch_exists: bool
) -> None:
    control_root = tmp_path / "repo"
    control_root.mkdir()
    worktrees_dir = tmp_path / ".worktrees"
    phrases_path = tmp_path / "phrases.txt"
    client = git_ops.GitClient(control_root=control_root)
    calls: list[tuple[list[str], Path]] = []

    monkeypatch.setattr(client, "worktrees", lambda: {})
    monkeypatch.setattr(git_ops, "generate_unique_phrase", lambda **kwargs: "fresh-phrase")
    monkeypatch.setattr(client, "branch_exists", lambda branch: branch_exists)
    monkeypatch.setattr(
        client,
        "_git",
        lambda args, cwd: calls.append((args, cwd)) or "",
    )

    result = client.ensure_worktree(
        branch="feature/new",
        base_ref="main",
        worktrees_dir=worktrees_dir,
        phrases_path=phrases_path,
    )

    expected_path = (worktrees_dir / "fresh-phrase").resolve()
    assert result == git_ops.GitWorktree(branch="feature/new", path=expected_path)
    assert worktrees_dir.exists()
    if branch_exists:
        assert calls == [(["worktree", "add", str(expected_path), "feature/new"], control_root)]
    else:
        assert calls == [(["worktree", "add", "-b", "feature/new", str(expected_path), "main"], control_root)]


def test_commit_all_if_needed_skips_when_clean(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = git_ops.GitClient(control_root=tmp_path)
    calls: list[list[str]] = []

    def fake_git(args: list[str], cwd: Path) -> str:
        calls.append(args)
        if args == ["status", "--porcelain"]:
            return ""
        return ""

    monkeypatch.setattr(client, "_git", fake_git)
    monkeypatch.setattr(
        git_ops.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("commit should not run")),
    )

    client.commit_all_if_needed(cwd=tmp_path, message="msg")

    assert calls == [["status", "--porcelain"]]


def test_commit_all_if_needed_stages_and_attempts_commit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = git_ops.GitClient(control_root=tmp_path)
    git_calls: list[list[str]] = []
    commit_calls: list[tuple[list[str], Path, bool]] = []

    def fake_git(args: list[str], cwd: Path) -> str:
        git_calls.append(args)
        if args == ["status", "--porcelain"]:
            return " M satrap/git_ops.py\n"
        if args == ["add", "-A"]:
            return ""
        raise AssertionError(f"unexpected git args: {args}")

    def fake_run(cmd: list[str], cwd: Path, check: bool) -> SimpleNamespace:
        commit_calls.append((cmd, cwd, check))
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(client, "_git", fake_git)
    monkeypatch.setattr(git_ops.subprocess, "run", fake_run)

    client.commit_all_if_needed(cwd=tmp_path, message="commit message")

    assert git_calls == [["status", "--porcelain"], ["add", "-A"]]
    assert commit_calls == [(["git", "commit", "-m", "commit message"], tmp_path, False)]


def test_dry_run_client_returns_noop_semantics(tmp_path: Path) -> None:
    client = git_ops.DryRunGitClient(control_root=tmp_path)

    assert client.current_branch(cwd=tmp_path) == "dryrun"
    assert client.ensure_worktree(
        branch="feature/dry",
        base_ref="main",
        worktrees_dir=tmp_path / ".worktrees",
        phrases_path=tmp_path / "phrases.txt",
    ) == git_ops.GitWorktree(branch="feature/dry", path=tmp_path)
    assert client.worktrees() == {}
    assert client.branch_exists("anything") is False
    assert client.merge_base(branch="a", other_ref="b", cwd=tmp_path) == "DRYRUN"
    assert client.diff_since("abc123", cwd=tmp_path) == ""
    assert client.commits_since("abc123", cwd=tmp_path) == []
    assert client.commit_all_if_needed(cwd=tmp_path, message="msg") is None
    assert client.reset_hard("HEAD~1", cwd=tmp_path) is None
    assert client.merge_into(source_branch="a", target_branch="b", cwd=tmp_path) is None
