"""Async wrapper around the Moonshot Kimi CLI."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncGenerator
from functools import partial
from pathlib import Path
from shutil import which
from typing import Any

from ductor_bot.cli.base import BaseCLI, CLIConfig, docker_wrap
from ductor_bot.cli.executor import (
    SubprocessResult,
    SubprocessSpec,
    run_oneshot_subprocess,
    run_streaming_subprocess,
)
from ductor_bot.cli.stream_events import (
    AssistantTextDelta,
    ResultEvent,
    StreamEvent,
    ToolResultEvent,
    ToolUseEvent,
)
from ductor_bot.cli.types import CLIResponse

logger = logging.getLogger(__name__)
_RESUME_SESSION_RE = re.compile(r"(?:--resume|-r)\s+([A-Za-z0-9._:-]+)")


class _KimiStreamState:
    """Mutable accumulator for Kimi streaming output."""

    __slots__ = ("accumulated_text", "session_id")

    def __init__(self, session_id: str | None) -> None:
        self.accumulated_text: list[str] = []
        self.session_id = session_id

    def track(self, event: StreamEvent) -> None:
        """Update state from one stream event."""
        if isinstance(event, AssistantTextDelta) and event.text:
            self.accumulated_text.append(event.text)
        if isinstance(event, ResultEvent) and event.session_id:
            self.session_id = event.session_id


class KimiCLI(BaseCLI):
    """Async wrapper around the Moonshot Kimi CLI."""

    def __init__(self, config: CLIConfig) -> None:
        self._config = config
        self._working_dir = Path(config.working_dir).resolve()
        self._cli = "kimi" if config.docker_container else self._find_cli()
        logger.info("Kimi CLI wrapper: cwd=%s model=%s", self._working_dir, config.model)

    @staticmethod
    def _find_cli() -> str:
        path = which("kimi")
        if not path:
            msg = "kimi CLI not found on PATH. Install via: uv tool install --python 3.13 kimi-cli"
            raise FileNotFoundError(msg)
        return path

    def _compose_prompt(self, prompt: str) -> str:
        """Inject system context into user prompt."""
        cfg = self._config
        parts: list[str] = []
        if cfg.system_prompt:
            parts.append(cfg.system_prompt)
        parts.append(prompt)
        if cfg.append_system_prompt:
            parts.append(cfg.append_system_prompt)
        return "\n\n".join(parts)

    def _build_command(
        self,
        prompt: str,
        *,
        resume_session: str | None,
        continue_session: bool,
    ) -> tuple[list[str], str | None]:
        """Build Kimi CLI command and effective session id."""
        cfg = self._config
        effective_session_id = resume_session
        cmd = [self._cli, "--print", "--output-format", "stream-json"]
        if cfg.model:
            cmd += ["--model", cfg.model]
        if effective_session_id:
            cmd += ["--resume", effective_session_id]
        elif continue_session:
            cmd.append("--continue")
        if cfg.cli_parameters:
            cmd.extend(cfg.cli_parameters)

        cmd += ["--prompt", self._compose_prompt(prompt)]
        return cmd, effective_session_id

    async def send(
        self,
        prompt: str,
        resume_session: str | None = None,
        continue_session: bool = False,
        timeout_seconds: float | None = None,
        timeout_controller: Any | None = None,
    ) -> CLIResponse:
        """Send a prompt and return the final result."""
        cmd, effective_session_id = self._build_command(
            prompt,
            resume_session=resume_session,
            continue_session=continue_session,
        )
        exec_cmd, use_cwd = docker_wrap(cmd, self._config)
        _log_cmd(exec_cmd)
        return await run_oneshot_subprocess(
            config=self._config,
            spec=SubprocessSpec(exec_cmd, use_cwd, prompt, timeout_seconds, timeout_controller),
            parse_output=partial(_parse_response, fallback_session_id=effective_session_id),
            provider_label="Kimi",
        )

    async def send_streaming(
        self,
        prompt: str,
        resume_session: str | None = None,
        continue_session: bool = False,
        timeout_seconds: float | None = None,
        timeout_controller: Any | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Send a prompt and yield streaming events."""
        cmd, effective_session_id = self._build_command(
            prompt,
            resume_session=resume_session,
            continue_session=continue_session,
        )
        exec_cmd, use_cwd = docker_wrap(cmd, self._config)
        _log_cmd(exec_cmd, streaming=True)

        state = _KimiStreamState(session_id=effective_session_id)

        async def line_handler(line: str) -> AsyncGenerator[StreamEvent, None]:
            for event in parse_kimi_stream_line(line):
                state.track(event)
                yield event

        async def post_handler(result: SubprocessResult) -> AsyncGenerator[StreamEvent, None]:
            yield _kimi_final_result(result, state.accumulated_text, state.session_id)

        async for event in run_streaming_subprocess(
            config=self._config,
            spec=SubprocessSpec(exec_cmd, use_cwd, prompt, timeout_seconds, timeout_controller),
            line_handler=line_handler,
            provider_label="Kimi",
            post_handler=post_handler,
        ):
            yield event


