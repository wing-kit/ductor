"""Tests for config and model registry."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ductor_bot.config import (
    DEFAULT_KIMI_MODEL,
    AgentConfig,
    DockerConfig,
    ModelRegistry,
    StreamingConfig,
    deep_merge_config,
    reset_gemini_models,
    reset_kimi_models,
)

# -- AgentConfig defaults --


@pytest.fixture(autouse=True)
def _reset_gemini_models() -> None:
    reset_gemini_models()
    reset_kimi_models()


def test_agent_config_defaults() -> None:
    cfg = AgentConfig()
    assert cfg.provider == "claude"
    assert cfg.model == "opus"
    assert cfg.idle_timeout_minutes == 1440
    assert cfg.daily_reset_hour == 4
    assert cfg.cli_timeout == 1800.0
    assert cfg.permission_mode == "bypassPermissions"
    assert cfg.gemini_api_key is None
    assert cfg.telegram_token == ""
    assert cfg.allowed_user_ids == []
    assert cfg.disabled_providers == []


def test_agent_config_normalizes_nullish_gemini_api_key() -> None:
    assert AgentConfig(gemini_api_key="null").gemini_api_key is None
    assert AgentConfig(gemini_api_key=" NONE ").gemini_api_key is None
    assert AgentConfig(gemini_api_key="   ").gemini_api_key is None


def test_agent_config_streaming_defaults() -> None:
    cfg = AgentConfig()
    assert cfg.streaming.enabled is True
    assert cfg.streaming.min_chars == 200
    assert cfg.streaming.max_chars == 4000


def test_agent_config_docker_defaults() -> None:
    cfg = AgentConfig()
    assert cfg.docker.enabled is False
    assert cfg.docker.image_name == "ductor-sandbox"


def test_agent_config_rejects_invalid_types() -> None:
    with pytest.raises(ValidationError, match="idle_timeout_minutes"):
        AgentConfig(idle_timeout_minutes="not_a_number")  # type: ignore[arg-type]


def test_agent_config_normalizes_disabled_providers() -> None:
    cfg = AgentConfig(disabled_providers=[" KIMI ", "gemini", "kimi"])
    assert cfg.disabled_providers == ["kimi", "gemini"]
    assert cfg.is_provider_disabled("kimi") is True
    assert cfg.is_provider_disabled("codex") is False


def test_agent_config_rejects_unknown_disabled_provider() -> None:
    with pytest.raises(ValidationError, match="Unknown provider in disabled_providers"):
        AgentConfig(disabled_providers=["unknown"])


# -- deep_merge_config --


def test_deep_merge_adds_new_keys() -> None:
    user: dict[str, object] = {"model": "sonnet"}
    defaults: dict[str, object] = {"model": "opus", "provider": "claude"}
    merged, changed = deep_merge_config(user, defaults)
    assert merged["model"] == "sonnet"
    assert merged["provider"] == "claude"
    assert changed is True


def test_deep_merge_preserves_user_values() -> None:
    user: dict[str, object] = {"model": "sonnet", "provider": "codex"}
    defaults: dict[str, object] = {"model": "opus", "provider": "claude"}
    merged, changed = deep_merge_config(user, defaults)
    assert merged["model"] == "sonnet"
    assert merged["provider"] == "codex"
    assert changed is False


def test_deep_merge_nested() -> None:
    user: dict[str, object] = {"streaming": {"enabled": False}}
    defaults: dict[str, object] = {"streaming": {"enabled": True, "min_chars": 200}}
    merged, changed = deep_merge_config(user, defaults)
    streaming = merged["streaming"]
    assert isinstance(streaming, dict)
    assert streaming["enabled"] is False
    assert streaming["min_chars"] == 200
    assert changed is True


def test_deep_merge_no_change() -> None:
    data: dict[str, object] = {"a": 1, "b": 2}
    defaults: dict[str, object] = {"a": 99, "b": 99}
    _, changed = deep_merge_config(data, defaults)
    assert changed is False


# -- ModelRegistry --


def test_registry_provider_for_claude() -> None:
    reg = ModelRegistry()
    assert reg.provider_for("opus") == "claude"
    assert reg.provider_for("sonnet") == "claude"
    assert reg.provider_for("haiku") == "claude"


def test_registry_provider_for_codex() -> None:
    reg = ModelRegistry()
    assert reg.provider_for("gpt-5.2-codex") == "codex"
    assert reg.provider_for("gpt-5.3-codex") == "codex"
    assert reg.provider_for("o3") == "codex"


def test_registry_provider_for_gemini_prefix() -> None:
    reg = ModelRegistry()
    reset_gemini_models()
    assert reg.provider_for("gemini-2.5-pro") == "gemini"


def test_registry_provider_for_kimi_prefix() -> None:
    reg = ModelRegistry()
    reset_kimi_models()
    assert reg.provider_for("kimi-k2-0905-preview") == "kimi"


def test_default_kimi_model_constant() -> None:
    assert DEFAULT_KIMI_MODEL == "kimi-for-coding"


def test_streaming_config_fields() -> None:
    s = StreamingConfig(enabled=False, min_chars=100)
    assert s.enabled is False
    assert s.min_chars == 100


def test_docker_config_fields() -> None:
    d = DockerConfig(enabled=True, image_name="custom")
    assert d.enabled is True
    assert d.image_name == "custom"


# -- AgentConfig transports normalization --


def test_transport_backward_compat_populates_transports() -> None:
    """Legacy single ``transport`` field fills ``transports`` list."""
    cfg = AgentConfig(transport="telegram")
    assert cfg.transports == ["telegram"]
    assert cfg.transport == "telegram"


def test_transport_matrix_backward_compat() -> None:
    """transport='matrix' with empty transports normalizes correctly."""
    cfg = AgentConfig(transport="matrix")
    assert cfg.transports == ["matrix"]
    assert cfg.transport == "matrix"


def test_transports_multi_sets_primary_transport() -> None:
    """Explicit multi-transport sets ``transport`` to first entry."""
    cfg = AgentConfig(transports=["telegram", "matrix"])
    assert cfg.transports == ["telegram", "matrix"]
    assert cfg.transport == "telegram"


def test_transports_multi_reversed_order() -> None:
    """Primary transport is always the first in the list."""
    cfg = AgentConfig(transports=["matrix", "telegram"])
    assert cfg.transport == "matrix"


def test_is_multi_transport_single() -> None:
    cfg = AgentConfig(transport="telegram")
    assert cfg.is_multi_transport is False


def test_is_multi_transport_multiple() -> None:
    cfg = AgentConfig(transports=["telegram", "matrix"])
    assert cfg.is_multi_transport is True


def test_transports_default_is_telegram() -> None:
    """Default AgentConfig has transports=['telegram']."""
    cfg = AgentConfig()
    assert cfg.transports == ["telegram"]
    assert cfg.is_multi_transport is False
