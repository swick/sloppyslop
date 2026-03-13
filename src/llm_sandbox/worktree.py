"""Git worktree management."""

import shutil
import subprocess
from pathlib import Path
from typing import Optional

import git


class WorktreeManager:
    """Manages git worktrees for isolated work."""

    def __init__(self, repo_path: Path):
        """
        Initialize worktree manager.

        Args:
            repo_path: Path to git repository
        """
        self.repo_path = repo_path
        self._check_git_repo()

    def _check_git_repo(self) -> None:
        """Check if path is a git repository."""
        try:
            git.Repo(self.repo_path)
        except git.InvalidGitRepositoryError as e:
            raise ValueError(f"Not a git repository: {self.repo_path}") from e

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
        cmd = [
            "git",
            "-C",
            str(self.repo_path),
            "worktree",
            "add",
            "--detach",
            str(worktree_dir),
            commit,
        ]

        try:
            subprocess.run(
                cmd,
                check=True,
                capture_output=True,
                text=True,
            )
            return worktree_dir

        except subprocess.CalledProcessError as e:
            raise RuntimeError(
                f"Failed to create worktree: {e.stderr}\n"
                f"Commit: {commit}\n"
                f"Command: {' '.join(cmd)}"
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
        cmd = [
            "git",
            "-C",
            str(self.repo_path),
            "worktree",
            "remove",
            "--force",
            str(worktree_path),
        ]

        try:
            subprocess.run(
                cmd,
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError:
            # If git worktree remove fails, manually delete
            try:
                shutil.rmtree(worktree_path)
            except Exception:
                pass

        # Clean up any leftover worktree metadata
        try:
            prune_cmd = [
                "git",
                "-C",
                str(self.repo_path),
                "worktree",
                "prune",
            ]
            subprocess.run(prune_cmd, capture_output=True, text=True)
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
        repo = git.Repo(self.repo_path)
        try:
            commit_obj = repo.commit(commit)
            return str(commit_obj.hexsha)
        except git.GitCommandError as e:
            raise ValueError(f"Invalid commit reference: {commit}") from e
