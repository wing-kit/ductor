"""Streaming via Matrix message replacement (m.replace).

Matrix supports editing messages by sending a new event that references
the original via ``m.relates_to`` with ``rel_type: m.replace``.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from ductor_bot.matrix.formatting import markdown_to_matrix_html

if TYPE_CHECKING:
    from nio import AsyncClient

import logging

logger = logging.getLogger(__name__)


class MatrixStreamEditor:
    """Streaming via Matrix message replacement (m.replace)."""

    def __init__(
        self,
        client: AsyncClient,
        room_id: str,
        *,
        min_edit_interval: float = 2.0,
    ) -> None:
        self._client = client
        self._room_id = room_id
        self._event_id: str | None = None
        self._accumulated = ""
        self._min_interval = min_edit_interval
        self._last_edit: float = 0

    @property
    def event_id(self) -> str | None:
        """The event_id of the message being streamed."""
        return self._event_id

    async def append_text(self, delta: str) -> None:
        """Append text and update the message (rate-limited)."""
        self._accumulated += delta
        now = time.time()

        # Rate limit: don't edit faster than min_interval
        if self._event_id and (now - self._last_edit) < self._min_interval:
            return

        plain, html_body = markdown_to_matrix_html(self._accumulated)

        if self._event_id is None:
            # Send initial message
            resp = await self._client.room_send(
                self._room_id,
                "m.room.message",
                {
                    "msgtype": "m.text",
                    "body": plain,
                    "format": "org.matrix.custom.html",
                    "formatted_body": html_body,
                },
            )
            if hasattr(resp, "event_id"):
                self._event_id = resp.event_id
        else:
            # Edit existing message
            await self._client.room_send(
                self._room_id,
                "m.room.message",
                {
                    "msgtype": "m.text",
                    "body": f"* {plain}",
                    "format": "org.matrix.custom.html",
                    "formatted_body": html_body,
                    "m.new_content": {
                        "msgtype": "m.text",
                        "body": plain,
                        "format": "org.matrix.custom.html",
                        "formatted_body": html_body,
                    },
                    "m.relates_to": {
                        "rel_type": "m.replace",
                        "event_id": self._event_id,
                    },
                },
            )

        self._last_edit = now

    async def finalize(self, full_text: str) -> None:
        """Final edit with complete content."""
        self._accumulated = full_text
        self._last_edit = 0  # force immediate edit
        await self.append_text("")
