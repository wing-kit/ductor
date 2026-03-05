"""Matrix-specific startup sequence.

Reuses orchestrator creation from the core but skips Telegram-specific
parts (bot username lookup, command registration, group audit).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from ductor_bot.infra.restart import consume_restart_marker

if TYPE_CHECKING:
    from ductor_bot.matrix.bot import MatrixBot

logger = logging.getLogger(__name__)


async def run_matrix_startup(bot: MatrixBot) -> None:
    """Matrix-specific startup: orchestrator, observers, recovery."""
    from ductor_bot.orchestrator.core import Orchestrator

    bot._orchestrator = await Orchestrator.create(bot._config, agent_name=bot._agent_name)

    # Wire message bus
    bot._bus.set_injector(bot._orchestrator)

    # Wire observers to bus (cron, heartbeat, webhooks, background)
    _wire_observers(bot)

    # Handle restart sentinel
    restart_reason = _consume_restart_sentinel(bot)

    # Notify restart
    if restart_reason:
        await bot.notification_service.notify_all(
            f"**Bot restarted** ({restart_reason})"
        )

    # Update checker
    try:
        from ductor_bot.infra.updater import UpdateObserver, is_upgradeable

        if is_upgradeable() and bot._config.update_check:
            async def _on_update(version: str) -> None:
                await bot.notification_service.notify_all(
                    f"**Update available:** `{version}`\n"
                    "Use `/upgrade` to update."
                )

            bot._update_observer = UpdateObserver(notify=_on_update)
            bot._update_observer.start()
    except ImportError:
        pass

    logger.info(
        "Matrix bot online: %s on %s",
        bot._config.matrix.user_id,
        bot._config.matrix.homeserver,
    )

    # Run registered startup hooks (supervisor injection)
    for hook in bot._startup_hooks:
        await hook()


def _wire_observers(bot: MatrixBot) -> None:
    """Wire orchestrator observers to the message bus."""
    orch = bot._orchestrator
    if orch is None:
        return

    # Background observer
    if orch.bg_observer:
        from ductor_bot.bus.adapters import from_background_result

        async def _on_bg_result(result: object) -> None:
            await bot._bus.submit(from_background_result(result))

        orch.bg_observer.set_result_callback(_on_bg_result)

    # Cron observer
    if orch.cron_observer:
        from ductor_bot.bus.adapters import from_cron_result

        async def _on_cron_result(title: str, result: str, status: str) -> None:
            await bot._bus.submit(from_cron_result(title, result, status))

        orch.cron_observer.set_result_callback(_on_cron_result)

    # Heartbeat observer
    if orch.heartbeat_observer:
        from ductor_bot.bus.adapters import from_heartbeat

        async def _on_heartbeat(text: str) -> None:
            chat_id = bot._default_chat_id()
            await bot._bus.submit(from_heartbeat(chat_id, text))

        orch.heartbeat_observer.set_result_callback(_on_heartbeat)

    # Webhook observer
    if orch.webhook_observer:
        from ductor_bot.bus.adapters import from_webhook_cron_result, from_webhook_wake

        async def _on_webhook_cron(result: object) -> None:
            await bot._bus.submit(from_webhook_cron_result(result))

        async def _on_webhook_wake(prompt: str) -> None:
            chat_id = bot._default_chat_id()
            await bot._bus.submit(from_webhook_wake(chat_id, prompt))

        orch.webhook_observer.set_cron_callback(_on_webhook_cron)
        orch.webhook_observer.set_wake_callback(_on_webhook_wake)


def _consume_restart_sentinel(bot: MatrixBot) -> str:
    """Check and consume restart marker."""
    paths_obj = bot._orchestrator.paths if bot._orchestrator else None
    if paths_obj is None:
        return ""
    marker = consume_restart_marker(paths_obj.ductor_home)
    return marker or ""
