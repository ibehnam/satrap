from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import satrap.claude_cli as claude_cli


def test_parse_envelope_supports_json_and_jsonl_fallback() -> None:
    direct = '[{"type":"result","result":"ok"}]'
    assert claude_cli._parse_envelope(direct) == [{"type": "result", "result": "ok"}]

    jsonl = "{not-json}\n{\"type\":\"log\"}\n\n{\"type\":\"result\",\"result\":\"ok\"}\n"
    assert claude_cli._parse_envelope(jsonl) == [
        {"type": "log"},
        {"type": "result", "result": "ok"},
    ]

    assert claude_cli._parse_envelope("   \n") is None


def test_extract_prefers_structured_output_over_result_string() -> None:
    env = json.dumps(
        [
            {
                "type": "result",
                "structured_output": {"z": 1, "a": 2},
                "result": '{"ignored": true}',
            }
        ]
    )

    extracted = claude_cli._extract_structured_or_printed_result(env)

    assert extracted.data == {"z": 1, "a": 2}
    assert extracted.print_text == '{\n  "a": 2,\n  "z": 1\n}'


def test_extract_falls_back_to_result_string_and_best_effort_parse() -> None:
    env = json.dumps(
        [
            {
                "type": "result",
                "result": 'prefix {"ok": true, "count": 2} suffix',
            }
        ]
    )

    extracted = claude_cli._extract_structured_or_printed_result(env)

    assert extracted.print_text == 'prefix {"ok": true, "count": 2} suffix'
    assert extracted.data == {"ok": True, "count": 2}


def test_jq_compact_json_raises_runtime_error_when_jq_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def _raise(*_args: Any, **_kwargs: Any) -> str:
        raise FileNotFoundError("jq not found")

    monkeypatch.setattr(claude_cli.subprocess, "check_output", _raise)

    with pytest.raises(RuntimeError, match="jq is required"):
        claude_cli._jq_compact_json(tmp_path / "schema.json", cwd=tmp_path)


class _FakeStream:
    def __init__(self, lines: list[str]) -> None:
        self._lines = lines
        self._idx = 0

    def readline(self) -> str:
        if self._idx >= len(self._lines):
            return ""
        line = self._lines[self._idx]
        self._idx += 1
        return line

    @property
    def exhausted(self) -> bool:
        return self._idx >= len(self._lines)


class _FakeProcess:
    def __init__(self, stdout_lines: list[str], stderr_lines: list[str]) -> None:
        self.stdout = _FakeStream(stdout_lines)
        self.stderr = _FakeStream(stderr_lines)
        self.returncode = 0

    def poll(self) -> int | None:
        if self.stdout.exhausted and self.stderr.exhausted:
            return self.returncode
        return None

    def wait(self) -> int:
        return self.returncode


class _FakeSelector:
    def __init__(self) -> None:
        self._fileobjs: list[Any] = []

    def register(self, fileobj: Any, _event: Any) -> None:
        self._fileobjs.append(fileobj)

    def unregister(self, fileobj: Any) -> None:
        self._fileobjs = [f for f in self._fileobjs if f is not fileobj]

    def get_map(self) -> dict[int, Any]:
        return {id(f): f for f in self._fileobjs}

    def select(self, timeout: float = 0.0) -> list[tuple[SimpleNamespace, int]]:
        _ = timeout
        return [(SimpleNamespace(fileobj=f), 1) for f in list(self._fileobjs)]


def test_run_claude_json_from_files_streams_stderr_and_emits_normalized_stdout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    prompt_file = tmp_path / "prompt.md"
    schema_file = tmp_path / "schema.json"
    prompt_file.write_text("build a plan", encoding="utf-8")
    schema_file.write_text("{}", encoding="utf-8")

    captured: dict[str, Any] = {}
    envelope = json.dumps(
        [
            {"type": "event", "message": "ignore"},
            {"type": "result", "structured_output": {"b": 2, "a": 1}, "result": "not-used"},
        ]
    )

    def _fake_popen(
        cmd: list[str],
        *,
        cwd: str,
        text: bool,
        stdout: Any,
        stderr: Any,
        bufsize: int,
    ) -> _FakeProcess:
        captured["cmd"] = cmd
        captured["cwd"] = cwd
        captured["text"] = text
        captured["stdout_pipe"] = stdout
        captured["stderr_pipe"] = stderr
        captured["bufsize"] = bufsize
        return _FakeProcess(stdout_lines=[envelope + "\n"], stderr_lines=["warn 1\n", "warn 2\n"])

    monkeypatch.setattr(claude_cli, "_jq_compact_json", lambda _schema, *, cwd: '{"type":"object"}')
    monkeypatch.setattr(claude_cli.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(claude_cli.selectors, "DefaultSelector", _FakeSelector)

    result = claude_cli.run_claude_json_from_files(
        executable="claude",
        model="ccss-sonnet",
        prompt_file=prompt_file,
        schema_file=schema_file,
        cwd=tmp_path,
    )

    printed = capsys.readouterr()

    assert result.exit_code == 0
    assert result.data == {"b": 2, "a": 1}
    assert result.stdout == '{\n  "a": 1,\n  "b": 2\n}'
    assert result.stderr == "warn 1\nwarn 2"
    assert printed.out == '{\n  "a": 1,\n  "b": 2\n}\n'
    assert printed.err == "warn 1\nwarn 2\n"
    assert envelope not in printed.out
    assert captured["cmd"] == [
        "claude",
        "--model",
        "ccss-sonnet",
        "-p",
        "build a plan",
        "--json-schema",
        '{"type":"object"}',
        "--output-format",
        "json",
    ]
    assert captured["cmd"][-2:] == ["--output-format", "json"]
