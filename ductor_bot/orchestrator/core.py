"""Core orchestrator: routes messages through command and conversation flows."""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ductor_bot.background import (
    BackgroundResult,
    BackgroundSubmit,
    BackgroundTask,
)
from ductor_bot.cli.process_registry import ProcessRegistry
from ductor_bot.cli.service import CLIService, CLIServiceConfig
from ductor_bot.config import (
    _GEMINI_ALIASES,
    CLAUDE_MODELS,
    AgentConfig,
    ModelRegistry,
    get_gemini_models,
    set_gemini_models,
)
from ductor_bot.cron.manager import CronManager
from ductor_bot.errors import (
    CLIError,
    CronError,
    SessionError,
    StreamError,
    WebhookError,
    WorkspaceError,
)
from ductor_bot.files.allowed_roots import resolve_allowed_roots
from ductor_bot.infra.docker import DockerManager
from ductor_bot.infra.inflight import InflightTracker
from ductor_bot.orchestrator.commands import (
    cmd_cron,
    cmd_diagnose,
    cmd_memory,
    cmd_model,
    cmd_reset,
    cmd_sessions,
    cmd_status,
    cmd_tasks,
    cmd_upgrade,
)
from ductor_bot.orchestrator.directives import parse_directives
from ductor_bot.orchestrator.flows import (
    StreamingCallbacks,
    heartbeat_flow,
    named_session_flow,
    named_session_streaming,
    normal,
    normal_streaming,
)
from ductor_bot.orchestrator.hooks import (
    DELEGATION_BRIEF,
    DELEGATION_REMINDER,
    MAINMEMORY_REMINDER,
    MessageHookRegistry,
)
from ductor_bot.orchestrator.observers import ObserverManager
from ductor_bot.orchestrator.registry import CommandRegistry, OrchestratorResult
from ductor_bot.security import detect_suspicious_patterns
from ductor_bot.session import SessionManager
from ductor_bot.session.named import NamedSessionRegistry
from ductor_bot.webhook.manager import WebhookManager
from ductor_bot.webhook.models import WebhookResult
from ductor_bot.workspace.init import inject_runtime_environment
from ductor_bot.workspace.paths import DuctorPaths, resolve_paths
from ductor_bot.workspace.skill_sync import (
    cleanup_ductor_links,
    sync_bundled_skills,
    sync_skills,
)

if TYPE_CHECKING:
    from ductor_bot.background import BackgroundObserver
    from ductor_bot.cli.auth import AuthResult, AuthStatus
    from ductor_bot.multiagent.bus import AsyncInterAgentResult
    from ductor_bot.multiagent.supervisor import AgentSupervisor
    from ductor_bot.session.named import NamedSession
    from ductor_bot.tasks.hub import TaskHub
    from ductor_bot.tasks.models import TaskResult

logger = logging.getLogger(__name__)


_TextCallback = Callable[[str], Awaitable[None]]
_SystemStatusCallback = Callable[[str | None], Awaitable[None]]


@dataclass(slots=True)
class NamedSessionRequest:
    """Parameters for submitting a named background session."""

    message_id: int
    thread_id: int | None
    provider_override: str | None = None
    model_override: str | None = None


@dataclass(slots=True)
class _MessageDispatch:
    """Normalized input for one orchestrator message routing pass."""

    chat_id: int
    text: str
    cmd: str
    streaming: bool = False
    on_text_delta: _TextCallback | None = None
    on_tool_activity: _TextCallback | None = None
    on_system_status: _SystemStatusCallback | None = None

    def streaming_callbacks(self) -> StreamingCallbacks:
        """Bundle the streaming callbacks into a StreamingCallbacks instance."""
        return StreamingCallbacks(
            on_text_delta=self.on_text_delta,
            on_tool_activity=self.on_tool_activity,
            on_system_status=self.on_system_status,
        )


def _docker_skill_resync(paths: DuctorPaths) -> None:
    """Re-run skill sync with copies so skills resolve inside Docker."""
    sync_bundled_skills(paths, docker_active=True)
    sync_skills(paths, docker_active=True)


