"""Git operations for managing worktrees and branches."""

import shutil
from pathlib import Path
from typing import List

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

            # Patch the .git file to point to /project instead of host path
            # This is needed for the container to access the git repository correctly
            self._patch_worktree_gitdir(worktree_dir)

            return worktree_dir

        except git.GitCommandError as e:
            raise RuntimeError(
                f"Failed to create worktree on branch: {e.stderr}\n"
                f"Commit: {commit}\n"
                f"Branch: {branch_name}"
            ) from e

    def _patch_worktree_gitdir(self, worktree_dir: Path) -> None:
        """
        Patch the .git file in a worktree to point to /project instead of host path.

        The .git file in a worktree contains:
            gitdir: /host/path/to/repo/.git/worktrees/name

        We need to change it to:
            gitdir: /project/.git/worktrees/name

        Args:
            worktree_dir: Path to the worktree directory
        """
        git_file = worktree_dir / ".git"

        if not git_file.exists():
            return

        try:
            # Read the .git file
            content = git_file.read_text()

            # Parse gitdir line
            if not content.startswith("gitdir: "):
                return

            # Extract the worktree name from the path
            # e.g., gitdir: /var/home/swick/Projects/llm-container-sandbox/.git/worktrees/pr-1
            # Extract: pr-1
            parts = content.strip().split("/.git/worktrees/")
            if len(parts) != 2:
                return

            worktree_name = parts[1]

            # Create new gitdir pointing to /project
            new_content = f"gitdir: /project/.git/worktrees/{worktree_name}\n"

            # Write back
            git_file.write_text(new_content)

        except Exception:
            # If patching fails, silently continue
            # The worktree will still work on the host, just not in the container
            pass

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
