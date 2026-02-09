"""Git operations and worktree management for Satrap.

This module provides two implementations with the same public surface:

- `GitClient`: the real implementation that shells out to `git` via `subprocess`.
- `DryRunGitClient`: a no-op implementation used by `--dry-run` / smoke tests to avoid
  mutating git state while still exercising higher-level orchestration logic.

The core data model is:

- `GitWorktree`: an immutable `(branch, path)` pair describing where a given branch is
  checked out on disk.

GitClient API (public methods)
All methods are intentionally small and map closely to a single git command, so callers
can reason about side effects.

- `current_branch(cwd=...) -> str`
  Returns the current branch name for the repository/worktree at `cwd`. Raises
  `RuntimeError` if `HEAD` is detached (Satrap expects to run from a named branch).
- `ensure_worktree(branch, base_ref, worktrees_dir, phrases_path) -> GitWorktree`
  Ensures there is a git worktree for `branch`. If `branch` already has an associated
  worktree (as reported by `worktrees()`), it is reused. Otherwise:
  1) `worktrees_dir` is created if missing.
  2) A unique directory name ("phrase") is allocated via `generate_unique_phrase()`,
     backed by `phrases_path` (a ledger to avoid collisions/reuse).
  3) A worktree is created at `worktrees_dir/<phrase>`:
     - If `branch` already exists locally: `git worktree add <path> <branch>`
     - Else: `git worktree add -b <branch> <path> <base_ref>`
- `worktrees() -> dict[str, Path]`
  Parses `git worktree list --porcelain` and returns a mapping of local branch name (e.g.
  `main`) to absolute worktree path. Only `refs/heads/*` entries are included.
- `branch_exists(branch) -> bool`
  Checks whether `refs/heads/<branch>` exists (local branch).
- `merge_base(branch, other_ref, cwd=...) -> str`
  Returns the merge base commit SHA between `branch` and `other_ref`.
- `diff_since(base_commit, cwd=...) -> str`
  Returns the unified diff between `base_commit` and `HEAD` in the given worktree.
- `commits_since(base_commit, cwd=...) -> list[str]`
  Returns commit SHAs (oldest to newest) between `base_commit` (exclusive) and `HEAD`.
- `commit_all_if_needed(cwd=..., message=...) -> None`
  If the worktree has changes (`git status --porcelain`), stages everything (`git add -A`)
  and attempts a commit with the provided message.
- `reset_hard(ref, cwd=...) -> None`
  Runs `git reset --hard <ref>`. This is destructive by design and is intended for
  cleaning up failed worker attempts.
- `merge_into(source_branch, target_branch, cwd=...) -> None`
  Merges `source_branch` into the currently checked out branch in the `cwd` worktree using
  a merge commit for traceability (`--no-ff --no-edit`). The `target_branch` parameter is
  informational: the caller is expected to run this inside the target worktree with
  `target_branch` checked out.

Worktree strategy
Satrap isolates work by creating (or reusing) a dedicated git worktree per branch under a
caller-specified `worktrees_dir` (typically a generated/ignored directory such as
`.worktrees/`). New worktree directories are named using a unique phrase drawn from a
shared ledger (`phrases_path`, typically `phrases.txt`) to reduce collisions and keep
paths human-readable.

`ensure_worktree()` is idempotent with respect to branch names: if a worktree already
exists for the branch, it returns the existing path rather than creating another.

Side effects and safety notes
- `GitClient` executes external commands (`git`) and will mutate the repository in methods
  like `ensure_worktree()`, `commit_all_if_needed()`, `reset_hard()`, and `merge_into()`.
- Most git invocations go through `_git(...)`, which uses `check=True` and will raise
  `subprocess.CalledProcessError` on failure. Callers should expect and handle these
  exceptions at higher levels.
- `commit_all_if_needed()` intentionally uses `check=False` for the final `git commit`, so
  a failed commit (e.g. hooks, missing identity, or no-op commit) will not raise.
- `reset_hard()` discards uncommitted work; only call it on a worktree you are willing to
  wipe.
- `DryRunGitClient` does not touch git state and returns placeholder values (e.g. branch
  `"dryrun"`, merge base `"DRYRUN"`, empty diffs/commit lists). Its `ensure_worktree()`
  returns the `control_root` path, meaning there is no filesystem isolation in dry-run
  mode; higher-level code must treat dry-run outputs as approximations rather than an
  exact simulation of git behavior.

Terminology:
- `control_root` is the repository root where Satrap anchors operations like worktree
  creation and `git worktree list`.
- `cwd` is the working directory (usually a specific worktree) in which a command should
  run; callers are responsible for passing the correct worktree path for the operation.
"""

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