class Orchestrator:
    """Routes messages through command dispatch and conversation flows."""

    def __init__(
        self,
        config: AgentConfig,
        paths: DuctorPaths,
        *,
        docker_container: str = "",
        agent_name: str = "main",
        interagent_port: int = 8799,
    ) -> None:
        self._config = config
        self._paths: DuctorPaths = paths
        self._docker: DockerManager | None = None
        self._models = ModelRegistry()
        self._known_model_ids: frozenset[str] = frozenset()
        self._refresh_known_model_ids()
        self._sessions = SessionManager(paths.sessions_path, config)
        self._named_sessions = NamedSessionRegistry(paths.named_sessions_path)
        self._process_registry = ProcessRegistry()
        self._available_providers: frozenset[str] = frozenset()
        self._cli_service = CLIService(
            config=CLIServiceConfig(
                working_dir=str(paths.workspace),
                default_model=config.model,
                provider=config.provider,
                max_turns=config.max_turns,
                max_budget_usd=config.max_budget_usd,
                permission_mode=config.permission_mode,
                reasoning_effort=config.reasoning_effort,
                gemini_api_key=config.gemini_api_key,
                docker_container=docker_container,
                claude_cli_parameters=tuple(config.cli_parameters.claude),
                codex_cli_parameters=tuple(config.cli_parameters.codex),
                gemini_cli_parameters=tuple(config.cli_parameters.gemini),
                agent_name=agent_name,
                interagent_port=interagent_port,
            ),
            models=self._models,
            available_providers=frozenset(),
            process_registry=self._process_registry,
        )
        self._cron_manager = CronManager(jobs_path=paths.cron_jobs_path)
        self._webhook_manager = WebhookManager(hooks_path=paths.webhooks_path)
        self._observers = ObserverManager(config, paths)
        self._observers.heartbeat.set_heartbeat_handler(self.handle_heartbeat)
        self._observers.heartbeat.set_busy_check(self._process_registry.has_active)
        stale_max = config.cli_timeout * 2
        self._observers.heartbeat.set_stale_cleanup(
            lambda: self._process_registry.kill_stale(stale_max)
        )
        self._api_stop: Callable[[], Awaitable[None]] | None = None
        self._gemini_api_key_mode: bool | None = None
        self._inflight_tracker = InflightTracker(paths.inflight_turns_path)
        self._hook_registry = MessageHookRegistry()
        self._hook_registry.register(MAINMEMORY_REMINDER)
        self._hook_registry.register(DELEGATION_BRIEF)
        self._hook_registry.register(DELEGATION_REMINDER)
        self._supervisor: AgentSupervisor | None = None  # Set by AgentSupervisor after creation
        self._task_hub: TaskHub | None = None  # Set by supervisor or __main__.py
        self._command_registry = CommandRegistry()
        self._register_commands()

    @property
    def paths(self) -> DuctorPaths:
        """Public access to resolved workspace paths."""
        return self._paths

    @property
    def task_hub(self) -> TaskHub | None:
        """Public access to the task hub (None when tasks are disabled)."""
        return self._task_hub

    @property
    def config(self) -> AgentConfig:
        """Public access to the agent config."""
        return self._config

    @property
    def inflight_tracker(self) -> InflightTracker:
        """Public access to the inflight turn tracker."""
        return self._inflight_tracker

    @property
    def named_sessions(self) -> NamedSessionRegistry:
        """Public access to the named session registry."""
        return self._named_sessions

    @property
    def available_providers(self) -> frozenset[str]:
        """Public access to the set of authenticated providers."""
        return self._available_providers

    @property
    def cli_service(self) -> CLIService:
        """Public access to the CLI service."""
        return self._cli_service

    @property
    def process_registry(self) -> ProcessRegistry:
        """Public access to the process registry."""
        return self._process_registry

    @property
    def bg_observer(self) -> BackgroundObserver | None:
        """Public access to the background observer."""
        return self._observers.background

    @property
    def supervisor(self) -> AgentSupervisor | None:
        """Public access to the agent supervisor."""
        return self._supervisor

    @supervisor.setter
    def supervisor(self, value: AgentSupervisor | None) -> None:
        self._supervisor = value

    def set_task_hub(self, hub: TaskHub) -> None:
        """Inject the task hub (called by supervisor or startup wiring)."""
        self._task_hub = hub
        hub.start_maintenance()

    @classmethod
    async def create(
        cls,
        config: AgentConfig,
        *,
        agent_name: str = "main",
    ) -> Orchestrator:
        """Async factory: build Orchestrator.

        Workspace must already be initialized by the caller (``__main__.load_config``).
        """
        paths = resolve_paths(ductor_home=config.ductor_home)

        # Only set the process-wide env var for the main agent to avoid
        # race conditions in multi-agent mode (sub-agents use per-subprocess env).
        if agent_name == "main":
            os.environ["DUCTOR_HOME"] = str(paths.ductor_home)

        docker_container = ""
        docker_mgr: DockerManager | None = None
        if config.docker.enabled:
            docker_mgr = DockerManager(config.docker, paths)
            container = await docker_mgr.setup()
            if container:
                docker_container = container
            else:
                logger.warning("Docker enabled but setup failed; running on host")

        if docker_container:
            await asyncio.to_thread(_docker_skill_resync, paths)

        await asyncio.to_thread(
            inject_runtime_environment,
            paths,
            docker_container=docker_container,
            agent_name=agent_name,
        )

        orch = cls(config, paths, docker_container=docker_container, agent_name=agent_name)
        orch._docker = docker_mgr

        from ductor_bot.cli.auth import AuthStatus, check_all_auth

        auth_results = await asyncio.to_thread(check_all_auth)
        orch._apply_auth_results(auth_results, auth_status_enum=AuthStatus)

        if not orch._available_providers:
            logger.error("No authenticated providers found! CLI calls will fail.")
        else:
            logger.info("Available providers: %s", ", ".join(sorted(orch._available_providers)))

        await asyncio.to_thread(orch._init_gemini_state)

        codex_cache = await orch._observers.init_model_caches(
            on_gemini_refresh=orch._on_gemini_models_refresh
        )
        orch._observers.init_task_observers(
            cron_manager=orch._cron_manager,
            webhook_manager=orch._webhook_manager,
            cli_service=orch._cli_service,
            codex_cache=codex_cache,
        )
        await orch._observers.start_all(docker_container=docker_container)

        # Direct API server (WebSocket, designed for Tailscale)
        if config.api.enabled:
            await orch._start_api_server(config, paths)

        await orch._observers.start_config_reloader(
            on_hot_reload=orch._on_config_hot_reload,
            on_restart_needed=lambda fields: logger.warning(
                "Config changed but requires restart: %s", ", ".join(fields)
            ),
        )

        return orch

    def _on_gemini_models_refresh(self, models: tuple[str, ...]) -> None:
        """Callback for GeminiCacheObserver: update model registry."""
        set_gemini_models(frozenset(models))
        self._refresh_known_model_ids()
        self._gemini_api_key_mode = None  # Invalidate to re-check on next access

    def _refresh_known_model_ids(self) -> None:
        """Refresh directive-known model IDs from dynamic provider registries."""
        self._known_model_ids = CLAUDE_MODELS | _GEMINI_ALIASES | get_gemini_models()

    def _apply_auth_results(
        self,
        auth_results: dict[str, AuthResult],
        *,
        auth_status_enum: type[AuthStatus],
    ) -> None:
        """Log provider auth states and update the runtime provider set."""
        authenticated = auth_status_enum.AUTHENTICATED
        installed = auth_status_enum.INSTALLED

        for provider, result in auth_results.items():
            if result.status == authenticated:
                logger.info("Provider [%s]: authenticated", provider)
            elif result.status == installed:
                logger.warning("Provider [%s]: installed but NOT authenticated", provider)
            else:
                logger.info("Provider [%s]: not found", provider)

        self._available_providers = frozenset(
            name for name, res in auth_results.items() if res.is_authenticated
        )
        self._cli_service.update_available_providers(self._available_providers)

    def _init_gemini_state(self) -> None:
        """Cache Gemini API-key mode and trust workspace once at startup."""
        from ductor_bot.cli.auth import gemini_uses_api_key_mode

        self._gemini_api_key_mode = gemini_uses_api_key_mode()
        if "gemini" in self._available_providers:
            from ductor_bot.cli.gemini_utils import trust_workspace

            trust_workspace(self._paths.workspace)

    @property
    def gemini_api_key_mode(self) -> bool:
        """Return cached Gemini API-key mode status."""
        if self._gemini_api_key_mode is None:
            from ductor_bot.cli.auth import gemini_uses_api_key_mode

            self._gemini_api_key_mode = gemini_uses_api_key_mode()
        return self._gemini_api_key_mode

    def _build_provider_info(self) -> list[dict[str, object]]:
        """Build provider metadata for the API auth_ok response.

        Only includes authenticated providers.
        """
        provider_meta: dict[str, tuple[str, str]] = {
            "claude": ("Claude Code", "#F97316"),
            "gemini": ("Gemini", "#8B5CF6"),
            "codex": ("Codex", "#10B981"),
        }
        providers: list[dict[str, object]] = []
        for pid in sorted(self._available_providers):
            name, color = provider_meta.get(pid, (pid.title(), "#A1A1AA"))
            models: list[str]
            if pid == "claude":
                models = sorted(CLAUDE_MODELS)
            elif pid == "gemini":
                gemini = get_gemini_models()
                models = sorted(gemini) if gemini else sorted(_GEMINI_ALIASES)
            elif pid == "codex":
                cache = (
                    self._observers.codex_cache_obs.get_cache()
                    if self._observers.codex_cache_obs
                    else None
                )
                models = [m.id for m in cache.models] if cache and cache.models else []
            else:
                models = []
            providers.append({"id": pid, "name": name, "color": color, "models": models})
        return providers

    async def handle_message(self, chat_id: int, text: str) -> OrchestratorResult:
        """Main entry point: route message to appropriate handler."""
        dispatch = _MessageDispatch(chat_id=chat_id, text=text, cmd=text.strip().lower())
        return await self._handle_message_impl(dispatch)

    async def handle_message_streaming(
        self,
        chat_id: int,
        text: str,
        *,
        on_text_delta: _TextCallback | None = None,
        on_tool_activity: _TextCallback | None = None,
        on_system_status: _SystemStatusCallback | None = None,
    ) -> OrchestratorResult:
        """Main entry point with streaming support."""
        dispatch = _MessageDispatch(
            chat_id=chat_id,
            text=text,
            cmd=text.strip().lower(),
            streaming=True,
            on_text_delta=on_text_delta,
            on_tool_activity=on_tool_activity,
            on_system_status=on_system_status,
        )
        return await self._handle_message_impl(dispatch)

    async def _handle_message_impl(self, dispatch: _MessageDispatch) -> OrchestratorResult:
        self._process_registry.clear_abort(dispatch.chat_id)
        logger.info("Message received text=%s", dispatch.cmd[:80])

        patterns = detect_suspicious_patterns(dispatch.text)
        if patterns:
            logger.warning("Suspicious input patterns: %s", ", ".join(patterns))

        try:
            return await self._route_message(dispatch)
        except asyncio.CancelledError:
            raise
        except (CLIError, StreamError, SessionError, CronError, WebhookError, WorkspaceError):
            logger.exception("Domain error in handle_message")
            return OrchestratorResult(text="An internal error occurred. Please try again.")
        except (OSError, RuntimeError, ValueError, TypeError, KeyError):
            logger.exception("Unexpected error in handle_message")
            return OrchestratorResult(text="An internal error occurred. Please try again.")

    async def _route_message(self, dispatch: _MessageDispatch) -> OrchestratorResult:
        result = await self._command_registry.dispatch(
            dispatch.cmd,
            self,
            dispatch.chat_id,
            dispatch.text,
        )
        if result is not None:
            return result

        await self._ensure_docker()

        directives = parse_directives(dispatch.text, self._known_model_ids)

        # Check if a leading @directive matches a named session
        if directives.raw_directives:
            first_key = next(iter(directives.raw_directives))
            ns = self._named_sessions.get(dispatch.chat_id, first_key)
            if ns is not None:
                session_prompt = directives.cleaned or dispatch.text
                if dispatch.streaming:
                    return await named_session_streaming(
                        self,
                        dispatch.chat_id,
                        first_key,
                        session_prompt,
                        cbs=dispatch.streaming_callbacks(),
                    )
                return await named_session_flow(self, dispatch.chat_id, first_key, session_prompt)

        if directives.is_directive_only and directives.has_model:
            return OrchestratorResult(
                text=f"Next message will use: {directives.model}\n"
                f"(Send a message with @{directives.model} <text> to use it.)",
            )

        prompt_text = directives.cleaned or dispatch.text

        if dispatch.streaming:
            return await normal_streaming(
                self,
                dispatch.chat_id,
                prompt_text,
                model_override=directives.model,
                cbs=dispatch.streaming_callbacks(),
            )

        return await normal(
            self,
            dispatch.chat_id,
            prompt_text,
            model_override=directives.model,
        )

    def _register_commands(self) -> None:
        reg = self._command_registry
        reg.register_async("/new", cmd_reset)
        # /stop is handled entirely by the Middleware abort path (before the lock)
        # and never reaches the orchestrator command registry.
        reg.register_async("/status", cmd_status)
        reg.register_async("/model", cmd_model)
        reg.register_async("/model ", cmd_model)
        reg.register_async("/memory", cmd_memory)
        reg.register_async("/cron", cmd_cron)
        reg.register_async("/diagnose", cmd_diagnose)
        reg.register_async("/upgrade", cmd_upgrade)
        reg.register_async("/sessions", cmd_sessions)
        reg.register_async("/tasks", cmd_tasks)

    def register_multiagent_commands(self) -> None:
        """Register /agents, /agent_start, /agent_stop, /agent_restart commands.

        Called by the AgentSupervisor after setting ``_supervisor``.
        """
        from ductor_bot.multiagent.commands import (
            cmd_agent_restart,
            cmd_agent_start,
            cmd_agent_stop,
            cmd_agents,
        )

        reg = self._command_registry
        reg.register_async("/agents", cmd_agents)
        reg.register_async("/agent_start", cmd_agent_start)
        reg.register_async("/agent_start ", cmd_agent_start)
        reg.register_async("/agent_stop", cmd_agent_stop)
        reg.register_async("/agent_stop ", cmd_agent_stop)
        reg.register_async("/agent_restart", cmd_agent_restart)
        reg.register_async("/agent_restart ", cmd_agent_restart)
        logger.info("Multi-agent commands registered")

    async def reset_session(self, chat_id: int) -> None:
        """Reset the session for a given chat."""
        await self._sessions.reset_session(chat_id)
        logger.info("Session reset")

    async def reset_active_provider_session(self, chat_id: int) -> str:
        """Reset only the active provider session bucket for a given chat."""
        active = await self._sessions.get_active(chat_id)
        if active is not None:
            provider = active.provider
            model = active.model
        else:
            model, provider = self.resolve_runtime_target(self._config.model)

        await self._sessions.reset_provider_session(
            chat_id,
            provider=provider,
            model=model,
        )
        logger.info("Active provider session reset provider=%s", provider)
        return provider

    async def abort(self, chat_id: int) -> int:
        """Kill all active CLI processes and background tasks for chat_id."""
        killed = await self._process_registry.kill_all(chat_id)
        if self._observers.background:
            killed += await self._observers.background.cancel_all(chat_id)
        self._named_sessions.end_all(chat_id)
        return killed

    def resolve_runtime_target(self, requested_model: str | None = None) -> tuple[str, str]:
        """Resolve requested model to the effective ``(model, provider)`` pair."""
        model_name = requested_model or self._config.model
        return model_name, self._models.provider_for(model_name)

    def set_cron_result_handler(
        self,
        handler: Callable[[str, str, str], Awaitable[None]],
    ) -> None:
        """Forward cron job results to an external handler (e.g. Telegram)."""
        self._observers.set_cron_result_handler(handler)

    def set_heartbeat_handler(
        self,
        handler: Callable[[int, str], Awaitable[None]],
    ) -> None:
        """Forward heartbeat alert messages to an external handler (e.g. Telegram)."""
        self._observers.set_heartbeat_result_handler(handler)

    async def handle_heartbeat(self, chat_id: int) -> str | None:
        """Run a heartbeat turn in the main session. Returns alert text or None."""
        logger.debug("Heartbeat flow starting")
        return await heartbeat_flow(self, chat_id)

    def set_webhook_result_handler(
        self,
        handler: Callable[[WebhookResult], Awaitable[None]],
    ) -> None:
        """Forward webhook results to an external handler (e.g. Telegram)."""
        self._observers.set_webhook_result_handler(handler)

    def set_webhook_wake_handler(
        self,
        handler: Callable[[int, str], Awaitable[str | None]],
    ) -> None:
        """Set the webhook wake handler (provided by the bot layer)."""
        self._observers.set_webhook_wake_handler(handler)

    def set_session_result_handler(
        self,
        handler: Callable[[BackgroundResult], Awaitable[None]],
    ) -> None:
        """Forward background task results to an external handler (e.g. Telegram)."""
        self._observers.set_session_result_handler(handler)

    def submit_background_task(
        self,
        chat_id: int,
        prompt: str,
        message_id: int,
        thread_id: int | None,
    ) -> str:
        """Submit a background task using the current provider/model. Returns task_id."""
        from ductor_bot.cli.param_resolver import resolve_cli_config

        if self._observers.background is None:
            msg = "Background observer not initialized"
            raise RuntimeError(msg)
        exec_config = resolve_cli_config(self._config, self._observers.codex_cache)
        sub = BackgroundSubmit(
            chat_id=chat_id, prompt=prompt, message_id=message_id, thread_id=thread_id
        )
        return self._observers.background.submit(sub, exec_config)

    def submit_named_session(
        self,
        chat_id: int,
        prompt: str,
        request: NamedSessionRequest,
    ) -> tuple[str, str]:
        """Submit a new named background session. Returns (task_id, session_name)."""
        from ductor_bot.cli.param_resolver import resolve_cli_config

        if self._observers.background is None:
            msg = "Background observer not initialized"
            raise RuntimeError(msg)

        model_name, provider_name = self.resolve_runtime_target(self._config.model)
        if request.provider_override:
            provider_name = request.provider_override
            model_name = request.model_override or self.default_model_for_provider(
                request.provider_override
            )

        ns = self._named_sessions.create(chat_id, provider_name, model_name, prompt)
        exec_config = resolve_cli_config(self._config, self._observers.codex_cache)
        sub = BackgroundSubmit(
            chat_id=chat_id,
            prompt=prompt,
            message_id=request.message_id,
            thread_id=request.thread_id,
            session_name=ns.name,
            provider_override=provider_name,
            model_override=model_name,
        )
        task_id = self._observers.background.submit(sub, exec_config)
        return task_id, ns.name

    def submit_named_followup_bg(
        self,
        chat_id: int,
        session_name: str,
        prompt: str,
        message_id: int,
        thread_id: int | None,
    ) -> str:
        """Submit a background follow-up to an existing named session. Returns task_id."""
        from ductor_bot.cli.param_resolver import resolve_cli_config

        if self._observers.background is None:
            msg = "Background observer not initialized"
            raise RuntimeError(msg)

        ns = self._named_sessions.get(chat_id, session_name)
        if ns is None:
            msg = f"Session '{session_name}' not found"
            raise ValueError(msg)
        if ns.status == "ended":
            msg = f"Session '{session_name}' has ended"
            raise ValueError(msg)
        if ns.status == "running":
            msg = f"Session '{session_name}' is still processing"
            raise ValueError(msg)

        self._named_sessions.mark_running(chat_id, session_name, prompt)
        exec_config = resolve_cli_config(self._config, self._observers.codex_cache)
        sub = BackgroundSubmit(
            chat_id=chat_id,
            prompt=prompt,
            message_id=message_id,
            thread_id=thread_id,
            session_name=session_name,
            resume_session_id=ns.session_id,
            provider_override=ns.provider,
            model_override=ns.model,
        )
        return self._observers.background.submit(sub, exec_config)

    async def end_named_session(self, chat_id: int, name: str) -> bool:
        """Kill process and end a named session."""
        ns = self._named_sessions.get(chat_id, name)
        if ns is None:
            return False
        await self._process_registry.kill_by_label(chat_id, f"ns:{name}")
        self._process_registry.clear_label_abort(chat_id, f"ns:{name}")
        return self._named_sessions.end_session(chat_id, name)

    def is_known_model(self, candidate: str) -> bool:
        """Return True if *candidate* is a recognized model ID for any provider."""
        if candidate in self._known_model_ids:
            return True
        codex = self._observers.codex_cache
        return bool(codex and codex.validate_model(candidate))

    def default_model_for_provider(self, provider: str) -> str:
        """Return the default model ID for a provider, or empty string if unknown."""
        if provider == "claude":
            return self._config.model if self._config.provider == "claude" else "sonnet"
        if provider == "codex":
            codex = self._observers.codex_cache
            if codex:
                for m in codex.models:
                    if m.is_default:
                        return m.id
            return ""
        if provider == "gemini":
            return ""
        return ""

    def resolve_session_directive(self, key: str) -> tuple[str, str] | None:
        """Resolve a ``@key`` directive to ``(provider, model)`` or ``None``.

        Handles three cases:
        - provider name (``@codex``) → (provider, default_model)
        - known model   (``@opus``)  → (inferred_provider, model)
        - unknown                     → None
        """
        if key in ("claude", "codex", "gemini"):
            return key, self.default_model_for_provider(key)
        if self.is_known_model(key):
            provider = self._models.provider_for(key)
            return provider, key
        return None

    def get_named_session(self, chat_id: int, name: str) -> NamedSession | None:
        """Look up a named session."""
        return self._named_sessions.get(chat_id, name)

    def list_named_sessions(self, chat_id: int) -> list[NamedSession]:
        """List active named sessions for a chat."""
        return self._named_sessions.list_active(chat_id)

    def active_background_tasks(self, chat_id: int | None = None) -> list[BackgroundTask]:
        """Return active background tasks, optionally filtered by chat_id."""
        if self._observers.background is None:
            return []
        return self._observers.background.active_tasks(chat_id)

    @property
    def active_provider_name(self) -> str:
        """Human-readable name for the active CLI provider."""
        _model, provider = self.resolve_runtime_target(self._config.model)
        if provider == "claude":
            return "Claude Code"
        if provider == "gemini":
            return "Gemini"
        return "Codex"

    def is_chat_busy(self, chat_id: int) -> bool:
        """Check if a chat has active CLI processes."""
        return self._process_registry.has_active(chat_id)

    async def _ensure_docker(self) -> None:
        """Health-check Docker before CLI calls; auto-recover or fall back."""
        if not self._docker:
            return
        container = await self._docker.ensure_running()
        if container:
            self._cli_service.update_docker_container(container)
        elif self._cli_service._config.docker_container:
            logger.warning("Docker recovery failed, falling back to host execution")
            self._cli_service.update_docker_container("")

    async def _start_api_server(
        self,
        config: AgentConfig,
        paths: DuctorPaths,
    ) -> None:
        """Initialize and start the direct WebSocket API server."""
        try:
            from ductor_bot.api.server import ApiServer
        except ImportError:
            logger.warning(
                "API server enabled but PyNaCl is not installed. "
                "Install with: pip install ductor[api]"
            )
            return

        if not config.api.token:
            from ductor_bot.config import update_config_file_async

            token = secrets.token_urlsafe(32)
            config.api.token = token
            await update_config_file_async(
                paths.config_path,
                api={**config.api.model_dump(), "token": token},
            )
            logger.info("Generated API auth token (persisted to config)")

        default_chat_id = config.api.chat_id or (
            config.allowed_user_ids[0] if config.allowed_user_ids else 1
        )
        server = ApiServer(config.api, default_chat_id=default_chat_id)
        server.set_message_handler(self.handle_message_streaming)
        server.set_abort_handler(self.abort)
        server.set_file_context(
            allowed_roots=resolve_allowed_roots(config.file_access, paths.workspace),
            upload_dir=paths.api_files_dir,
            workspace=paths.workspace,
        )
        server.set_provider_info(self._build_provider_info())
        server.set_active_state_getter(lambda: self.resolve_runtime_target(self._config.model))

        try:
            await server.start()
        except OSError:
            logger.exception(
                "Failed to start API server on %s:%d",
                config.api.host,
                config.api.port,
            )
            return

        self._api_stop = server.stop

    def _on_config_hot_reload(self, config: AgentConfig, hot: dict[str, object]) -> None:
        """Apply hot-reloaded config fields to dependent services."""
        if any(
            k in hot
            for k in (
                "model",
                "provider",
                "max_turns",
                "max_budget_usd",
                "permission_mode",
                "reasoning_effort",
                "cli_parameters",
            )
        ):
            self._cli_service.update_config(
                CLIServiceConfig(
                    working_dir=str(self._paths.workspace),
                    default_model=config.model,
                    provider=config.provider,
                    max_turns=config.max_turns,
                    max_budget_usd=config.max_budget_usd,
                    permission_mode=config.permission_mode,
                    reasoning_effort=config.reasoning_effort,
                    gemini_api_key=config.gemini_api_key,
                    docker_container=self._cli_service._config.docker_container,
                    claude_cli_parameters=tuple(config.cli_parameters.claude),
                    codex_cli_parameters=tuple(config.cli_parameters.codex),
                    gemini_cli_parameters=tuple(config.cli_parameters.gemini),
                )
            )

        if "model" in hot:
            self._refresh_known_model_ids()

        logger.info("Hot-reload applied to orchestrator services")

    # -- Inter-agent communication ------------------------------------------

    async def handle_interagent_message(
        self,
        sender: str,
        message: str,
        *,
        new_session: bool = False,
    ) -> tuple[str, str, str]:
        """Process a message from another agent via the InterAgentBus."""
        from ductor_bot.orchestrator.injection import (
            handle_interagent_message as _handle_ia,
        )

        return await _handle_ia(self, sender, message, new_session=new_session)

    async def handle_async_interagent_result(
        self,
        result: AsyncInterAgentResult,
        *,
        chat_id: int = 0,
    ) -> str:
        """Inject an async inter-agent result into the current active session."""
        from ductor_bot.orchestrator.injection import (
            handle_async_interagent_result as _handle_async_ia,
        )

        return await _handle_async_ia(self, result, chat_id=chat_id)

    async def handle_task_result(
        self,
        result: TaskResult,
        *,
        chat_id: int = 0,
    ) -> str:
        """Inject a background task result into the current active session."""
        from ductor_bot.orchestrator.injection import (
            handle_task_result as _handle_task,
        )

        return await _handle_task(self, result, chat_id=chat_id)

    async def handle_task_question(
        self,
        task_id: str,
        question: str,
        task_preview: str,
        chat_id: int,
    ) -> str:
        """Inject a task worker's question into the main agent's session."""
        from ductor_bot.orchestrator.injection import (
            handle_task_question as _handle_question,
        )

        return await _handle_question(self, task_id, question, task_preview, chat_id)

    async def shutdown(self) -> None:
        """Cleanup on bot shutdown."""
        killed = await self._process_registry.kill_all_active()
        if killed:
            logger.info("Shutdown terminated %d active CLI process(es)", killed)
        if self._api_stop is not None:
            await self._api_stop()
        await asyncio.to_thread(cleanup_ductor_links, self._paths)
        await self._observers.stop_all()
        if self._docker:
            await self._docker.teardown()
        logger.info("Orchestrator shutdown")
