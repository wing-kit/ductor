"""Tests for Orchestrator._build_provider_info."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from ductor_bot.config import set_gemini_models


@pytest.fixture(autouse=True)
def _reset_gemini():
    set_gemini_models(frozenset())
    yield
    set_gemini_models(frozenset())


def _make_orchestrator(
    available: frozenset[str],
    codex_models: list[MagicMock] | None = None,
) -> object:
    """Create a minimal Orchestrator with mocked internals for provider info tests."""
    from ductor_bot.orchestrator.core import Orchestrator

    orch = object.__new__(Orchestrator)
    orch._available_providers = available

    codex_obs = None
    if codex_models is not None:
        cache = MagicMock()
        cache.models = codex_models
        codex_obs = MagicMock()
        codex_obs.get_cache.return_value = cache
    observers = MagicMock()
    observers.codex_cache_obs = codex_obs
    orch._observers = observers

    return orch


class TestBuildProviderInfo:
    def test_claude_only(self) -> None:
        orch = _make_orchestrator(frozenset({"claude"}))
        info = orch._build_provider_info()
        assert len(info) == 1
        assert info[0]["id"] == "claude"
        assert info[0]["name"] == "Claude Code"
        assert info[0]["color"] == "#F97316"
        assert sorted(info[0]["models"]) == ["haiku", "opus", "sonnet"]

    def test_multiple_providers_sorted(self) -> None:
        orch = _make_orchestrator(frozenset({"gemini", "claude"}))
        info = orch._build_provider_info()
        assert len(info) == 2
        assert info[0]["id"] == "claude"
        assert info[1]["id"] == "gemini"

    def test_gemini_with_runtime_models(self) -> None:
        set_gemini_models(frozenset({"gemini-2.5-pro", "gemini-2.5-flash"}))
        orch = _make_orchestrator(frozenset({"gemini"}))
        info = orch._build_provider_info()
        assert info[0]["models"] == ["gemini-2.5-flash", "gemini-2.5-pro"]

    def test_gemini_falls_back_to_aliases(self) -> None:
        orch = _make_orchestrator(frozenset({"gemini"}))
        info = orch._build_provider_info()
        assert "auto" in info[0]["models"]

    def test_codex_with_cache(self) -> None:
        model1 = MagicMock()
        model1.id = "o3-mini"
        model2 = MagicMock()
        model2.id = "o4-mini"
        orch = _make_orchestrator(frozenset({"codex"}), codex_models=[model1, model2])
        info = orch._build_provider_info()
        assert info[0]["models"] == ["o3-mini", "o4-mini"]

    def test_codex_without_cache(self) -> None:
        orch = _make_orchestrator(frozenset({"codex"}))
        info = orch._build_provider_info()
        assert info[0]["models"] == []

    def test_empty_providers(self) -> None:
        orch = _make_orchestrator(frozenset())
        info = orch._build_provider_info()
        assert info == []
