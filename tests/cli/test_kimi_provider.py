"""Tests for KimiCLI provider: command building and parsing."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ductor_bot.cli.base import CLIConfig
from ductor_bot.cli.kimi_provider import (
    KimiCLI,
    _extract_text_content,
    _kimi_final_result,
    _parse_response,
    parse_kimi_stream_line,
)
from ductor_bot.cli.stream_events import AssistantTextDelta, ResultEvent, ToolResultEvent, ToolUseEvent
from ductor_bot.cli.executor import SubprocessResult


def _make_cli(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> KimiCLI:
    monkeypatch.setattr("ductor_bot.cli.kimi_provider.which", lambda _: "/usr/bin/kimi")
    cfg = CLIConfig(
        provider="kimi",
        working_dir=overrides.pop("working_dir", "."),
        model=overrides.pop("model", "kimi-k2-0905-preview"),
        system_prompt=overrides.pop("system_prompt", None),
        append_system_prompt=overrides.pop("append_system_prompt", None),
        chat_id=overrides.pop("chat_id", 123),
        topic_id=overrides.pop("topic_id", 9),
        cli_parameters=overrides.pop("cli_parameters", []),
    )
    return KimiCLI(cfg)


def test_build_command_has_stream_json(monkeypatch: pytest.MonkeyPatch) -> None:
    cli = _make_cli(monkeypatch)
    cmd, sid = cli._build_command("hello", resume_session=None, continue_session=False)
    assert cmd[0] == "/usr/bin/kimi"
    assert "--print" in cmd
    assert "--output-format" in cmd
    fmt_idx = cmd.index("--output-format")
    assert cmd[fmt_idx + 1] == "stream-json"
    assert "--model" in cmd
    assert sid is None
    assert "--resume" not in cmd


def test_build_command_continue_without_resume(monkeypatch: pytest.MonkeyPatch) -> None:
    cli = _make_cli(monkeypatch)
    cmd, sid = cli._build_command("hello", resume_session=None, continue_session=True)
    assert "--continue" in cmd
    assert "--resume" not in cmd
    assert sid is None


def test_build_command_uses_given_resume(monkeypatch: pytest.MonkeyPatch) -> None:
    cli = _make_cli(monkeypatch)
    cmd, sid = cli._build_command("hello", resume_session="abc-123", continue_session=False)
    assert sid == "abc-123"
    idx = cmd.index("--resume")
    assert cmd[idx + 1] == "abc-123"


def test_parse_response_extracts_resume_session_id_from_stderr_hint() -> None:
    stderr = b"To resume this session: kimi -r ductor-123-0-abcde"
    resp = _parse_response(b"", stderr, 1, fallback_session_id=None)
    assert resp.session_id == "ductor-123-0-abcde"
    assert resp.is_error is True


def test_compose_prompt_includes_system_context(monkeypatch: pytest.MonkeyPatch) -> None:
    cli = _make_cli(
        monkeypatch,
        system_prompt="SYSTEM",
        append_system_prompt="TAIL",
    )
    composed = cli._compose_prompt("USER")
    assert composed == "SYSTEM\n\nUSER\n\nTAIL"


def test_parse_kimi_stream_line_assistant_text_and_tool() -> None:
    line = json.dumps(
        {
            "role": "assistant",
            "content": "hello",
            "tool_calls": [
                {
                    "id": "t1",
                    "function": {
                        "name": "read_file",
                        "arguments": {"path": "/tmp/x"},
                    },
                }
            ],
        }
    )
    events = parse_kimi_stream_line(line)
    assert any(isinstance(e, AssistantTextDelta) and e.text == "hello" for e in events)
    assert any(
        isinstance(e, ToolUseEvent) and e.tool_name == "read_file" and e.tool_id == "t1"
        for e in events
    )


def test_parse_kimi_stream_line_assistant_tool_with_string_arguments() -> None:
    line = json.dumps(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "t2",
                    "function": {
                        "name": "Bash",
                        "arguments": '{"command": "echo hi", "timeout": 60}',
                    },
                }
            ],
        }
    )
    events = parse_kimi_stream_line(line)
    tool_events = [e for e in events if isinstance(e, ToolUseEvent)]
    assert len(tool_events) == 1
    assert tool_events[0].tool_name == "Bash"
    assert tool_events[0].parameters == {"command": "echo hi", "timeout": 60}


def test_parse_kimi_stream_line_tool_result() -> None:
    line = json.dumps({"role": "tool", "tool_call_id": "t1", "content": "done"})
    events = parse_kimi_stream_line(line)
    assert len(events) == 1
    evt = events[0]
    assert isinstance(evt, ToolResultEvent)
    assert evt.tool_id == "t1"
    assert evt.output == "done"


def test_parse_kimi_stream_line_bad_json() -> None:
    assert parse_kimi_stream_line("not-json") == []


def test_extract_text_content_variants() -> None:
    assert _extract_text_content("abc") == "abc"
    assert _extract_text_content([{"type": "text", "text": "a"}, {"content": "b"}]) == "ab"
    assert _extract_text_content({"text": "x"}) == "x"
    assert _extract_text_content(123) == ""


def test_parse_response_collects_assistant_lines() -> None:
    stdout = (
        b'{"role":"assistant","content":"Hello "}\n'
        b'{"role":"assistant","content":"world","session_id":"sid-1"}\n'
    )
    resp = _parse_response(stdout, b"", 0, fallback_session_id="fallback")
    assert resp.result == "Hello world"
    assert resp.session_id == "sid-1"
    assert resp.is_error is False


def test_parse_response_empty_stdout_is_error() -> None:
    resp = _parse_response(b"", b"stderr", 1, fallback_session_id="sid-x")
    assert resp.is_error is True
    assert resp.session_id == "sid-x"
    assert "stderr" in resp.result


def test_kimi_final_result_success() -> None:
    class _Proc:
        returncode = 0

    result = _kimi_final_result(
        SubprocessResult(process=_Proc(), stderr_bytes=b""),
        ["abc", "def"],
        "sid-1",
    )
    assert isinstance(result, ResultEvent)
    assert result.is_error is False
    assert result.result == "abcdef"
    assert result.session_id == "sid-1"


def test_kimi_final_result_error() -> None:
    class _Proc:
        returncode = 2

    result = _kimi_final_result(
        SubprocessResult(process=_Proc(), stderr_bytes=b"boom"),
        [],
        "sid-2",
    )
    assert result.is_error is True
    assert result.returncode == 2
    assert "boom" in result.result
