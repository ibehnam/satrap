from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from .phrases import generate_unique_phrase


@dataclass(frozen=True)
class GitWorktree:
    branch: str
    path: Path


class GitClient:
    def __init__(self, *, control_root: Path) -> None:
        self.control_root = control_root

    def current_branch(self, *, cwd: Path) -> str:
        out = self._git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd)
        out = out.strip()
        if out == "HEAD":
            raise RuntimeError("Detached HEAD; please run satrap from a named branch.")
        return out

    def ensure_worktree(
        self,
        *,
        branch: str,
        base_ref: str,
        worktrees_dir: Path,
        phrases_path: Path,
    ) -> GitWorktree:
        worktrees = self.worktrees()
        if branch in worktrees:
            return GitWorktree(branch=branch, path=worktrees[branch])

        worktrees_dir.mkdir(parents=True, exist_ok=True)
        phrase = generate_unique_phrase(phrases_path=phrases_path)
        wt_path = (worktrees_dir / phrase).resolve()

        if self.branch_exists(branch):
            self._git(["worktree", "add", str(wt_path), branch], cwd=self.control_root)
        else:
            self._git(["worktree", "add", "-b", branch, str(wt_path), base_ref], cwd=self.control_root)

        return GitWorktree(branch=branch, path=wt_path)

    def worktrees(self) -> dict[str, Path]:
        """Return mapping of branch name -> worktree path."""
        out = self._git(["worktree", "list", "--porcelain"], cwd=self.control_root)
        lines = out.splitlines()
        current_path: Path | None = None
        branch: str | None = None
        result: dict[str, Path] = {}

        def flush() -> None:
            nonlocal current_path, branch
            if current_path is not None and branch is not None and branch.startswith("refs/heads/"):
                result[branch.removeprefix("refs/heads/")] = current_path
            current_path = None
            branch = None

        for line in lines:
            if not line.strip():
                continue
            if line.startswith("worktree "):
                flush()
                current_path = Path(line.split(" ", 1)[1]).resolve()
            elif line.startswith("branch "):
                branch = line.split(" ", 1)[1].strip()

        flush()
        return result

    def branch_exists(self, branch: str) -> bool:
        p = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            cwd=self.control_root,
            check=False,
        )
        return p.returncode == 0

    def merge_base(self, *, branch: str, other_ref: str, cwd: Path) -> str:
        return self._git(["merge-base", branch, other_ref], cwd=cwd).strip()

    def diff_since(self, base_commit: str, *, cwd: Path) -> str:
        return self._git(["diff", f"{base_commit}..HEAD"], cwd=cwd)

    def commits_since(self, base_commit: str, *, cwd: Path) -> list[str]:
        out = self._git(["rev-list", "--reverse", f"{base_commit}..HEAD"], cwd=cwd).strip()
        return [line.strip() for line in out.splitlines() if line.strip()]

    def commit_all_if_needed(self, *, cwd: Path, message: str) -> None:
        status = self._git(["status", "--porcelain"], cwd=cwd)
        if not status.strip():
            return
        self._git(["add", "-A"], cwd=cwd)
        subprocess.run(["git", "commit", "-m", message], cwd=cwd, check=False)

    def reset_hard(self, ref: str, *, cwd: Path) -> None:
        # Destructive by design; used to undo failed worker attempts.
        self._git(["reset", "--hard", ref], cwd=cwd)

    def merge_into(self, *, source_branch: str, target_branch: str, cwd: Path) -> None:
        # Caller is expected to run this in the target worktree where `target_branch` is checked out.
        # This uses a merge commit for traceability; adjust strategy later if you prefer ff-only.
        self._git(["merge", "--no-ff", "--no-edit", source_branch], cwd=cwd)

    def _git(self, args: list[str], *, cwd: Path) -> str:
        p = subprocess.run(
            ["git", *args],
            cwd=cwd,
            text=True,
            check=True,
            capture_output=True,
        )
        return p.stdout


class DryRunGitClient(GitClient):
    """A no-op Git client for smoke testing without touching git state."""

    def current_branch(self, *, cwd: Path) -> str:  # type: ignore[override]
        return "dryrun"

    def ensure_worktree(  # type: ignore[override]
        self,
        *,
        branch: str,
        base_ref: str,
        worktrees_dir: Path,
        phrases_path: Path,
    ) -> GitWorktree:
        return GitWorktree(branch=branch, path=self.control_root)

    def worktrees(self) -> dict[str, Path]:  # type: ignore[override]
        return {}

    def branch_exists(self, branch: str) -> bool:  # type: ignore[override]
        return False

    def merge_base(self, *, branch: str, other_ref: str, cwd: Path) -> str:  # type: ignore[override]
        return "DRYRUN"

    def diff_since(self, base_commit: str, *, cwd: Path) -> str:  # type: ignore[override]
        return ""

    def commits_since(self, base_commit: str, *, cwd: Path) -> list[str]:  # type: ignore[override]
        return []

    def commit_all_if_needed(self, *, cwd: Path, message: str) -> None:  # type: ignore[override]
        return

    def reset_hard(self, ref: str, *, cwd: Path) -> None:  # type: ignore[override]
        return

    def merge_into(self, *, source_branch: str, target_branch: str, cwd: Path) -> None:  # type: ignore[override]
        return
