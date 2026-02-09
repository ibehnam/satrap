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

    extracted = _extract_structured_or_printed_result(raw_stdout)
    stdout = extracted.print_text.strip()
    if stdout:
        sys.stdout.write(stdout + "\n")
        sys.stdout.flush()
    data: Any | None = extracted.data

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


@dataclass(frozen=True)
class _ClaudeExtracted:
    # Schema-validated payload (present when `--json-schema` is used).
    data: Any | None
    # Human/debug output to print.
    print_text: str


def _parse_envelope(raw_stdout: str) -> Any | None:
    """Parse `claude --output-format json` stdout.

    Claude Code typically emits a single JSON array. If that changes to JSONL,
    we fall back to parsing line-by-line.
    """
    if not raw_stdout.strip():
        return None

    env = _best_effort_parse_json(raw_stdout)
    if env is not None:
        return env

    items: list[Any] = []
    for ln in raw_stdout.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            items.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return items if items else None


def _extract_structured_or_printed_result(raw_stdout: str) -> _ClaudeExtracted:
    """Extract structured output (preferred) or printed result text.

    With `--json-schema`, Claude Code may return a `structured_output` payload on the final
    `type=="result"` event. The printed `result` string is often non-JSON and should not be
    used as the structured output source of truth.
    """
    env = _parse_envelope(raw_stdout)
    if env is None:
        return _ClaudeExtracted(data=None, print_text=raw_stdout.strip())

    result_ev: dict | None = None
    if isinstance(env, dict):
        result_ev = env
    elif isinstance(env, list):
        for it in reversed(env):
            if isinstance(it, dict) and it.get("type") == "result":
                result_ev = it
                break

    if result_ev is None:
        return _ClaudeExtracted(data=None, print_text=raw_stdout.strip())

    structured = result_ev.get("structured_output")
    if structured is not None:
        # Prefer printing normalized JSON for observability.
        try:
            return _ClaudeExtracted(data=structured, print_text=json.dumps(structured, indent=2, sort_keys=True))
        except TypeError:
            return _ClaudeExtracted(data=structured, print_text=str(structured))

    res = result_ev.get("result")
    if isinstance(res, str) and res.strip():
        return _ClaudeExtracted(data=_best_effort_parse_json(res), print_text=res.strip())

    return _ClaudeExtracted(data=None, print_text=raw_stdout.strip())
