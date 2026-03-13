"""Git operations for pulling branches from worktree to main repo."""

import shutil
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
        self._check_git_repo()
        self.repo = git.Repo(repo_path)

    def _check_git_repo(self) -> None:
        """Check if path is a git repository."""
        try:
            git.Repo(self.repo_path)
        except git.InvalidGitRepositoryError as e:
            raise ValueError(f"Not a git repository: {self.repo_path}") from e

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

    def create_worktree(self, commit: str, worktree_dir: Path) -> Path:
        """
        Create git worktree from specified commit.

        Args:
            commit: Git commit/branch/tag reference
            worktree_dir: Directory to create worktree in

        Returns:
            Path to created worktree
        """
        # Ensure parent directory exists
        worktree_dir.parent.mkdir(parents=True, exist_ok=True)

        # Remove existing worktree if present
        if worktree_dir.exists():
            self.remove_worktree(worktree_dir)

        # Create worktree
        try:
            self.repo.git.worktree("add", "--detach", str(worktree_dir), commit)
            return worktree_dir

        except git.GitCommandError as e:
            raise RuntimeError(
                f"Failed to create worktree: {e.stderr}\n"
                f"Commit: {commit}"
            ) from e

    def remove_worktree(self, worktree_path: Path) -> None:
        """
        Remove git worktree.

        Args:
            worktree_path: Path to worktree to remove
        """
        if not worktree_path.exists():
            return

        # First try git worktree remove
        try:
            self.repo.git.worktree("remove", "--force", str(worktree_path))
        except git.GitCommandError:
            # If git worktree remove fails, manually delete
            try:
                shutil.rmtree(worktree_path)
            except Exception:
                pass

        # Clean up any leftover worktree metadata
        try:
            self.repo.git.worktree("prune")
        except Exception:
            pass

    def get_commit_hash(self, commit: str) -> str:
        """
        Resolve commit reference to full hash.

        Args:
            commit: Commit reference (hash/branch/tag)

        Returns:
            Full commit hash
        """
        try:
            commit_obj = self.repo.commit(commit)
            return str(commit_obj.hexsha)
        except git.GitCommandError as e:
            raise ValueError(f"Invalid commit reference: {commit}") from e

    def create_worktree_on_branch(
        self,
        commit: str,
        worktree_dir: Path,
        branch_name: str,
    ) -> Path:
        """
        Create worktree from commit on a new branch.

        Args:
            commit: Git commit/branch/tag reference
            worktree_dir: Directory to create worktree in
            branch_name: Name of the new branch to create

        Returns:
            Path to created worktree
        """
        # Ensure parent directory exists
        worktree_dir.parent.mkdir(parents=True, exist_ok=True)

        # Remove existing worktree if present
        if worktree_dir.exists():
            self.remove_worktree(worktree_dir)

        # GitPython doesn't have direct worktree support, use git command
        try:
            self.repo.git.worktree("add", "-b", branch_name, str(worktree_dir), commit)
            return worktree_dir

        except git.GitCommandError as e:
            raise RuntimeError(
                f"Failed to create worktree on branch: {e.stderr}\n"
                f"Commit: {commit}\n"
                f"Branch: {branch_name}"
            ) from e

    def delete_branch(self, branch_name: str, force: bool = True) -> None:
        """
        Delete branch from repository.

        Args:
            branch_name: Name of branch to delete
            force: Use force delete (default: True)
        """
        try:
            if force:
                self.repo.git.branch("-D", branch_name)
            else:
                self.repo.git.branch("-d", branch_name)
        except git.GitCommandError:
            # Ignore errors for cleanup robustness
            pass

    def commit_files(
        self,
        worktree_path: Path,
        files: List[str],
        message: str,
    ) -> None:
        """
        Commit files in a worktree.

        Args:
            worktree_path: Path to worktree
            files: List of file paths to commit (relative to worktree)
            message: Commit message

        Raises:
            RuntimeError: If git commands fail
        """
        if not worktree_path.exists():
            raise ValueError(f"Worktree does not exist: {worktree_path}")

        try:
            # Open the worktree as a git repository
            worktree_repo = git.Repo(worktree_path)

            # Stage files
            worktree_repo.index.add(files)

            # Commit changes
            worktree_repo.index.commit(message)

        except git.GitCommandError as e:
            raise RuntimeError(
                f"Failed to commit files: {e.stderr}\n"
                f"Files: {', '.join(files)}\n"
                f"Worktree: {worktree_path}"
            ) from e
        except Exception as e:
            raise RuntimeError(
                f"Failed to commit files: {str(e)}\n"
                f"Files: {', '.join(files)}\n"
                f"Worktree: {worktree_path}"
            ) from e
