"""Command handlers for all slash commands."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from ductor_bot.cli.auth import check_all_auth
from ductor_bot.infra.version import check_pypi, get_current_version
from ductor_bot.orchestrator.cron_selector import cron_selector_start
from ductor_bot.orchestrator.model_selector import model_selector_start, switch_model
from ductor_bot.orchestrator.registry import OrchestratorResult
from ductor_bot.orchestrator.session_selector import session_selector_start
from ductor_bot.orchestrator.task_selector import task_selector_start
from ductor_bot.text.response_format import SEP, fmt, new_session_text
from ductor_bot.workspace.loader import read_mainmemory

if TYPE_CHECKING:
    from ductor_bot.orchestrator.core import Orchestrator

logger = logging.getLogger(__name__)


# -- Command wrappers (registered by Orchestrator._register_commands) --


async def cmd_reset(orch: Orchestrator, chat_id: int, _text: str) -> OrchestratorResult:
    """Handle /new: kill processes and reset only active provider session."""
    logger.info("Reset requested")
    await orch._process_registry.kill_all(chat_id)
    provider = await orch.reset_active_provider_session(chat_id)
    return OrchestratorResult(text=new_session_text(provider))


async def cmd_status(orch: Orchestrator, chat_id: int, _text: str) -> OrchestratorResult:
    """Handle /status."""
    logger.info("Status requested")
    return OrchestratorResult(text=await _build_status(orch, chat_id))


async def cmd_model(orch: Orchestrator, chat_id: int, text: str) -> OrchestratorResult:
    """Handle /model [name]."""
    logger.info("Model requested")
    parts = text.split(None, 1)
    if len(parts) < 2:
        msg_text, keyboard = await model_selector_start(orch, chat_id)
        return OrchestratorResult(text=msg_text, reply_markup=keyboard)
    name = parts[1].strip()
    result_text = await switch_model(orch, chat_id, name)
    return OrchestratorResult(text=result_text)


async def cmd_memory(orch: Orchestrator, _chat_id: int, _text: str) -> OrchestratorResult:
    """Handle /memory."""
    logger.info("Memory requested")
    content = await asyncio.to_thread(read_mainmemory, orch.paths)
    if not content.strip():
        return OrchestratorResult(
            text=fmt(
                "**Main Memory**",
                SEP,
                "Empty. The agent will build memory as you interact.",
                SEP,
                '*Tip: Ask your agent to "remember" something to get started.*',
            ),
        )
    return OrchestratorResult(
        text=fmt(
            "**Main Memory**",
            SEP,
            content,
            SEP,
            "*Tip: The agent reads and updates this automatically.*",
        ),
    )


async def cmd_sessions(orch: Orchestrator, chat_id: int, _text: str) -> OrchestratorResult:
    """Handle /sessions."""
    logger.info("Sessions requested")
    text, keyboard = await session_selector_start(orch, chat_id)
    return OrchestratorResult(text=text, reply_markup=keyboard)


async def cmd_tasks(orch: Orchestrator, chat_id: int, _text: str) -> OrchestratorResult:
    """Handle /tasks."""
    logger.info("Tasks requested")
    hub = orch.task_hub
    if hub is None:
        return OrchestratorResult(
            text=fmt("**Background Tasks**", SEP, "Task system is not enabled."),
        )
    text, keyboard = task_selector_start(hub, chat_id)
    return OrchestratorResult(text=text, reply_markup=keyboard)


async def cmd_cron(orch: Orchestrator, _chat_id: int, _text: str) -> OrchestratorResult:
    """Handle /cron."""
    logger.info("Cron requested")
    text, keyboard = await cron_selector_start(orch)
    return OrchestratorResult(text=text, reply_markup=keyboard)


async def cmd_upgrade(_orch: Orchestrator, _chat_id: int, _text: str) -> OrchestratorResult:
    """Handle /upgrade: check for updates and offer upgrade."""
    logger.info("Upgrade check requested")

    from ductor_bot.infra.install import detect_install_mode

    if detect_install_mode() == "dev":
        return OrchestratorResult(
            text=fmt(
                "**Running From Source**",
                SEP,
                "Self-upgrade is not available for development installs.\n"
                "Update with `git pull` in your project directory.",
            ),
        )

    info = await check_pypi(fresh=True)

    if info is None:
        return OrchestratorResult(
            text="Could not reach PyPI to check for updates. Try again later.",
        )

    if not info.update_available:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=f"Changelog v{info.current}",
                        callback_data=f"upg:cl:{info.current}",
                    ),
                ],
            ],
        )
        return OrchestratorResult(
            text=fmt(
                "**Already Up to Date**",
                SEP,
                f"Installed: `{info.current}`\n"
                f"Latest:    `{info.latest}`\n\n"
                "You're running the latest version.",
            ),
            reply_markup=keyboard,
        )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"Changelog v{info.latest}",
                    callback_data=f"upg:cl:{info.latest}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Yes, upgrade now", callback_data=f"upg:yes:{info.latest}"
                ),
                InlineKeyboardButton(text="Not now", callback_data="upg:no"),
            ],
        ],
    )

    return OrchestratorResult(
        text=fmt(
            "**Update Available**",
            SEP,
            f"Installed: `{info.current}`\nNew:       `{info.latest}`\n\nUpgrade now?",
        ),
        reply_markup=keyboard,
    )


def _build_codex_cache_block(orch: Orchestrator) -> str:
    """Build the Codex model cache section for /diagnose."""
    if not orch._observers.codex_cache_obs:
        return "\n🔄 Codex Model Cache: Observer not initialized"
    cache = orch._observers.codex_cache_obs.get_cache()
    if not cache or not cache.models:
        return "\n🔄 Codex Model Cache: Not loaded"
    default_model = next((m.id for m in cache.models if m.is_default), "N/A")
    return (
        f"\n🔄 Codex Model Cache:\n"
        f"  Last updated: {cache.last_updated}\n"
        f"  Models cached: {len(cache.models)}\n"
        f"  Default model: {default_model}"
    )


def _build_diagnose_health_block(orch: Orchestrator) -> str:
    """Build the multi-agent health section for /diagnose."""
    supervisor = orch._supervisor
    if supervisor is None:
        return ""
    status_icon = {"running": "●", "starting": "◐", "crashed": "✖", "stopped": "○"}
    agent_lines = ["\n**Multi-Agent Health:**"]
    for name in sorted(supervisor.health.keys()):
        h = supervisor.health[name]
        icon = status_icon.get(h.status, "?")
        role = "main" if name == "main" else "sub"
        line = f"  {icon} `{name}` [{role}] — {h.status}"
        if h.status == "running" and h.uptime_human:
            line += f" ({h.uptime_human})"
        if h.restart_count > 0:
            line += f" | restarts: {h.restart_count}"
        if h.status == "crashed" and h.last_crash_error:
            line += f"\n      `{h.last_crash_error[:100]}`"
        agent_lines.append(line)
    return "\n".join(agent_lines)


def _resolve_log_path(orch: Orchestrator) -> Path:
    """Return the best available log file path.

    Sub-agents don't have their own log files — fall back to the central
    log in the main ductor home (parent of ``agents/<name>``).
    """
    log_path = orch.paths.logs_dir / "agent.log"
    if not log_path.exists():
        main_logs = orch.paths.ductor_home.parent.parent / "logs" / "agent.log"
        if main_logs.exists():
            return main_logs
    return log_path


async def cmd_diagnose(orch: Orchestrator, _chat_id: int, _text: str) -> OrchestratorResult:
    """Handle /diagnose."""
    logger.info("Diagnose requested")
    version = get_current_version()
    effective_model, effective_provider = orch.resolve_runtime_target(orch._config.model)
    info_block = (
        f"Version: `{version}`\n"
        f"Configured: {orch._config.provider} / {orch._config.model}\n"
        f"Effective runtime: {effective_provider} / {effective_model}"
    )

    cache_block = _build_codex_cache_block(orch)
    agent_block = _build_diagnose_health_block(orch)

    log_tail = await _read_log_tail(_resolve_log_path(orch))
    log_block = (
        f"Recent logs (last 50 lines):\n```\n{log_tail}\n```" if log_tail else "No log file found."
    )

    return OrchestratorResult(
        text=fmt(
            "**System Diagnostics**", SEP, info_block, cache_block, agent_block, SEP, log_block
        ),
    )


# -- Helpers ------------------------------------------------------------------


def _build_agent_health_block(orch: Orchestrator) -> str:
    """Build the multi-agent health section for /status (main agent only)."""
    supervisor = orch._supervisor
    if supervisor is None or len(supervisor.health) <= 1:
        return ""

    status_icon = {
        "running": "●",
        "starting": "◐",
        "crashed": "✖",
        "stopped": "○",
    }
    agent_lines = ["Agents:"]
    for name in sorted(supervisor.health.keys()):
        if name == "main":
            continue
        h = supervisor.health[name]
        icon = status_icon.get(h.status, "?")
        line = f"  {icon} {name} — {h.status}"
        if h.status == "running" and h.uptime_human:
            line += f" ({h.uptime_human})"
        if h.restart_count > 0:
            line += f" ⟳{h.restart_count}"
        if h.status == "crashed" and h.last_crash_error:
            line += f"\n      {h.last_crash_error[:80]}"
        agent_lines.append(line)
    return "\n".join(agent_lines)


async def _build_status(orch: Orchestrator, chat_id: int) -> str:
    """Build the /status response text."""
    runtime_model, _runtime_provider = orch.resolve_runtime_target(orch._config.model)
    configured_model = orch._config.model

    def _model_line(model_name: str) -> str:
        if model_name == configured_model:
            return f"Model: {model_name}"
        return f"Model: {model_name} (configured: {configured_model})"

    session = await orch._sessions.get_active(chat_id)
    if session:
        session_block = (
            f"Session: `{session.session_id[:8]}...`\n"
            f"Messages: {session.message_count}\n"
            f"Tokens: {session.total_tokens:,}\n"
            f"Cost: ${session.total_cost_usd:.4f}\n"
            f"{_model_line(session.model)}"
        )
    else:
        session_block = f"No active session.\n{_model_line(runtime_model)}"

    bg_tasks = orch.active_background_tasks(chat_id)
    bg_block = ""
    if bg_tasks:
        import time

        bg_lines = [f"Background tasks: {len(bg_tasks)} running"]
        for t in bg_tasks:
            age = time.monotonic() - t.submitted_at
            bg_lines.append(f"  `{t.task_id}` {t.prompt[:40]}... ({age:.0f}s)")
        bg_block = "\n".join(bg_lines)

    auth = await asyncio.to_thread(check_all_auth)
    auth_lines: list[str] = []
    for provider, result in auth.items():
        age_label = f" ({result.age_human})" if result.age_human else ""
        auth_lines.append(f"  [{provider}] {result.status.value}{age_label}")
    auth_block = "Auth:\n" + "\n".join(auth_lines)

    agent_block = _build_agent_health_block(orch)

    blocks = ["**Status**", SEP, session_block]
    if bg_block:
        blocks += [SEP, bg_block]
    blocks += [SEP, auth_block]
    if agent_block:
        blocks += [SEP, agent_block]
    return fmt(*blocks)


async def _read_log_tail(log_path: Path, lines: int = 50) -> str:
    """Read the last *lines* of a log file without blocking the event loop."""

    def _read() -> str:
        if not log_path.is_file():
            return ""
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
            return "\n".join(text.strip().splitlines()[-lines:])
        except OSError:
            return "(could not read log file)"

    return await asyncio.to_thread(_read)
