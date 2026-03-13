"""Tests for LLM provider."""

import os
import pytest

from llm_sandbox.config import ProviderConfig
from llm_sandbox.llm_provider import LLMProvider, ClaudeProvider, create_llm_provider


def test_claude_provider_inherits_from_llm_provider():
    """Test that ClaudeProvider inherits from LLMProvider."""
    assert issubclass(ClaudeProvider, LLMProvider)


def test_claude_provider_init(monkeypatch):
    """Test ClaudeProvider initialization."""
    monkeypatch.setenv("TEST_KEY", "test-api-key")
    config = ProviderConfig(api_key_env="TEST_KEY", model="claude-sonnet-4-5")
    provider = ClaudeProvider(config)
    assert provider.api_key == "test-api-key"
    assert provider.model == "claude-sonnet-4-5"
    assert provider.provider_config == config


def test_claude_provider_missing_api_key():
    """Test ClaudeProvider raises error when API key not found."""
    config = ProviderConfig(api_key_env="MISSING_KEY", model="claude-sonnet-4-5")
    with pytest.raises(ValueError, match="API key not found"):
        ClaudeProvider(config)


def test_create_llm_provider_anthropic(monkeypatch):
    """Test creating an Anthropic provider."""
    monkeypatch.setenv("TEST_ANTHROPIC_KEY", "test-key-value")
    config = ProviderConfig(api_key_env="TEST_ANTHROPIC_KEY", model="claude-sonnet-4-5")
    provider = create_llm_provider("anthropic", config)
    assert isinstance(provider, ClaudeProvider)
    assert isinstance(provider, LLMProvider)
    assert provider.api_key == "test-key-value"
    assert provider.model == "claude-sonnet-4-5"


def test_create_llm_provider_missing_api_key():
    """Test creating provider without API key raises error."""
    config = ProviderConfig(api_key_env="NONEXISTENT_KEY", model="claude-sonnet-4-5")
    with pytest.raises(ValueError, match="API key not found"):
        create_llm_provider("anthropic", config)


def test_create_llm_provider_unsupported(monkeypatch):
    """Test creating an unsupported provider raises error."""
    monkeypatch.setenv("TEST_KEY", "test-value")
    config = ProviderConfig(api_key_env="TEST_KEY", model="some-model")
    with pytest.raises(ValueError, match="Unsupported LLM provider"):
        create_llm_provider("unsupported", config)


def test_llm_provider_is_abstract():
    """Test that LLMProvider cannot be instantiated directly."""
    with pytest.raises(TypeError):
        LLMProvider()
