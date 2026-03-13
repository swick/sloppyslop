"""Tests for project analyzer."""

import tempfile
from pathlib import Path
from unittest.mock import Mock

import pytest

from llm_sandbox.analyzer import ProjectAnalyzer


class MockLLMProvider:
    """Mock LLM provider for testing."""

    def generate_text(self, prompt: str, max_tokens: int = 2000) -> str:
        return "FROM alpine\nRUN echo 'test'"


def test_search_containerfiles():
    """Test searching for containerfiles."""
    with tempfile.TemporaryDirectory() as tmpdir:
        project_path = Path(tmpdir)

        # Create test containerfiles
        (project_path / "Containerfile").write_text("FROM alpine")
        (project_path / "Dockerfile").write_text("FROM ubuntu")

        docker_dir = project_path / "docker"
        docker_dir.mkdir()
        (docker_dir / "Dockerfile").write_text("FROM python")

        # Create analyzer with mock provider
        analyzer = ProjectAnalyzer(MockLLMProvider())

        # Search
        found = analyzer.search_containerfiles(project_path)

        # Should find all 3
        assert len(found) >= 3
        names = [f.name for f in found]
        assert "Containerfile" in names or "Dockerfile" in names


def test_save_containerfile():
    """Test saving containerfile."""
    with tempfile.TemporaryDirectory() as tmpdir:
        project_path = Path(tmpdir)

        # Create analyzer with mock provider
        analyzer = ProjectAnalyzer(MockLLMProvider())

        # Save
        content = "FROM alpine\nRUN apk add python3"
        saved_path = analyzer.save_containerfile(content, project_path)

        # Check
        assert saved_path.exists()
        assert saved_path.read_text() == content
        assert saved_path == project_path / ".llm-sandbox" / "Containerfile"
