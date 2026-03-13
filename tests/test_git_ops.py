"""Tests for git operations."""

import subprocess
import tempfile
from pathlib import Path

import pytest

from llm_sandbox.git_ops import GitOperations


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


def test_create_worktree_on_branch(test_git_repo):
    """Test creating a worktree on a new branch."""
    git_ops = GitOperations(test_git_repo)

    worktree_dir = test_git_repo / "worktrees" / "feature-branch"
    branch_name = "llm-container/test-instance/feature-1"

    worktree_path = git_ops.create_worktree_on_branch("HEAD", worktree_dir, branch_name)

    # Verify worktree exists
    assert worktree_path.exists()
    assert (worktree_path / "README.md").exists()

    # Verify branch was created
    result = subprocess.run(
        ["git", "branch", "--list", branch_name],
        cwd=test_git_repo,
        capture_output=True,
        text=True,
    )
    assert branch_name in result.stdout


def test_delete_branch(test_git_repo):
    """Test deleting a branch."""
    git_ops = GitOperations(test_git_repo)

    # Create a branch using worktree
    worktree_dir = test_git_repo / "worktrees" / "test"
    branch_name = "test-branch"
    git_ops.create_worktree_on_branch("HEAD", worktree_dir, branch_name)

    # Remove worktree first
    git_ops.remove_worktree(worktree_dir)

    # Delete branch
    git_ops.delete_branch(branch_name)

    # Verify branch is deleted
    result = subprocess.run(
        ["git", "branch", "--list", branch_name],
        cwd=test_git_repo,
        capture_output=True,
        text=True,
    )
    assert branch_name not in result.stdout


def test_delete_nonexistent_branch(test_git_repo):
    """Test deleting a nonexistent branch doesn't raise error."""
    git_ops = GitOperations(test_git_repo)

    # Should not raise error (silently fails for cleanup robustness)
    git_ops.delete_branch("nonexistent-branch")


def test_remove_worktree(test_git_repo):
    """Test removing a worktree."""
    git_ops = GitOperations(test_git_repo)

    worktree_dir = test_git_repo / "worktrees" / "test"
    git_ops.create_worktree_on_branch("HEAD", worktree_dir, "test-branch")

    assert worktree_dir.exists()

    git_ops.remove_worktree(worktree_dir)

    assert not worktree_dir.exists()


def test_branch_exists(test_git_repo):
    """Test checking if branch exists."""
    git_ops = GitOperations(test_git_repo)

    # HEAD should be on a branch (usually master or main)
    # Create a known branch
    subprocess.run(
        ["git", "branch", "test-branch"],
        cwd=test_git_repo,
        check=True,
        capture_output=True,
    )

    assert git_ops.branch_exists("test-branch")
    assert not git_ops.branch_exists("nonexistent-branch")


def test_branch_already_in_main_repo(test_git_repo):
    """Test that branches created via create_worktree_on_branch are already in main repo."""
    git_ops = GitOperations(test_git_repo)

    # Create worktree with branch
    worktree_dir = test_git_repo / "worktrees" / "feature"
    branch_name = "feature-branch"
    git_ops.create_worktree_on_branch("HEAD", worktree_dir, branch_name)

    # Verify branch exists in main repo immediately
    assert git_ops.branch_exists(branch_name)

    # Make a change in worktree and commit
    (worktree_dir / "new_file.txt").write_text("test content")
    git_ops.commit_files(worktree_dir, ["new_file.txt"], "Add new file")

    # Get commit hash from worktree
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=worktree_dir,
        capture_output=True,
        text=True,
        check=True,
    )
    worktree_commit = result.stdout.strip()

    # Verify branch in main repo has same commit (no pull needed)
    result = subprocess.run(
        ["git", "rev-parse", branch_name],
        cwd=test_git_repo,
        capture_output=True,
        text=True,
        check=True,
    )
    main_repo_commit = result.stdout.strip()

    assert main_repo_commit == worktree_commit


def test_commit_files(test_git_repo):
    """Test committing files in a worktree."""
    git_ops = GitOperations(test_git_repo)

    # Create worktree
    worktree_dir = test_git_repo / "worktrees" / "test"
    branch_name = "test-branch"
    git_ops.create_worktree_on_branch("HEAD", worktree_dir, branch_name)

    # Create test files
    (worktree_dir / "file1.txt").write_text("content 1")
    (worktree_dir / "file2.txt").write_text("content 2")

    # Commit files using GitOperations
    git_ops.commit_files(worktree_dir, ["file1.txt", "file2.txt"], "Add test files")

    # Verify commit exists
    result = subprocess.run(
        ["git", "log", "--oneline", "-1"],
        cwd=worktree_dir,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "Add test files" in result.stdout

    # Verify files are committed
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=worktree_dir,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "file1.txt" in result.stdout
    assert "file2.txt" in result.stdout


def test_commit_files_nonexistent_worktree(test_git_repo):
    """Test committing files in nonexistent worktree raises error."""
    git_ops = GitOperations(test_git_repo)

    nonexistent_dir = test_git_repo / "nonexistent"

    with pytest.raises(ValueError, match="does not exist"):
        git_ops.commit_files(nonexistent_dir, ["file.txt"], "Test commit")


def test_commit_files_invalid_file(test_git_repo):
    """Test committing nonexistent file raises error."""
    git_ops = GitOperations(test_git_repo)

    # Create worktree
    worktree_dir = test_git_repo / "worktrees" / "test"
    git_ops.create_worktree_on_branch("HEAD", worktree_dir, "test-branch")

    # Try to commit nonexistent file
    with pytest.raises(RuntimeError, match="Failed to commit files"):
        git_ops.commit_files(worktree_dir, ["nonexistent.txt"], "Test commit")
