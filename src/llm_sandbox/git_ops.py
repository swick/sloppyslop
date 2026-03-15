"""Git operations for managing worktrees and branches."""

import shutil
from pathlib import Path
from typing import Any, Dict, List

import git


class GitOperations:
    """Handles git operations for worktrees and branches using GitPython."""

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



    def branch_exists(self, branch_name: str) -> bool:
        """
        Check if branch exists in main repository.

        Args:
            branch_name: Branch name

        Returns:
            True if branch exists
        """
        return branch_name in self.repo.heads


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

    def get_diff(self, base_ref: str, head_ref: str) -> str:
        """
        Get the diff between two refs.

        Args:
            base_ref: Base commit/branch reference
            head_ref: Head commit/branch reference

        Returns:
            Unified diff format string

        Raises:
            RuntimeError: If diff cannot be obtained
        """
        try:
            # Use git diff with three-dot notation (merge base)
            diff = self.repo.git.diff(f"{base_ref}...{head_ref}")
            return diff
        except git.GitCommandError as e:
            raise RuntimeError(f"Failed to get diff: {e.stderr}") from e
        except Exception as e:
            raise RuntimeError(f"Failed to get diff: {str(e)}") from e

    def get_commits(self, base_ref: str, head_ref: str) -> List["git.Commit"]:
        """
        Get the list of commits between two refs.

        Args:
            base_ref: Base commit/branch reference
            head_ref: Head commit/branch reference

        Returns:
            List of git.Commit objects

        Raises:
            RuntimeError: If commits cannot be obtained
        """
        try:
            # Get commits from base_ref to head_ref (two-dot notation)
            commit_range = f"{base_ref}..{head_ref}"
            return list(self.repo.iter_commits(commit_range))
        except git.GitCommandError as e:
            raise RuntimeError(f"Failed to get commits: {e.stderr}") from e
        except Exception as e:
            raise RuntimeError(f"Failed to get commits: {str(e)}") from e

    def fetch_ref(self, remote: str, refspec: str) -> None:
        """
        Fetch a ref from a remote.

        Args:
            remote: Remote name (e.g., 'origin')
            refspec: Refspec to fetch (e.g., 'pull/123/head:pr-123')

        Raises:
            RuntimeError: If fetch fails
        """
        try:
            self.repo.git.fetch(remote, refspec)
        except git.GitCommandError as e:
            raise RuntimeError(f"Failed to fetch {refspec} from {remote}: {e.stderr}") from e
        except Exception as e:
            raise RuntimeError(f"Failed to fetch {refspec} from {remote}: {str(e)}") from e

    def get_remote_url(self, remote_name: str = "origin") -> str:
        """
        Get the URL of a remote.

        Args:
            remote_name: Name of the remote (default: 'origin')

        Returns:
            Remote URL string

        Raises:
            RuntimeError: If remote URL cannot be retrieved
        """
        try:
            remote = self.repo.remote(remote_name)
            return remote.url
        except ValueError as e:
            raise RuntimeError(f"Remote '{remote_name}' not found") from e
        except Exception as e:
            raise RuntimeError(f"Failed to get remote URL: {str(e)}") from e
