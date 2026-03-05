"""Matrix typing indicator context manager."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nio import AsyncClient
    from types import TracebackType


class MatrixTypingContext:
    """Context manager that shows typing indicator in a Matrix room."""

    def __init__(self, client: AsyncClient, room_id: str) -> None:
        self._client = client
        self._room_id = room_id

    async def __aenter__(self) -> MatrixTypingContext:
        with contextlib.suppress(Exception):
            await self._client.room_typing(
                self._room_id, typing_state=True, timeout=30000
            )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        with contextlib.suppress(Exception):
            await self._client.room_typing(self._room_id, typing_state=False)
