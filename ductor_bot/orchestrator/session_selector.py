"""Interactive session selector for viewing and managing named sessions."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from ductor_bot.orchestrator.selector_utils import format_age
from ductor_bot.text.response_format import SEP, fmt

if TYPE_CHECKING:
    from ductor_bot.orchestrator.core import Orchestrator

logger = logging.getLogger(__name__)

NSC_PREFIX = "nsc:"


def is_session_selector_callback(data: str) -> bool:
    """Return True if *data* belongs to the session selector."""
    return data.startswith(NSC_PREFIX)


async def session_selector_start(
    orch: Orchestrator,
    chat_id: int,
) -> tuple[str, InlineKeyboardMarkup | None]:
    """Build the initial ``/sessions`` response with inline controls."""
    return _build_page(orch, chat_id)


async def handle_session_callback(
    orch: Orchestrator,
    chat_id: int,
    data: str,
) -> tuple[str, InlineKeyboardMarkup | None]:
    """Route a ``nsc:*`` callback to the correct session selector action."""
    logger.debug("Session selector step=%s", data[:40])
    action = data[len(NSC_PREFIX) :]

    if action == "r":
        return _build_page(orch, chat_id)

    if action == "endall":
        count = orch._named_sessions.end_all(chat_id)
        note = f"All {count} session(s) ended." if count else "No active sessions."
        return _build_page(orch, chat_id, note=note)

    if action.startswith("end:"):
        name = action[4:]
        ended = await orch.end_named_session(chat_id, name)
        note = f"Session '{name}' ended." if ended else f"Session '{name}' not found."
        return _build_page(orch, chat_id, note=note)

    logger.warning("Unknown session selector callback: %s", data)
    return _build_page(orch, chat_id, note="Unknown action.")


def _build_page(
    orch: Orchestrator,
    chat_id: int,
    *,
    note: str = "",
) -> tuple[str, InlineKeyboardMarkup | None]:
    sessions = orch.list_named_sessions(chat_id)
    if not sessions:
        body = "No active sessions."
        if note:
            body = f"{note}\n\n{body}"
        return (
            fmt(
                "**Sessions**",
                SEP,
                body,
                SEP,
                "Start one with `/session <prompt>`.",
            ),
            None,
        )

    lines: list[str] = []
    rows: list[list[InlineKeyboardButton]] = []
    now = time.time()
    for idx, ns in enumerate(sessions, 1):
        status_label = ns.status
        age_seconds = now - ns.created_at
        age = format_age(age_seconds)
        provider_label = ns.provider
        msgs = f"{ns.message_count} msg" if ns.message_count == 1 else f"{ns.message_count} msgs"
        lines.append(
            f"{idx}. **{ns.name}** | {provider_label}/{ns.model} | {status_label} ({msgs}, {age})"
        )
        lines.append(f"   > _{ns.prompt_preview}_")
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"End {ns.name}",
                    callback_data=f"nsc:end:{ns.name}",
                ),
            ]
        )

    nav_row: list[InlineKeyboardButton] = [
        InlineKeyboardButton(text="Refresh", callback_data="nsc:r"),
    ]
    rows.append(nav_row)
    if len(sessions) > 1:
        rows.append([InlineKeyboardButton(text="End All", callback_data="nsc:endall")])

    info_lines: list[str] = [f"Active: {len(sessions)}"]
    if note:
        info_lines.append(note)

    text = fmt(
        "**Sessions**",
        SEP,
        "\n".join(lines),
        SEP,
        "\n".join(info_lines),
        "Follow up: `@<name> <message>`",
    )
    return text, InlineKeyboardMarkup(inline_keyboard=rows)
