from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path


def in_tmux() -> bool:
    return bool(os.environ.get("TMUX"))


def ensure_window(*, window_name: str, cwd: Path) -> str:
    """Ensure a tmux window exists (creates a base shell pane to keep it alive)."""
    session = subprocess.check_output(["tmux", "display-message", "-p", "#S"], text=True).strip()
    existing = subprocess.check_output(["tmux", "list-windows", "-F", "#W"], text=True).splitlines()
    if window_name not in set(existing):
        subprocess.check_call(["tmux", "new-window", "-d", "-n", window_name, "-c", str(cwd)])
    return f"{session}:{window_name}"


def spawn_pane(
    *,
    window_target: str,
    argv: list[str],
    cwd: Path,
    title: str,
    env: dict[str, str] | None = None,
    keep_pane: bool = False,
    select: bool = True,
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
        script = cmd
    else:
        script = f"{cmd}; code=$?; tmux kill-pane -t $TMUX_PANE; exit $code"

    pane_id = subprocess.check_output(
        ["tmux", "split-window", "-t", window_target, "-P", "-F", "#{pane_id}", "-c", str(cwd), "bash", "-lc", script],
        text=True,
    ).strip()

    # Cosmetic: best-effort set pane title.
    subprocess.run(["tmux", "select-pane", "-t", pane_id, "-T", title], check=False)

    if select:
        subprocess.run(["tmux", "select-window", "-t", window_target], check=False)
        subprocess.run(["tmux", "select-pane", "-t", pane_id], check=False)

    return pane_id

