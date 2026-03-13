"""Tests for configuration management."""

import tempfile
from pathlib import Path

import pytest
import yaml

from llm_sandbox.config import (
    GlobalConfig,
    ProjectConfig,
    load_global_config,
    load_project_config,
    save_project_config,
)


def test_global_config_defaults():
    """Test global config with defaults."""
    config = GlobalConfig()
    assert config.llm.api_key_env == "ANTHROPIC_API_KEY"
    assert config.llm.model == "claude-sonnet-4-5"
    assert config.container.network == "isolated"
    assert config.container.timeout == 600


def test_project_config():
    """Test project config."""
    config = ProjectConfig(
        containerfile="Containerfile",
        image_tag="test-image",
    )
    assert config.containerfile == "Containerfile"
    assert config.image_tag == "test-image"


def test_save_and_load_project_config():
    """Test saving and loading project config."""
    with tempfile.TemporaryDirectory() as tmpdir:
        project_path = Path(tmpdir)

        # Save config
        config = ProjectConfig(
            containerfile="Containerfile",
            image_tag="test-image",
        )
        save_project_config(project_path, config)

        # Check file exists
        config_file = project_path / ".llm-sandbox" / "config.yaml"
        assert config_file.exists()

        # Load config
        loaded_config = load_project_config(project_path)
        assert loaded_config.containerfile == config.containerfile
        assert loaded_config.image_tag == config.image_tag


def test_load_project_config_not_initialized():
    """Test loading config from uninitialized project."""
    with tempfile.TemporaryDirectory() as tmpdir:
        project_path = Path(tmpdir)

        with pytest.raises(FileNotFoundError):
            load_project_config(project_path)
