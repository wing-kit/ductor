"""Base classes for provider model-cache persistence and periodic refresh."""

from __future__ import annotations

import asyncio
import json
import logging
from abc import ABC, abstractmethod
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Self

from ductor_bot.infra.atomic_io import atomic_text_save
from ductor_bot.infra.base_observer import BaseObserver

logger = logging.getLogger(__name__)

_CACHE_MAX_AGE = timedelta(hours=24)
REFRESH_INTERVAL_S: int = 3600


class BaseModelCache(ABC):
    """Abstract base for immutable model caches with disk persistence.

    Subclasses must be frozen dataclasses with ``last_updated: str`` and a
    ``models`` field (any sequence type).  They must implement the four
    abstract hooks below plus ``to_json`` / ``from_json``.
    """

    last_updated: str
    models: Any

    @classmethod
    @abstractmethod
    def _provider_name(cls) -> str:
        """Short label for log messages (e.g. ``"Codex"``)."""

    @classmethod
    @abstractmethod
    async def _discover(cls) -> Any:
        """Run provider-specific model discovery and return the models value."""

    @classmethod
    @abstractmethod
    def _empty_models(cls) -> Any:
        """Return the empty sentinel for the models field (e.g. ``[]`` or ``()``)."""

    @abstractmethod
    def to_json(self) -> dict[str, Any]: ...

    @classmethod
    @abstractmethod
    def from_json(cls, data: dict[str, Any]) -> Self: ...

    @classmethod
    async def load_or_refresh(
        cls,
        cache_path: Path,
        *,
        force_refresh: bool = False,
    ) -> Self:
        """Load from disk, refresh if stale (>24 h) or missing.

        Args:
            cache_path: Path to JSON cache file.
            force_refresh: If True, ignore on-disk cache and rediscover models.

        Returns:
            Cache instance (possibly refreshed).
        """
        name = cls._provider_name()

        if force_refresh:
            logger.info("%s cache refresh forced", name)
            return await cls._refresh_and_save(cache_path)

        exists = await asyncio.to_thread(cache_path.exists)
        if exists:
            try:
                content = await asyncio.to_thread(cache_path.read_text)
                data = json.loads(content)
                cache = cls.from_json(data)

                last_updated = datetime.fromisoformat(cache.last_updated)
                age = datetime.now(UTC) - last_updated

                if age < _CACHE_MAX_AGE:
                    if cache.models:
                        logger.debug("%s cache is fresh, using cached models", name)
                        return cache

                    logger.info("%s cache is fresh but empty, forcing refresh", name)
                else:
                    logger.info("%s cache is stale (age: %s), refreshing", name, age)
            except Exception:
                logger.warning("Failed to load %s cache, will refresh", name, exc_info=True)

        return await cls._refresh_and_save(cache_path)

    @classmethod
    async def _refresh_and_save(cls, cache_path: Path) -> Self:
        """Discover models and save to disk."""
        name = cls._provider_name()

        try:
            models = await cls._discover()
            model_count = len(models) if isinstance(models, Sequence) else 0
            logger.info("Discovered %d %s models", model_count, name)
        except Exception:
            logger.exception("Failed to discover %s models, using empty cache", name)
            models = cls._empty_models()

        cache = cls(  # type: ignore[call-arg]
            last_updated=datetime.now(UTC).isoformat(),
            models=models,
        )

        try:
            content = json.dumps(cache.to_json(), indent=2)
            await asyncio.to_thread(atomic_text_save, cache_path, content)
            logger.debug("Saved %s cache to %s", name, cache_path)
        except Exception:
            logger.exception("Failed to save %s cache to disk", name)

        return cache


class BaseModelCacheObserver(BaseObserver, ABC):
    """Abstract base for periodic model-cache refresh observers.

    Subclasses must implement ``_provider_name``, ``_load_cache``,
    ``_model_count``, and ``_last_updated``.
    """

    def __init__(self, cache_path: Path) -> None:
        super().__init__()
        self._cache_path = cache_path
        self._cache: Any = None

    @abstractmethod
    def _provider_name(self) -> str:
        """Short label for log messages (e.g. ``"Codex"``)."""

    @abstractmethod
    async def _load_cache(self, *, initial: bool) -> Any:
        """Load or refresh the cache. *initial* is True on first call."""

    @abstractmethod
    def _model_count(self) -> int: ...

    @abstractmethod
    def _last_updated(self) -> str: ...

    def _on_cache_loaded(self) -> None:
        """Called after every successful cache load. Override for notifications."""

    async def start(self) -> None:
        """Load initial cache and start refresh loop."""
        name = self._provider_name()
        obs = f"{name}CacheObserver"
        logger.info("%s starting, cache_path=%s", obs, self._cache_path)
        self._cache = await self._load_cache(initial=True)
        self._on_cache_loaded()
        logger.info(
            "%s cache loaded: %d models, last_updated=%s",
            name,
            self._model_count(),
            self._last_updated(),
        )
        await super().start()

    async def stop(self) -> None:
        """Stop refresh loop."""
        logger.info("%sCacheObserver stopping", self._provider_name())
        await super().stop()

    def get_cache(self) -> Any:
        """Return current cache (may be None if never loaded)."""
        return self._cache

    async def _run(self) -> None:
        """Refresh cache every 60 minutes."""
        name = self._provider_name()
        obs = f"{name}CacheObserver"
        try:
            while self._running:
                await asyncio.sleep(REFRESH_INTERVAL_S)
                if not self._running:
                    break  # type: ignore[unreachable]
                try:
                    logger.info("%s: refreshing cache", obs)
                    self._cache = await self._load_cache(initial=False)
                    self._on_cache_loaded()
                    logger.info(
                        "%s cache refreshed: %d models",
                        name,
                        self._model_count(),
                    )
                except Exception:
                    logger.exception("%s cache refresh failed, will retry in 60 minutes", name)
        except asyncio.CancelledError:
            logger.debug("%s refresh loop cancelled", obs)
            raise
