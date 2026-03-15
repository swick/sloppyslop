"""Rebase functionality for applying review suggestions."""

import os
import subprocess
from pathlib import Path
from typing import List, Optional

import click
from git import Repo

from llm_sandbox.git_ops import GitOperations
from .models import FeedbackItem, Review


class ReviewRebase:
    """Handles rebasing commits with review suggestions applied."""

    def __init__(self, project_dir: Path, review: Review):
        """Initialize rebase handler.

        Args:
            project_dir: Project root directory
            review: Review object with suggestions
        """
        self.project_dir = project_dir
        self.review = review
        self.git_ops = GitOperations(project_dir)
        self.worktrees_base = project_dir / ".llm-sandbox" / "worktrees"
        self.created_worktrees = []
        self.fixup_worktree: Optional[Path] = None

    def apply_suggestions(self, suggestions: List[FeedbackItem], branch_name: str) -> None:
        """Apply suggestions by creating fixup commits and rebasing.

        Args:
            suggestions: List of FeedbackItem to apply
            branch_name: Name for the new branch to create

        Raises:
            RuntimeError: If rebase operations fail
        """
        if not suggestions:
            click.echo("No suggestions to apply")
            return

        # Verify all suggestions have commits
        for item in suggestions:
            if not item.commit:
                raise RuntimeError(f"Suggestion {item.get_short_id()} has no commit SHA")

        click.echo(f"\nApplying {len(suggestions)} suggestion(s)...")

        try:
            self._apply_suggestions_internal(suggestions, branch_name)
        finally:
            # Always cleanup worktrees
            click.echo("\nCleaning up worktrees...")
            self.cleanup()

    def _apply_suggestions_internal(self, suggestions: List[FeedbackItem], branch_name: str) -> None:
        """Internal method to apply suggestions.

        Args:
            suggestions: List of FeedbackItem to apply
            branch_name: Name for the new branch to create
        """

        # Step 1: Create a single worktree for making fixup commits
        self.fixup_worktree = self.worktrees_base / "llm-review-fixup"
        try:
            # Create worktree at base_ref initially
            self.git_ops.repo.git.worktree("add", "--detach", str(self.fixup_worktree), self.review.base_ref)
            self.created_worktrees.append(self.fixup_worktree)
        except Exception as e:
            raise RuntimeError(f"Failed to create fixup worktree: {e}") from e

        # Step 2: Create fixup commits for each suggestion
        fixup_commits = []
        try:
            for item in suggestions:
                click.echo(f"\n[{item.get_short_id()}] Creating fixup for {item.file}:{item.line_start}-{item.line_end}")
                fixup_commit = self._create_fixup_commit(item)
                fixup_commits.append(fixup_commit)
                click.echo(f"  Created fixup commit: {fixup_commit[:7]}")
        finally:
            # Clean up fixup worktree
            if self.fixup_worktree:
                self.git_ops.remove_worktree(self.fixup_worktree)
                if self.fixup_worktree in self.created_worktrees:
                    self.created_worktrees.remove(self.fixup_worktree)
                self.fixup_worktree = None

        # Step 3: Create new branch at base_ref
        click.echo(f"\n{'='*60}")
        click.echo(f"Creating branch '{branch_name}' at {self.review.base_ref}")
        click.echo(f"{'='*60}")

        # Create worktree for the new branch
        worktree_dir = self.worktrees_base / f"llm-review-{branch_name}"
        try:
            self.git_ops.create_worktree_on_branch(
                self.review.base_ref,
                worktree_dir,
                branch_name
            )
            self.created_worktrees.append(worktree_dir)
        except Exception as e:
            raise RuntimeError(f"Failed to create branch worktree: {e}") from e

        # Step 3: Get all commits from base to head
        try:
            commits = self.git_ops.get_commits(self.review.base_ref, self.review.head_ref)
            commit_shas = [c.hexsha for c in commits]
            click.echo(f"\nCherry-picking {len(commit_shas)} original commits with {len(fixup_commits)} fixup commits")
        except Exception as e:
            raise RuntimeError(f"Failed to get commit list: {e}") from e

        # Step 4: Group fixup commits by their target commit
        # Parse fixup commit messages to find which commit they fix
        fixup_map = {}  # commit_sha -> list of fixup commit shas
        for idx, item in enumerate(suggestions):
            target_commit = item.commit
            if target_commit not in fixup_map:
                fixup_map[target_commit] = []
            fixup_map[target_commit].append(fixup_commits[idx])

        # Step 5: Cherry-pick commits with fixups interleaved
        click.echo(f"\nCherry-picking commits to '{branch_name}'...")
        worktree_repo = Repo(worktree_dir)

        # Cherry-pick in order: original commit, then any fixups for it
        for commit_sha in reversed(commit_shas):  # Oldest first
            # Cherry-pick original commit
            try:
                worktree_repo.git.cherry_pick(commit_sha)
                click.echo(f"  ✓ {commit_sha[:7]}")
            except Exception as e:
                if not self._resolve_cherry_pick_conflict(worktree_repo, worktree_dir, commit_sha, is_fixup=False):
                    raise RuntimeError(f"Cherry-pick conflict: {e}") from e
                click.echo(f"  ✓ {commit_sha[:7]} (resolved)")

            # Cherry-pick any fixup commits for this commit
            if commit_sha in fixup_map:
                for fixup_commit in fixup_map[commit_sha]:
                    try:
                        worktree_repo.git.cherry_pick(fixup_commit)
                        click.echo(f"  ✓ {fixup_commit[:7]} (fixup)")
                    except Exception as e:
                        if not self._resolve_cherry_pick_conflict(worktree_repo, worktree_dir, fixup_commit, is_fixup=True):
                            raise RuntimeError(f"Fixup cherry-pick conflict: {e}") from e
                        click.echo(f"  ✓ {fixup_commit[:7]} (fixup, resolved)")

        click.echo(f"\n{'='*60}")
        click.echo(f"✓ Successfully created branch '{branch_name}'")
        click.echo(f"{'='*60}")
        click.echo(f"\nTo squash fixup commits:")
        click.echo(f"  git checkout {branch_name}")
        click.echo(f"  git rebase -i --autosquash {self.review.base_ref}")
        click.echo(f"\nTo push the branch:")
        click.echo(f"  git push origin {branch_name}")

    def _create_fixup_commit(self, item: FeedbackItem) -> str:
        """Create a fixup commit for a single suggestion.

        Args:
            item: FeedbackItem with the suggestion to apply

        Returns:
            SHA of the created fixup commit

        Raises:
            RuntimeError: If fixup commit creation fails
        """
        if not self.fixup_worktree:
            raise RuntimeError("Fixup worktree not initialized")

        # Create a Repo object for the worktree
        worktree_repo = Repo(self.fixup_worktree)

        try:
            # Checkout the suggestion's commit
            worktree_repo.git.checkout(item.commit)

            # Apply the suggestion
            file_path = self.fixup_worktree / item.file
            if not file_path.exists():
                raise RuntimeError(f"File {item.file} not found in worktree")

            # Read original file
            original_lines = file_path.read_text().splitlines()

            # Apply suggestion (replace lines)
            start_idx = item.line_start - 1
            end_idx = item.line_end
            suggested_lines = item.suggested_code.splitlines() if item.suggested_code else []

            modified_lines = (
                original_lines[:start_idx] +
                suggested_lines +
                original_lines[end_idx:]
            )

            # Write modified file
            file_path.write_text('\n'.join(modified_lines) + '\n')

            # Stage the change
            worktree_repo.git.add(item.file)

            # Create fixup commit
            fixup_message = f"fixup! {item.commit[:7]} - {item.file}:{item.line_start}-{item.line_end}"
            worktree_repo.git.commit('-m', fixup_message)

            # Get the commit SHA
            fixup_sha = worktree_repo.git.rev_parse('HEAD')

            return fixup_sha

        except Exception as e:
            raise RuntimeError(f"Failed to create fixup commit for {item.file}: {e}") from e

    def _resolve_cherry_pick_conflict(self, repo: Repo, worktree_dir: Path, commit_sha: str, is_fixup: bool) -> bool:
        """Interactively resolve a cherry-pick conflict.

        Args:
            repo: Repo object for the worktree
            worktree_dir: Path to the worktree
            commit_sha: Commit SHA that caused the conflict
            is_fixup: Whether this is a fixup commit

        Returns:
            True if conflict was resolved, False if user aborted
        """
        commit_type = "fixup" if is_fixup else "commit"
        click.echo(f"\n{'='*60}")
        click.echo(f"⚠ Cherry-pick conflict on {commit_type} {commit_sha[:7]}")
        click.echo(f"{'='*60}")

        editor = os.environ.get('EDITOR', 'vi')

        while True:
            # Get list of conflicted files
            try:
                conflicted_files = repo.git.diff('--name-only', '--diff-filter=U').strip().split('\n')
                conflicted_files = [f for f in conflicted_files if f]  # Remove empty strings
            except Exception:
                conflicted_files = []

            if not conflicted_files:
                # No conflicts, check if there are staged changes
                try:
                    diff_staged = repo.git.diff('--cached', '--name-only')
                    if not diff_staged.strip():
                        click.echo("\nNo conflicts and no staged changes. Aborting cherry-pick.", err=True)
                        repo.git.cherry_pick('--abort')
                        return False
                except Exception:
                    pass
                break

            click.echo(f"\nConflicted files ({len(conflicted_files)}):")
            for f in conflicted_files:
                click.echo(f"  {f}")

            click.echo(f"\nOpening conflicted files in {editor}...")
            click.echo("Resolve conflicts and save. The files will be staged automatically.")
            click.echo("Press Enter to continue, or Ctrl+C to abort.")

            try:
                input()
            except KeyboardInterrupt:
                click.echo("\n\nAborting cherry-pick.", err=True)
                repo.git.cherry_pick('--abort')
                return False

            # Open each conflicted file in the editor
            for conflicted_file in conflicted_files:
                file_path = worktree_dir / conflicted_file
                if not file_path.exists():
                    click.echo(f"Warning: {conflicted_file} not found, skipping", err=True)
                    continue

                try:
                    subprocess.run([editor, str(file_path)])
                except Exception as e:
                    click.echo(f"Error opening {conflicted_file}: {e}", err=True)
                    continue

                # Stage the file after editing
                try:
                    repo.git.add(conflicted_file)
                    click.echo(f"✓ Staged {conflicted_file}")
                except Exception as e:
                    click.echo(f"Warning: Failed to stage {conflicted_file}: {e}", err=True)

            # Check if conflicts remain (loop back if they do)
            try:
                remaining_conflicts = repo.git.diff('--name-only', '--diff-filter=U').strip()
                if remaining_conflicts:
                    click.echo(f"\nUnresolved conflicts remain:")
                    click.echo(remaining_conflicts)
                    click.echo("Continuing to resolve...")
                    continue
            except Exception:
                pass

            # No more conflicts, break the loop
            break

        # Continue the cherry-pick
        try:
            repo.git.cherry_pick('--continue')
            return True
        except Exception as e:
            click.echo(f"\nError continuing cherry-pick: {e}", err=True)
            return False

    def cleanup(self) -> None:
        """Clean up any created worktrees."""
        for worktree_path in self.created_worktrees:
            try:
                self.git_ops.remove_worktree(worktree_path)
            except Exception:
                pass
        self.created_worktrees.clear()
