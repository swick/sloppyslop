"""Tests for runner dynamic worktree management."""

import re
import subprocess
import tempfile
from pathlib import Path

import pytest

from llm_sandbox.config import GlobalConfig, ProjectConfig
from llm_sandbox.runner import SandboxRunner


@pytest.fixture
def test_git_repo():
    """Create a temporary git repository for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = Path(tmpdir)

        # Initialize git repo
        subprocess.run(["git", "init"], cwd=repo_path, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=repo_path,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=repo_path,
            check=True,
            capture_output=True,
        )

        # Create initial commit
        (repo_path / "README.md").write_text("# Test Project\n")
        subprocess.run(["git", "add", "README.md"], cwd=repo_path, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "Initial commit"],
            cwd=repo_path,
            check=True,
            capture_output=True,
        )

        yield repo_path


@pytest.fixture
def sandbox_runner(test_git_repo):
    """Create a SandboxRunner instance for testing."""
    global_config = GlobalConfig()
    project_config = ProjectConfig(
        containerfile=".llm-sandbox/Containerfile",
        image_tag="test-image",
    )

    return SandboxRunner(test_git_repo, global_config, project_config)


class TestInstanceIdGeneration:
    """Tests for instance ID generation."""

    def test_generate_instance_id_format(self, sandbox_runner):
        """Test that instance ID has correct format."""
        instance_id = sandbox_runner._generate_instance_id()

        # Format: YYYYMMDD-HHMMSS-{8-char-uuid}
        pattern = r"^\d{8}-\d{6}-[a-f0-9]{8}$"
        assert re.match(pattern, instance_id), f"Invalid format: {instance_id}"

    def test_generate_instance_id_uniqueness(self, sandbox_runner):
        """Test that instance IDs are unique."""
        ids = [sandbox_runner._generate_instance_id() for _ in range(10)]

        # All IDs should be unique
        assert len(ids) == len(set(ids)), "Instance IDs should be unique"

    def test_generate_instance_id_timestamp(self, sandbox_runner):
        """Test that instance ID contains valid timestamp."""
        instance_id = sandbox_runner._generate_instance_id()

        # Extract timestamp part
        timestamp = instance_id.split("-")[0] + instance_id.split("-")[1]

        # Should be 14 digits (YYYYMMDDHHMMSS)
        assert len(timestamp) == 14
        assert timestamp.isdigit()


class TestCleanupWorktrees:
    """Tests for worktree cleanup functionality."""

    def test_cleanup_with_no_worktrees(self, sandbox_runner):
        """Test cleanup when no worktrees were created."""
        # Should not raise error
        sandbox_runner._cleanup_worktrees([])

    def test_cleanup_with_output_branches(self, sandbox_runner, test_git_repo):
        """Test cleanup that preserves output branches."""
        # Set up instance
        sandbox_runner.instance_id = "test-instance-123"
        sandbox_runner.worktrees_base_dir = test_git_repo / ".llm-sandbox" / "worktrees" / "test-instance-123"
        sandbox_runner.worktrees_base_dir.mkdir(parents=True, exist_ok=True)

        # Create test worktrees
        worktree1 = sandbox_runner.worktrees_base_dir / "feature-1"
        worktree2 = sandbox_runner.worktrees_base_dir / "feature-2"

        branch1 = "llm-container/test-instance-123/feature-1"
        branch2 = "llm-container/test-instance-123/feature-2"

        sandbox_runner.git_ops.create_worktree_on_branch("HEAD", worktree1, branch1)
        sandbox_runner.git_ops.create_worktree_on_branch("HEAD", worktree2, branch2)

        sandbox_runner.created_worktrees = ["feature-1", "feature-2"]

        # Cleanup, keeping only feature-1
        sandbox_runner._cleanup_worktrees(["feature-1"])

        # Verify feature-1 branch exists in main repo
        result = subprocess.run(
            ["git", "branch", "--list", branch1],
            cwd=test_git_repo,
            capture_output=True,
            text=True,
        )
        assert branch1 in result.stdout, "Output branch should exist"

        # Verify feature-2 branch is deleted
        result = subprocess.run(
            ["git", "branch", "--list", branch2],
            cwd=test_git_repo,
            capture_output=True,
            text=True,
        )
        assert branch2 not in result.stdout, "Non-output branch should be deleted"

        # Verify worktrees are removed
        assert not worktree1.exists()
        assert not worktree2.exists()

        # Verify instance directory is removed
        assert not sandbox_runner.worktrees_base_dir.exists()

    def test_cleanup_without_output_branches(self, sandbox_runner, test_git_repo):
        """Test cleanup that deletes all branches."""
        # Set up instance
        sandbox_runner.instance_id = "test-instance-456"
        sandbox_runner.worktrees_base_dir = test_git_repo / ".llm-sandbox" / "worktrees" / "test-instance-456"
        sandbox_runner.worktrees_base_dir.mkdir(parents=True, exist_ok=True)

        # Create test worktree
        worktree = sandbox_runner.worktrees_base_dir / "temp-work"
        branch = "llm-container/test-instance-456/temp-work"

        sandbox_runner.git_ops.create_worktree_on_branch("HEAD", worktree, branch)
        sandbox_runner.created_worktrees = ["temp-work"]

        # Cleanup with no output branches
        sandbox_runner._cleanup_worktrees([])

        # Verify branch is deleted
        result = subprocess.run(
            ["git", "branch", "--list", branch],
            cwd=test_git_repo,
            capture_output=True,
            text=True,
        )
        assert branch not in result.stdout, "All branches should be deleted"

        # Verify worktree is removed
        assert not worktree.exists()

        # Verify instance directory is removed
        assert not sandbox_runner.worktrees_base_dir.exists()

    def test_cleanup_handles_missing_worktrees(self, sandbox_runner, test_git_repo):
        """Test cleanup handles missing worktrees gracefully."""
        # Set up instance
        sandbox_runner.instance_id = "test-instance-789"
        sandbox_runner.worktrees_base_dir = test_git_repo / ".llm-sandbox" / "worktrees" / "test-instance-789"
        sandbox_runner.worktrees_base_dir.mkdir(parents=True, exist_ok=True)

        # Track worktrees that don't exist
        sandbox_runner.created_worktrees = ["nonexistent-1", "nonexistent-2"]

        # Should not raise error
        sandbox_runner._cleanup_worktrees([])

        # Instance directory should still be cleaned up
        assert not sandbox_runner.worktrees_base_dir.exists()


class TestSandboxRunnerInitialization:
    """Tests for SandboxRunner initialization."""

    def test_runner_initialization(self, sandbox_runner):
        """Test that runner initializes with correct state."""
        assert sandbox_runner.instance_id is None
        assert sandbox_runner.worktrees_base_dir is None
        assert sandbox_runner.created_worktrees == []

    def test_runner_components_initialized(self, sandbox_runner):
        """Test that runner components are properly initialized."""
        assert sandbox_runner.container_manager is not None
        assert sandbox_runner.git_ops is not None
        assert sandbox_runner.provider_name is not None
        assert sandbox_runner.provider_config is not None
