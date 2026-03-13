"""Git operations for pulling branches from worktree to main repo."""

import subprocess
from pathlib import Path
from typing import List

import git


class GitOperations:
    """Handles git operations between worktree and main repository."""

    def __init__(self, repo_path: Path):
        """
        Initialize git operations.

        Args:
            repo_path: Path to main git repository
        """
        self.repo_path = repo_path
        self.repo = git.Repo(repo_path)

    def get_worktree_branches(self, worktree_path: Path) -> List[str]:
        """
        Get list of branches in worktree.

        Args:
            worktree_path: Path to worktree

        Returns:
            List of branch names
        """
        worktree_repo = git.Repo(worktree_path)
        branches = []

        for ref in worktree_repo.references:
            if isinstance(ref, git.Head):
                branches.append(ref.name)

        return branches

    def pull_branch_to_repo(
        self,
        worktree_path: Path,
        branch_name: str,
        repo_path: Path,
    ) -> None:
        """
        Pull branch from worktree to main repository.

        Args:
            worktree_path: Path to worktree
            branch_name: Name of branch to pull
            repo_path: Path to main repository
        """
        # Verify branch exists in worktree
        worktree_repo = git.Repo(worktree_path)

        try:
            branch_ref = worktree_repo.heads[branch_name]
        except (IndexError, AttributeError) as e:
            raise ValueError(
                f"Branch '{branch_name}' not found in worktree"
            ) from e

        # Get the commit hash for the branch
        commit_hash = str(branch_ref.commit.hexsha)

        # In main repo, create or update the branch
        main_repo = git.Repo(repo_path)

        try:
            # Check if branch exists in main repo
            if branch_name in main_repo.heads:
                # Update existing branch
                main_repo.heads[branch_name].commit = commit_hash
            else:
                # Create new branch
                main_repo.create_head(branch_name, commit_hash)

        except Exception as e:
            raise RuntimeError(
                f"Failed to pull branch '{branch_name}' to main repo: {e}"
            ) from e

    def pull_branches(
        self,
        worktree_path: Path,
        branch_names: List[str],
        repo_path: Path,
    ) -> None:
        """
        Pull multiple branches from worktree to main repository.

        Args:
            worktree_path: Path to worktree
            branch_names: List of branch names to pull
            repo_path: Path to main repository
        """
        if not branch_names:
            return

        for branch_name in branch_names:
            self.pull_branch_to_repo(worktree_path, branch_name, repo_path)

    def branch_exists(self, branch_name: str) -> bool:
        """
        Check if branch exists in main repository.

        Args:
            branch_name: Branch name

        Returns:
            True if branch exists
        """
        return branch_name in self.repo.heads
