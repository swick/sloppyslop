"""Configuration management for LLM Sandbox."""

import os
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field


class ProviderConfig(BaseModel):
    """Individual provider configuration."""

    api_key_env: str
    model: str
    backend: str = "anthropic"  # "anthropic" or "vertex-ai"
    # Vertex AI specific fields
    region: Optional[str] = None  # e.g., "us-east5"
    project_id: Optional[str] = None  # GCP project ID


class LLMConfig(BaseModel):
    """LLM configuration with multiple providers."""

    default_provider: str = "anthropic"
    providers: dict[str, ProviderConfig] = Field(
        default_factory=lambda: {
            "anthropic": ProviderConfig(
                api_key_env="ANTHROPIC_API_KEY",
                model="claude-sonnet-4-5",
            )
        }
    )


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
    """Load project configuration from .llm-sandbox/config.yaml."""
    config_path = project_path / ".llm-sandbox" / "config.yaml"

    if not config_path.exists():
        raise FileNotFoundError(
            f"Project not initialized. Run 'llm-sandbox init' first. "
            f"Missing: {config_path}"
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


def get_provider_config(config: GlobalConfig, provider: Optional[str] = None) -> tuple[str, ProviderConfig]:
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
