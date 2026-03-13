"""Tests for Vertex AI configuration and provider."""

import os
from unittest.mock import MagicMock, patch

import pytest

from llm_sandbox.config import ProviderConfig
from llm_sandbox.llm_provider import ClaudeProvider


class TestVertexAIConfiguration:
    """Tests for Vertex AI configuration."""

    def test_provider_config_with_vertex_ai(self):
        """Test ProviderConfig with Vertex AI backend."""
        config = ProviderConfig(
            api_key_env="ANTHROPIC_API_KEY",
            model="claude-sonnet-4-5",
            backend="vertex-ai",
            region="us-east5",
            project_id="my-gcp-project",
        )

        assert config.backend == "vertex-ai"
        assert config.region == "us-east5"
        assert config.project_id == "my-gcp-project"
        assert config.model == "claude-sonnet-4-5"

    def test_provider_config_defaults_to_anthropic(self):
        """Test that backend defaults to 'anthropic'."""
        config = ProviderConfig(
            api_key_env="ANTHROPIC_API_KEY",
            model="claude-sonnet-4-5",
        )

        assert config.backend == "anthropic"
        assert config.region is None
        assert config.project_id is None

    def test_provider_config_anthropic_with_optional_fields(self):
        """Test Anthropic backend ignores Vertex AI fields."""
        config = ProviderConfig(
            api_key_env="ANTHROPIC_API_KEY",
            model="claude-sonnet-4-5",
            backend="anthropic",
            region="us-east5",  # Ignored for anthropic backend
            project_id="my-project",  # Ignored for anthropic backend
        )

        assert config.backend == "anthropic"
        assert config.region == "us-east5"  # Stored but not used
        assert config.project_id == "my-project"  # Stored but not used


class TestClaudeProviderVertexAI:
    """Tests for ClaudeProvider with Vertex AI backend."""

    @patch("llm_sandbox.llm_provider.AnthropicVertex")
    def test_initialize_with_vertex_ai(self, mock_vertex):
        """Test initializing ClaudeProvider with Vertex AI backend."""
        config = ProviderConfig(
            api_key_env="ANTHROPIC_API_KEY",
            model="claude-sonnet-4-5",
            backend="vertex-ai",
            region="us-east5",
            project_id="my-gcp-project",
        )

        provider = ClaudeProvider(config)

        # Verify AnthropicVertex was called with correct params
        mock_vertex.assert_called_once_with(
            region="us-east5",
            project_id="my-gcp-project",
        )

        assert provider.backend == "vertex-ai"
        assert provider.model == "claude-sonnet-4-5"
        assert provider.client == mock_vertex.return_value

    @patch("llm_sandbox.llm_provider.Anthropic")
    def test_initialize_with_anthropic_backend(self, mock_anthropic):
        """Test initializing ClaudeProvider with direct Anthropic API."""
        config = ProviderConfig(
            api_key_env="ANTHROPIC_API_KEY",
            model="claude-sonnet-4-5",
            backend="anthropic",
        )

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            provider = ClaudeProvider(config)

            # Verify Anthropic was called with API key
            mock_anthropic.assert_called_once_with(api_key="test-key")

            assert provider.backend == "anthropic"
            assert provider.model == "claude-sonnet-4-5"
            assert provider.api_key == "test-key"
            assert provider.client == mock_anthropic.return_value

    def test_vertex_ai_missing_region(self):
        """Test that Vertex AI backend requires region."""
        config = ProviderConfig(
            api_key_env="ANTHROPIC_API_KEY",
            model="claude-sonnet-4-5",
            backend="vertex-ai",
            project_id="my-gcp-project",
            # Missing region
        )

        with pytest.raises(ValueError, match="requires 'region'"):
            ClaudeProvider(config)

    def test_vertex_ai_missing_project_id(self):
        """Test that Vertex AI backend requires project_id."""
        config = ProviderConfig(
            api_key_env="ANTHROPIC_API_KEY",
            model="claude-sonnet-4-5",
            backend="vertex-ai",
            region="us-east5",
            # Missing project_id
        )

        with pytest.raises(ValueError, match="requires 'project_id'"):
            ClaudeProvider(config)

    def test_anthropic_backend_missing_api_key(self):
        """Test that Anthropic backend requires API key."""
        config = ProviderConfig(
            api_key_env="ANTHROPIC_API_KEY",
            model="claude-sonnet-4-5",
            backend="anthropic",
        )

        # Ensure API key is not in environment
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(ValueError, match="API key not found"):
                ClaudeProvider(config)

    @patch("llm_sandbox.llm_provider.AnthropicVertex")
    def test_vertex_ai_uses_same_interface(self, mock_vertex):
        """Test that Vertex AI client has same interface as Anthropic client."""
        config = ProviderConfig(
            api_key_env="ANTHROPIC_API_KEY",
            model="claude-sonnet-4-5",
            backend="vertex-ai",
            region="us-east5",
            project_id="my-gcp-project",
        )

        # Mock the client's messages.create method
        mock_client = MagicMock()
        mock_vertex.return_value = mock_client

        provider = ClaudeProvider(config)

        # Verify we can call generate_text (interface is the same)
        assert hasattr(provider.client, "messages")
        assert provider.client == mock_client
