"""Transport-agnostic notification delivery protocol."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class NotificationService(Protocol):
    """Transport-agnostic notification delivery.

    Implemented by both TelegramNotificationService and
    MatrixNotificationService so the supervisor and bus can send
    notifications without knowing which transport is active.
    """

    async def notify(self, chat_id: int, text: str) -> None:
        """Send a notification to a specific chat/room."""
        ...

    async def notify_all(self, text: str) -> None:
        """Send a notification to all authorized users/rooms."""
        ...
