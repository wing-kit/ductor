"""Telegram delivery adapter for the MessageBus.

Translates :class:`Envelope` instances into Telegram messages using
the formatting logic that was previously spread across
``bot/result_delivery.py``.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING

from ductor_bot.bot.sender import SendRichOpts, send_rich
from ductor_bot.bus.envelope import Envelope, Origin
from ductor_bot.text.response_format import SEP, fmt

if TYPE_CHECKING:
    from ductor_bot.bot.app import TelegramBot

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Cron result sanitisation (moved from result_delivery)
# ---------------------------------------------------------------------------

_CRON_ACK_MARKERS = ("message sent successfully", "delivered to telegram")


def _is_cron_transport_ack_line(line: str) -> bool:
    normalized = " ".join(line.lower().split())
    return all(marker in normalized for marker in _CRON_ACK_MARKERS)


def _sanitize_cron_result_text(result: str) -> str:
    if not result:
        return ""
    lines = [line for line in result.splitlines() if not _is_cron_transport_ack_line(line)]
    return "\n".join(lines).strip()


# ---------------------------------------------------------------------------
# Transport adapter
# ---------------------------------------------------------------------------


class TelegramTransport:
    """Implements the ``TransportAdapter`` protocol for Telegram delivery."""

    def __init__(self, bot: TelegramBot) -> None:
        self._bot = bot

    # -- Protocol methods ---------------------------------------------------

    async def deliver(self, envelope: Envelope) -> None:
        """Deliver a unicast envelope to the target chat_id."""
        handler = _HANDLERS.get(envelope.origin)
        if handler is not None:
            await handler(self, envelope)
        else:
            logger.warning("No handler for origin=%s", envelope.origin.value)

    async def deliver_broadcast(self, envelope: Envelope) -> None:
        """Deliver an envelope to all allowed users."""
        handler = _BROADCAST_HANDLERS.get(envelope.origin)
        if handler is not None:
            await handler(self, envelope)
        else:
            logger.warning("No broadcast handler for origin=%s", envelope.origin.value)

    # -- Internal helpers ---------------------------------------------------

    def _roots(self) -> list[Path] | None:
        return self._bot.file_roots(self._bot._orch.paths)

    def _opts(self, envelope: Envelope) -> SendRichOpts:
        return SendRichOpts(
            reply_to_message_id=envelope.reply_to_message_id,
            allowed_roots=self._roots(),
            thread_id=envelope.topic_id or envelope.thread_id,
        )

    # -- Origin handlers (unicast) -----------------------------------------

    async def _deliver_background(self, env: Envelope) -> None:
        """Deliver background session / stateless task result."""
        elapsed = f"{env.elapsed_seconds:.0f}s"

        if env.session_name:
            # Update named session registry
            self._bot._orch.named_sessions.update_after_response(
                env.chat_id, env.session_name, env.session_id
            )
            text = self._format_named_session(env, elapsed)
            from ductor_bot.bot.buttons import extract_buttons_for_session

            cleaned, markup = extract_buttons_for_session(text, env.session_name)
            opts = self._opts(env)
            opts.reply_markup = markup
            await send_rich(self._bot.bot_instance, env.chat_id, cleaned, opts)
        else:
            text = self._format_stateless(env, elapsed)
            await send_rich(self._bot.bot_instance, env.chat_id, text, self._opts(env))

    @staticmethod
    def _format_named_session(env: Envelope, elapsed: str) -> str:
        name = env.session_name
        if env.status == "aborted":
            return fmt(f"**[{name}] Cancelled**", SEP, f"_{env.prompt_preview}_")
        if env.is_error:
            body = env.result_text[:2000] if env.result_text else "_No output._"
            return fmt(f"**[{name}] Failed** ({elapsed})", SEP, body)
        return fmt(f"**[{name}] Complete** ({elapsed})", SEP, env.result_text or "_No output._")

    @staticmethod
    def _format_stateless(env: Envelope, elapsed: str) -> str:
        task_id = env.metadata.get("task_id", "?")
        if env.status == "aborted":
            return fmt(
                "**Background Task Cancelled**",
                SEP,
                f"Task `{task_id}` was cancelled.\nPrompt: _{env.prompt_preview}_",
            )
        if env.is_error:
            return fmt(
                f"**Background Task Failed** ({elapsed})",
                SEP,
                f"Task `{task_id}` failed ({env.status}).\n"
                f"Prompt: _{env.prompt_preview}_\n\n"
                + (env.result_text[:2000] if env.result_text else "_No output._"),
            )
        return fmt(
            f"**Background Task Complete** ({elapsed})",
            SEP,
            env.result_text or "_No output._",
        )

    async def _deliver_heartbeat(self, env: Envelope) -> None:
        logger.debug("Heartbeat delivery chars=%d", len(env.result_text))
        await send_rich(
            self._bot.bot_instance,
            env.chat_id,
            env.result_text,
            SendRichOpts(allowed_roots=self._roots()),
        )
        logger.info("Heartbeat delivered")

    async def _deliver_interagent(self, env: Envelope) -> None:
        """Deliver inter-agent result (error notification or injected response)."""
        roots = self._roots()

        if env.is_error:
            session_info = f"\nSession: `{env.session_name}`" if env.session_name else ""
            error_text = (
                f"**Inter-Agent Request Failed**\n\n"
                f"Agent: `{env.metadata.get('recipient', '?')}`{session_info}\n"
                f"Error: {env.metadata.get('error', 'unknown')}\n"
                f"Request: _{env.prompt_preview}_"
            )
            await send_rich(
                self._bot.bot_instance, env.chat_id, error_text, SendRichOpts(allowed_roots=roots)
            )
            return

        # Provider switch notice (before result)
        notice = env.metadata.get("provider_switch_notice", "")
        if notice:
            await send_rich(
                self._bot.bot_instance,
                env.chat_id,
                f"**Provider Switch Detected**\n\n{notice}",
                SendRichOpts(allowed_roots=roots),
            )

        # Result text (filled by bus injection)
        if env.result_text:
            await send_rich(
                self._bot.bot_instance,
                env.chat_id,
                env.result_text,
                SendRichOpts(allowed_roots=roots),
            )

    async def _deliver_task_result(self, env: Envelope) -> None:
        """Deliver task result notification + injected response."""
        opts = self._opts(env)
        name = env.metadata.get("name", env.metadata.get("task_id", "?"))

        # 1. Notification (skip "waiting" — question already shown)
        note = ""
        if env.status == "done":
            duration = f"{env.elapsed_seconds:.0f}s"
            target = f"{env.provider}/{env.model}" if env.provider else ""
            detail = f"{duration}, {target}" if target else duration
            note = f"**Task `{name}` completed** ({detail})"
        elif env.status == "cancelled":
            note = f"**Task `{name}` cancelled**"
        elif env.status == "failed":
            note = f"**Task `{name}` failed**\nReason: {env.metadata.get('error', 'unknown')}"

        if note:
            await send_rich(self._bot.bot_instance, env.chat_id, note, opts)

        # 2. Injected response (filled by bus injection for done/failed)
        if env.needs_injection and env.result_text:
            await send_rich(self._bot.bot_instance, env.chat_id, env.result_text, opts)

    async def _deliver_task_question(self, env: Envelope) -> None:
        """Deliver task question notification + injected agent response."""
        opts = self._opts(env)
        task_id = env.metadata.get("task_id", "?")

        # 1. Notification
        note = f"**Task `{task_id}` has a question:**\n{env.prompt}"
        await send_rich(self._bot.bot_instance, env.chat_id, note, opts)

        # 2. Agent response (filled by bus injection)
        if env.result_text:
            await send_rich(self._bot.bot_instance, env.chat_id, env.result_text, opts)

    async def _deliver_webhook_wake(self, env: Envelope) -> None:
        """Deliver webhook wake result."""
        if env.result_text:
            await send_rich(
                self._bot.bot_instance,
                env.chat_id,
                env.result_text,
                SendRichOpts(allowed_roots=self._roots()),
            )

    # -- Origin handlers (broadcast) ----------------------------------------

    async def _broadcast_cron(self, env: Envelope) -> None:
        title = env.metadata.get("title", "?")
        clean_result = _sanitize_cron_result_text(env.result_text)
        if env.result_text and not clean_result and env.status == "success":
            logger.debug(
                "Cron result only had transport confirmations; skipping broadcast task=%s", title
            )
            return
        text = (
            f"**TASK: {title}**\n\n{clean_result}"
            if clean_result
            else f"**TASK: {title}**\n\n_{env.status}_"
        )
        await self._bot.broadcast(text, SendRichOpts(allowed_roots=self._roots()))

    async def _broadcast_webhook_cron(self, env: Envelope) -> None:
        title = env.metadata.get("hook_title", "?")
        if env.result_text:
            text = f"**WEBHOOK (CRON TASK): {title}**\n\n{env.result_text}"
        else:
            text = f"**WEBHOOK (CRON TASK): {title}**\n\n_{env.status}_"
        await self._bot.broadcast(text, SendRichOpts(allowed_roots=self._roots()))


# ---------------------------------------------------------------------------
# Handler dispatch tables
# ---------------------------------------------------------------------------

_Handler = Callable[[TelegramTransport, Envelope], Awaitable[None]]

_HANDLERS: dict[Origin, _Handler] = {
    Origin.BACKGROUND: TelegramTransport._deliver_background,
    Origin.HEARTBEAT: TelegramTransport._deliver_heartbeat,
    Origin.INTERAGENT: TelegramTransport._deliver_interagent,
    Origin.TASK_RESULT: TelegramTransport._deliver_task_result,
    Origin.TASK_QUESTION: TelegramTransport._deliver_task_question,
    Origin.WEBHOOK_WAKE: TelegramTransport._deliver_webhook_wake,
}

_BROADCAST_HANDLERS: dict[Origin, _Handler] = {
    Origin.CRON: TelegramTransport._broadcast_cron,
    Origin.WEBHOOK_CRON: TelegramTransport._broadcast_webhook_cron,
}
