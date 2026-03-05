"""Matrix transport bot, parallel to TelegramBot.

Implements BotProtocol so the supervisor can manage it identically
to TelegramBot without knowing which transport is active.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING

from ductor_bot.bus.bus import MessageBus
from ductor_bot.bus.lock_pool import LockPool
from ductor_bot.config import AgentConfig
from ductor_bot.files.allowed_roots import resolve_allowed_roots
from ductor_bot.matrix.buttons import ButtonTracker
from ductor_bot.matrix.credentials import login_or_restore
from ductor_bot.matrix.id_map import MatrixIdMap
from ductor_bot.matrix.sender import send_rich as matrix_send_rich
from ductor_bot.matrix.streaming import MatrixStreamEditor
from ductor_bot.matrix.typing import MatrixTypingContext
from ductor_bot.notifications import NotificationService
from ductor_bot.session.key import SessionKey

if TYPE_CHECKING:
    from nio import AsyncClient

    from ductor_bot.infra.updater import UpdateObserver
    from ductor_bot.multiagent.bus import AsyncInterAgentResult
    from ductor_bot.orchestrator.core import Orchestrator
    from ductor_bot.tasks.models import TaskResult
    from ductor_bot.workspace.paths import DuctorPaths

logger = logging.getLogger(__name__)


class MatrixNotificationService:
    """NotificationService implementation for Matrix."""

    def __init__(self, bot: MatrixBot) -> None:
        self._bot = bot

    async def notify(self, chat_id: int, text: str) -> None:
        room_id = self._bot.id_map.int_to_room(chat_id)
        if room_id:
            await matrix_send_rich(self._bot.client, room_id, text)

    async def notify_all(self, text: str) -> None:
        for room_id in self._bot.config.matrix.allowed_rooms:
            await matrix_send_rich(self._bot.client, room_id, text)


class MatrixBot:
    """Matrix transport bot implementing BotProtocol."""

    def __init__(self, config: AgentConfig, *, agent_name: str = "main") -> None:
        try:
            from nio import AsyncClient
        except ImportError:
            raise ImportError(
                "matrix-nio is required for Matrix transport. "
                "Install with: pip install 'ductor[matrix]'"
            ) from None

        self._config = config
        self._agent_name = agent_name
        mx = config.matrix
        self._store_path = Path(config.ductor_home).expanduser() / mx.store_path
        self._store_path.mkdir(parents=True, exist_ok=True)

        self._client = AsyncClient(mx.homeserver, mx.user_id)
        self._id_map = MatrixIdMap(self._store_path)
        self._button_tracker = ButtonTracker()
        self._lock_pool = LockPool()
        self._bus = MessageBus(lock_pool=self._lock_pool)

        from ductor_bot.matrix.transport import MatrixTransport

        self._bus.register_transport(MatrixTransport(self))

        self._orchestrator: Orchestrator | None = None
        self._startup_hooks: list[Callable[[], Awaitable[None]]] = []
        self._notification_service: NotificationService = MatrixNotificationService(self)
        self._abort_all_callback: Callable[[], Awaitable[int]] | None = None
        self._exit_code: int = 0
        self._update_observer: UpdateObserver | None = None
        self._restart_watcher: asyncio.Task[None] | None = None
        self._sync_task: asyncio.Task[None] | None = None

        # Pre-compute allowed rooms set (resolve aliases later if needed)
        self._allowed_rooms_set: set[str] = set(mx.allowed_rooms)

    # --- BotProtocol implementation ---

    @property
    def _orch(self) -> Orchestrator:
        if self._orchestrator is None:
            msg = "Orchestrator not initialized -- call after startup"
            raise RuntimeError(msg)
        return self._orchestrator

    @property
    def orchestrator(self) -> Orchestrator | None:
        return self._orchestrator

    @property
    def config(self) -> AgentConfig:
        return self._config

    @property
    def notification_service(self) -> NotificationService:
        return self._notification_service

    @property
    def client(self) -> AsyncClient:
        """The nio AsyncClient instance."""
        return self._client

    @property
    def id_map(self) -> MatrixIdMap:
        return self._id_map

    def register_startup_hook(self, hook: Callable[[], Awaitable[None]]) -> None:
        self._startup_hooks.append(hook)

    def set_abort_all_callback(self, callback: Callable[[], Awaitable[int]]) -> None:
        self._abort_all_callback = callback

    def file_roots(self, paths: DuctorPaths) -> list[Path] | None:
        return resolve_allowed_roots(self._config.file_access, paths.workspace)

    async def run(self) -> int:
        """Login, sync, run event loop."""
        from nio import InviteMemberEvent, RoomMessageText

        await login_or_restore(self._client, self._config.matrix, self._store_path)

        # Restore sync token (Risk R2 mitigation)
        self._restore_sync_token()

        # Register event callbacks
        self._client.add_event_callback(self._on_message, RoomMessageText)
        self._client.add_event_callback(self._on_invite, InviteMemberEvent)

        # Run startup (orchestrator, observers, hooks)
        from ductor_bot.matrix.startup import run_matrix_startup

        await run_matrix_startup(self)

        # Start restart marker watcher
        self._restart_watcher = asyncio.create_task(self._watch_restart_marker())

        # Sync loop (blocks) — wrap in task so restart watcher can cancel it
        self._exit_code = 0
        self._sync_task = asyncio.current_task()
        try:
            await self._client.sync_forever(timeout=30000, full_state=True)
        except asyncio.CancelledError:
            pass

        return self._exit_code

    async def shutdown(self) -> None:
        """Gracefully shut down."""
        if self._restart_watcher:
            self._restart_watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._restart_watcher

        if self._update_observer:
            self._update_observer.stop()

        self._save_sync_token()
        await self._client.close()

        if self._orchestrator:
            await self._orchestrator.shutdown()

        logger.info("MatrixBot shut down")

    # --- Message handling ---

    async def _on_message(self, room: object, event: object) -> None:
        """Handle incoming room messages."""
        # Import here to avoid import errors when nio not installed
        from nio import MatrixRoom, RoomMessageText

        if not isinstance(room, MatrixRoom) or not isinstance(event, RoomMessageText):
            return

        if event.sender == self._client.user_id:
            return  # Ignore own messages

        if not self._is_authorized(room, event):
            return

        text = event.body.strip()
        if not text:
            return

        room_id = room.room_id
        chat_id = self._id_map.room_to_int(room_id)

        # Check button match
        button_match = self._button_tracker.match_input(room_id, text)
        if button_match:
            text = button_match

        # Handle commands
        if text.startswith("/"):
            await self._handle_command(text, room_id, chat_id, event)
            return

        key = SessionKey(chat_id=chat_id, topic_id=None)

        # Dispatch
        if self._config.streaming.enabled:
            await self._run_streaming(key, text, room_id, event)
        else:
            await self._run_non_streaming(key, text, room_id, event)

    async def _handle_command(
        self, text: str, room_id: str, chat_id: int, event: object
    ) -> None:
        """Handle slash commands in Matrix."""
        from ductor_bot.infra.restart import mark_restart_requested, EXIT_RESTART

        cmd = text.split()[0].lower().lstrip("/")

        if cmd == "stop":
            orch = self._orchestrator
            if orch:
                killed = await orch.process_registry.kill_all_active()
                msg = f"Stopped {killed} process(es)." if killed else "No active processes."
                await matrix_send_rich(self._client, room_id, msg)
            return

        if cmd == "stop_all":
            if self._abort_all_callback:
                killed = await self._abort_all_callback()
                msg = f"Stopped {killed} process(es) across all agents." if killed else "No active processes."
            else:
                msg = "Multi-agent abort not available (single-agent mode)."
            await matrix_send_rich(self._client, room_id, msg)
            return

        if cmd == "restart":
            mark_restart_requested(
                Path(self._config.ductor_home).expanduser(), reason="user /restart"
            )
            self._exit_code = EXIT_RESTART
            # Stop sync loop
            raise asyncio.CancelledError

        if cmd == "new":
            orch = self._orchestrator
            if orch:
                orch.end_session(SessionKey(chat_id=chat_id, topic_id=None))
                await matrix_send_rich(self._client, room_id, "Session reset.")
            return

        if cmd in ("help", "start"):
            await matrix_send_rich(
                self._client,
                room_id,
                "**Ductor Matrix Bot**\n\n"
                "Send any message to start.\n\n"
                "Commands: `/new`, `/stop`, `/restart`, `/help`",
            )
            return

        # Unknown command — treat as regular message
        key = SessionKey(chat_id=chat_id, topic_id=None)
        if self._config.streaming.enabled:
            await self._run_streaming(key, text, room_id, event)
        else:
            await self._run_non_streaming(key, text, room_id, event)

    async def _run_streaming(
        self, key: SessionKey, text: str, room_id: str, event: object
    ) -> None:
        """Run with streaming edits."""
        orch = self._orchestrator
        if orch is None:
            return

        editor = MatrixStreamEditor(
            self._client,
            room_id,
            min_edit_interval=self._config.streaming.edit_interval_seconds,
        )

        async with MatrixTypingContext(self._client, room_id):
            async def _on_delta(delta: str) -> None:
                await editor.append_text(delta)

            response = await orch.run_message(key, text, stream_callback=_on_delta)
            if response:
                # Format with buttons
                formatted = self._button_tracker.extract_and_format(room_id, response)
                await editor.finalize(formatted)

    async def _run_non_streaming(
        self, key: SessionKey, text: str, room_id: str, event: object
    ) -> None:
        """Run without streaming."""
        orch = self._orchestrator
        if orch is None:
            return

        async with MatrixTypingContext(self._client, room_id):
            response = await orch.run_message(key, text)

        if response:
            formatted = self._button_tracker.extract_and_format(room_id, response)
            await matrix_send_rich(self._client, room_id, formatted)

    def _is_authorized(self, room: object, event: object) -> bool:
        """Check if the sender/room is authorized."""
        mx = self._config.matrix
        room_id = getattr(room, "room_id", "")
        sender = getattr(event, "sender", "")

        room_ok = not mx.allowed_rooms or room_id in self._allowed_rooms_set
        user_ok = not mx.allowed_users or sender in mx.allowed_users
        return room_ok and user_ok

    # --- Room invite handling ---

    async def _on_invite(self, room: object, event: object) -> None:
        """Auto-join if room is in allowed_rooms."""
        room_id = getattr(room, "room_id", "")
        if room_id in self._allowed_rooms_set:
            await self._client.join(room_id)
            logger.info("Auto-joined allowed room: %s", room_id)

    # --- Sync token persistence ---

    def _restore_sync_token(self) -> None:
        token_file = self._store_path / "next_batch"
        if token_file.exists():
            self._client.next_batch = token_file.read_text(encoding="utf-8").strip()

    def _save_sync_token(self) -> None:
        if self._client.next_batch:
            token_file = self._store_path / "next_batch"
            token_file.parent.mkdir(parents=True, exist_ok=True)
            token_file.write_text(self._client.next_batch, encoding="utf-8")

    # --- Inter-agent & task handlers (BotProtocol) ---

    async def on_async_interagent_result(self, result: AsyncInterAgentResult) -> None:
        from ductor_bot.bus.adapters import from_interagent_result

        chat_id = self._default_chat_id()
        await self._bus.submit(from_interagent_result(result, chat_id))

    async def on_task_result(self, result: TaskResult) -> None:
        from ductor_bot.bus.adapters import from_task_result

        await self._bus.submit(from_task_result(result))

    async def on_task_question(
        self,
        task_id: str,
        question: str,
        prompt_preview: str,
        chat_id: int,
        thread_id: int | None = None,
    ) -> None:
        from ductor_bot.bus.adapters import from_task_question

        if not chat_id:
            chat_id = self._default_chat_id()
        await self._bus.submit(from_task_question(task_id, question, prompt_preview, chat_id))

    def _default_chat_id(self) -> int:
        """First allowed room as default delivery target."""
        if self._config.matrix.allowed_rooms:
            return self._id_map.room_to_int(self._config.matrix.allowed_rooms[0])
        return 0

    # --- Restart watcher ---

    async def _watch_restart_marker(self) -> None:
        """Watch for restart marker file (created by /restart command)."""
        from ductor_bot.infra.restart import EXIT_RESTART

        marker = Path(self._config.ductor_home).expanduser() / ".restart_requested"
        while True:
            await asyncio.sleep(2)
            if marker.exists():
                logger.info("Restart marker detected")
                self._exit_code = EXIT_RESTART
                if self._sync_task and not self._sync_task.done():
                    self._sync_task.cancel()
                break

    async def broadcast(self, text: str) -> None:
        """Send a message to all allowed rooms."""
        for room_id in self._config.matrix.allowed_rooms:
            await matrix_send_rich(self._client, room_id, text)
