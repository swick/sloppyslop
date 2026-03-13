"""Tests for dynamic worktree MCP tools."""

import re
import subprocess
import tempfile
from pathlib import Path

import pytest

from llm_sandbox.mcp_tools import CheckoutCommitTool, GitCommitTool


class MockContainerManager:
    """Mock container manager for testing."""

    def __init__(self, return_code=0, stdout="success", stderr=""):
        self.return_code = return_code
        self.stdout = stdout
        self.stderr = stderr
        self.last_command = None
        self.last_workdir = None

    def exec_command(self, container_id, command, workdir):
        self.last_command = command
        self.last_workdir = workdir
        return self.return_code, self.stdout, self.stderr


class MockRunner:
    """Mock runner for testing."""

    def __init__(self):
        self.created_worktrees = []


@pytest.fixture
def mock_container():
    """Create a mock container manager."""
    return MockContainerManager()


@pytest.fixture
def mock_runner():
    """Create a mock runner."""
    return MockRunner()


@pytest.fixture
def instance_id():
    """Provide a test instance ID."""
    return "20260313-152345-abc123"


class TestCheckoutCommitTool:
    """Tests for CheckoutCommitTool."""

    def test_validate_worktree_name_valid(self, mock_container, mock_runner, instance_id):
        """Test validation of valid worktree names."""
        tool = CheckoutCommitTool(mock_container, "container-123", instance_id, mock_runner)

        valid_names = [
            "feature-1",
            "bugfix_2",
            "test-branch-123",
            "wt-abc",
            "MY_FEATURE",
            "a",
            "123",
        ]

        for name in valid_names:
            assert tool._validate_worktree_name(name), f"'{name}' should be valid"

    def test_validate_worktree_name_invalid(self, mock_container, mock_runner, instance_id):
        """Test validation of invalid worktree names."""
        tool = CheckoutCommitTool(mock_container, "container-123", instance_id, mock_runner)

        invalid_names = [
            "../etc",
            "path/to/file",
            "name with spaces",
            "name@special",
            "name!",
            "",
            "name.dot",
        ]

        for name in invalid_names:
            assert not tool._validate_worktree_name(name), f"'{name}' should be invalid"

    def test_generate_worktree_name(self, mock_container, mock_runner, instance_id):
        """Test auto-generation of worktree names."""
        tool = CheckoutCommitTool(mock_container, "container-123", instance_id, mock_runner)

        # Generate multiple names
        names = [tool._generate_worktree_name() for _ in range(5)]

        # Verify format
        for name in names:
            assert name.startswith("wt-"), f"Name should start with 'wt-': {name}"
            assert tool._validate_worktree_name(name), f"Generated name should be valid: {name}"

        # Verify uniqueness
        assert len(names) == len(set(names)), "Generated names should be unique"

    def test_execute_with_valid_worktree_name(self, mock_container, mock_runner, instance_id):
        """Test executing checkout with explicit worktree name."""
        tool = CheckoutCommitTool(mock_container, "container-123", instance_id, mock_runner)

        result = tool.execute({"commit": "main", "worktree_name": "my-feature"})

        assert result["success"] is True
        assert result["worktree_name"] == "my-feature"
        assert result["worktree_path"] == "/worktrees/my-feature"
        assert result["branch_name"] == f"llm-container/{instance_id}/my-feature"
        assert result["commit"] == "main"
        assert "my-feature" in mock_runner.created_worktrees

    def test_execute_with_auto_generated_name(self, mock_container, mock_runner, instance_id):
        """Test executing checkout with auto-generated worktree name."""
        tool = CheckoutCommitTool(mock_container, "container-123", instance_id, mock_runner)

        result = tool.execute({"commit": "main"})

        assert result["success"] is True
        assert result["worktree_name"].startswith("wt-")
        assert result["worktree_path"].startswith("/worktrees/wt-")
        assert f"llm-container/{instance_id}/wt-" in result["branch_name"]

    def test_execute_with_invalid_worktree_name(self, mock_container, mock_runner, instance_id):
        """Test executing checkout with invalid worktree name."""
        tool = CheckoutCommitTool(mock_container, "container-123", instance_id, mock_runner)

        result = tool.execute({"commit": "main", "worktree_name": "../etc/passwd"})

        assert result["success"] is False
        assert "Invalid worktree name" in result["error"]

    def test_execute_with_duplicate_worktree_name(self, mock_container, mock_runner, instance_id):
        """Test executing checkout with duplicate worktree name."""
        tool = CheckoutCommitTool(mock_container, "container-123", instance_id, mock_runner)

        # First checkout
        result1 = tool.execute({"commit": "main", "worktree_name": "my-feature"})
        assert result1["success"] is True

        # Duplicate checkout
        result2 = tool.execute({"commit": "main", "worktree_name": "my-feature"})
        assert result2["success"] is False
        assert "already exists" in result2["error"]

    def test_execute_git_command_failure(self, mock_runner, instance_id):
        """Test handling git command failure."""
        failing_container = MockContainerManager(return_code=1, stderr="fatal: invalid commit")
        tool = CheckoutCommitTool(failing_container, "container-123", instance_id, mock_runner)

        result = tool.execute({"commit": "invalid-commit", "worktree_name": "my-feature"})

        assert result["success"] is False
        assert "Failed to create worktree" in result["error"]

    def test_tool_definition(self, mock_container, mock_runner, instance_id):
        """Test tool definition structure."""
        tool = CheckoutCommitTool(mock_container, "container-123", instance_id, mock_runner)

        assert tool.name == "checkout_commit"
        assert "commit" in tool.parameters["properties"]
        assert "worktree_name" in tool.parameters["properties"]
        assert tool.parameters["required"] == ["commit"]


