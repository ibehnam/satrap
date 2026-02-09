"""tmux helpers for Satrap runtime orchestration.

This module centralizes non-focus-stealing tmux operations used by Satrap:
- detect tmux session presence,
- ensure/create a target window,
- create detached panes,
- send commands to existing panes,
- synchronize with `tmux wait-for`,
- collect pane metadata, and
- tear down panes when work is finished.

Design constraints:
- pane creation is detached by default (`split-window -d`),
- callers must opt in to focus switching (`select=True`),
- helpers are safe to use from control panes without stealing user focus.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
import shlex
import subprocess
from pathlib import Path


@dataclass(frozen=True)
class PaneContext:
    pane_id: str
    window_target: str
    label: str
    worktree_path: Path
    color: str | None = None


def in_tmux() -> bool:
    return bool(os.environ.get("TMUX"))


def _login_shell() -> str:
    # Use a consistent shell for panes to avoid surprises from user login shell config.
    # Override with SATRAP_PANE_SHELL if needed.
    return os.environ.get("SATRAP_PANE_SHELL", "/bin/zsh")


def shell_argv(*, script: str) -> list[str]:
    # Both bash and zsh accept `-lc`.
    return [_login_shell(), "-lc", script]


def ensure_window(*, window_name: str, cwd: Path) -> str:
    """Ensure a tmux window exists (creates a base shell pane to keep it alive)."""
    session = subprocess.check_output(["tmux", "display-message", "-p", "#S"], text=True).strip()
    existing = subprocess.check_output(["tmux", "list-windows", "-F", "#W"], text=True).splitlines()
    if window_name not in set(existing):
        subprocess.check_call(["tmux", "new-window", "-d", "-n", window_name, "-c", str(cwd)])
    return f"{session}:{window_name}"


def _split_window_detached(*, window_target: str, argv: list[str], cwd: Path) -> str:
    return subprocess.check_output(
        [
            "tmux",
            "split-window",
            "-d",
            "-t",
            window_target,
            "-P",
            "-F",
            "#{pane_id}",
            "-c",
            str(cwd),
            *argv,
        ],
        text=True,
    ).strip()


def current_window_name() -> str:
    """Return current tmux window name for this client."""
    return subprocess.check_output(["tmux", "display-message", "-p", "#W"], text=True).strip()


def pane_target(*, pane_id: str) -> str:
    return subprocess.check_output(
        ["tmux", "display-message", "-p", "-t", pane_id, "#{session_name}:#{window_name}.#{pane_index}"],
        text=True,
    ).strip()


def set_pane_color(*, pane_id: str, color: str) -> None:
    subprocess.run(["tmux", "set-option", "-pt", pane_id, "pane-border-style", f"fg={color}"], check=False)
    subprocess.run(["tmux", "set-option", "-pt", pane_id, "pane-active-border-style", f"fg={color}"], check=False)


def _set_pane_title(*, pane_id: str, title: str, select: bool) -> None:
    if select:
        subprocess.run(["tmux", "select-pane", "-t", pane_id, "-T", title], check=False)
        return
    # Keep focus on current pane while setting title.
    subprocess.run(["tmux", "select-pane", "-d", "-t", pane_id, "-T", title], check=False)


def spawn_worktree_pane(
    *,
    window_target: str,
    cwd: Path,
    title: str,
    color: str | None = None,
    select: bool = False,
) -> str:
    """Create a detached shell pane for a worktree execution context."""
    pane_id = _split_window_detached(window_target=window_target, argv=[_login_shell(), "-l"], cwd=cwd)
    subprocess.run(["tmux", "set-option", "-pt", pane_id, "remain-on-exit", "on"], check=False)
    _set_pane_title(pane_id=pane_id, title=title, select=select)
    if color:
        set_pane_color(pane_id=pane_id, color=color)

    if select:
        subprocess.run(["tmux", "select-window", "-t", window_target], check=False)
        subprocess.run(["tmux", "select-pane", "-t", pane_id], check=False)

    return pane_id


def spawn_pane(
    *,
    window_target: str,
    argv: list[str],
    cwd: Path,
    title: str,
    env: dict[str, str] | None = None,
    keep_pane: bool = False,
    select: bool = False,
) -> str:
    """Spawn a command in a new pane inside `window_target`.

    When `keep_pane` is false, the pane kills itself after the command exits.
    """
    env = env or {}
    env_prefix = " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items())
    cmd = " ".join(shlex.quote(a) for a in argv)
    if env_prefix:
        cmd = f"env {env_prefix} {cmd}"

    if keep_pane:
        shell = shlex.quote(_login_shell())
        script = f"{cmd}; code=$?; echo; echo \"[satrap] exited $code\"; exec {shell} -l"
    else:
        # Keep pane visible briefly so users can see terminal output before auto-close.
        script = f"{cmd}; code=$?; sleep 5; tmux kill-pane -t $TMUX_PANE; exit $code"

    pane_id = _split_window_detached(
        window_target=window_target,
        argv=[_login_shell(), "-lc", script],
        cwd=cwd,
    )
    _set_pane_title(pane_id=pane_id, title=title, select=select)

    if select:
        subprocess.run(["tmux", "select-window", "-t", window_target], check=False)
        subprocess.run(["tmux", "select-pane", "-t", pane_id], check=False)

    return pane_id


def spawn_pane_remain_on_exit(
    *,
    window_target: str,
    argv: list[str],
    cwd: Path,
    title: str,
    env: dict[str, str] | None = None,
    select: bool = False,
) -> str:
    """Spawn a command in a new detached pane and keep it visible after exit."""
    env = env or {}
    cmd_argv = (["env", *[f"{k}={v}" for k, v in env.items()]] + argv) if env else argv

    pane_id = _split_window_detached(window_target=window_target, argv=cmd_argv, cwd=cwd)

    subprocess.run(["tmux", "set-option", "-pt", pane_id, "remain-on-exit", "on"], check=False)
    _set_pane_title(pane_id=pane_id, title=title, select=select)

    if select:
        subprocess.run(["tmux", "select-window", "-t", window_target], check=False)
        subprocess.run(["tmux", "select-pane", "-t", pane_id], check=False)

    return pane_id


def send_command(*, pane_id: str, argv: list[str]) -> None:
    """Send a command argv to an existing pane without switching focus."""
    cmd = " ".join(shlex.quote(a) for a in argv)
    subprocess.check_call(["tmux", "send-keys", "-t", pane_id, "-l", cmd])
    subprocess.check_call(["tmux", "send-keys", "-t", pane_id, "Enter"])


def wait_for(*, key: str) -> None:
    subprocess.check_call(["tmux", "wait-for", key])


def kill_pane(*, pane_id: str) -> None:
    subprocess.run(["tmux", "kill-pane", "-t", pane_id], check=False)