def parse_kimi_stream_line(line: str) -> list[StreamEvent]:
    """Parse one Kimi stream-json line into normalized stream events."""
    stripped = line.strip()
    if not stripped:
        return []
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        logger.debug("Kimi unparseable line: %.200s", stripped)
        return []
    if not isinstance(data, dict):
        return []

    role = data.get("role")
    if role == "assistant":
        events: list[StreamEvent] = []
        text = _extract_text_content(data.get("content"))
        if text:
            events.append(AssistantTextDelta(type="assistant", text=text))
        for tool_call in _iter_tool_calls(data):
            events.append(
                ToolUseEvent(
                    type="assistant",
                    tool_name=tool_call.get("name", ""),
                    tool_id=tool_call.get("id"),
                    parameters=tool_call.get("arguments"),
                )
            )
        return events

    if role == "tool":
        output = _extract_text_content(data.get("content"))
        return [
            ToolResultEvent(
                type="tool",
                tool_id=str(data.get("tool_call_id", "")),
                status="ok",
                output=output,
            )
        ]

    return []


def _iter_tool_calls(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract tool call objects from a Kimi assistant event."""
    raw = data.get("tool_calls")
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        fn = item.get("function")
        if isinstance(fn, dict):
            out.append(
                {
                    "id": str(item.get("id", "")),
                    "name": str(fn.get("name", "")),
                    "arguments": _parse_tool_arguments(fn.get("arguments")),
                }
            )
    return out


def _parse_tool_arguments(value: object) -> dict[str, Any] | None:
    """Normalize tool arguments: parse JSON string into dict if needed."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            logger.debug("Kimi unparseable tool arguments: %.200s", value)
            return None
        if isinstance(parsed, dict):
            return parsed
        return None
    return None


def _extract_text_content(value: object) -> str:
    """Extract readable text from Kimi message content payloads."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        chunks: list[str] = []
        for item in value:
            if isinstance(item, str):
                chunks.append(item)
                continue
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text" and isinstance(item.get("text"), str):
                chunks.append(item["text"])
                continue
            text_val = item.get("content")
            if isinstance(text_val, str):
                chunks.append(text_val)
        return "".join(chunks)
    if isinstance(value, dict):
        for key in ("text", "content", "message", "result"):
            candidate = value.get(key)
            if isinstance(candidate, str):
                return candidate
    return ""


def _parse_response(
    stdout: bytes,
    stderr: bytes,
    returncode: int | None,
    *,
    fallback_session_id: str | None,
) -> CLIResponse:
    """Parse Kimi subprocess output into a CLIResponse."""
    stderr_text = stderr.decode(errors="replace")[:2000] if stderr else ""
    raw = stdout.decode(errors="replace").strip()
    hinted_session_id = _extract_resume_session_id(raw) or _extract_resume_session_id(stderr_text)
    if not raw:
        return CLIResponse(
            session_id=fallback_session_id or hinted_session_id,
            result=stderr_text[:500] if stderr_text else "",
            is_error=True,
            returncode=returncode,
            stderr=stderr_text,
        )

    text_parts: list[str] = []
    discovered_session_id = fallback_session_id or hinted_session_id
    for line in raw.splitlines():
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        if isinstance(data.get("session_id"), str):
            discovered_session_id = data["session_id"]
        if data.get("role") == "assistant":
            text = _extract_text_content(data.get("content"))
            if text:
                text_parts.append(text)

    result_text = "".join(text_parts).strip() or raw[:2000]
    if returncode and returncode != 0 and stderr_text:
        result_text = stderr_text[:500]

    return CLIResponse(
        session_id=discovered_session_id,
        result=result_text,
        is_error=bool(returncode and returncode != 0),
        returncode=returncode,
        stderr=stderr_text,
    )


def _kimi_final_result(
    result: SubprocessResult,
    accumulated_text: list[str],
    session_id: str | None,
) -> ResultEvent:
    """Build final stream ResultEvent for Kimi."""
    stderr_text = result.stderr_bytes.decode(errors="replace")[:2000] if result.stderr_bytes else ""
    discovered_session_id = (
        session_id
        or _extract_resume_session_id(stderr_text)
        or _extract_resume_session_id("\n".join(accumulated_text))
    )
    if result.process.returncode != 0:
        detail = stderr_text or "\n".join(accumulated_text) or "(no output)"
        return ResultEvent(
            type="result",
            session_id=discovered_session_id,
            result=detail[:500],
            is_error=True,
            returncode=result.process.returncode,
        )
    return ResultEvent(
        type="result",
        session_id=discovered_session_id,
        result="".join(accumulated_text),
        is_error=False,
        returncode=result.process.returncode,
    )


def _extract_resume_session_id(text: str) -> str | None:
    """Parse session ID from Kimi resume hint text, if present."""
    match = _RESUME_SESSION_RE.search(text)
    if not match:
        return None
    return match.group(1)


def _log_cmd(cmd: list[str], *, streaming: bool = False) -> None:
    """Log Kimi command with truncated long values."""
    safe_cmd = [(c[:80] + "...") if len(c) > 80 else c for c in cmd]
    prefix = "Kimi stream cmd" if streaming else "Kimi cmd"
    logger.info("%s: %s", prefix, " ".join(safe_cmd))