class TestGitCommitTool:
    """Tests for GitCommitTool."""

    def test_validate_branch_pattern_valid(self, mock_container, mock_runner, instance_id):
        """Test validation of valid branch patterns."""
        tool = GitCommitTool(mock_container, "container-123", instance_id, mock_runner)

        valid_branches = [
            f"llm-container/{instance_id}/feature-1",
            f"llm-container/{instance_id}/bugfix_2",
            f"llm-container/{instance_id}/wt-abc123",
        ]

        for branch in valid_branches:
            assert tool._validate_branch_pattern(branch), f"'{branch}' should be valid"

    def test_validate_branch_pattern_invalid(self, mock_container, mock_runner, instance_id):
        """Test validation of invalid branch patterns."""
        tool = GitCommitTool(mock_container, "container-123", instance_id, mock_runner)

        invalid_branches = [
            "my-branch",
            "llm-container/wrong-id/feature",
            f"llm-container/{instance_id}",
            f"llm-container/{instance_id}/",
            "feature/branch",
            f"llm-container/{instance_id}/invalid@name",
        ]

        for branch in invalid_branches:
            assert not tool._validate_branch_pattern(branch), f"'{branch}' should be invalid"

    def test_execute_with_invalid_branch(self, mock_container, mock_runner, instance_id):
        """Test executing commit with invalid branch."""
        tool = GitCommitTool(mock_container, "container-123", instance_id, mock_runner)

        result = tool.execute({
            "files": ["file1.txt"],
            "message": "Test commit",
            "branch": "invalid-branch-name",
        })

        assert result["success"] is False
        assert "Invalid branch name" in result["error"]

    def test_execute_with_missing_worktree(self, mock_container, mock_runner, instance_id):
        """Test executing commit when worktree doesn't exist."""
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmpdir:
            mock_runner.worktrees_base_dir = Path(tmpdir)

            tool = GitCommitTool(mock_container, "container-123", instance_id, mock_runner)

            result = tool.execute({
                "files": ["file1.txt"],
                "message": "Test commit",
                "branch": f"llm-container/{instance_id}/nonexistent",
            })

            assert result["success"] is False
            assert "does not exist" in result["error"]

    def test_tool_definition(self, mock_container, mock_runner, instance_id):
        """Test tool definition structure."""
        tool = GitCommitTool(mock_container, "container-123", instance_id, mock_runner)

        assert tool.name == "git_commit"
        assert "files" in tool.parameters["properties"]
        assert "message" in tool.parameters["properties"]
        assert "branch" in tool.parameters["properties"]
        assert "branch" in tool.parameters["required"]
        assert "files" in tool.parameters["required"]
        assert "message" in tool.parameters["required"]


class TestGitCommitToolIntegration:
    """Integration tests for GitCommitTool with real git repository."""

    @pytest.fixture
    def test_git_repo(self):
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

    def test_commit_files_on_host(self, mock_container, test_git_repo, instance_id):
        """Test that GitCommitTool commits files on the host git repository using GitOperations."""
        from llm_sandbox.git_ops import GitOperations

        # Create worktree directory
        worktree_dir = test_git_repo / ".llm-sandbox" / "worktrees" / instance_id / "my-feature"
        worktree_dir.mkdir(parents=True, exist_ok=True)

        # Create worktree
        branch_name = f"llm-container/{instance_id}/my-feature"
        subprocess.run(
            ["git", "worktree", "add", "-b", branch_name, str(worktree_dir), "HEAD"],
            cwd=test_git_repo,
            check=True,
            capture_output=True,
        )

        # Create runner mock with worktree base dir and git_ops
        mock_runner = MockRunner()
        mock_runner.worktrees_base_dir = test_git_repo / ".llm-sandbox" / "worktrees" / instance_id
        mock_runner.git_ops = GitOperations(test_git_repo)

        # Create a file in the worktree
        test_file = worktree_dir / "test.txt"
        test_file.write_text("test content\n")

        # Execute git commit tool
        tool = GitCommitTool(mock_container, "container-123", instance_id, mock_runner)

        result = tool.execute({
            "files": ["test.txt"],
            "message": "Add test file",
            "branch": branch_name,
        })

        # Verify commit succeeded
        assert result["success"] is True, f"Commit failed: {result}"
        assert result["branch"] == branch_name

        # Verify commit exists in git history
        git_result = subprocess.run(
            ["git", "log", "--oneline", "-1"],
            cwd=worktree_dir,
            capture_output=True,
            text=True,
            check=True,
        )
        assert "Add test file" in git_result.stdout

        # Cleanup worktree
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree_dir)],
            cwd=test_git_repo,
            capture_output=True,
        )
