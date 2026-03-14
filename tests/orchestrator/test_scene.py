"""Tests for scene features: OrchestratorResult metadata and footer appending."""

from __future__ import annotations

from ductor_bot.cli.types import AgentResponse
from ductor_bot.config import SceneConfig
from ductor_bot.messenger.telegram.message_dispatch import _maybe_append_footer
from ductor_bot.orchestrator.flows import _finish_normal
from ductor_bot.orchestrator.registry import OrchestratorResult


class TestOrchestratorResultMetadata:
    def test_defaults(self) -> None:
        r = OrchestratorResult(text="hello")
        assert r.model_name is None
        assert r.total_tokens == 0
        assert r.input_tokens == 0
        assert r.cost_usd == 0.0
        assert r.duration_ms is None

    def test_explicit_metadata(self) -> None:
        r = OrchestratorResult(
            text="hello",
            model_name="opus",
            total_tokens=1000,
            input_tokens=800,
            cost_usd=0.05,
            duration_ms=3000.0,
        )
        assert r.model_name == "opus"
        assert r.total_tokens == 1000
        assert r.input_tokens == 800
        assert r.cost_usd == 0.05
        assert r.duration_ms == 3000.0


class TestFinishNormalMetadata:
    def test_populates_metadata(self) -> None:
        response = AgentResponse(
            result="Agent output",
            total_tokens=500,
            input_tokens=300,
            cost_usd=0.02,
            duration_ms=2500.0,
        )
        result = _finish_normal(response, model_name="opus")
        assert result.model_name == "opus"
        assert result.total_tokens == 500
        assert result.input_tokens == 300
        assert result.cost_usd == 0.02
        assert result.duration_ms == 2500.0

    def test_error_response_no_metadata(self) -> None:
        response = AgentResponse(result="fail", is_error=True)
        result = _finish_normal(response, model_name="opus")
        assert result.model_name is None


class TestMaybeAppendFooter:
    def test_footer_appended_when_enabled(self) -> None:
        scene = SceneConfig(technical_footer=True)
        result = OrchestratorResult(
            text="Hello",
            model_name="opus",
            total_tokens=1000,
            input_tokens=800,
            cost_usd=0.05,
            duration_ms=3000.0,
        )
        _maybe_append_footer(result, scene)
        assert "Model: opus" in result.text
        assert "Tokens: 1000" in result.text

    def test_footer_not_appended_when_disabled(self) -> None:
        scene = SceneConfig(technical_footer=False)
        result = OrchestratorResult(text="Hello", model_name="opus")
        _maybe_append_footer(result, scene)
        assert result.text == "Hello"

    def test_footer_not_appended_without_model(self) -> None:
        scene = SceneConfig(technical_footer=True)
        result = OrchestratorResult(text="Hello")
        _maybe_append_footer(result, scene)
        assert result.text == "Hello"

    def test_footer_not_appended_with_none_config(self) -> None:
        result = OrchestratorResult(text="Hello", model_name="opus")
        _maybe_append_footer(result, None)
        assert result.text == "Hello"
