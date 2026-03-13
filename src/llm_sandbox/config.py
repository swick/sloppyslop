"""Configuration management for LLM Sandbox."""

import os
from pathlib import Path
from typing import Optional, Union

import yaml
from pydantic import BaseModel, Field


class BaseProviderConfig(BaseModel):
    """Base provider configuration."""

    model: str = "claude-sonnet-4-5"  # Model name


class AnthropicConfig(BaseProviderConfig):
    """Anthropic API provider configuration."""

    api_key_env: str = "ANTHROPIC_API_KEY"  # Environment variable for API key


class VertexAIConfig(BaseProviderConfig):
    """Google Cloud Vertex AI provider configuration.

    Environment variables:
    - CLOUD_ML_REGION: Fallback for region
    - ANTHROPIC_VERTEX_PROJECT_ID: Fallback for project_id
    """

    region: str = "us-east5"  # GCP region, or set CLOUD_ML_REGION
    project_id: Optional[str] = None  # GCP project ID, or set ANTHROPIC_VERTEX_PROJECT_ID

    def model_post_init(self, __context):
        """Post-initialization to populate from environment variables."""
        # Populate region from environment if set (overrides default)
        env_region = os.getenv("CLOUD_ML_REGION")
        if env_region:
            object.__setattr__(self, "region", env_region)

        # Populate project_id from environment if not set
        if self.project_id is None:
            env_project_id = os.getenv("ANTHROPIC_VERTEX_PROJECT_ID")
            if env_project_id:
                object.__setattr__(self, "project_id", env_project_id)


class LLMConfig(BaseModel):
    """LLM configuration with multiple providers.

    Auto-detects default_provider based on CLAUDE_CODE_USE_VERTEX environment variable.
    """

    default_provider: Optional[str] = None
    providers: dict[str, Union[AnthropicConfig, VertexAIConfig]] = Field(
        default_factory=lambda: {
            "anthropic": AnthropicConfig(),
            "vertex-ai": VertexAIConfig(),
        }
    )

    def model_post_init(self, __context):
        """Auto-detect default_provider if not set."""
        if self.default_provider is None:
            if os.getenv("CLAUDE_CODE_USE_VERTEX"):
                object.__setattr__(self, "default_provider", "vertex-ai")
            else:
                object.__setattr__(self, "default_provider", "anthropic")


class ContainerConfig(BaseModel):
    """Container runtime configuration."""

    network: str = "isolated"  # isolated or enabled


class GlobalConfig(BaseModel):
    """Global configuration."""

    llm: LLMConfig = Field(default_factory=LLMConfig)
    container: ContainerConfig = Field(default_factory=ContainerConfig)


class ProjectConfig(BaseModel):
    """Project-specific configuration (from .llm-sandbox/config.yaml)."""

    containerfile: str = "Containerfile"
    image_tag: str


def get_config_dir() -> Path:
    """Get XDG config directory for llm-sandbox."""
    xdg_config = os.getenv("XDG_CONFIG_HOME")
    if xdg_config:
        config_dir = Path(xdg_config) / "llm-sandbox"
    else:
        config_dir = Path.home() / ".config" / "llm-sandbox"

    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir


def load_global_config() -> GlobalConfig:
    """Load global configuration from XDG_CONFIG_HOME/llm-sandbox/config.yaml."""
    config_path = get_config_dir() / "config.yaml"

    if config_path.exists():
        with open(config_path) as f:
            data = yaml.safe_load(f) or {}
        return GlobalConfig(**data)

    return GlobalConfig()


def load_project_config(project_path: Path) -> ProjectConfig:
    """Load project configuration from .llm-sandbox/config.yaml.

    If the config file doesn't exist, returns a default configuration.
    """
    config_path = project_path / ".llm-sandbox" / "config.yaml"

    if not config_path.exists():
        # Return default configuration
        project_name = project_path.name
        return ProjectConfig(
            containerfile="Containerfile",
            image_tag=f"llm-sandbox-{project_name}",
        )

    with open(config_path) as f:
        data = yaml.safe_load(f)

    return ProjectConfig(**data)


def save_project_config(project_path: Path, config: ProjectConfig) -> None:
    """Save project configuration to .llm-sandbox/config.yaml."""
    config_dir = project_path / ".llm-sandbox"
    config_dir.mkdir(parents=True, exist_ok=True)

    config_path = config_dir / "config.yaml"

    with open(config_path, "w") as f:
        yaml.safe_dump(config.model_dump(), f, default_flow_style=False)


def get_provider_config(config: GlobalConfig, provider: Optional[str] = None) -> tuple[str, Union[AnthropicConfig, VertexAIConfig]]:
    """
    Get provider name and configuration.

    Args:
        config: Global configuration
        provider: Optional provider name (defaults to default_provider)

    Returns:
        Tuple of (provider_name, provider_config)
    """
    if provider is None:
        provider = config.llm.default_provider

    if provider not in config.llm.providers:
        raise ValueError(f"Provider '{provider}' not found in configuration")

    return provider, config.llm.providers[provider]

