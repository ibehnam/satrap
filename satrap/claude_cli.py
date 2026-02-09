from __future__ import annotations

import json
import selectors
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ClaudeJSONResult:
    exit_code: int
    stdout: str
    stderr: str
    data: Any | None


def run_claude_json_from_files(
    *,
    executable: str = "claude",
    model: str,
    prompt_file: Path,
    schema_file: Path,
    cwd: Path,
) -> ClaudeJSONResult:
    """Run Claude with `--json-schema "$(jq -c . schema_file)"` and parse JSON from stdout.

    Important: we intentionally avoid passing schemas inline. The schema is always loaded from a file
    and compacted with `jq -c .` (matching the documented CLI usage).

    Note: Claude Code's structured output is most reliable when using `--output-format json`.
    We run with that and extract the final `result` payload before parsing JSON.
    """
    prompt = prompt_file.read_text(encoding="utf-8")
    schema_str = _jq_compact_json(schema_file, cwd=cwd)

    # Ordering matters with Claude Code CLI: keep `--output-format json` at the end
    # (matching the documented `-p "<prompt>" ...` usage).
    cmd = [executable, "--model", model, "-p", prompt, "--json-schema", schema_str, "--output-format", "json"]
    p = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=1,
    )
    assert p.stdout is not None
    assert p.stderr is not None

    sel = selectors.DefaultSelector()
    sel.register(p.stdout, selectors.EVENT_READ)
    sel.register(p.stderr, selectors.EVENT_READ)

    out_chunks: list[str] = []
    err_chunks: list[str] = []

    # Avoid streaming stdout: `--output-format json` can emit a huge JSON event envelope.
    # We print a normalized payload after extracting the final result.
    while sel.get_map():
        for key, _ in sel.select(timeout=0.1):
            line = key.fileobj.readline()
            if line == "":
                sel.unregister(key.fileobj)
                continue

            if key.fileobj is p.stdout:
                out_chunks.append(line)
            else:
                err_chunks.append(line)
                sys.stderr.write(line)
                sys.stderr.flush()

        if p.poll() is not None and not sel.get_map():
            break

    code = p.wait()
    raw_stdout = "".join(out_chunks).strip()
    stderr = "".join(err_chunks).strip()

    stdout = _extract_print_result(raw_stdout) or raw_stdout
    stdout = stdout.strip()
    if stdout:
        sys.stdout.write(stdout + "\n")
        sys.stdout.flush()

    data: Any | None = None
    if stdout:
        data = _best_effort_parse_json(stdout)

    return ClaudeJSONResult(exit_code=code, stdout=stdout, stderr=stderr, data=data)


def _jq_compact_json(schema_file: Path, *, cwd: Path) -> str:
    try:
        out = subprocess.check_output(["jq", "-c", ".", str(schema_file)], cwd=str(cwd), text=True)
    except FileNotFoundError as e:
        raise RuntimeError("jq is required to compact JSON schemas for claude --json-schema.") from e
    return out.strip()


def _best_effort_parse_json(text: str) -> Any | None:
    """Parse the first JSON value in `text`."""
    # Fast path: strict parse.
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Recovery: find the first JSON object/array and parse it.
    start = None
    for i, ch in enumerate(text):
        if ch in "{[":
            start = i
            break
    if start is None:
        return None

    decoder = json.JSONDecoder()
    try:
        val, _end = decoder.raw_decode(text[start:])
        return val
    except json.JSONDecodeError:
        return None


def _extract_print_result(raw_stdout: str) -> str | None:
    """Extract the assistant's final `result` string from `claude --output-format json`.

    Claude Code emits a JSON envelope (often a list of event objects). We care about the final
    `type=="result"` object's `result` field, which contains the printed assistant response.
    """
    if not raw_stdout.strip():
        return None

    env = _best_effort_parse_json(raw_stdout)
    if env is None:
        return None

    if isinstance(env, dict):
        res = env.get("result")
        return res if isinstance(res, str) else None

    if isinstance(env, list):
        for it in reversed(env):
            if not isinstance(it, dict):
                continue
            if it.get("type") != "result":
                continue
            res = it.get("result")
            return res if isinstance(res, str) else None

    return None
